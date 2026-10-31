"""Only entry-point and destructive-reset integration, on isolated SQLite."""
import time
import uuid
from io import StringIO

from django.core.management import call_command
from django.test import TestCase, override_settings

from inventory.models import NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot
from village import caravan_orders
from village.agent_queries import execute_agent_query
from village.caravan_presentation import public_caravan
from village.models import (NpcProfile, NpcRuntimeState, VillageCaravanOrder,
    VillageCaravanOrderLine, VillageCaravanOrderReceipt, VillageCaravanOrderDelivery,
    VillageCaravanOrderCompletion, VillageCaravanReward, VillageCaravanVisit,
    VillageDailyEvent, VillageDailyEventTemplate, VillageFarmPlot, VillageFarmExpansionSite,
    VillageSimulationConfig, VillageWorldEvent, EconomicLedgerEntry)
from village.phase4_visibility import public_story_event_fields
from village.player_actions import execute_player_action
from village.runtime_services import CommandService
from village.star_actions import accept_action
from village.tests import test_caravan_orders as fixtures
from village.tests import test_reset_village_world as reset_fixtures


class CaravanEntryIntegrationTests(TestCase):
    setUp = fixtures.CaravanOrderTests.setUp
    set_minute = fixtures.CaravanOrderTests.set_minute
    publish_and_arrive = fixtures.CaravanOrderTests.publish_and_arrive
    params = fixtures.CaravanOrderTests.params

    def test_native_agent_tool_discovery_settlement_and_replay(self):
        order, _ = self.publish_and_arrive()
        npc, _ = NpcDefinition.objects.get_or_create(agent_id="10011", defaults={"name": "Bill"})
        NpcProfile.objects.update_or_create(npc=npc, defaults=dict(display_name="Bill", is_active=True,
            capabilities=["INSPECT_CARAVAN_ORDERS", "DELIVER_CARAVAN_ORDER"]))
        NpcRuntimeState.objects.update_or_create(npc=npc, defaults=dict(
            current_location_code="TRADE_ROAD", action_status="IDLE", health_fp=1000))
        bag, _ = NpcGlobalInventory.objects.update_or_create(npc=npc, defaults={"money": 10})
        slot = NpcGlobalInventorySlot.objects.create(bag=bag, slot_index=0, item_id=1030, item_amount=2, quality=4)
        self.assertTrue(execute_agent_query("10011", "INSPECT_CARAVAN_ORDERS")[0])
        # Turning off future publication preserves the published promise.
        self.cfg.caravan_orders_enabled = False
        self.cfg.save(update_fields=["caravan_orders_enabled"])
        self.assertIn("DELIVER_CARAVAN_ORDER", CommandService.allowed_actions_for(npc))
        message = dict(id=str(uuid.uuid4()), timestamp=int(time.time() * 1000), type="message",
            sender="10011", recipient="WuxiVillage", payload=dict(type="action", content=dict(
                id="deliver-1", name="DELIVER_CARAVAN_ORDER", params=self.params(order, slot, 1))))
        ok, code, task, replayed = accept_action(message)
        self.assertTrue(ok, (code, task.result))
        self.assertFalse(replayed)
        self.assertIsNone(task.command_id)
        self.assertGreater(task.result["data"]["total_income"], 0)
        self.assertTrue(accept_action(message)[3])
        self.assertEqual(EconomicLedgerEntry.objects.filter(request_id=task.pk).count(), 1)
        slot.refresh_from_db()
        self.assertEqual(slot.item_amount, 1)

    def test_player_entry_refreshes_wallet_and_public_view_has_no_private_batches(self):
        order, _ = self.publish_and_arrive()
        answer = execute_player_action(dict(player_id=str(self.player.pk), request_id=str(uuid.uuid4()),
            action="deliver_caravan_order", params=self.params(order, self.corn, 1), current_location_code="TRADE_ROAD"))
        self.assertTrue(answer["success"], answer)
        self.bag.refresh_from_db()
        self.assertEqual(answer["data"]["overview"]["money"], self.bag.money)
        public = public_caravan("NORMAL")["caravan_orders"]
        self.assertTrue(public["active_order"])
        self.assertTrue(all(not line["deliverable_batches"] for line in public["active_order"]["lines"]))
        event = VillageWorldEvent.objects.get(event_type="CARAVAN_ORDER_PUBLISHED")
        self.assertEqual(public_story_event_fields(event)["order_id"], str(order.pk))


@override_settings(DEBUG=True, DEPLOYMENT_ENV="development")
class DevelopmentResetIntegrationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        reset_fixtures.ResetVillageWorldTests.setUpTestData.__func__(cls)

    def test_reset_clears_protected_orders_and_old_daily_events_then_relocks_new_land(self):
        from village.farm_expansion import ensure_expansion_sites
        cfg = VillageSimulationConfig.objects.get(config_key="default")
        cfg.farm_expansion_enabled = True
        cfg.save(update_fields=["farm_expansion_enabled"])
        ensure_expansion_sites(100)
        template = VillageDailyEventTemplate.objects.create(code="reset-development", rule_type="FARM_PREPARATION")
        VillageDailyEvent.objects.create(template=template, source_id="old", active_key="old-development",
            opened_world_minute=100, invite_until_world_minute=200)
        visit = VillageCaravanVisit.objects.create(day_index=123, route="NORMAL")
        order = VillageCaravanOrder.objects.create(period_index=123, announcement_world_minute=100,
            deadline_world_minute=200, budget_total=100)
        line = VillageCaravanOrderLine.objects.create(order=order, item_id=1030, target_quantity=1)
        receipt = VillageCaravanOrderReceipt.objects.create(request_id=uuid.uuid4(), actor_type="NPC",
            actor_id="10011", payload_hash="a" * 64, world_minute=100)
        VillageCaravanOrderDelivery.objects.create(receipt=receipt, line=line, visit=visit, actor_type="NPC",
            actor_id="10011", source_instance_id=uuid.uuid4(), quantity=1, quality=1, unit_price=1,
            total_income=1, world_minute=100)
        completion = VillageCaravanOrderCompletion.objects.create(order=order, request_id=uuid.uuid4(), world_minute=100)
        VillageCaravanReward.objects.create(completion=completion, earned_world_minute=100, visit=visit)
        for _ in range(2):
            call_command("reset_village_world", "--confirm", "RESET_WUXI_VILLAGE", stdout=StringIO())
        for model in (VillageCaravanOrder, VillageCaravanOrderReceipt, VillageCaravanOrderDelivery,
                      VillageCaravanReward, VillageDailyEvent):
            self.assertFalse(model.objects.exists(), model.__name__)
        self.assertEqual(VillageFarmExpansionSite.objects.count(), 2)
        self.assertEqual(VillageFarmPlot.objects.filter(expansion_site_code__in=["E", "F"], reclamation_status="LOCKED").count(), 18)
        self.assertTrue(VillageDailyEventTemplate.objects.filter(pk=template.pk).exists())
