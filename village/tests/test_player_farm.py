import uuid

from django.db import transaction
from django.test import TestCase

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.models import InventoryBag, InventorySlot, NpcDefinition, NpcGlobalInventory
from items.models import Item
from player.models import PlayerState
from village.models import VillageFarmPlot, VillageSimulationConfig, EconomicLedgerEntry, VillageDeliveryOutbox
from village.player_actions import execute_player_action
from village.player_farm import maintain_leases, project_plot
from village.phase3_production import plant_preflight, _do_harvest


class PlayerFarmTests(TestCase):
    def setUp(self):
        VillageSimulationConfig.objects.update_or_create(config_key="default", defaults={"village_economy_enabled": True})
        self.clock = GlobalGameTimeState.get_instance()
        self.clock.world_minute = 100
        self.clock.save()
        self.player = GameAccount.objects.create(username="farmer")
        self.bag = InventoryBag.objects.create(player=self.player, character_id="farmer", money=300)
        npc, _ = NpcDefinition.objects.get_or_create(agent_id="10011", defaults={"name": "Bill"})
        self.bill, _ = NpcGlobalInventory.objects.update_or_create(npc=npc, defaults={"money": 200})
        self.plot, _ = VillageFarmPlot.objects.update_or_create(plot_code="FARM_PLOT_D_01", defaults={"lease_enabled": True, "status": "EMPTY"})
        PlayerState.objects.create(player_id=str(self.player.pk), action_points=100)
        for item_id, name in [(4001, "农具"), (1051, "玉米种子"), (1030, "玉米")]:
            Item.objects.update_or_create(item_id=item_id, defaults={"item_name": name, "item_type": "Commodity", "item_price": 5})

    def action(self, action, params, rid=None):
        return execute_player_action(dict(player_id=str(self.player.pk), request_id=str(rid or uuid.uuid4()),
            action=action, params={"plot_code": self.plot.plot_code, **params}, current_location_code="BILL_FARM"))

    def lease(self, days=1, rid=None):
        return self.action("lease_farm_plot", dict(days=days, expected_plot_version=0, expected_daily_price=10), rid)

    def test_lease_replay_conserves_money_and_blocks_npc(self):
        rid = uuid.uuid4()
        first = self.lease(7, rid)
        self.assertTrue(first["success"], first)
        replay = self.lease(7, rid)
        self.assertTrue(replay["replayed"])
        self.bag.refresh_from_db(); self.bill.refresh_from_db(); self.plot.refresh_from_db()
        self.assertEqual((self.bag.money, self.bill.money), (230, 270))
        self.assertEqual(self.plot.lease_ends_world_minute, 100+7*1440)
        self.assertEqual(EconomicLedgerEntry.objects.filter(entry_type="LEASE_FARM_PLOT").count(), 1)
        self.assert_lease_notice("PLAYER_LEASE_FARM_PLOT", 7, 70, 270, 100+7*1440)
        self.assertIsNotNone(plant_preflight(10011, {"plot_code": self.plot.plot_code, "crop_code": "CORN"}, 100))
        self.assertEqual(_do_harvest(10011, {"plot_code": self.plot.plot_code}, 100, uuid.uuid4())["status"], "FAILED")

    def assert_lease_notice(self, event_type, days, paid, balance, end):
        deliveries = VillageDeliveryOutbox.objects.filter(event__event_type=event_type).order_by("-event__event_sequence")
        event_id = deliveries.first().event_id
        rows = {row.recipient: row.payload for row in deliveries.filter(event_id=event_id)}
        self.assertEqual(set(rows), {"10011", "ENV_CLIENTS"})
        notice = rows["10011"]
        self.assertIn("farmer", notice["summary"])
        self.assertIn("D1", notice["summary"])
        self.assertIn(f"本次{days}个游戏天", notice["summary"])
        self.assertIn(f"已收到租金{paid}金币", notice["summary"])
        self.assertIn(f"世界分钟{end}", notice["summary"])
        self.assertIn("无需再次收款或审批", notice["summary"])
        details = notice["public_data"]["private_data"]["farm_lease"]
        self.assertEqual((details["rent_paid"], details["bill_balance_after"], details["lease_ends_world_minute"]), (paid, balance, end))
        self.assertEqual(details["player_id"], str(self.player.pk))
        self.assertNotIn("private_data", rows["ENV_CLIENTS"]["public_data"])
        self.assertNotIn("rent_paid", str(rows["ENV_CLIENTS"]))
        self.assertEqual(rows["ENV_CLIENTS"]["plot"]["lessee_player_id"], str(self.player.pk))
        return details, notice["summary"]

    def test_active_and_grace_renewal_send_bill_settled_rent(self):
        self.assertTrue(self.lease(7)["success"])
        renewed = self.action("renew_farm_plot", dict(days=3, expected_plot_version=1, expected_daily_price=10))
        self.assertTrue(renewed["success"], renewed)
        end = 100+10*1440
        details, summary = self.assert_lease_notice("PLAYER_RENEW_FARM_PLOT", 3, 30, 300, end)
        self.assertEqual(details["lease_period_starts_world_minute"], 100+7*1440)
        self.assertIn("从原到期时间追加", summary)
        self.plot.refresh_from_db()
        self.plot.status, self.plot.crop_code = "GROWING", "CORN"
        self.plot.batch_id, self.plot.mature_world_minute = uuid.uuid4(), end+5
        self.plot.save()
        self.clock.world_minute = end+1; self.clock.save()
        with transaction.atomic():
            maintain_leases(self.clock.world_minute)
        self.plot.refresh_from_db()
        renewed = self.action("renew_farm_plot", dict(days=2, expected_plot_version=self.plot.plot_version, expected_daily_price=10))
        self.assertTrue(renewed["success"], renewed)
        details, summary = self.assert_lease_notice("PLAYER_RENEW_FARM_PLOT", 2, 20, 320, end+1+2*1440)
        self.assertEqual(details["lease_period_starts_world_minute"], end+1)
        self.assertIn("从当前游戏时间重新起算", summary)
        self.assertEqual(set(VillageDeliveryOutbox.objects.filter(event__event_type__in=[
            "PLAYER_LEASE_FARM_PLOT", "PLAYER_RENEW_FARM_PLOT", "FARM_LEASE_HARVEST_ONLY",
        ]).values_list("recipient", flat=True)), {"10011", "ENV_CLIENTS"})

    def test_one_day_crop_grace_harvest_and_tool_stack(self):
        self.assertTrue(self.lease()["success"])
        InventorySlot.objects.create(bag=self.bag, slot_index=0, item_id=1051, item_amount=1)
        InventorySlot.objects.create(bag=self.bag, slot_index=1, item_id=4001, item_amount=2, remaining_uses=20)
        self.clock.world_minute = 105; self.clock.save()
        planted = self.action("plant_farm_plot", dict(crop_code="CORN", expected_plot_version=1))
        self.assertTrue(planted["success"], planted)
        self.plot.refresh_from_db()
        self.assertEqual(sorted(self.bag.slots.filter(item_id=4001).values_list("usage__remaining", flat=True)), [19, 20])
        batch = str(self.plot.batch_id)
        self.clock.world_minute = 1545; self.clock.save()
        with transaction.atomic():
            maintain_leases(1545)
        self.plot.refresh_from_db()
        self.assertEqual(project_plot(self.plot, 1545)["lease_state"], "HARVEST_ONLY")
        self.assertEqual(project_plot(self.plot, 1545)["harvest_deadline_world_minute"], 2985)
        harvested = self.action("harvest_farm_plot", dict(expected_plot_version=self.plot.plot_version, expected_batch_id=batch))
        self.assertTrue(harvested["success"], harvested)
        self.plot.refresh_from_db()
        self.assertIsNone(self.plot.lessee_player_id)
        self.assertEqual(self.plot.status, "EMPTY")
        self.assertIn(self.bag.slots.get(item_id=1030).item_amount, (4, 5, 6))

    def test_failed_seed_consumption_rolls_back_tool_and_expiry_releases_once(self):
        self.assertTrue(self.lease()["success"])
        tool = InventorySlot.objects.create(bag=self.bag, slot_index=0, item_id=4001, item_amount=1, remaining_uses=20)
        response = self.action("plant_farm_plot", dict(crop_code="CORN", expected_plot_version=1))
        self.assertFalse(response["success"])
        tool.refresh_from_db(); self.plot.refresh_from_db()
        self.assertEqual(tool.remaining_uses, 20)
        self.assertEqual(self.plot.plot_version, 1)
        with transaction.atomic():
            maintain_leases(1540)
            maintain_leases(1540)
        self.plot.refresh_from_db()
        self.assertIsNone(self.plot.lessee_player_id)
        self.assertEqual(self.plot.plot_version, 2)
