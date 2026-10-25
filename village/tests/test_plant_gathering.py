from datetime import timedelta
import uuid
import json
from time import perf_counter
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.models import InventoryBag, InventorySlot
from items.models import Item
from player.models import PlayerSkillProgress, PlayerState
from village.models import VillagePlantNode, VillagePlayerGathering, VillageSimulationConfig
from village.plant_catalog import NODES, PLANTS
from village.plant_gathering import overview
from village.player_actions import execute_player_action


class PlantGatheringTests(TestCase):
    def test_plant_poll_is_read_only_and_avoids_full_panel_projection(self):
        from village.models import VillagePlayerActionReceipt
        receipt_count = VillagePlayerActionReceipt.objects.count()
        started = perf_counter()
        with CaptureQueriesContext(connection) as compact_queries, patch("village.player_actions.overview", side_effect=AssertionError("full projection")):
            compact = self.call("plant_state", read_only=True)
        compact_ms = (perf_counter()-started)*1000
        self.assertTrue(compact["success"], compact)
        self.assertEqual(len(compact["data"]["plants"]["nodes"]), len(NODES))
        self.assertNotIn("overview", compact["data"])
        writes = [query["sql"] for query in compact_queries if query["sql"].lstrip().split(" ", 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"}]
        self.assertEqual(writes, [])
        self.assertEqual(VillagePlayerActionReceipt.objects.count(), receipt_count)
        started = perf_counter()
        with CaptureQueriesContext(connection) as full_queries:
            full = self.call("overview", read_only=True)
        full_ms = (perf_counter()-started)*1000
        self.assertTrue(full["success"], full)
        self.assertLess(len(compact_queries), len(full_queries)/2)
        print(f"Plant refresh: compact={len(compact_queries)} queries/{compact_ms:.1f}ms/{len(json.dumps(compact))} bytes; "
              f"full={len(full_queries)} queries/{full_ms:.1f}ms/{len(json.dumps(full))} bytes")

    def test_start_and_cancel_do_not_refresh_inventory_or_economy(self):
        with patch("village.player_actions.overview", side_effect=AssertionError("full projection")), \
                patch("village.daily_events.evaluate_daily_events", side_effect=AssertionError("world update")):
            attempt = self.start()
            cancelled = self.call("plant_cancel", dict(attempt_id=attempt["attempt_id"]))
        self.assertTrue(cancelled["success"], cancelled)
        self.assertIsNone(cancelled["data"]["plants"]["attempt"])
        self.assertEqual(self.ap(), 200)

    def setUp(self):
        VillageSimulationConfig.objects.update_or_create(config_key="default", defaults=dict(village_economy_enabled=True))
        self.clock = GlobalGameTimeState.get_instance()
        self.clock.world_minute = 100
        self.clock.save()
        self.player = GameAccount.objects.create(username="plant-gatherer")
        self.other = GameAccount.objects.create(username="plant-neighbour")
        self.bag = InventoryBag.objects.create(player=self.player, character_id=self.player.username)
        self.other_bag = InventoryBag.objects.create(player=self.other, character_id=self.other.username)
        for player in (self.player, self.other):
            PlayerState.objects.create(player_id=str(player.pk), action_points=200)
        for spec in PLANTS.values():
            Item.objects.get_or_create(item_id=spec["item_id"], defaults=dict(item_name="采集原料", item_price=4))
        self.node = next(key for key, value in NODES.items() if value["species"] == "OYSTER")
        self.session = str(uuid.uuid4())

    def call(self, action, params=None, *, player=None, rid=None, location="FOREST_EDGE", **context):
        return execute_player_action(dict(player_id=str((player or self.player).pk), request_id=str(rid or uuid.uuid4()),
            action=action, params=params or {}, current_location_code=location,
            authority_session_id=self.session, **context))

    def start(self, node=None):
        reply = self.call("plant_start", dict(node_id=node or self.node))
        self.assertTrue(reply["success"], reply)
        return reply["data"]["result"]["attempt"]

    def finish(self, attempt, **kwargs):
        ready = timezone.now()+timedelta(seconds=10)
        with patch("village.plant_gathering.timezone.now", return_value=ready):
            return self.call("collect", dict(node_id=attempt["node_id"], attempt_id=attempt["attempt_id"]), **kwargs)

    def ap(self):
        return PlayerState.objects.get(player_id=str(self.player.pk)).action_points

    def tool(self, item_id=4101, uses=20):
        PlayerSkillProgress.objects.update_or_create(player=self.player, skill_code="gathering", defaults=dict(total_xp=440))
        return InventorySlot.objects.create(bag=self.bag, item_id=item_id, item_amount=1, slot_index=10, remaining_uses=uses)

    def test_completion_has_exact_plant_output_and_one_charge_on_replay(self):
        attempt = self.start()
        self.assertEqual(self.ap(), 200)
        rid = uuid.uuid4()
        reply = self.finish(attempt, rid=rid)
        self.assertTrue(reply["success"], reply)
        self.assertEqual([(b["item_id"], b["quantity"]) for b in reply["data"]["result"]["collected_items"]], [(1035, 3)])
        self.assertEqual(self.ap(), 195)
        repeated = self.finish(attempt, rid=rid, replay_only=True)
        self.assertTrue(repeated["success"], repeated)
        self.assertEqual(self.ap(), 195)
        self.assertEqual(self.bag.slots.filter(item_id=1035).count(), 1)

    def test_bill_house_node_uses_its_trusted_location_for_start_and_collection(self):
        node_id, definition = next((key, row) for key, row in NODES.items()
            if row.get("location_code") == "BILL_FARM")
        spec = PLANTS[definition["species"]]
        projected = next(row for row in overview(self.player, self.bag, self.clock.world_minute)["nodes"]
            if row["node_id"] == node_id)
        self.assertEqual(projected["location_code"], "BILL_FARM")
        self.assertEqual(projected["required_level"], 1)
        self.assertEqual(self.call("plant_start", dict(node_id=node_id))["result_code"], "LOCATION_INVALID")
        forged = self.call("plant_start", dict(node_id=node_id, location_code="FOREST_EDGE"), location="BILL_FARM")
        self.assertEqual(forged["result_code"], "INVALID_PARAMS")
        started = self.call("plant_start", dict(node_id=node_id), location="BILL_FARM")
        self.assertTrue(started["success"], started)
        attempt = started["data"]["result"]["attempt"]
        self.assertEqual(self.finish(attempt)["result_code"], "LOCATION_INVALID")
        self.assertEqual(self.ap(), 200)
        collected = self.finish(attempt, location="BILL_FARM")
        self.assertTrue(collected["success"], collected)
        self.assertEqual([(row["item_id"], row["quantity"]) for row in collected["data"]["result"]["collected_items"]],
            [(spec["item_id"], spec["quantity"])])
        self.assertEqual(self.ap(), 200-spec["action_cost"])

    def test_early_finish_and_cancellation_never_charge_or_deplete(self):
        attempt = self.start()
        early = self.call("collect", dict(node_id=self.node, attempt_id=attempt["attempt_id"]))
        self.assertEqual(early["result_code"], "PLANT_NOT_READY")
        cancelled = self.call("plant_cancel", dict(attempt_id=attempt["attempt_id"]))
        self.assertTrue(cancelled["success"], cancelled)
        self.assertEqual(self.ap(), 200)
        self.assertEqual(VillagePlantNode.objects.get(pk=self.node).revision, 0)
        self.assertFalse(self.bag.slots.filter(item_id=1035).exists())

    def test_two_players_share_the_same_plant_and_lease_recovers(self):
        attempt = self.start()
        occupied = self.call("plant_start", dict(node_id=self.node), player=self.other)
        self.assertEqual(occupied["result_code"], "PLANT_OCCUPIED")
        with patch("village.plant_gathering.timezone.now", return_value=timezone.now()+timedelta(seconds=40)):
            recovered = self.call("plant_start", dict(node_id=self.node), player=self.other)
        self.assertTrue(recovered["success"], recovered)
        self.assertEqual(self.finish(attempt)["result_code"], "PLANT_ATTEMPT_INVALID")

    def test_advanced_plants_require_skill_and_a_level_five_tool(self):
        node = next(key for key, value in NODES.items() if value["species"] == "DEEP_MUSHROOM")
        low = self.call("plant_start", dict(node_id=node), location="DEEP_FOREST")
        self.assertEqual(low["result_code"], "SKILL_LEVEL_REQUIRED")
        PlayerSkillProgress.objects.create(player=self.player, skill_code="gathering", total_xp=1980)
        self.assertEqual(self.call("plant_start", dict(node_id=node), location="DEEP_FOREST")["result_code"], "TOOL_LEVEL_REQUIRED")
        tool = self.tool()
        tool.equipment_level = 5; tool.save(update_fields=["equipment_level"])
        PlayerSkillProgress.objects.filter(player=self.player, skill_code="gathering").update(total_xp=1980)
        self.call("equip_gathering_tool", dict(instance_id=str(tool.instance_id)))
        started = self.call("plant_start", dict(node_id=node), location="DEEP_FOREST")
        self.assertTrue(started["success"], started)
        result = self.finish(started["data"]["result"]["attempt"], location="DEEP_FOREST")
        self.assertTrue(result["success"], result)
        self.assertEqual(result["data"]["result"]["collected_items"][0]["item_id"], 29308)

    def test_equipped_owned_tool_reduces_duration_and_wears_only_after_success(self):
        tool = self.tool()
        equipped = self.call("equip_gathering_tool", dict(instance_id=str(tool.instance_id)))
        self.assertTrue(equipped["success"], equipped)
        attempt = self.start()
        self.assertEqual(attempt["gather_seconds"], 3.6)
        tool.refresh_from_db(); self.assertEqual(tool.remaining_uses, 20)
        self.assertTrue(self.finish(attempt)["success"])
        tool.refresh_from_db(); self.assertEqual(tool.remaining_uses, 19)

    def test_t2_tool_is_faster_and_cannot_change_during_harvest(self):
        tool = self.tool(4102, 50)
        self.assertTrue(self.call("equip_gathering_tool", dict(instance_id=str(tool.instance_id)))["success"])
        attempt = self.start()
        self.assertEqual(attempt["gather_seconds"], 3.0)
        blocked = self.call("equip_gathering_tool", dict(instance_id=None))
        self.assertEqual(blocked["result_code"], "GATHERING_ACTIVE")
        self.call("plant_cancel", dict(attempt_id=attempt["attempt_id"]))
        self.assertTrue(self.call("equip_gathering_tool", dict(instance_id=None))["success"])

    def test_precision_improves_quality_without_creating_extra_materials(self):
        tool = self.tool(4102, 50)
        self.call("equip_gathering_tool", dict(instance_id=str(tool.instance_id)))
        with patch("village.plant_gathering.production_quality", return_value=4):
            attempt = self.start()
        self.assertNotIn("quality", attempt)
        self.assertEqual(attempt["quality_bonus_chance"], .08)
        with patch("village.plant_gathering.random.random", return_value=0):
            reply = self.finish(attempt)
        self.assertTrue(reply["success"], reply)
        batch = reply["data"]["result"]["collected_items"][0]
        self.assertEqual((batch["quality"], batch["quantity"]), (4, 3))

    def test_persisted_attempt_before_precision_update_keeps_its_original_quality(self):
        attempt = self.start()
        state = VillagePlayerGathering.objects.get(player=self.player)
        state.attempt.pop("quality_bonus_chance")
        state.attempt.pop("quality", None)
        state.attempt.pop("quality_rule", None)
        state.save(update_fields=["attempt"])
        reply = self.finish(attempt)
        self.assertTrue(reply["success"], reply)
        self.assertEqual(reply["data"]["result"]["collected_items"][0]["quality"], 3)

    def test_failed_receipt_rolls_back_materials_durability_and_plant_together(self):
        tool = self.tool()
        self.call("equip_gathering_tool", dict(instance_id=str(tool.instance_id)))
        attempt = self.start()
        with patch("village.player_actions.VillagePlayerActionReceipt.objects.create", side_effect=RuntimeError("receipt failed")):
            with self.assertRaises(RuntimeError):
                self.finish(attempt)
        tool.refresh_from_db()
        self.assertEqual((self.ap(), tool.remaining_uses), (200, 20))
        self.assertFalse(self.bag.slots.filter(item_id=1035).exists())
        self.assertEqual(VillagePlantNode.objects.get(pk=self.node).revision, 0)
        self.assertEqual(VillagePlayerGathering.objects.get(player=self.player).attempt["attempt_id"], attempt["attempt_id"])

    def test_foreign_broken_and_removed_tools_cannot_generate_rewards(self):
        tool = self.tool()
        self.assertEqual(self.call("equip_gathering_tool", dict(instance_id=str(tool.instance_id)), player=self.other)["result_code"], "TOOL_MISSING")
        self.call("equip_gathering_tool", dict(instance_id=str(tool.instance_id)))
        attempt = self.start()
        tool.remaining_uses = 0; tool.save(update_fields=["remaining_uses"])
        reply = self.finish(attempt)
        self.assertEqual(reply["result_code"], "TOOL_BROKEN")
        self.assertEqual(self.ap(), 200)
        self.assertFalse(self.bag.slots.filter(item_id=1035).exists())

    def test_regrowth_is_world_time_and_read_only_projection_does_not_write(self):
        self.assertTrue(self.finish(self.start())["success"])
        self.assertEqual(self.call("plant_start", dict(node_id=self.node))["result_code"], "PLANT_REGROWING")
        self.clock.world_minute += 360; self.clock.save()
        with CaptureQueriesContext(connection) as queries:
            plants = overview(self.player, self.bag, self.clock.world_minute)
        self.assertTrue(next(n for n in plants["nodes"] if n["node_id"] == self.node)["ready"])
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        self.assertTrue(self.call("plant_start", dict(node_id=self.node))["success"])

    def test_reconnected_session_cannot_finish_previous_harvest(self):
        attempt = self.start(); self.session = str(uuid.uuid4())
        self.assertEqual(self.finish(attempt)["result_code"], "PLANT_ATTEMPT_INVALID")
        self.assertEqual(self.ap(), 200)

    def test_world_collection_rejects_legacy_selectors_and_client_reward_parameters(self):
        old = self.call("collect", dict(node_id=self.node, attempt_id=str(uuid.uuid4()), collect_type="采集食材"))
        self.assertEqual(old["result_code"], "INVALID_PARAMS")
        forged = self.call("plant_start", dict(node_id=self.node, gather_seconds=0, quantity=100))
        self.assertEqual(forged["result_code"], "INVALID_PARAMS")
        self.assertEqual(self.ap(), 200)
