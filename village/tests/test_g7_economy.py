"""World-day trends and deterministic, finite publication priorities."""
import copy
import uuid

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from items.models import Item
from player.models import PlayerSkillProgress
from restaurant.models import CustomerTransaction
from restaurant.player_stall import business_overview, business_trend
from village.business_orders import BusinessOrdersError, business_orders_overview, execute_business_orders
from village.models import VillageBusinessOrderDay, VillageBusinessOrderLink, VillagePersonalCommission
from village.tests.test_player_actions import PlayerFixture


class G7EconomyTests(PlayerFixture, TestCase):
    def sale(self, minute, *, player=None, price=10, cost=4):
        return CustomerTransaction.objects.create(player=player or self.a, world_minute=minute,
            game_day=0, customer_name="真实成交夹具", item_id=29201, item_name="清蒸玉米",
            amount_paid=price, unit_cost=cost, feedback_source="RULE_CUSTOMER")

    def test_trend_aggregates_all_sales_and_isolates_old_undated_and_other_players(self):
        minute = 8*1440+600
        CustomerTransaction.objects.bulk_create([CustomerTransaction(player=self.a, world_minute=minute,
            game_day=0, customer_name="顾客", item_id=29201, item_name="清蒸玉米", amount_paid=10,
            unit_cost=4, feedback_source="RULE_CUSTOMER") for _ in range(30)])
        self.sale(7*1440+1, price=12, cost=None)
        self.sale(7*1440+2, price=8, cost=3)
        self.sale(None, price=15, cost=None)
        self.sale(None, price=7, cost=2)
        self.sale(1440, price=999, cost=1)  # Before the seven-day window.
        self.sale(minute+1, price=999, cost=1)  # Later than this authoritative observation.
        self.sale(minute, player=self.b, price=999, cost=1)
        with CaptureQueriesContext(connection) as queries:
            view = business_overview(self.a, self.bag_a, minute)
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries))
        trend = view["trend"]
        self.assertEqual([r["day_index"] for r in trend["days"]], list(range(2, 9)))
        today = trend["days"][-1]
        self.assertEqual((today["sold"], today["revenue"], today["known_purchase_cost"],
                          today["unknown_cost_count"], today["purchase_margin_nullable"]), (30, 300, 120, 0, 180))
        self.assertEqual(len(view["history"]), 20)
        yesterday = trend["days"][-2]
        self.assertEqual((yesterday["revenue"], yesterday["known_purchase_cost"], yesterday["unknown_cost_count"],
                          yesterday["purchase_margin_nullable"], yesterday["cost_complete"]), (20, 3, 1, None, False))
        self.assertEqual(trend["days"][0], dict(day_index=2, sold=0, revenue=0,
            known_purchase_cost=0, unknown_cost_count=0, purchase_margin_nullable=0, cost_complete=True))
        self.assertEqual(trend["legacy_undated"], dict(sold=2, revenue=22, known_purchase_cost=2,
            unknown_cost_count=1, purchase_margin_nullable=None, cost_complete=False))
        self.assertEqual(trend["cost_basis"], "RECORDED_PURCHASE_COST_ONLY")

    def test_epoch_has_no_negative_days_and_invalid_cost_is_not_profit(self):
        self.sale(5, cost=0)
        self.sale(6, cost=-7)
        view = business_trend(self.a, 10)
        self.assertEqual((view["from_day_index"], view["to_day_index"], len(view["days"])), (0, 0, 1))
        self.assertEqual(view["days"][0]["known_purchase_cost"], 0)
        self.assertEqual(view["days"][0]["unknown_cost_count"], 1)
        self.assertIsNone(view["days"][0]["purchase_margin_nullable"])

    def prepare(self):
        return execute_business_orders(self.a, self.bag_a, {}, uuid.uuid4(), self.clock)

    def catalog(self):
        for item_id in (1025, 1042, 29204):
            Item.objects.update_or_create(pk=item_id, defaults=dict(item_name=str(item_id), item_price=10))

    def test_day_priority_rotates_without_query_or_request_reroll_and_freezes_published_rows(self):
        self.catalog()
        PlayerSkillProgress.objects.create(player=self.a, skill_code="business", total_xp=440)
        first_view = business_orders_overview(self.a, 100)
        self.assertEqual(first_view["rotation"]["publication_order"], ["VEGETABLES", "CORN", "WOOD", "FRESH_MEALS"])
        self.assertEqual(self.prepare()["spent_gold"], 108)
        rows = list(VillageBusinessOrderLink.objects.select_related("commission").filter(day_id=0))
        self.assertEqual({row.template_code for row in rows}, {"VEGETABLES", "WOOD"})
        frozen = {row.commission_id: (row.commission.reward_gold, copy.deepcopy(row.commission.requirements)) for row in rows}
        self.assertEqual(self.prepare()["spent_gold"], 0)
        self.assertEqual(business_orders_overview(self.a, 100)["rotation"], first_view["rotation"])
        self.clock.world_minute = 1540; self.clock.save()
        self.assertEqual(business_orders_overview(self.a, 1540)["rotation"]["publication_order"],
                         ["FRESH_MEALS", "CORN", "WOOD", "VEGETABLES"])
        self.assertEqual(self.prepare()["spent_gold"], 120)
        self.assertEqual(set(VillageBusinessOrderLink.objects.filter(day_id=1).values_list("template_code", flat=True)),
                         {"FRESH_MEALS", "CORN", "WOOD"})
        for row in rows:
            row.commission.refresh_from_db()
            self.assertEqual((row.commission.reward_gold, row.commission.requirements), frozen[row.commission_id])

    def test_level_one_keeps_two_real_basic_orders(self):
        self.catalog()
        self.assertEqual(self.prepare()["spent_gold"], 72)
        self.assertEqual(set(VillageBusinessOrderLink.objects.values_list("template_code", flat=True)), {"CORN", "WOOD"})

    def test_lucy_quote_uses_current_catalog_floor_then_freezes_old_commission_and_daily_budget(self):
        self.catalog()
        Item.objects.filter(pk=1030).update(item_price=8)
        corn = next(t for t in business_orders_overview(self.a, 100)["templates"] if t["code"] == "CORN")
        self.assertEqual((corn["reference_value"], corn["base_reward_gold"], corn["reward_gold"]), (32, 24, 39))
        result = self.prepare()
        self.assertEqual(result["spent_gold"], 87)  # Corn39 + wood48, from the exact published quote.
        old = VillageBusinessOrderLink.objects.get(day_id=0, template_code="CORN").commission
        frozen = copy.deepcopy(old.requirements)
        self.assertEqual((old.reward_gold, old.reserved_reward_gold), (39, 39))
        self.lucy.refresh_from_db()
        self.assertEqual(self.lucy.money, 913)
        self.assertEqual(sum(VillagePersonalCommission.objects.values_list("reserved_reward_gold", flat=True)), 87)
        Item.objects.filter(pk=1030).update(item_price=100)
        Item.objects.filter(pk=1002).update(item_price=20)
        self.assertEqual(self.prepare()["spent_gold"], 0)
        old.refresh_from_db()
        self.assertEqual((old.reward_gold, old.reserved_reward_gold, old.requirements), (39, 39, frozen))
        self.assertEqual(VillageBusinessOrderDay.objects.get(pk=0).published_gold, 87)
        self.clock.world_minute = 1540
        self.clock.save()
        result = self.prepare()  # Prior reserves refund on expiry; this day's corn480 cannot fit120.
        self.assertEqual((result["spent_gold"], result["business_orders"]["published_gold"]), (96, 96))
        self.assertEqual(set(VillageBusinessOrderLink.objects.filter(day_id=1).values_list("template_code", flat=True)), {"WOOD"})
        corn = next(t for t in result["business_orders"]["templates"] if t["code"] == "CORN")
        self.assertEqual((corn["reward_gold"], corn["reason"]), (480, "BUSINESS_BUDGET_EXHAUSTED"))
        self.lucy.refresh_from_db()
        self.assertEqual(self.lucy.money, 904)
        self.assertEqual(sum(VillagePersonalCommission.objects.values_list("reserved_reward_gold", flat=True)), 96)
        old.refresh_from_db()
        self.assertEqual((old.reward_gold, old.requirements), (39, frozen))
        self.assertEqual(self.prepare()["spent_gold"], 0)
        self.assertEqual(list(VillageBusinessOrderDay.objects.order_by("pk").values_list("published_gold", flat=True)), [87, 96])

    def test_lucy_nonpositive_catalog_price_cannot_publish_or_take_real_money(self):
        self.catalog()
        Item.objects.filter(pk=1030).update(item_price=0)
        Item.objects.filter(pk=1002).update(item_price=-1)
        view = business_orders_overview(self.a, 100)
        self.assertFalse(view["can_prepare"])
        for code in ("CORN", "WOOD"):
            row = next(t for t in view["templates"] if t["code"] == code)
            self.assertEqual((row["reason"], row["reward_gold"], row["reference_value"]), ("ITEM_PRICE_UNAVAILABLE", 0, None))
        with self.assertRaises(BusinessOrdersError) as error:
            self.prepare()
        self.assertEqual(error.exception.code, "ITEM_PRICE_UNAVAILABLE")
        self.assertFalse(VillageBusinessOrderDay.objects.exists())
        self.assertFalse(VillageBusinessOrderLink.objects.exists())
        self.assertFalse(VillagePersonalCommission.objects.exists())
        self.lucy.refresh_from_db()
        self.assertEqual(self.lucy.money, 1000)
