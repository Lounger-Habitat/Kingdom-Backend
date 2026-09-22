import uuid

from django.test import TestCase

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.models import InventoryBag, InventorySlot, NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot
from items.models import Item
from player.models import PlayerState
from village.affairs import contribute, resolve_condition, AffairError, project_affair, normalize_definition
from village.farm_irrigation import ensure_irrigation_affair, project_irrigation, CLEAR_CONDITIONS, WORKSITES
from village.models import VillageFarmPlot, VillageLocationDefinition, VillageSimulationConfig
from village.phase3_catalog import farm_crop_catalog
from village.phase3_production import _do_plant
from village.player_actions import execute_player_action, ActionError
from village.player_affair_labor import start_labor
from village.player_farm import farm_overview


class FarmIrrigationTests(TestCase):
    def setUp(self):
        self.clock = GlobalGameTimeState.get_instance()
        self.clock.world_minute = 100
        self.clock.save()
        VillageLocationDefinition.objects.update_or_create(location_code="BILL_FARM", defaults={
            "display_name": "农田", "center_anchor_code": "BILL_FARM", "location_type": "FARM", "is_active": True})
        for item_id in (1002, 4001, 1051, 1030):
            Item.objects.update_or_create(item_id=item_id, defaults={"item_name": str(item_id), "item_type": "Commodity", "item_price": 5})
        self.npc, _ = NpcDefinition.objects.get_or_create(agent_id="10011", defaults={"name": "Bill"})
        self.affair = ensure_irrigation_affair(100)

    def advance(self, condition_id, kind, amount, item_id=None):
        for _ in range(amount if kind == "labor" else 1):
            result = contribute(self.affair, kind, 1 if kind == "labor" else amount,
                                condition_id=condition_id, item_id=item_id,
                                world_minute=100, request_id=uuid.uuid4(), actor_npc=self.npc)
        return result

    def complete(self):
        for condition in CLEAR_CONDITIONS:
            self.advance(condition, "labor", 1)
        self.advance("canal_wood", "item", 30, 1002)
        self.advance("canal_gold", "gold", 100)
        self.advance("water_labor", "labor", 1)

    def test_stages_and_idempotent_seed(self):
        self.assertEqual(project_irrigation()["visual_state"], "SILTED")
        self.assertEqual(farm_crop_catalog()["CORN"]["growth_minutes"], 1440)
        for condition, (site, _, _) in zip(CLEAR_CONDITIONS, WORKSITES):
            self.assertEqual(resolve_condition(self.affair, condition)["locations"], [site])
        with self.assertRaises(ActionError) as error:
            start_labor(None, self.affair, uuid.uuid4(), "BILL_FARM", self.clock, uuid.uuid4(), CLEAR_CONDITIONS[0])
        self.assertEqual(error.exception.code, "LOCATION_INVALID")
        # Any section may be cleared first; its one labor cannot be repeated.
        self.advance("clear_east", "labor", 1)
        self.assertEqual(project_irrigation()["cleared_sections"], ["clear_east"])
        self.assertEqual(project_irrigation()["visual_state"], "SILTED")
        with self.assertRaises(AffairError):
            self.advance("clear_east", "labor", 1)
        for condition in CLEAR_CONDITIONS[:-1]:
            self.advance(condition, "labor", 1)
        self.assertEqual(project_irrigation()["visual_state"], "CLEARED")
        self.assertEqual(ensure_irrigation_affair(999).progress, self.affair.progress)
        self.assertEqual(resolve_condition(self.affair, "canal_wood")["locations"], ["FARM_CANAL_MATERIALS"])
        self.advance("canal_wood", "item", 30, 1002)
        self.advance("canal_gold", "gold", 100)
        self.assertEqual(project_irrigation()["visual_state"], "CONSTRUCTION")
        self.assertEqual(resolve_condition(self.affair, "water_labor")["locations"], ["FARM_CANAL_GATE"])
        self.advance("water_labor", "labor", 1)
        self.assertEqual(project_irrigation()["visual_state"], "FLOWING")
        self.assertEqual(ensure_irrigation_affair(999).status, "COMPLETED")
        self.assertEqual({v["growth_minutes"] for v in farm_crop_catalog().values()}, {1296})

    def test_upgrade_retains_old_labor_and_explicit_sites_do_not_change_legacy_defaults(self):
        legacy = {"stages": [dict(id="clear_silt", title="清淤", location_code="BILL_FARM", conditions=[
            dict(id="clear_labor", type="labor", target=4, unit_minutes=30)])]}
        self.affair.requirements = legacy
        self.affair.locations = ["BILL_FARM"]
        self.affair.progress = {"conditions": {"clear_labor": 2, "canal_wood": 8, "canal_gold": 10}}
        self.affair.save()
        updated = ensure_irrigation_affair(100)
        self.assertEqual(project_irrigation()["cleared_sections"], list(CLEAR_CONDITIONS[:2]))
        self.assertEqual(updated.progress["conditions"]["canal_wood"], 8)
        self.assertEqual(ensure_irrigation_affair(200).version, updated.version)
        self.assertEqual(project_affair(updated)["stages"][0]["locations"], [s[0] for s in WORKSITES[:4]])
        raw = {"stages": [dict(id="default", conditions=[dict(id="gold", type="gold", target=1, locations=["IGNORED"])])]}
        normal = normalize_definition(raw, ["BILL_FARM"])
        self.assertEqual(normal["stages"][0]["conditions"][0]["locations"], ["BILL_FARM"])

    def test_only_new_player_and_bill_crops_receive_bonus(self):
        bill, _ = NpcGlobalInventory.objects.get_or_create(npc=self.npc)
        bill.slots.all().delete()
        NpcGlobalInventorySlot.objects.create(bag=bill, slot_index=0, item_id=1051, item_amount=2)
        NpcGlobalInventorySlot.objects.create(bag=bill, slot_index=1, item_id=4001, item_amount=1, remaining_uses=20)
        for code in ("FARM_PLOT_A", "FARM_PLOT_B"):
            VillageFarmPlot.objects.update_or_create(plot_code=code, defaults={"status": "EMPTY", "lease_enabled": False})
        old = _do_plant(10011, dict(plot_code="FARM_PLOT_A", crop_code="CORN"), 100, uuid.uuid4())
        self.assertEqual(old["effects"]["plot"]["mature_world_minute"], 1540)
        self.complete()
        new = _do_plant(10011, dict(plot_code="FARM_PLOT_B", crop_code="CORN"), 100, uuid.uuid4())
        self.assertEqual(new["effects"]["plot"]["mature_world_minute"], 1396)
        self.assertEqual(VillageFarmPlot.objects.get(plot_code="FARM_PLOT_B").mature_world_minute, 1396)
        self.assertEqual(VillageFarmPlot.objects.get(plot_code="FARM_PLOT_A").mature_world_minute, 1540)

        VillageSimulationConfig.objects.update_or_create(config_key="default", defaults={"village_economy_enabled": True, "farm_leasing_enabled": True})
        player = GameAccount.objects.create(username="irrigation_farmer")
        bag = InventoryBag.objects.create(player=player, character_id=player.username, money=300)
        PlayerState.objects.create(player_id=str(player.pk), action_points=100)
        InventorySlot.objects.create(bag=bag, slot_index=0, item_id=1051, item_amount=1)
        InventorySlot.objects.create(bag=bag, slot_index=1, item_id=4001, item_amount=1, remaining_uses=20)
        plot, _ = VillageFarmPlot.objects.update_or_create(plot_code="FARM_PLOT_D_01", defaults={"status": "EMPTY", "lease_enabled": True})
        def action(name, **params):
            response = execute_player_action(dict(player_id=str(player.pk), request_id=str(uuid.uuid4()),
                action=name, params=dict(plot_code=plot.plot_code, **params), current_location_code="BILL_FARM"))
            self.assertTrue(response["success"], response.get("message"))
        action("lease_farm_plot", days=1, expected_plot_version=0, expected_daily_price=10)
        action("plant_farm_plot", crop_code="CORN", expected_plot_version=1)
        plot.refresh_from_db()
        self.assertEqual(plot.mature_world_minute, 1396)
        farm = farm_overview(player, bag, 100)
        self.assertEqual(farm["irrigation"]["growth_reduction_percent"], 10)
        self.assertEqual(farm["crops"]["CORN"]["growth_minutes"], 1296)
