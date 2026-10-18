"""Finite funded demand, shared eligibility, and immutable escrow revalidation."""
import copy
import uuid

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from inventory.models import InventorySlot
from items.models import Item
from player.models import PlayerGrowthState, PlayerSkillProgress, SkillRewardReceipt
from player.starter import record_order_completion
from village.business_orders import (BusinessOrdersError, business_orders_overview, execute_business_orders)
from village.models import (VillageBusinessOrderDay, VillageBusinessOrderLink, VillagePersonalCommission,
                            VillagePersonalCommissionEscrow)
from village.personal_commissions import (CommissionError, abandon_commission, accept_commission,
    contribute_commission, held_batch_issues, project_commission, publish_commission)
from village.tests.test_player_actions import PlayerFixture


class BusinessOrderTests(PlayerFixture, TestCase):
    def setUp(self):
        super().setUp()
        for item_id, name, kind in ((1025, "胡萝卜", "蔬菜"), (1042, "土豆", "蔬菜"),
                                    (29204, "三蔬清汤", "食物")):
            Item.objects.update_or_create(pk=item_id, defaults=dict(item_name=name, item_type=kind, item_price=10))

    def prepare(self, player=None, bag=None):
        return execute_business_orders(player or self.a, bag or self.bag_a, {}, uuid.uuid4(), self.clock)

    def level(self, player, xp):
        PlayerSkillProgress.objects.update_or_create(player=player, skill_code="business", defaults=dict(total_xp=xp))

    def row(self, code):
        return VillageBusinessOrderLink.objects.get(template_code=code, day_id=self.clock.world_minute // 1440).commission

    def accept(self, row, player=None):
        return accept_commission(row.pk, "PLAYER", (player or self.a).pk, uuid.uuid4(), self.clock.world_minute)

    def fulfill(self, row, *, player=None, bag=None):
        player, bag = player or self.a, bag or self.bag_a
        result = None
        for stage in row.requirements["stages"]:
            for condition in stage["conditions"]:
                item_id, quantity = condition["item_ids"][0], condition["target"]
                source = self.slot(quantity, item_id=item_id, bag=bag,
                    expires_world_minute=self.clock.world_minute + 1000 if condition["requires_freshness"] else None)
                result = contribute_commission(row.pk, "PLAYER", player.pk, condition["id"], quantity,
                    uuid.uuid4(), self.clock.world_minute, item_id=item_id, instance_id=source.instance_id, location="RESTAURANT")
        return result

    def test_queries_never_publish_initialize_or_reserve_and_ro_is_actionable_only_in_player_mode(self):
        before = self.lucy.money
        with CaptureQueriesContext(connection) as queries:
            view = business_orders_overview(self.a, 100)
            readonly = business_orders_overview(self.a, 100, read_only=True)
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries))
        self.assertTrue(view["can_prepare"])
        self.assertFalse(readonly["can_prepare"])
        self.assertEqual(readonly["reason"], "MODE_READ_ONLY")
        self.assertEqual(view["orders"], [])
        self.assertFalse(VillagePersonalCommission.objects.exists())
        self.assertFalse(VillageBusinessOrderDay.objects.exists())
        self.assertFalse(PlayerGrowthState.objects.exists())
        self.lucy.refresh_from_db()
        self.assertEqual(self.lucy.money, before)

    def test_explicit_prepare_uses_real_lucy_budget_and_once_per_day_template_markers(self):
        result = self.prepare()
        self.assertEqual(result["spent_gold"], 64)
        self.assertEqual(len(result["published_commission_ids"]), 2)
        self.lucy.refresh_from_db()
        self.assertEqual(self.lucy.money, 936)
        self.assertEqual(sum(VillagePersonalCommission.objects.values_list("reserved_reward_gold", flat=True)), 64)
        again = self.prepare(self.b, self.bag_b)
        self.assertEqual(again["published_commission_ids"], [])
        self.assertEqual(again["spent_gold"], 0)
        self.assertEqual(VillageBusinessOrderDay.objects.get().published_gold, 64)
        corn = self.row("CORN")
        self.accept(corn)
        abandon_commission(corn.pk, "PLAYER", self.a.pk, uuid.uuid4(), 100)
        self.lucy.refresh_from_db()
        self.assertEqual(self.lucy.money, 960)
        self.assertEqual(self.prepare()["spent_gold"], 0)
        self.assertEqual(VillageBusinessOrderDay.objects.get().published_gold, 64)
        self.assertEqual(VillageBusinessOrderLink.objects.filter(template_code="CORN").count(), 1)

    def test_stock_and_incoming_needs_never_publish_fake_or_overfilled_demand(self):
        self.lucy.slots.create(slot_index=0, item_id=1030, item_amount=4, quality=3,
                               produced_world_minute=90, expires_world_minute=2000)
        self.lucy.slots.create(slot_index=1, item_id=1002, item_amount=4, quality=3)
        view = business_orders_overview(self.a, 100)
        self.assertFalse(view["can_prepare"])
        with self.assertRaises(BusinessOrdersError):
            self.prepare()
        self.assertFalse(VillagePersonalCommission.objects.exists())
        self.lucy.slots.filter(item_id=1030).update(item_amount=2)
        view = business_orders_overview(self.a, 100)
        self.assertEqual(next(t for t in view["templates"] if t["code"] == "CORN")["reason"], "PARTIAL_STOCK_GAP")
        self.lucy.slots.filter(item_id=1030).update(item_amount=0)
        self.prepare()
        self.clock.world_minute = 1440
        self.clock.save()
        view = business_orders_overview(self.a, 1440)
        corn = next(t for t in view["templates"] if t["code"] == "CORN")
        self.assertEqual(corn["requirements"][0]["incoming_quantity"], 4)
        self.assertFalse(corn["publishable"])

    def test_insufficient_real_wallet_does_not_create_cash_or_reserve(self):
        self.lucy.money = 23
        self.lucy.save(update_fields=["money"])
        with self.assertRaises(BusinessOrdersError) as error:
            self.prepare()
        self.assertEqual(error.exception.code, "LUCY_BUDGET_UNAVAILABLE")
        self.assertFalse(VillagePersonalCommission.objects.exists())
        self.assertFalse(VillageBusinessOrderDay.objects.exists())
        self.lucy.refresh_from_db()
        self.assertEqual(self.lucy.money, 23)

    def test_shared_cap_and_unreachable_templates_never_spend_beyond_daily_120(self):
        self.level(self.a, 440)
        first = self.prepare()
        self.assertLessEqual(first["spent_gold"], 120)
        self.assertLessEqual(first["business_orders"]["open_count"], 4)
        self.assertEqual(first["spent_gold"], 112)
        corn = self.row("CORN")
        self.accept(corn)
        self.fulfill(corn)
        # Real NPC consumption removes its stock; this is still the same day,
        # so the template marker and spent budget cannot be refreshed.
        self.lucy.slots.filter(item_id=1030).update(item_amount=0)
        again = self.prepare()
        self.assertEqual(again["spent_gold"], 0)
        self.assertEqual(again["business_orders"]["published_gold"], 112)
        combo = next(t for t in again["business_orders"]["templates"] if t["code"] == "VEGETABLES")
        self.assertEqual(combo["reason"], "BUSINESS_BUDGET_EXHAUSTED")

    def test_line_level_and_one_active_are_backend_enforced_and_decorate_existing_board(self):
        self.level(self.b, 440)
        self.prepare(self.b, self.bag_b)
        meal = self.row("FRESH_MEALS")
        projection = project_commission(meal, actor_type="PLAYER", actor_id=self.a.pk)
        self.assertTrue(projection["business_order"])
        self.assertFalse(projection["can_accept"])
        self.assertEqual(projection["reason"], "BUSINESS_LEVEL_REQUIRED")
        with self.assertRaises(CommissionError) as error:
            self.accept(meal)
        self.assertEqual(error.exception.code, "BUSINESS_LEVEL_REQUIRED")
        self.accept(self.row("CORN"))
        with self.assertRaises(CommissionError) as error:
            self.accept(self.row("WOOD"))
        self.assertEqual(error.exception.code, "BUSINESS_ORDER_ACTIVE")
        ordinary = publish_commission(self.npc, dict(title="普通自主委托", reward_gold=1, valid_for_minutes=100,
            default_location_code="RESTAURANT", stages=[dict(id="s", conditions=[
                dict(id="wood", type="item", item_ids=[1002], target=1)])]), uuid.uuid4(), 100)
        row = VillagePersonalCommission.objects.get(pk=ordinary["commission_id"])
        self.assertNotIn("business_order", project_commission(row, actor_type="PLAYER", actor_id=self.a.pk))
        self.accept(row)

    def test_only_two_real_daily_completions_no_double_payment_xp_and_next_day_resets_qualification(self):
        self.prepare()
        for code in ("CORN", "WOOD"):
            row = self.row(code)
            self.accept(row)
            self.assertEqual(self.fulfill(row)["status"], "COMPLETED")
            record_order_completion(self.a, "personal_commission", row.pk)
            record_order_completion(self.a, "personal_commission", row.pk)
        view = business_orders_overview(self.a, 100)
        self.assertEqual((view["completed_today"], view["remaining_completions"]), (2, 0))
        self.assertEqual(SkillRewardReceipt.objects.filter(player=self.a, skill_code="business").count(), 2)
        self.assertEqual(PlayerSkillProgress.objects.get(player=self.a, skill_code="business").total_xp, 40)
        self.bag_a.refresh_from_db()
        self.assertEqual(self.bag_a.money, 164)
        self.level(self.b, 440)
        self.prepare(self.b, self.bag_b)
        meal = self.row("FRESH_MEALS")
        self.level(self.a, 440)
        with self.assertRaises(CommissionError) as error:
            self.accept(meal)
        self.assertEqual(error.exception.code, "BUSINESS_DAILY_LIMIT")
        self.clock.world_minute = 1440
        self.clock.save()
        self.assertEqual(business_orders_overview(self.a, 1440)["completed_today"], 0)
        self.accept(meal)

    def test_final_payment_revalidates_every_held_batch_and_abandon_preserves_expired_original(self):
        result = publish_commission(self.npc, dict(title="两种真实备货", reward_gold=10, valid_for_minutes=1000,
            default_location_code="RESTAURANT", stages=[dict(id="s", conditions=[
                dict(id="corn", type="item", item_ids=[1030], target=2, min_quality=3,
                     requires_freshness=True, min_remaining_minutes=60),
                dict(id="wood", type="item", item_ids=[1002], target=1)])]), uuid.uuid4(), 100)
        row = VillagePersonalCommission.objects.get(pk=result["commission_id"])
        self.accept(row)
        corn = self.slot(2, expires_world_minute=200, dish_metadata={"original": True})
        wood = self.slot(1, item_id=1002, expires_world_minute=None)
        contribute_commission(row.pk, "PLAYER", self.a.pk, "corn", 2, uuid.uuid4(), 100,
                              item_id=1030, instance_id=corn.instance_id, location="RESTAURANT")
        self.clock.world_minute = 210
        self.clock.save()
        row.refresh_from_db()
        view = project_commission(row, actor_type="PLAYER", actor_id=self.a.pk)
        self.assertTrue(view["held_stale"])
        self.assertFalse(view["can_act"])
        self.assertTrue(view["can_abandon"])
        with self.assertRaises(CommissionError) as error:
            contribute_commission(row.pk, "PLAYER", self.a.pk, "wood", 1, uuid.uuid4(), 210,
                                  item_id=1002, instance_id=wood.instance_id, location="RESTAURANT")
        self.assertEqual(error.exception.code, "HELD_BATCH_NO_LONGER_ELIGIBLE")
        row.refresh_from_db(); wood.refresh_from_db(); self.bag_a.refresh_from_db()
        self.assertEqual((row.status, wood.item_amount, self.bag_a.money), ("ACTIVE", 1, 100))
        self.assertEqual(row.progress["conditions"], {"corn": 2})
        abandon_commission(row.pk, "PLAYER", self.a.pk, uuid.uuid4(), 210)
        returned = self.bag_a.slots.get(item_id=1030, item_amount=2)
        self.assertEqual(returned.expires_world_minute, 200)
        self.assertEqual(returned.dish_metadata, {"original": True})
        self.assertFalse(held_batch_issues(row, 210))

    def test_final_payment_rejects_changed_held_quality_without_consuming_last_delivery(self):
        self.level(self.a, 80)
        # Publish a normal multi-condition commission so the shared final
        # validation also protects existing NPC work, independent of G4 gates.
        result = publish_commission(self.npc, dict(title="原有多条件委托", reward_gold=5, valid_for_minutes=500,
            default_location_code="RESTAURANT", stages=[dict(id="s", conditions=[
                dict(id="a", type="item", item_ids=[1030], target=1, min_quality=3),
                dict(id="b", type="item", item_ids=[1002], target=1)])]), uuid.uuid4(), 100)
        row = VillagePersonalCommission.objects.get(pk=result["commission_id"])
        self.accept(row)
        source = self.slot(1)
        contribute_commission(row.pk, "PLAYER", self.a.pk, "a", 1, uuid.uuid4(), 100,
                              item_id=1030, instance_id=source.instance_id, location="RESTAURANT")
        held = VillagePersonalCommissionEscrow.objects.get(commission=row)
        snapshot = copy.deepcopy(held.batch_snapshot); snapshot["quality"] = 2
        held.batch_snapshot = snapshot; held.save(update_fields=["batch_snapshot"])
        source = self.slot(1, item_id=1002, expires_world_minute=None)
        with self.assertRaises(CommissionError):
            contribute_commission(row.pk, "PLAYER", self.a.pk, "b", 1, uuid.uuid4(), 100,
                                  item_id=1002, instance_id=source.instance_id, location="RESTAURANT")
        source.refresh_from_db(); self.bag_a.refresh_from_db()
        self.assertEqual((source.item_amount, self.bag_a.money), (1, 100))

    def test_existing_autonomous_lucy_promise_counts_as_incoming_without_becoming_controlled(self):
        publish_commission(self.npc, dict(title="Lucy已有真实采购", reward_gold=10, valid_for_minutes=500,
            default_location_code="RESTAURANT", stages=[dict(id="s", conditions=[
                dict(id="corn", type="item", item_ids=[1030], target=4, min_quality=3,
                     requires_freshness=True, min_remaining_minutes=60)])]), uuid.uuid4(), 100)
        result = self.prepare()
        self.assertEqual(result["spent_gold"], 40)
        self.assertEqual(list(VillageBusinessOrderLink.objects.values_list("template_code", flat=True)), ["WOOD"])
        corn = next(t for t in result["business_orders"]["templates"] if t["code"] == "CORN")
        self.assertEqual(corn["requirements"][0]["incoming_quantity"], 4)
        self.assertEqual(corn["reason"], "STOCK_SUFFICIENT")
