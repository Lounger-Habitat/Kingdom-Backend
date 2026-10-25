import copy
import uuid

from django.db import connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from inventory.components import same_batch_attributes
from inventory.models import CollectionPoint, CollectionPointLevel, CollectionPointLevelItem, InventorySlot
from player.models import PlayerGrowthState, PlayerMilestoneProgress, PlayerSkillProgress, SkillRewardReceipt
from player.starter import (post_action_progress, record_order_completion, starter_overview,
                            track_goal, StarterError, GOAL_GOLD, _award_goal_rewards)
from village.models import (EconomicLedgerEntry, StarterReserveTransfer, StarterSupplyBatch,
                            StarterSupplyOrder, VillageAccount)
from village.starter_supply import (BATCH_CODE, SupplyError, ensure_supply_batch, execute_supply,
                                    expire_supply, spend_reserve, supply_overview)
from village.tests.test_player_actions import PlayerFixture


class StarterSupplyTests(PlayerFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.fund, _ = VillageAccount.objects.update_or_create(account_code="PUBLIC_FUND", defaults=dict(balance=1000))
        point = CollectionPoint.objects.get(collect_type="采集食材")
        level = CollectionPointLevel.objects.get(collection_point=point, level=1)
        level.items.all().delete()
        CollectionPointLevelItem.objects.create(level=level, item_id=1030, collect_min_amount=2, collect_max_amount=4)

    def prepare(self):
        self.batch = ensure_supply_batch(self.clock.world_minute)
        self.order = self.batch.orders.order_by("order_id").first()
        return self.order

    def supply(self, action, order=None, *, player=None, bag=None, rid=None, **params):
        return execute_supply(player or self.a, bag or self.bag_a, action,
            dict(order_id=str((order or self.order).pk), **params), rid or uuid.uuid4(), self.clock.world_minute)

    def fresh_slot(self, quantity=6, **kwargs):
        return self.slot(quantity=quantity, source_request_id=uuid.uuid4(), **kwargs)

    def assert_code(self, code, fn):
        with self.assertRaises(SupplyError) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)

    def test_legacy_goal_rewards_are_visible_read_only_and_paid_once_per_player(self):
        for code in GOAL_GOLD:
            PlayerMilestoneProgress.objects.create(player=self.a, code=code, title=code,
                source_type="historical_action", source_id=str(uuid.uuid4()))
        before = starter_overview(self.a, self.bag_a, 100)
        self.assertEqual({row["code"]: row["reward_gold"] for row in before["goals"]}, GOAL_GOLD)
        self.assertTrue(all(row["reward_claimable"] for row in before["goals"]))
        read_only = starter_overview(self.a, self.bag_a, 100, read_only=True)
        self.assertFalse(any(row["reward_claimable"] for row in read_only["goals"]))
        self.bag_a.refresh_from_db(); self.assertEqual(self.bag_a.money, 100)
        payload = self.payload("claim_goal_rewards")
        from village.player_actions import execute_player_action
        first = execute_player_action(payload)
        self.assertTrue(first["success"], first)
        self.assertEqual(sum(row["earned_gold"] for row in first["data"]["result"]["starter_rewards"]), 70)
        self.assertEqual(first, execute_player_action(copy.deepcopy(payload)))
        self.assertEqual(self.run_action("claim_goal_rewards")["data"]["result"]["starter_rewards"], [])
        self.bag_a.refresh_from_db(); self.assertEqual(self.bag_a.money, 170)
        self.assertTrue(all(row["reward_claimed"] for row in starter_overview(self.a, self.bag_a, 100)["goals"]))
        entries = EconomicLedgerEntry.objects.filter(entry_type="STARTER_GOAL_REWARD")
        self.assertEqual(entries.count(), 5)
        self.assertEqual(sum(entries.values_list("external_delta", flat=True)), 70)
        self.fund.refresh_from_db(); self.assertEqual(self.fund.balance, 1000)
        self.assertEqual(self.run_action("claim_goal_rewards", player=self.b)["data"]["result"]["starter_rewards"], [])
        self.assertEqual(self.run_action("claim_goal_rewards", {"gold": 999})["result_code"], "INVALID_PARAMS")
        denied = self.payload("claim_goal_rewards"); denied["read_only"] = True
        self.assertEqual(execute_player_action(denied)["result_code"], "MODE_READ_ONLY")

    def test_reward_gold_and_ledger_roll_back_with_the_enclosing_action(self):
        PlayerMilestoneProgress.objects.create(player=self.a, code="first_gathering", title="第一次采集",
            source_type="collect", source_id=str(uuid.uuid4()))
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.assertEqual(_award_goal_rewards(self.a, ["first_gathering"], 100)[0]["earned_gold"], 10)
                raise RuntimeError("later settlement failed")
        self.bag_a.refresh_from_db(); self.assertEqual(self.bag_a.money, 100)
        self.assertFalse(EconomicLedgerEntry.objects.filter(entry_type="STARTER_GOAL_REWARD").exists())

    def test_commission_reward_preview_uses_actual_cooperation_and_daily_cap(self):
        for _ in range(2):
            commission = self.commission(quantity=1)
            self.fresh_slot(quantity=1)
            result = self.run_action("commission_contribute", self.delivery_params(commission, 1))
            self.assertTrue(result["success"], result)
            completed = next(row for row in result["data"]["overview"]["my_commissions"] if row["commission_id"] == commission)
            self.assertEqual((completed["reward_business_xp"], completed["reward_cooperation"]), (20, 2))
        available_id = self.commission(quantity=1, accept=False)
        available = next(row for row in self.run_action("overview")["data"]["overview"]["commissions"] if row["commission_id"] == available_id)
        self.assertEqual(available["reward_cooperation"], 0)
        self.assertTrue(available["reward_cooperation_capped"])

    def test_views_never_create_campaign_money_or_growth(self):
        with CaptureQueriesContext(connection) as queries:
            first = starter_overview(self.a, self.bag_a, self.clock.world_minute, read_only=True)
        self.assertEqual(first["tracked_goal_code"], "first_gathering")
        self.assertEqual(first["supply"]["reason"], "FIRST_COLLECTION_REQUIRED")
        self.assertFalse(StarterSupplyBatch.objects.exists())
        self.assertFalse(PlayerGrowthState.objects.filter(player=self.a).exists())
        self.assertFalse([q for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))])

    def test_publication_reserves_exact_finite_money_once_and_audits_it(self):
        self.prepare()
        self.fund.refresh_from_db()
        self.assertEqual(self.fund.balance, 730)
        self.assertEqual((self.batch.reserved_gold, self.batch.escrow_gold), (270, 270))
        self.assertEqual(self.batch.orders.count(), 5)
        self.assertEqual(ensure_supply_batch(self.clock.world_minute).pk, self.batch.pk)
        self.fund.refresh_from_db()
        self.assertEqual(self.fund.balance, 730)
        entries = EconomicLedgerEntry.objects.filter(entry_type="STARTER_RESERVE")
        self.assertEqual(entries.count(), 1)
        self.assertEqual(entries.get().changes["PUBLIC_FUND"]["money"], -270)

    def test_insufficient_public_money_is_not_created_or_retried_by_refresh(self):
        self.fund.balance = 269
        self.fund.save(update_fields=["balance"])
        self.prepare()
        self.assertEqual(self.batch.status, "UNAVAILABLE")
        self.assertEqual(self.batch.orders.count(), 0)
        self.fund.balance = 1000
        self.fund.save(update_fields=["balance"])
        ensure_supply_batch(self.clock.world_minute)
        self.assertEqual(supply_overview(self.a, self.bag_a, 100)["reason"], "PUBLIC_BUDGET_UNAVAILABLE")
        self.fund.refresh_from_db()
        self.assertEqual(self.fund.balance, 1000)

    def test_exclusive_accept_single_active_and_receipt_replay_conflict(self):
        self.prepare()
        rid = uuid.uuid4()
        first = self.supply("supply_accept", rid=rid)
        self.assertEqual(first, self.supply("supply_accept", rid=rid))
        self.assert_code("ORDER_UNAVAILABLE", lambda: self.supply("supply_accept", player=self.b, bag=self.bag_b))
        other = self.batch.orders.exclude(pk=self.order.pk).first()
        self.assert_code("ANOTHER_ORDER_ACTIVE", lambda: self.supply("supply_accept", order=other))
        self.assert_code("REQUEST_CONFLICT", lambda: self.supply("supply_abandon", rid=rid))

    def test_partial_delivery_preview_and_abandon_restore_original_batch(self):
        self.prepare()
        self.supply("supply_accept")
        source = self.fresh_slot(6, remaining_uses=7, dish_metadata={"origin": "unchanged"})
        self.supply("supply_deliver", instance_id=str(source.instance_id), quantity=2)
        held = self.order.escrow_bag.slots.get(item_amount__gt=0)
        source.refresh_from_db()
        self.assertEqual(source.item_amount, 4)
        self.assertTrue(same_batch_attributes(source, held))
        preview = next(o for o in supply_overview(self.a, self.bag_a, 100)["orders"] if o["order_id"] == str(self.order.pk))
        self.assertEqual((preview["delivery_batches"][0]["quantity"], preview["delivery_batches"][0]["eligible_quantity"]), (4, 4))
        self.supply("supply_abandon")
        source.refresh_from_db(); self.order.refresh_from_db(); self.batch.refresh_from_db()
        self.assertEqual((source.item_amount, source.expires_world_minute, source.remaining_uses), (6, 2000, 7))
        self.assertEqual((self.order.status, self.order.delivered_quantity, self.batch.escrow_gold), ("OPEN", 0, 270))
        self.assertFalse(self.order.escrow_bag.slots.filter(item_amount__gt=0).exists())

    def test_completed_goods_enter_reserve_reward_once_and_one_order_per_player(self):
        self.prepare()
        self.supply("supply_accept")
        source = self.fresh_slot()
        rid = uuid.uuid4()
        first = self.supply("supply_deliver", instance_id=str(source.instance_id), quantity=6, rid=rid)
        self.assertEqual(first, self.supply("supply_deliver", instance_id=str(source.instance_id), quantity=6, rid=rid))
        self.bag_a.refresh_from_db(); self.batch.refresh_from_db()
        self.assertEqual((self.bag_a.money, self.batch.escrow_gold, self.batch.paid_gold), (154, 216, 54))
        reserve = self.batch.reserve_bag.slots.get(item_amount__gt=0)
        self.assertEqual((reserve.item_amount, reserve.expires_world_minute), (6, 2000))
        other = self.batch.orders.exclude(pk=self.order.pk).first()
        self.assert_code("PERSONAL_ORDER_COMPLETED", lambda: self.supply("supply_accept", order=other))

    def test_expiry_returns_original_expired_goods_and_refunds_only_unpaid_money(self):
        self.prepare()
        self.supply("supply_accept")
        source = self.fresh_slot()
        self.supply("supply_deliver", instance_id=str(source.instance_id), quantity=2)
        self.clock.world_minute = self.batch.expires_world_minute
        self.clock.save(update_fields=["world_minute"])
        self.assertEqual(expire_supply(self.clock.world_minute), 270)
        self.assertEqual(expire_supply(self.clock.world_minute), 0)
        source.refresh_from_db(); self.batch.refresh_from_db(); self.fund.refresh_from_db()
        self.assertEqual((source.item_amount, source.expires_world_minute), (6, 2000))
        self.assertEqual((self.batch.escrow_gold, self.batch.refunded_gold, self.fund.balance), (0, 270, 1000))
        self.assertTrue(all(o.status == "EXPIRED" for o in self.batch.orders.all()))

    def test_expiry_never_refunds_already_paid_reward(self):
        self.prepare()
        self.supply("supply_accept")
        source = self.fresh_slot()
        self.supply("supply_deliver", instance_id=str(source.instance_id), quantity=6)
        self.clock.world_minute = self.batch.expires_world_minute
        self.clock.save(update_fields=["world_minute"])
        self.assertEqual(expire_supply(self.clock.world_minute), 216)
        self.batch.refresh_from_db(); self.fund.refresh_from_db(); self.bag_a.refresh_from_db()
        self.assertEqual((self.batch.paid_gold, self.batch.refunded_gold, self.batch.escrow_gold), (54, 216, 0))
        self.assertEqual(self.fund.balance + self.bag_a.money, 1100)

    def test_unqualified_or_aged_held_batch_cannot_complete_or_refresh_freshness(self):
        self.prepare()
        self.supply("supply_accept")
        bad = self.slot(quantity=6)
        self.assert_code("PROVENANCE_REQUIRED", lambda: self.supply("supply_deliver", instance_id=str(bad.instance_id), quantity=6))
        first = self.fresh_slot(quantity=3, expires_world_minute=200)
        self.supply("supply_deliver", instance_id=str(first.instance_id), quantity=3)
        self.clock.world_minute = 150
        self.clock.save(update_fields=["world_minute"])
        second = self.fresh_slot(quantity=3)
        self.assert_code("HELD_BATCH_NO_LONGER_ELIGIBLE", lambda: self.supply("supply_deliver", instance_id=str(second.instance_id), quantity=3))
        second.refresh_from_db(); self.order.refresh_from_db()
        self.assertEqual((second.item_amount, self.order.delivered_quantity), (3, 3))

    def test_actual_reserve_transfer_preserves_components_and_is_idempotent(self):
        self.prepare()
        self.supply("supply_accept")
        source = self.fresh_slot(remaining_uses=7, dish_metadata={"provenance": "retained"})
        self.supply("supply_deliver", instance_id=str(source.instance_id), quantity=6)
        reserve = self.batch.reserve_bag.slots.get(item_amount__gt=0)
        args = dict(batch_id=BATCH_CODE, beneficiary_bag=self.lucy, allocations=[dict(instance_id=str(reserve.instance_id), quantity=2)],
                    request_id=uuid.uuid4(), minute=100, purpose="饭店实际领用")
        first = spend_reserve(**args)
        self.assertEqual(first, spend_reserve(**args))
        reserve.refresh_from_db()
        target = self.lucy.slots.get(item_amount__gt=0)
        self.assertEqual((reserve.item_amount, target.item_amount), (4, 2))
        self.assertTrue(same_batch_attributes(reserve, target))
        self.assertEqual(StarterReserveTransfer.objects.count(), 1)
        bad_args = dict(args, request_id=uuid.uuid4(), allocations=[dict(instance_id=str(reserve.instance_id), quantity=5)])
        self.assert_code("INSUFFICIENT_RESERVE", lambda: spend_reserve(**bad_args))
        reserve.refresh_from_db()
        self.assertEqual(reserve.item_amount, 4)

    def test_goal_tracking_persists_and_sales_do_not_farm_business_xp(self):
        track_goal(self.a, "first_gathering_tool")
        before = starter_overview(self.a, self.bag_a, 100)["revision"]
        first = post_action_progress(self.a, self.bag_a, "sell_store", {},
            dict(quantity=2, gold_amount=10), uuid.uuid4(), 100)
        second = post_action_progress(self.a, self.bag_a, "sell_store", {},
            dict(quantity=2, gold_amount=10), uuid.uuid4(), 100)
        state = starter_overview(self.a, self.bag_a, 100)
        self.assertEqual(state["tracked_goal_code"], "first_gathering_tool")
        self.assertGreater(state["revision"], before)
        self.assertEqual(len(first["starter_milestones"]), 1)
        self.assertEqual(second["starter_milestones"], [])
        self.assertEqual(PlayerSkillProgress.objects.get(player=self.a, skill_code="business").total_xp, 0)
        self.assertEqual(SkillRewardReceipt.objects.filter(player=self.a, skill_code="business").count(), 0)

    def test_only_real_completed_orders_award_business_xp_by_durable_order_id(self):
        self.prepare()
        self.supply("supply_accept")
        with self.assertRaises(StarterError):
            record_order_completion(self.a, "starter_supply", self.order.pk)
        source = self.fresh_slot()
        result = self.supply("supply_deliver", instance_id=str(source.instance_id), quantity=6)
        first = post_action_progress(self.a, self.bag_a, "supply_deliver", {}, result, uuid.uuid4(), 100)
        self.assertEqual(first["growth_rewards"][0]["xp_gained"], 20)
        post_action_progress(self.a, self.bag_a, "supply_deliver", {}, result, uuid.uuid4(), 100)
        self.assertEqual(PlayerSkillProgress.objects.get(player=self.a, skill_code="business").total_xp, 20)
        self.assertEqual(SkillRewardReceipt.objects.filter(player=self.a, skill_code="business").count(), 1)
        self.assertFalse(PlayerMilestoneProgress.objects.filter(player=self.b).exists())

    def test_real_npc_commission_completion_awards_business_once(self):
        commission = self.commission(quantity=2)
        self.fresh_slot(quantity=2)
        result = self.run_action("commission_contribute", self.delivery_params(commission, 2))
        self.assertTrue(result["success"], result)
        self.assertEqual(PlayerSkillProgress.objects.get(player=self.a, skill_code="business").total_xp, 20)
        record_order_completion(self.a, "personal_commission", commission)
        self.assertEqual(PlayerSkillProgress.objects.get(player=self.a, skill_code="business").total_xp, 20)
