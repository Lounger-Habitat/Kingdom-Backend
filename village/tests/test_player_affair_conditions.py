import uuid
from importlib import import_module

from django.apps import apps
from django.test import TestCase
from django.utils import timezone

from village.affairs import create_resource_affair
from village.models import VillageAffairContribution, VillagePlayerAffairLabor, VillageLocationDefinition
from village.player_actions import execute_player_action
from village.player_affair_labor import tick_player_labor
from village.tests.test_player_actions import PlayerFixture


class PlayerFlexibleAffairTests(PlayerFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.nonce = str(uuid.uuid4())
        for code in ("OLD_BRIDGE", "RESTAURANT"):
            VillageLocationDefinition.objects.get_or_create(location_code=code, defaults=dict(
                display_name=code, center_anchor_code=code, location_type="PUBLIC"))

    def affair(self, *stages, locations=None, stage_locations=None):
        return create_resource_affair("FLEX_TEST", "可配置事务", dict(stages=[
            dict(id=f"stage_{i}", title=f"阶段{i}", conditions=conditions,
                 location_code=(stage_locations or {}).get(i, ""))
            for i, conditions in enumerate(stages)]), locations or [], self.clock.world_minute)

    def action(self, action, condition_id=None, **params):
        location = params.pop("location", "")
        rid = params.pop("rid", None)
        parameters = dict(affair_id="FLEX_TEST", **params)
        if condition_id is not None:
            parameters["condition_id"] = condition_id
        payload = self.payload(action, parameters, location=location, rid=rid)
        payload["authority_session_id"] = self.nonce
        return execute_player_action(payload)

    def tick(self, labor, advance, **overrides):
        self.clock.world_minute += advance
        self.clock.time_revision += 1
        self.clock.save()
        return tick_player_labor(dict(player_id=str(self.a.pk), labor_id=labor["labor_id"],
            authority_session_id=self.nonce, current_location_code="", present=True,
            observed_world_minute=self.clock.world_minute, **overrides))

    def test_items_only_complete_with_alternatives_no_meal_gold_or_labor(self):
        row = self.affair([dict(id="supplies", type="item", item_ids=[1030, 1002], target=3)])
        self.slot(2, item_id=1030)
        self.slot(1, item_id=1002)
        first = self.action("contribute", "supplies", kind="item", item_id=1030, quantity=2)
        self.assertTrue(first["success"], first)
        rid = uuid.uuid4()
        done = self.action("contribute", "supplies", kind="item", item_id=1002, quantity=1, rid=rid)
        self.assertTrue(done["success"], done)
        self.assertEqual(done, self.action("contribute", "supplies", kind="item", item_id=1002, quantity=1, rid=rid))
        row.refresh_from_db()
        self.assertEqual(row.status, "COMPLETED")
        self.assertEqual(row.progress["conditions"]["supplies"], 3)
        self.assertEqual(sum(s.item_amount for s in self.bag_a.slots.all()), 0)
        self.bag_a.refresh_from_db()
        self.assertEqual(self.bag_a.money, 100)
        self.assertFalse(VillagePlayerAffairLabor.objects.exists())

    def test_gold_only_completes_and_deducts_exact_amount(self):
        row = self.affair([dict(id="fundraising", type="gold", target=70)])
        result = self.action("contribute", "fundraising", kind="fund", quantity=70)
        self.assertTrue(result["success"], result)
        row.refresh_from_db(); self.bag_a.refresh_from_db()
        self.assertEqual((row.status, self.bag_a.money), ("COMPLETED", 30))

    def test_ambiguous_item_needs_explicit_condition_and_counts_only_once(self):
        row = self.affair([dict(id="first", type="item", item_ids=[1030], target=1),
                          dict(id="second", type="item", item_ids=[1030], target=1)])
        slot = self.slot(2)
        self.assertFalse(self.action("contribute", kind="item", item_id=1030, quantity=1)["success"])
        slot.refresh_from_db(); self.assertEqual(slot.item_amount, 2)
        result = self.action("contribute", "first", kind="item", item_id=1030, quantity=1)
        self.assertTrue(result["success"], result)
        row.refresh_from_db()
        self.assertEqual(row.progress["conditions"]["first"], 1)
        self.assertEqual(row.progress["conditions"].get("second", 0), 0)
        self.assertEqual(VillageAffairContribution.objects.get(affair=row).condition_id, "first")

    def test_future_stage_rejects_without_consumption_then_opens(self):
        row = self.affair([dict(id="gold", type="gold", target=5)],
                          [dict(id="items", type="item", target=2, item_ids=[1030])])
        slot = self.slot(2)
        self.assertFalse(self.action("contribute", "items", kind="item", item_id=1030, quantity=2)["success"])
        slot.refresh_from_db(); self.assertEqual(slot.item_amount, 2)
        self.assertTrue(self.action("contribute", "gold", kind="fund", quantity=5)["success"])
        self.assertTrue(self.action("contribute", "items", kind="item", item_id=1030, quantity=2)["success"])
        row.refresh_from_db(); self.assertEqual(row.status, "COMPLETED")

    def test_condition_quality_freshness_and_location_are_enforced_for_any_item(self):
        row = self.affair([dict(id="fresh", type="item", target=1, item_ids=[1030], min_quality=3,
            requires_freshness=True, min_remaining_minutes=50)], stage_locations={0: "RESTAURANT"})
        bad_slots = [self.slot(1, quality=2), self.slot(1, quality=3, expires_world_minute=149),
                     self.slot(1, quality=3, expires_world_minute=None),
                     self.slot(1, quality=3, freshness_unknown=True)]
        self.assertFalse(self.action("contribute", "fresh", kind="item", item_id=1030, quantity=1,
                                    location="RESTAURANT")["success"])
        valid = self.slot(1, quality=3, expires_world_minute=150)
        self.assertEqual(self.action("contribute", "fresh", kind="item", item_id=1030, quantity=1,
                                    location="OLD_BRIDGE")["result_code"], "LOCATION_INVALID")
        valid.refresh_from_db(); self.assertEqual(valid.item_amount, 1)
        self.assertTrue(self.action("contribute", "fresh", kind="item", item_id=1030, quantity=1,
                                   location="RESTAURANT")["success"])
        for slot in bad_slots:
            slot.refresh_from_db(); self.assertEqual(slot.item_amount, 1)
        row.refresh_from_db(); self.assertEqual(row.status, "COMPLETED")

    def test_nonperishable_condition_still_rejects_expired_or_unknown_stock(self):
        self.affair([dict(id="items", type="item", target=1, item_ids=[1030])])
        self.slot(1, expires_world_minute=100)
        self.slot(1, freshness_unknown=True)
        result = self.action("contribute", "items", kind="item", item_id=1030, quantity=1)
        self.assertEqual(result["result_code"], "INSUFFICIENT_QUALIFIED_ITEMS")

    def test_minimum_remaining_time_requires_known_expiry_without_freshness_checkbox(self):
        row = self.affair([dict(id="items", type="item", target=1, item_ids=[1030],
                               requires_freshness=False, min_remaining_minutes=30)])
        unknown = self.slot(1, expires_world_minute=None)
        short = self.slot(1, expires_world_minute=self.clock.world_minute+29)
        result = self.action("contribute", "items", kind="item", item_id=1030, quantity=1)
        self.assertEqual(result["result_code"], "INSUFFICIENT_QUALIFIED_ITEMS")
        unknown.refresh_from_db(); short.refresh_from_db()
        self.assertEqual((unknown.item_amount, short.item_amount), (1, 1))
        valid = self.slot(1, expires_world_minute=self.clock.world_minute+30)
        result = self.action("contribute", "items", kind="item", item_id=1030, quantity=1)
        self.assertTrue(result["success"], result)
        valid.refresh_from_db(); unknown.refresh_from_db(); short.refresh_from_db(); row.refresh_from_db()
        self.assertEqual((valid.item_amount, unknown.item_amount, short.item_amount), (0, 1, 1))
        self.assertEqual(row.status, "COMPLETED")

    def test_labor_only_anywhere_uses_configured_duration_and_multiple_units(self):
        row = self.affair([dict(id="work", type="labor", target=4, unit_minutes=3)])
        result = self.action("labor_start", "work")
        self.assertTrue(result["success"], result)
        labor = result["data"]["result"]
        self.assertEqual((labor["condition_id"], labor["unit_minutes"], labor["locations"]), ("work", 3, []))
        first = self.tick(labor, 8)
        self.assertEqual(first["data"]["credited_units"], 2)
        self.assertEqual(first["data"]["labor"]["accumulated_minutes"], 2)
        done = self.tick(labor, 4)
        self.assertEqual(done["data"]["credited_units"], 2)
        self.assertEqual(self.tick(labor, 0)["data"]["credited_units"], 0)
        row.refresh_from_db(); self.assertEqual(row.status, "COMPLETED")

    def test_labor_session_cannot_spill_into_next_stage(self):
        row = self.affair([dict(id="first", type="labor", target=1, unit_minutes=5)],
                          [dict(id="second", type="labor", target=1, unit_minutes=5)])
        start = self.action("labor_start", "first")
        self.assertTrue(start["success"], start)
        done = self.tick(start["data"]["result"], 20)
        self.assertEqual(done["data"]["credited_units"], 1)
        self.assertEqual(done["data"]["labor"]["status"], "COMPLETED")
        row.refresh_from_db()
        self.assertEqual(row.progress["conditions"].get("second", 0), 0)
        start = self.action("labor_start", "second")
        self.assertTrue(start["success"], start)
        self.tick(start["data"]["result"], 5)
        row.refresh_from_db(); self.assertEqual(row.status, "COMPLETED")

    def test_unrestricted_labor_still_requires_continuous_online_presence(self):
        self.affair([dict(id="work", type="labor", target=1, unit_minutes=5)])
        start = self.action("labor_start", "work")
        self.assertTrue(start["success"], start)
        labor = start["data"]["result"]
        self.tick(labor, 4)
        self.clock.world_minute += 1; self.clock.save()
        stopped = tick_player_labor(dict(player_id=str(self.a.pk), labor_id=labor["labor_id"],
            authority_session_id=self.nonce, current_location_code="", present=False,
            observed_world_minute=self.clock.world_minute))
        self.assertEqual(stopped["data"]["labor"]["status"], "CANCELLED")
        self.assertFalse(VillageAffairContribution.objects.filter(kind="labor").exists())

    def test_stage_location_overrides_default_and_next_stage_inherits_default(self):
        row = self.affair([dict(id="fund", type="gold", target=5)],
                          [dict(id="items", type="item", target=1, item_ids=[1030])],
                          locations=["OLD_BRIDGE"], stage_locations={0: "RESTAURANT"})
        slot = self.slot(1)
        result = self.action("contribute", "fund", kind="fund", quantity=5, location="OLD_BRIDGE")
        self.assertEqual(result["result_code"], "LOCATION_INVALID")
        self.bag_a.refresh_from_db(); self.assertEqual(self.bag_a.money, 100)
        self.assertFalse(self.action("contribute", "items", kind="item", item_id=1030, quantity=1,
                                     location="OLD_BRIDGE")["success"])
        result = self.action("contribute", "fund", kind="fund", quantity=5, location="RESTAURANT")
        self.assertTrue(result["success"], result)
        result = self.action("contribute", "items", kind="item", item_id=1030, quantity=1, location="RESTAURANT")
        self.assertEqual(result["result_code"], "LOCATION_INVALID")
        slot.refresh_from_db(); self.assertEqual(slot.item_amount, 1)
        result = self.action("contribute", "items", kind="item", item_id=1030, quantity=1, location="OLD_BRIDGE")
        self.assertTrue(result["success"], result)
        row.refresh_from_db(); self.assertEqual(row.status, "COMPLETED")

    def test_migration_preserves_credited_work_and_discards_only_unfinished_session(self):
        row = self.affair([dict(id="legacy_labor", type="labor", target=4, unit_minutes=60)])
        row.progress["conditions"]["legacy_labor"] = 2
        row.save()
        labor = VillagePlayerAffairLabor.objects.create(player=self.a, affair=row,
            authority_session_id=self.nonce, location_code="", accumulated_minutes=45, completed_units=2,
            last_world_minute=self.clock.world_minute, last_time_revision=self.clock.time_revision,
            last_seen_at=timezone.now())
        import_module("village.migrations.0050_player_labor_conditions").interrupt_legacy_sessions(apps, None)
        labor.refresh_from_db(); row.refresh_from_db()
        self.assertEqual((labor.condition_id, labor.unit_minutes, labor.completed_units), ("legacy_labor", 60, 2))
        self.assertEqual((labor.status, labor.accumulated_minutes, labor.stop_reason),
                         ("CANCELLED", 0, "CONDITIONS_MIGRATED"))
        self.assertEqual(row.progress["conditions"]["legacy_labor"], 2)
