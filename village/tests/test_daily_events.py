import uuid
from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import TestCase

from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot
from items.models import Item
from recipes.models import Recipe, RecipeIngredient
from village.daily_events import evaluate_daily_events, project_daily_events
from village.models import (VillageAffair, VillageDailyEvent, VillageDailyEventTemplate,
    VillageDishReview, VillageCaravanVisit, VillageSimulationConfig, VillageFarmPlot,
    VillageStore, VillageStoreAccount, VillageStoreListing, VillageStoreInventorySlot,
    VillageAccount, VillageCommunityMealDailyUsage, VillageWorldEvent, VillageDeliveryOutbox)
from village.runtime_services import TimeService


class DailyEventTests(TestCase):
    def setUp(self):
        VillageDailyEventTemplate.objects.all().delete()
        VillageSimulationConfig.objects.update_or_create(config_key="default", defaults={"village_story_enabled": True})
        self.clock = GlobalGameTimeState.get_instance()
        self.clock.world_minute = 100
        self.clock.time_revision = 100
        self.clock.save()
        self.npcs, self.bags = {}, {}
        Item.objects.get_or_create(item_id=29201, defaults={"item_name": "样品"})
        for ident, name in [("10011", "Bill"), ("10012", "Jack"), ("10013", "Mark"), ("10014", "Lucy"), ("10015", "Nora")]:
            self.npcs[ident], _ = NpcDefinition.objects.get_or_create(agent_id=ident, defaults={"name": name})
            self.bags[ident], _ = NpcGlobalInventory.objects.get_or_create(npc=self.npcs[ident])
            self.bags[ident].slots.all().delete()

    def template(self, rule="INVENTORY_SHORTAGE", **parameters):
        return VillageDailyEventTemplate.objects.create(code=str(uuid.uuid4()), title="测试机会", rule_type=rule,
            npc_scope="PUBLIC" if rule in {"PUBLIC_MEAL", "AFFAIR_STAGE", "NORMAL_CARAVAN_RETURN"} else "PERSONAL",
            parameters=parameters or {"npc_id": "10014", "item_ids": [1030], "minimum": 2},
            summary="真实需求出现", participation_hint="自主决定如何参与", resolved_summary="真实条件已满足")

    def slot(self, npc_id="10014", item_id=1030, amount=2, **kwargs):
        return NpcGlobalInventorySlot.objects.create(bag=self.bags[npc_id], item_id=item_id,
            item_amount=amount, slot_index=self.bags[npc_id].slots.count(), **kwargs)

    def at(self, minute):
        self.clock.world_minute = minute
        self.clock.time_revision += 1
        self.clock.save()

    def event(self):
        return VillageDailyEvent.objects.get()

    def test_zero_players_opens_once_and_clock_expiry_does_not_create_assets(self):
        self.template()
        evaluate_daily_events()
        evaluate_daily_events()
        row = self.event()
        self.assertEqual((row.status, row.invite_until_world_minute), ("OPEN", 700))
        self.assertEqual(VillageWorldEvent.objects.count(), 1)
        self.assertEqual(self.bags["10014"].slots.count(), 0)
        ok, _, _ = TimeService.receive_time(700, 101)
        self.assertTrue(ok)
        row.refresh_from_db()
        self.assertEqual(row.status, "EXPIRED")
        self.assertIsNone(row.active_key)
        evaluate_daily_events()
        self.assertEqual(VillageDailyEvent.objects.count(), 1)
        self.assertEqual(VillageWorldEvent.objects.count(), 2)
        self.assertEqual(self.bags["10014"].slots.count(), 0)

    def test_actual_restock_ends_early_recovery_and_frozen_configuration(self):
        template = self.template()
        evaluate_daily_events()
        template.parameters = {"npc_id": "10014", "item_ids": [1056], "minimum": 100}
        template.invitation_minutes = 5
        template.enabled = False
        template.npc_scope = "PUBLIC"
        template.save()
        self.slot()
        self.at(101)
        evaluate_daily_events()
        row = self.event()
        self.assertEqual((row.status, row.closed_world_minute, row.invite_until_world_minute), ("RESOLVED", 101, 700))
        self.assertEqual(row.rule_snapshot["npc_scope"], "PERSONAL")
        self.assertEqual(set(VillageDeliveryOutbox.objects.values_list("recipient", flat=True)), {"10014", "ENV_CLIENTS"})
        restored = project_daily_events(101)
        self.assertEqual(restored[0]["id"], str(row.pk))
        self.assertEqual(restored[0]["resolution_summary"], "真实条件已满足")
        self.assertNotIn("facts", restored[0])
        self.assertNotIn("parameters", restored[0])
        evaluate_daily_events()
        self.assertEqual(VillageWorldEvent.objects.count(), 2)

    def test_failed_transaction_rolls_back_invitation_and_broadcast(self):
        self.template()
        with self.assertRaises(RuntimeError), transaction.atomic():
            evaluate_daily_events()
            raise RuntimeError("business settlement failed")
        self.assertFalse(VillageDailyEvent.objects.exists())
        self.assertFalse(VillageWorldEvent.objects.exists())

    def test_reserved_and_expired_stock_does_not_resolve_shortage(self):
        self.template()
        evaluate_daily_events()
        self.slot(expires_world_minute=100)
        self.slot(amount=100, freshness_unknown=True)
        evaluate_daily_events()
        self.assertEqual(self.event().status, "OPEN")

    def test_recipe_preparation_is_true_recipe_availability(self):
        recipe = Recipe.objects.create(pk=29201, item_id=29201, recipe_name="测试菜")
        RecipeIngredient.objects.create(recipe=recipe, item_id=1030, item_name="原料", count=2)
        self.template("RECIPE_PREPARATION", npc_id="10014", recipe_ids=[recipe.pk])
        self.slot(amount=1)
        evaluate_daily_events()
        self.assertEqual(self.event().status, "OPEN")
        self.assertEqual(set(VillageDeliveryOutbox.objects.values_list("recipient", flat=True)), {"10014", "ENV_CLIENTS"})
        self.assertEqual(project_daily_events(npc_id="10011"), [])
        self.slot(amount=1)
        evaluate_daily_events()
        self.assertEqual(self.event().status, "RESOLVED")

    def test_deleted_recipe_does_not_claim_restock_success(self):
        recipe = Recipe.objects.create(pk=29201, item_id=29201, recipe_name="测试菜")
        RecipeIngredient.objects.create(recipe=recipe, item_id=1030, item_name="原料", count=2)
        self.template("RECIPE_PREPARATION", npc_id="10014", recipe_ids=[recipe.pk])
        evaluate_daily_events()
        recipe.delete()
        evaluate_daily_events()
        self.assertEqual(self.event().status, "EXPIRED")

    def test_farm_ignores_leased_land_and_requires_durable_tool(self):
        VillageFarmPlot.objects.all().delete()
        plot = VillageFarmPlot.objects.create(plot_code="FARM_PLOT_A", status="EMPTY", lease_enabled=True)
        self.template("FARM_PREPARATION", npc_id="10011")
        evaluate_daily_events()
        self.assertFalse(VillageDailyEvent.objects.exists())
        plot.lease_enabled = False
        plot.save()
        evaluate_daily_events()
        self.assertEqual(self.event().status, "OPEN")
        self.slot("10011", 1051, remaining_uses=None)
        self.slot("10011", 4001, remaining_uses=0)
        evaluate_daily_events()
        self.assertEqual(self.event().status, "OPEN")
        self.slot("10011", 4001, remaining_uses=2)
        evaluate_daily_events()
        self.assertEqual(self.event().status, "RESOLVED")

    def test_perishable_expiry_is_not_success_and_transfer_can_end_early(self):
        self.template("PERISHABLE_STOCK", npc_id="10012", item_ids=[1056], warning_minutes=600)
        slot = self.slot("10012", 1056, expires_world_minute=300)
        evaluate_daily_events()
        self.assertEqual(self.event().invite_until_world_minute, 300)
        self.at(300)
        evaluate_daily_events()
        self.assertEqual(self.event().status, "EXPIRED")
        self.assertIn("保鲜", self.event().resolution_summary)

    def test_review_completion_and_expiry_follow_original_review(self):
        slot = self.slot(item_id=29201, amount=1, expires_world_minute=1000)
        review = VillageDishReview.objects.create(owner=self.npcs["10014"], slot=slot, request_id=uuid.uuid4(),
            created_world_minute=100, expires_world_minute=600)
        self.template("DISH_REVIEW", npc_id="10013")
        evaluate_daily_events()
        review.status = "COMPLETED"
        review.completed_world_minute = 100
        review.save()
        evaluate_daily_events()
        self.assertEqual(self.event().status, "RESOLVED")
        evaluate_daily_events()
        self.assertEqual(VillageDailyEvent.objects.count(), 1)

    def test_unusable_review_sample_does_not_open_or_claim_success(self):
        slot = self.slot(item_id=29201, amount=1, expires_world_minute=1000)
        review = VillageDishReview.objects.create(owner=self.npcs["10014"], slot=slot, request_id=uuid.uuid4(),
            created_world_minute=100, expires_world_minute=600)
        self.template("DISH_REVIEW", npc_id="10013")
        evaluate_daily_events()
        slot.freshness_unknown = True
        slot.save()
        evaluate_daily_events()
        self.assertEqual(self.event().status, "EXPIRED")

    def test_public_meal_uses_real_stock_fund_day_index_and_claim(self):
        from village.lucy_catalog import PUBLIC_MEALS
        from village.lucy_restaurant import take_public_meal
        code, spec = next(iter(PUBLIC_MEALS.items()))
        store, _ = VillageStore.objects.update_or_create(store_code="RESTAURANT_STORE", defaults=dict(
            display_name="饭店", location_code="RESTAURANT", operator_npc=self.npcs["10014"], status="OPEN"))
        VillageStoreAccount.objects.get_or_create(store=store)
        fund, _ = VillageAccount.objects.update_or_create(account_code="PUBLIC_FUND", defaults={"balance": 10000})
        listing = VillageStoreListing.objects.create(store=store, item_id=spec["item_id"],
            unit_price=0, status="ACTIVE", public_meal_code=code, created_request_id=uuid.uuid4(),
            listed_by_npc=self.npcs["10014"], listed_world_minute=100)
        stock = VillageStoreInventorySlot.objects.create(store=store, listing=listing, item_id=spec["item_id"],
            item_amount=3, expires_world_minute=1000, dish_metadata={"maker_npc_id": "10014"})
        template = self.template("PUBLIC_MEAL", npc_id="10014")
        evaluate_daily_events()
        self.assertEqual(self.event().status, "OPEN")
        self.assertEqual(project_daily_events(npc_id="10011")[0]["npc_scope"], "PUBLIC")
        self.assertEqual(set(VillageDeliveryOutbox.objects.values_list("recipient", flat=True)),
            {"10011", "10012", "10013", "10014", "10015", "ENV_CLIENTS"})
        template.npc_scope = "PERSONAL"
        template.save(update_fields=["npc_scope"])
        fund.refresh_from_db()
        self.assertEqual(fund.balance, 10000)
        result, error = take_public_meal(store, listing, 100, uuid.uuid4(), npc_id="10011")
        self.assertIsNone(error)
        usage = VillageCommunityMealDailyUsage.objects.get(day_index=0)
        self.assertNotEqual(usage.pk, usage.day_index)
        evaluate_daily_events()
        self.assertEqual(self.event().status, "RESOLVED")
        self.assertEqual(self.event().rule_snapshot["npc_scope"], "PUBLIC")
        self.assertEqual(project_daily_events(npc_id="10011")[0]["status"], "RESOLVED")
        stock.refresh_from_db()
        self.assertEqual(stock.item_amount, 2)
        fund.refresh_from_db()
        self.assertEqual(fund.balance, 10000-spec["subsidy"])
        evaluate_daily_events()
        self.assertEqual(VillageDailyEvent.objects.count(), 1)

    def test_personal_public_affair_configuration_requires_a_named_owner(self):
        template = self.template("AFFAIR_STAGE", affair_id="BRIDGE_REPAIR")
        template.npc_scope = "PERSONAL"
        with self.assertRaises(ValidationError) as error:
            template.full_clean()
        self.assertIn("npc_scope", error.exception.message_dict)
        template.save(update_fields=["npc_scope"])
        evaluate_daily_events()
        self.assertFalse(VillageDailyEvent.objects.exists())

    def test_non_executable_role_or_recipe_configuration_is_rejected(self):
        template = self.template("FARM_PREPARATION", npc_id="10012")
        with self.assertRaises(ValidationError):
            template.full_clean()
        evaluate_daily_events()
        self.assertFalse(VillageDailyEvent.objects.exists())

    def test_caravan_uses_actual_visit_and_real_import_sequence(self):
        self.at(660)
        self.template("CARAVAN_VISIT", npc_id="10015")
        evaluate_daily_events()
        self.assertFalse(VillageDailyEvent.objects.exists())
        visit = VillageCaravanVisit.objects.create(day_index=0, created_world_minute=660, route="NORMAL",
            location_code="TRADE_ROAD", stock={"1030": 3})
        evaluate_daily_events()
        self.assertEqual(self.event().invite_until_world_minute, 900)
        VillageWorldEvent.objects.create(dedupe_key="real-import", event_type="ECONOMY_IMPORT_GOODS",
            actor_npc=self.npcs["10015"], world_minute=660, time_revision=101)
        evaluate_daily_events()
        self.assertEqual(self.event().status, "RESOLVED")
        visit.stock = {}
        visit.save()
        evaluate_daily_events()
        self.assertEqual(VillageDailyEvent.objects.count(), 1)

    def test_already_imported_same_minute_does_not_open_false_new_invitation(self):
        self.at(660)
        self.template("CARAVAN_VISIT", npc_id="10015")
        VillageCaravanVisit.objects.create(day_index=0, created_world_minute=660, route="NORMAL",
            location_code="TRADE_ROAD", stock={"1030": 3})
        VillageWorldEvent.objects.create(dedupe_key="prior-import", event_type="ECONOMY_IMPORT_GOODS",
            actor_npc=self.npcs["10015"], world_minute=660, time_revision=101)
        evaluate_daily_events()
        self.assertFalse(VillageDailyEvent.objects.exists())

    def test_bridge_expired_invitation_preserves_project_and_late_contribution(self):
        from village.affairs import create_resource_affair, contribute
        Item.objects.get_or_create(item_id=1002, defaults={"item_name": "木材"})
        affair = create_resource_affair("TEST_PROJECT", "工程", {"materials": {"1002": 2}}, world_minute=100)
        self.template("AFFAIR_STAGE", affair_id=affair.pk)
        evaluate_daily_events()
        self.at(700)
        evaluate_daily_events()
        affair.refresh_from_db()
        self.assertEqual((self.event().status, affair.status), ("EXPIRED", "ACTIVE"))
        contribute(affair, "item", 2, item_id=1002, world_minute=700, request_id=uuid.uuid4(), actor_npc=self.npcs["10011"])
        evaluate_daily_events()
        self.assertEqual(affair.status, "COMPLETED")
        self.assertEqual(self.event().status, "EXPIRED")

    def test_first_normal_caravan_requires_creation_after_completion_even_when_sold_out(self):
        self.at(800)
        affair = VillageAffair.objects.create(code="TEST_BRIDGE", title="桥", requirements={}, status="COMPLETED",
            opened_world_minute=100, completed_world_minute=790)
        self.template("NORMAL_CARAVAN_RETURN", affair_id=affair.pk)
        VillageCaravanVisit.objects.create(day_index=0, route="NORMAL", created_world_minute=660, stock={"1030": 1})
        evaluate_daily_events()
        self.assertFalse(VillageDailyEvent.objects.exists())
        VillageCaravanVisit.objects.filter(pk=0).update(created_world_minute=800, stock={})
        evaluate_daily_events()
        self.assertEqual(self.event().affair_id, affair.pk)
        self.assertIn("800", str(self.event().opened_world_minute))

    def test_invalid_rule_config_is_rejected_without_evaluating_scripts(self):
        template = self.template()
        template.parameters["expression"] = "arbitrary code"
        with self.assertRaises(ValidationError):
            template.full_clean()
        template.save()
        evaluate_daily_events()
        self.assertFalse(VillageDailyEvent.objects.exists())
