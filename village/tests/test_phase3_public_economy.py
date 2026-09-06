"""Tests for Phase 3 public economy actions: IMPORT_GOODS, EXPORT_GOODS,
RATE_DISH, PUBLISH_PRICE_INDEX, AUDIT_PUBLIC_FUND, EAT, COMMUNITY_MEAL.

Covers success, cooldown, FEFO, salary, owner payment, insufficient resources.
"""

import uuid

from django.test import TestCase

from game_time.models import GlobalGameTimeState
from inventory.models import (
    NpcDefinition,
    NpcGlobalInventory,
    NpcGlobalInventorySlot,
)
from items.models import Item, ItemType
from recipes.models import Recipe, RecipeIngredient
from village.models import (
    DailyPriceSnapshot,
    NpcNeedsDaySummary,
    NpcRuntimeState,
    VillageAccount,
    VillageSimulationConfig,
)
from village.phase3_asset_service import commit_economy_action
from village.phase3_contract import (
    VILLAGE_ECONOMY_ACTION_V1,
    DaySnapshotStatus,
    ResultCode,
)


def _payload(command_name, npc_id, location, params, request_id=None):
    return {
        "schema_version": VILLAGE_ECONOMY_ACTION_V1,
        "command_id": str(uuid.uuid4()),
        "request_id": str(request_id or uuid.uuid4()),
        "message_id": str(uuid.uuid4()),
        "command_name": command_name,
        "npc_id": npc_id,
        "completed_world_minute": 2000,
        "time_revision": 2000,
        "current_location_code": location,
        "params": params,
    }


class PublicEconomyTestBase(TestCase):
    """Shared setUp for public economy tests."""

    def setUp(self):
        VillageSimulationConfig.objects.create(
            config_key="default", village_economy_enabled=True,
        )
        self.time = GlobalGameTimeState.get_instance()
        self.time.world_minute = 2000
        self.time.time_revision = 2000
        self.time.save()

        # NPCs
        self.bill, _ = NpcDefinition.objects.get_or_create(
            agent_id="10011", defaults={"name": "bill"},
        )
        self.jack, _ = NpcDefinition.objects.get_or_create(
            agent_id="10012", defaults={"name": "jack"},
        )
        self.mark, _ = NpcDefinition.objects.get_or_create(
            agent_id="10013", defaults={"name": "mark"},
        )
        self.lucy, _ = NpcDefinition.objects.get_or_create(
            agent_id="10014", defaults={"name": "lucy"},
        )
        self.nora, _ = NpcDefinition.objects.get_or_create(
            agent_id="10015", defaults={"name": "nora"},
        )

        # Inventories
        self.bill_inv, _ = NpcGlobalInventory.objects.get_or_create(
            npc=self.bill, defaults={"money": 200},
        )
        self.jack_inv, _ = NpcGlobalInventory.objects.get_or_create(
            npc=self.jack, defaults={"money": 240},
        )
        self.lucy_inv, _ = NpcGlobalInventory.objects.get_or_create(
            npc=self.lucy, defaults={"money": 300},
        )
        self.nora_inv, _ = NpcGlobalInventory.objects.get_or_create(
            npc=self.nora, defaults={"money": 450},
        )

        # Runtime states
        self.bill_rt, _ = NpcRuntimeState.objects.get_or_create(
            npc=self.bill,
            defaults={"current_location_code": "BILL_FARM", "health_fp": 1000},
        )
        self.jack_rt, _ = NpcRuntimeState.objects.get_or_create(
            npc=self.jack,
            defaults={"current_location_code": "HUNTING_GATE", "health_fp": 1000},
        )
        self.lucy_rt, _ = NpcRuntimeState.objects.get_or_create(
            npc=self.lucy,
            defaults={"current_location_code": "RESTAURANT", "health_fp": 1000},
        )
        self.nora_rt, _ = NpcRuntimeState.objects.get_or_create(
            npc=self.nora,
            defaults={"current_location_code": "TRADE_ROAD", "health_fp": 1000},
        )
        self.mark_rt, _ = NpcRuntimeState.objects.get_or_create(
            npc=self.mark,
            defaults={"current_location_code": "RECORD_HALL", "health_fp": 1000},
        )

        # PUBLIC_FUND
        self.account, _ = VillageAccount.objects.get_or_create(
            account_code="PUBLIC_FUND",
            defaults={"balance": 500, "last_closed_day_index": -1},
        )

        # Recipes
        self.recipe2, _ = Recipe.objects.update_or_create(
            pk=2, defaults={"item_id": 1053, "recipe_name": "辣子鸡丁"},
        )
        RecipeIngredient.objects.get_or_create(
            recipe=self.recipe2, item_id=1056,
            defaults={"item_name": "鸡肉", "count": 2},
        )
        self.recipe12, _ = Recipe.objects.update_or_create(
            pk=12, defaults={"item_id": 1067, "recipe_name": "奶酪焗玉米"},
        )
        RecipeIngredient.objects.get_or_create(
            recipe=self.recipe12, item_id=1030,
            defaults={"item_name": "玉米", "count": 1},
        )

        # Clean slots
        NpcGlobalInventorySlot.objects.filter(
            bag__in=[self.bill_inv, self.jack_inv, self.lucy_inv, self.nora_inv],
        ).delete()

        # Published price snapshot
        DailyPriceSnapshot.objects.create(
            day_index=0,
            prices={"1040": 8, "1026": 10, "1050": 4, "1030": 8, "1056": 12,
                    "1053": 25, "1067": 22},
            status=DaySnapshotStatus.PUBLISHED,
        )


# ============================================================================
# IMPORT_GOODS
# ============================================================================


class ImportGoodsTest(PublicEconomyTestBase):
    def test_import_success(self):
        """Nora imports items from caravan."""
        resp = commit_economy_action(_payload(
            "IMPORT_GOODS", 10015, "TRADE_ROAD",
            {"item_id": 1040, "amount": 6},
        ))
        self.assertEqual(resp["result_code"], ResultCode.OK)

        # Items added to Nora's inventory
        pepper_slots = NpcGlobalInventorySlot.objects.filter(
            bag=self.nora_inv, item_id=1040,
        )
        total = sum(s.item_amount for s in pepper_slots)
        self.assertEqual(total, 6)

        # Nora pays for her own stock; PUBLIC_FUND is unchanged.
        self.nora_inv.refresh_from_db()
        self.assertEqual(self.nora_inv.money, 450 - 8 * 6)
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, 500)
        self.assertEqual(resp["effects"]["nora_balance"], 450 - 8 * 6)

        # external_delta negative
        self.assertEqual(resp["external_delta"], -48)

        # last_caravan_day updated
        self.account.refresh_from_db()
        self.assertEqual(self.account.last_caravan_day, 2000 // 1440)

    def test_import_cooldown_check(self):
        """Cannot import within 3 days of last caravan."""
        self.account.last_caravan_day = 2000 // 1440  # today
        self.account.save(update_fields=["last_caravan_day"])

        resp = commit_economy_action(_payload(
            "IMPORT_GOODS", 10015, "TRADE_ROAD",
            {"item_id": 1040, "amount": 6},
        ))
        self.assertEqual(resp["result_code"], ResultCode.P3_CARAVAN_COOLDOWN)

    def test_import_max_to_safe_stock(self):
        """Import amount capped at safe_stock - available."""
        # Nora already has 3 peppers
        NpcGlobalInventorySlot.objects.create(
            bag=self.nora_inv, slot_index=0, item_id=1040, item_amount=3,
        )
        # safe_stock for 1040 = 6, so max import = 3
        resp = commit_economy_action(_payload(
            "IMPORT_GOODS", 10015, "TRADE_ROAD",
            {"item_id": 1040, "amount": 10},
        ))
        self.assertEqual(resp["result_code"], ResultCode.OK)
        self.assertEqual(resp["effects"]["import"]["amount"], 3)

    def test_import_owner_payment(self):
        """Import costs come from Nora, not PUBLIC_FUND."""
        self.account.balance = 10
        self.account.save(update_fields=["balance"])

        resp = commit_economy_action(_payload(
            "IMPORT_GOODS", 10015, "TRADE_ROAD",
            {"item_id": 1050, "amount": 3},  # sugar: 4 * 3 = 12
        ))
        self.assertEqual(resp["result_code"], ResultCode.OK)
        self.nora_inv.refresh_from_db()
        self.account.refresh_from_db()
        self.assertEqual(self.nora_inv.money, 450 - 12)
        self.assertEqual(self.account.balance, 10)

    def test_import_fails_when_nora_cannot_pay(self):
        """A full PUBLIC_FUND cannot cover Nora's insufficient balance."""
        self.nora_inv.money = 10
        self.nora_inv.save(update_fields=["money"])

        resp = commit_economy_action(_payload(
            "IMPORT_GOODS", 10015, "TRADE_ROAD",
            {"item_id": 1050, "amount": 3},
        ))
        self.assertEqual(resp["result_code"], ResultCode.P3_INSUFFICIENT_MONEY)
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, 500)

    def test_import_wrong_item(self):
        """Only import whitelist items allowed."""
        resp = commit_economy_action(_payload(
            "IMPORT_GOODS", 10015, "TRADE_ROAD",
            {"item_id": 1030, "amount": 5},  # corn not in import whitelist
        ))
        self.assertEqual(resp["result_code"], ResultCode.P3_INVALID_PARAMS)

    def test_import_tools_seasonings_and_seeds(self):
        """Tools, current seasonings, and current seeds can be imported."""
        for item_id in (4001, 4002, 1003, 1022, 1051, 1049, 1058, 1076, 1099):
            with self.subTest(item_id=item_id):
                self.account.last_caravan_day = None
                self.account.balance = 500
                self.account.save(update_fields=["last_caravan_day", "balance"])
                resp = commit_economy_action(_payload(
                    "IMPORT_GOODS", 10015, "TRADE_ROAD",
                    {"item_id": item_id, "amount": 1},
                ))
                self.assertEqual(resp["result_code"], ResultCode.OK)

    def test_import_wrong_actor(self):
        """Only Nora can import."""
        resp = commit_economy_action(_payload(
            "IMPORT_GOODS", 10011, "TRADE_ROAD",
            {"item_id": 1040, "amount": 5},
        ))
        self.assertEqual(resp["result_code"], ResultCode.P3_FORBIDDEN_ACTOR)


# ============================================================================
# EXPORT_GOODS
# ============================================================================


class ExportGoodsTest(PublicEconomyTestBase):
    def test_export_success(self):
        """Nora exports surplus items."""
        # Give Nora corn above safe_stock (safe_stock=10)
        NpcGlobalInventorySlot.objects.create(
            bag=self.nora_inv, slot_index=0, item_id=1030, item_amount=15,
        )

        resp = commit_economy_action(_payload(
            "EXPORT_GOODS", 10015, "TRADE_ROAD",
            {"item_id": 1030, "amount": 5,
             "expected_asset_version": self.nora_inv.asset_version},
        ))
        self.assertEqual(resp["result_code"], ResultCode.OK)

        # Items deducted
        slot = NpcGlobalInventorySlot.objects.get(
            bag=self.nora_inv, item_id=1030,
        )
        self.assertEqual(slot.item_amount, 10)

        # PUBLIC_FUND received money
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, 500 + 8 * 5)  # 8 is published price

        # external_delta positive
        self.assertEqual(resp["external_delta"], 40)

    def test_export_above_safe_stock_only(self):
        """Cannot export below safe_stock."""
        # Nora has exactly safe_stock (10 corn)
        NpcGlobalInventorySlot.objects.create(
            bag=self.nora_inv, slot_index=0, item_id=1030, item_amount=10,
        )

        resp = commit_economy_action(_payload(
            "EXPORT_GOODS", 10015, "TRADE_ROAD",
            {"item_id": 1030, "amount": 5,
             "expected_asset_version": self.nora_inv.asset_version},
        ))
        self.assertEqual(resp["result_code"], ResultCode.P3_INSUFFICIENT_ITEM)

    def test_export_wrong_item(self):
        """Only export whitelist items allowed."""
        resp = commit_economy_action(_payload(
            "EXPORT_GOODS", 10015, "TRADE_ROAD",
            {"item_id": 1040, "amount": 5,
             "expected_asset_version": self.nora_inv.asset_version},
        ))
        self.assertEqual(resp["result_code"], ResultCode.P3_INVALID_PARAMS)

    def test_export_version_conflict(self):
        """Version mismatch should fail."""
        NpcGlobalInventorySlot.objects.create(
            bag=self.nora_inv, slot_index=0, item_id=1030, item_amount=15,
        )

        resp = commit_economy_action(_payload(
            "EXPORT_GOODS", 10015, "TRADE_ROAD",
            {"item_id": 1030, "amount": 5,
             "expected_asset_version": 999},
        ))
        self.assertEqual(resp["result_code"], ResultCode.P3_ASSET_VERSION_CONFLICT)


# ============================================================================
# RATE_DISH
# ============================================================================


class RateDishTest(PublicEconomyTestBase):
    def test_rate_invalid_instance_id_is_business_failure(self):
        """Malformed user input must not escape as a UUIDField exception."""
        resp = commit_economy_action(_payload(
            "RATE_DISH", 10013, "RECORD_HALL",
            {"owner_npc_id": 10014, "food_instance_id": "1", "score": 4},
        ))
        self.assertEqual(resp["result_code"], ResultCode.P3_INVALID_PARAMS)

    def test_rate_dish_success(self):
        """Mark rates a dish successfully."""
        # Create Lucy's dish
        dish = NpcGlobalInventorySlot.objects.create(
            bag=self.lucy_inv, slot_index=0, item_id=1053, item_amount=1,
            produced_world_minute=1000, expires_world_minute=4000,
            dish_quality=3, rated=False,
        )

        resp = commit_economy_action(_payload(
            "RATE_DISH", 10013, "RECORD_HALL",
            {"owner_npc_id": 10014, "food_instance_id": str(dish.instance_id),
             "score": 4},
        ))
        self.assertEqual(resp["result_code"], ResultCode.OK)

        dish.refresh_from_db()
        self.assertTrue(dish.rated)
        self.assertEqual(dish.overall_score, 4)

        # Mark salary paid
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, 500 - 1)  # 1 gold salary

    def test_rate_already_rated_fails(self):
        """Cannot rate an already rated dish."""
        dish = NpcGlobalInventorySlot.objects.create(
            bag=self.lucy_inv, slot_index=0, item_id=1053, item_amount=1,
            produced_world_minute=1000, expires_world_minute=4000,
            dish_quality=3, rated=True, overall_score=3,
        )

        resp = commit_economy_action(_payload(
            "RATE_DISH", 10013, "RECORD_HALL",
            {"owner_npc_id": 10014, "food_instance_id": str(dish.instance_id),
             "score": 5},
        ))
        self.assertEqual(resp["result_code"], ResultCode.P3_INVALID_PARAMS)

    def test_rate_mark_salary(self):
        """Mark receives 1 gold from PUBLIC_FUND."""
        dish = NpcGlobalInventorySlot.objects.create(
            bag=self.lucy_inv, slot_index=0, item_id=1053, item_amount=1,
            produced_world_minute=1000, expires_world_minute=4000,
            rated=False,
        )

        balance_before = self.account.balance
        commit_economy_action(_payload(
            "RATE_DISH", 10013, "RECORD_HALL",
            {"owner_npc_id": 10014, "food_instance_id": str(dish.instance_id),
             "score": 3},
        ))

        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, balance_before - 1)

    def test_rate_salary_skipped_when_fund_empty(self):
        """Salary skipped when PUBLIC_FUND is empty."""
        self.account.balance = 0
        self.account.save(update_fields=["balance"])

        dish = NpcGlobalInventorySlot.objects.create(
            bag=self.lucy_inv, slot_index=0, item_id=1053, item_amount=1,
            produced_world_minute=1000, expires_world_minute=4000,
            rated=False,
        )

        resp = commit_economy_action(_payload(
            "RATE_DISH", 10013, "RECORD_HALL",
            {"owner_npc_id": 10014, "food_instance_id": str(dish.instance_id),
             "score": 3},
        ))
        # Action still succeeds
        self.assertEqual(resp["result_code"], ResultCode.OK)
        self.assertTrue(resp["effects"].get("salary_skipped", False))


# ============================================================================
# PUBLISH_PRICE_INDEX
# ============================================================================


class PublishPriceIndexTest(PublicEconomyTestBase):
    def test_publish_success(self):
        """Mark publishes daily price index."""
        DailyPriceSnapshot.objects.create(
            day_index=5,
            prices={"1030": 8, "1056": 12},
            status=DaySnapshotStatus.DRAFT,
        )

        resp = commit_economy_action(_payload(
            "PUBLISH_PRICE_INDEX", 10013, "RECORD_HALL",
            {"day_index": 5},
        ))
        self.assertEqual(resp["result_code"], ResultCode.OK)

        snapshot = DailyPriceSnapshot.objects.get(day_index=5)
        self.assertEqual(snapshot.status, DaySnapshotStatus.PUBLISHED)
        self.assertEqual(snapshot.published_by_npc_id, 10013)

    def test_publish_no_draft_fails(self):
        """Cannot publish if no DRAFT exists."""
        resp = commit_economy_action(_payload(
            "PUBLISH_PRICE_INDEX", 10013, "RECORD_HALL",
            {"day_index": 999},
        ))
        self.assertEqual(resp["result_code"], ResultCode.P3_PRICE_UNAVAILABLE)

    def test_publish_mark_salary(self):
        """Mark receives salary for publishing."""
        DailyPriceSnapshot.objects.create(
            day_index=5,
            prices={"1030": 8},
            status=DaySnapshotStatus.DRAFT,
        )

        balance_before = self.account.balance
        commit_economy_action(_payload(
            "PUBLISH_PRICE_INDEX", 10013, "RECORD_HALL",
            {"day_index": 5},
        ))

        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, balance_before - 1)


# ============================================================================
# AUDIT_PUBLIC_FUND
# ============================================================================


class AuditPublicFundTest(PublicEconomyTestBase):
    def test_audit_success(self):
        """Mark audits public fund successfully."""
        resp = commit_economy_action(_payload(
            "AUDIT_PUBLIC_FUND", 10013, "RECORD_HALL",
            {"day_index": 0},
        ))
        self.assertEqual(resp["result_code"], ResultCode.OK)

        audit = resp["effects"]["audit"]
        self.assertEqual(audit["actual_balance"], self.account.balance)
        self.assertTrue(audit["balance_matches"])

    def test_audit_mark_salary(self):
        """Mark receives salary for audit."""
        balance_before = self.account.balance
        commit_economy_action(_payload(
            "AUDIT_PUBLIC_FUND", 10013, "RECORD_HALL",
            {"day_index": 0},
        ))

        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, balance_before - 1)


# ============================================================================
# EAT personal
# ============================================================================


class EatPersonalTest(PublicEconomyTestBase):
    def test_eat_item_typed_as_food(self):
        Item.objects.update_or_create(
            item_id=1019,
            defaults={"item_name": "bread", "item_type": ItemType.FOOD, "is_deleted": False},
        )
        slot = NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1019, item_amount=1,
        )

        resp = commit_economy_action(_payload(
            "EAT", 10011, "VILLAGE_SQUARE",
            {"source": "PERSONAL_INVENTORY", "food_instance_id": str(slot.instance_id)},
        ))

        self.assertEqual(resp["result_code"], ResultCode.OK)
        self.assertEqual(
            NpcGlobalInventorySlot.objects.get(bag=self.bill_inv, item_id=1019).item_amount,
            0,
        )

    def test_eat_personal_success(self):
        """NPC eats the requested item from personal inventory."""
        slot = NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1030, item_amount=3,
            produced_world_minute=1000, expires_world_minute=4000,
        )

        resp = commit_economy_action(_payload(
            "EAT", 10011, "VILLAGE_SQUARE",
            {"source": "PERSONAL_INVENTORY", "food_instance_id": str(slot.instance_id)},
        ))
        self.assertEqual(resp["result_code"], ResultCode.OK)

        # 1 corn consumed
        slot = NpcGlobalInventorySlot.objects.get(
            bag=self.bill_inv, item_id=1030,
        )
        self.assertEqual(slot.item_amount, 2)

        self.assertEqual(resp["needs_delta"], {"hunger_fp": -450, "energy_fp": 50})
        self.assertEqual(
            resp["data"]["public_needs"],
            {"hunger": 0.0, "energy": 85.0, "health": 100.0, "social": 20.0, "stress": 10.0},
        )
        self.assertEqual(resp["data"]["needs_delta"], {"hunger": -20.0, "energy": 5.0})
        self.bill_rt.refresh_from_db()
        self.assertEqual((self.bill_rt.hunger_fp, self.bill_rt.energy_fp), (0, 850))
        self.assertEqual(
            NpcNeedsDaySummary.objects.get(npc=self.bill, day_index=1).successful_meals,
            1,
        )

    def test_eat_at_day_boundary_counts_the_completed_day(self):
        self.time.world_minute = 2880
        self.time.save(update_fields=["world_minute"])
        slot = NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1030, item_amount=1,
        )

        commit_economy_action(_payload(
            "EAT", 10011, "VILLAGE_SQUARE",
            {"source": "PERSONAL_INVENTORY", "food_instance_id": str(slot.instance_id)},
        ))

        self.assertTrue(NpcNeedsDaySummary.objects.filter(npc=self.bill, day_index=1).exists())

    def test_eat_consumes_requested_instance(self):
        """The requested bread is consumed even when chicken expires first."""
        Item.objects.update_or_create(
            item_id=1019,
            defaults={"item_name": "bread", "item_type": ItemType.FOOD, "is_deleted": False},
        )
        bread = NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1019, item_amount=4,
        )
        chicken = NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=1, item_id=1056, item_amount=1,
            produced_world_minute=800, expires_world_minute=3000,
        )

        resp = commit_economy_action(_payload(
            "EAT", 10011, "VILLAGE_SQUARE",
            {"source": "PERSONAL_INVENTORY", "food_instance_id": str(bread.instance_id)},
        ))
        self.assertEqual(resp["result_code"], ResultCode.OK)

        bread.refresh_from_db()
        chicken.refresh_from_db()
        self.assertEqual(bread.item_amount, 3)
        self.assertEqual(chicken.item_amount, 1)
        self.assertEqual(resp["effects"]["eat"]["food_instance_id"], str(bread.instance_id))

    def test_eat_no_food_fails(self):
        """EAT fails when NPC has no food."""
        resp = commit_economy_action(_payload(
            "EAT", 10011, "VILLAGE_SQUARE",
            {"source": "PERSONAL_INVENTORY", "food_instance_id": str(uuid.uuid4())},
        ))
        self.assertEqual(resp["result_code"], ResultCode.P3_INSUFFICIENT_ITEM)

    def test_eat_requires_food_instance_id(self):
        resp = commit_economy_action(_payload(
            "EAT", 10011, "VILLAGE_SQUARE", {"source": "PERSONAL_INVENTORY"},
        ))
        self.assertEqual(resp["result_code"], ResultCode.P3_INVALID_PARAMS)

    def test_eat_cooked_dish(self):
        """NPC can eat cooked dishes from personal inventory."""
        slot = NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1053, item_amount=1,
            produced_world_minute=1000, expires_world_minute=4000,
            dish_quality=3, rated=False,
        )

        resp = commit_economy_action(_payload(
            "EAT", 10011, "VILLAGE_SQUARE",
            {"source": "PERSONAL_INVENTORY", "food_instance_id": str(slot.instance_id)},
        ))
        self.assertEqual(resp["result_code"], ResultCode.OK)


# ============================================================================
# EAT community / COMMUNITY_MEAL
# ============================================================================


class EatCommunityTest(PublicEconomyTestBase):
    def test_community_meal_success(self):
        """Community meal: consume Lucy's dish, PUBLIC_FUND pays."""
        # Lucy has a dish
        NpcGlobalInventorySlot.objects.create(
            bag=self.lucy_inv, slot_index=0, item_id=1053, item_amount=2,
            produced_world_minute=1000, expires_world_minute=4000,
            dish_quality=3, rated=False,
        )

        lucy_money_before = self.lucy_inv.money
        fund_balance_before = self.account.balance

        resp = commit_economy_action(_payload(
            "EAT", 10011, "RESTAURANT",
            {"source": "COMMUNITY_MEAL"},
        ))
        self.assertEqual(resp["result_code"], ResultCode.OK)

        # Lucy received payment
        self.lucy_inv.refresh_from_db()
        self.assertEqual(self.lucy_inv.money, lucy_money_before + 25)

        # PUBLIC_FUND deducted
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, fund_balance_before - 25)

        # Dish consumed
        dish = NpcGlobalInventorySlot.objects.get(bag=self.lucy_inv, item_id=1053)
        self.assertEqual(dish.item_amount, 1)

    def test_community_meal_insufficient_fund(self):
        """Community meal fails when PUBLIC_FUND cannot pay."""
        self.account.balance = 0
        self.account.save(update_fields=["balance"])

        NpcGlobalInventorySlot.objects.create(
            bag=self.lucy_inv, slot_index=0, item_id=1053, item_amount=1,
            produced_world_minute=1000, expires_world_minute=4000,
        )

        resp = commit_economy_action(_payload(
            "EAT", 10011, "RESTAURANT",
            {"source": "COMMUNITY_MEAL"},
        ))
        self.assertEqual(resp["result_code"], ResultCode.P3_INSUFFICIENT_MONEY)

    def test_community_meal_no_dishes(self):
        """Community meal fails when Lucy has no dishes."""
        resp = commit_economy_action(_payload(
            "EAT", 10011, "RESTAURANT",
            {"source": "COMMUNITY_MEAL"},
        ))
        self.assertEqual(resp["result_code"], ResultCode.P3_INSUFFICIENT_ITEM)

    def test_community_meal_via_handler(self):
        """COMMUNITY_MEAL action also works."""
        NpcGlobalInventorySlot.objects.create(
            bag=self.lucy_inv, slot_index=0, item_id=1067, item_amount=1,
            produced_world_minute=1000, expires_world_minute=4000,
            dish_quality=3, rated=False,
        )

        resp = commit_economy_action(_payload(
            "COMMUNITY_MEAL", 10012, "RESTAURANT",
            {"expected_asset_version": self.jack_inv.asset_version},
        ))
        self.assertEqual(resp["result_code"], ResultCode.OK)


# ============================================================================
# Caravan cooldown
# ============================================================================


class CaravanCooldownTest(PublicEconomyTestBase):
    def test_cooldown_3_days(self):
        """Cannot import again within 3 days."""
        # First import
        resp1 = commit_economy_action(_payload(
            "IMPORT_GOODS", 10015, "TRADE_ROAD",
            {"item_id": 1040, "amount": 1},
        ))
        self.assertEqual(resp1["result_code"], ResultCode.OK)

        # Second import same day should fail
        resp2 = commit_economy_action(_payload(
            "IMPORT_GOODS", 10015, "TRADE_ROAD",
            {"item_id": 1026, "amount": 1},
        ))
        self.assertEqual(resp2["result_code"], ResultCode.P3_CARAVAN_COOLDOWN)

    def test_cooldown_resets_after_3_days(self):
        """Import allowed after 3 days."""
        self.account.last_caravan_day = (2000 // 1440) - 3
        self.account.save(update_fields=["last_caravan_day"])

        resp = commit_economy_action(_payload(
            "IMPORT_GOODS", 10015, "TRADE_ROAD",
            {"item_id": 1040, "amount": 1},
        ))
        self.assertEqual(resp["result_code"], ResultCode.OK)
