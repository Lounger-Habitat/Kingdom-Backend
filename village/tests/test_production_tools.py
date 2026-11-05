import uuid

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.models import InventoryBag, InventorySlot, NpcDefinition
from items.models import Item
from player.models import PlayerSkillProgress, SkillRewardReceipt
from village.models import VillageStore, VillageStoreAccount, VillageStoreListing, EconomicLedgerEntry, VillageSimulationConfig
from village.player_actions import execute_player_action
from village.production_tools import (ToolError, ensure_production_catalog, execute_tool_action,
                                      resolve_tool, tools_overview, wear_tool)


class ProductionToolTests(TestCase):
    def setUp(self):
        self.clock = GlobalGameTimeState.get_instance()
        self.clock.world_minute = 100
        self.clock.save()
        self.player = GameAccount.objects.create(username="production-tools")
        self.bag = InventoryBag.objects.create(player=self.player, character_id=self.player.username, money=500)
        nora, _ = NpcDefinition.objects.get_or_create(agent_id="10015", defaults=dict(name="Nora"))
        self.store, _ = VillageStore.objects.update_or_create(store_code="GENERAL_STORE", defaults=dict(
            display_name="杂货铺", location_code="GENERAL_STORE", operator_npc=nora, status="OPEN"))
        self.account, _ = VillageStoreAccount.objects.update_or_create(store=self.store, defaults=dict(balance=500))
        for item_id in (1002, 1051, 4003):
            Item.objects.get_or_create(item_id=item_id, defaults=dict(item_name=str(item_id), item_price=5))

    def slot(self, item_id, amount=1, uses=None):
        return InventorySlot.objects.create(bag=self.bag, slot_index=self.bag.slots.count(), item_id=item_id,
                                            item_amount=amount, remaining_uses=uses)

    def level(self, skill, xp=440):
        PlayerSkillProgress.objects.update_or_create(player=self.player, skill_code=skill, defaults=dict(total_xp=xp))

    def act(self, action, slot=None, quote=None):
        params = dict(instance_id=str(slot.instance_id), expected_quote=quote) if slot else {}
        return execute_tool_action(self.player, self.bag, action, params, uuid.uuid4(), self.clock)

    def quote(self, slot, kind):
        return next(t for t in tools_overview(self.player, self.bag, 100)["tools"]
                    if t["instance_id"] == str(slot.instance_id))[kind]

    def test_projection_quotes_match_execution_and_do_not_query_per_tool(self):
        from village.production_tools import _quote
        ensure_production_catalog(100)
        self.level("forestry")
        axe = self.slot(4104, uses=10)
        self.slot(1002, amount=20)
        first = tools_overview(self.player, self.bag, 100)["tools"][0]
        for operation in ("repair", "upgrade"):
            self.assertEqual(first[operation], _quote(self.player, self.bag, axe, operation, 100))
        with CaptureQueriesContext(connection) as one:
            tools_overview(self.player, self.bag, 100)
        for _ in range(8):
            self.slot(4104, uses=10)
        with CaptureQueriesContext(connection) as many:
            tools_overview(self.player, self.bag, 100)
        self.assertEqual(len(one), len(many), "Repeated axes must share skill, material and catalog reads.")

    def test_readonly_tool_validation_and_once_only_free_axe(self):
        with CaptureQueriesContext(connection) as queries:
            self.assertFalse(tools_overview(self.player, self.bag, 100)["can_claim_starter_axe"])
        self.assertFalse([q for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))])
        from inventory.services import ensure_initial_equipment
        ensure_initial_equipment(self.player, self.bag)
        axe = resolve_tool(self.player, self.bag, "forestry", minute=100)
        self.assertEqual((axe.item_id, axe.remaining_uses, str(axe.instance_id)), (4104, 20, str(axe.instance_id)))
        with self.assertRaises(ToolError):
            self.act("claim_starter_axe")
        self.assertEqual(self.bag.slots.filter(item_id=4104).count(), 1)
        self.assertEqual(SkillRewardReceipt.objects.filter(player=self.player).count(), 0)

    def test_upgrade_preserves_selected_instance_and_other_stack_components(self):
        self.level("gathering")
        tool = self.slot(4101, amount=2, uses=5)
        material = self.slot(1002, amount=8)
        original = tool.instance_id
        quote = self.quote(tool, "upgrade")
        self.assertTrue(quote["available"])
        self.act("upgrade_tool", tool, quote["quote_hash"])
        tool.refresh_from_db(); material.refresh_from_db(); self.bag.refresh_from_db(); self.account.refresh_from_db()
        self.assertEqual((tool.instance_id, tool.item_id, tool.item_amount, tool.remaining_uses), (original, 4101, 1, 5))
        other = self.bag.slots.filter(item_id=4101).exclude(pk=tool.pk).get()
        self.assertEqual((other.item_amount, other.remaining_uses), (1, 5))
        self.assertEqual((material.item_amount, self.bag.money, self.account.balance), (6, 480, 520))

    def test_upgrade_quotes_explain_current_and_target_attributes(self):
        self.level("gathering")
        quote = self.quote(self.slot(4101, uses=5), "upgrade")
        current = {row["code"]: row["value"] for row in quote["current_attributes"]}
        target = {row["code"]: row["value"] for row in quote["target_attributes"]}
        self.assertEqual((current["durability"], target["durability"]), ("20", "30"))
        self.assertIn("gathering_time_reduction", current)
        self.assertNotEqual(current["gathering_time_reduction"], target["gathering_time_reduction"])

    def test_initial_equipment_is_idempotent_and_never_replaces_sold_or_upgraded_items(self):
        from inventory.services import ensure_initial_equipment
        ensure_initial_equipment(self.player, self.bag)
        sword = self.bag.slots.get(item_id=4114)
        axe = self.bag.slots.get(item_id=4104)
        sword.item_amount = 0; sword.save()
        axe.item_id = 4105; axe.remaining_uses = 12; axe.save()
        ensure_initial_equipment(self.player, self.bag)
        self.assertFalse(self.bag.slots.filter(item_id=4114, item_amount__gt=0).exists())
        self.assertFalse(self.bag.slots.filter(item_id=4104, item_amount__gt=0).exists())
        axe.refresh_from_db(); self.assertEqual((axe.item_id, axe.remaining_uses), (4105, 12))

    def test_existing_world_weapon_supply_migration_never_replenishes_purchased_stock(self):
        from importlib import import_module
        from types import SimpleNamespace
        from django.apps import apps
        migration = import_module("village.migrations.0084_initial_weapon_shop_stock")
        editor = SimpleNamespace(connection=connection)
        migration.supply_weapons(apps, editor)
        listing = VillageStoreListing.objects.get(store=self.store, item_id=4110)
        self.assertEqual(sum(listing.inventory_slots.values_list("item_amount", flat=True)), 20)
        row = listing.inventory_slots.first(); row.item_amount = 0; row.save()
        migration.supply_weapons(apps, editor)
        ensure_production_catalog(100)
        self.assertEqual(sum(listing.inventory_slots.values_list("item_amount", flat=True)), 19)
        self.assertEqual(VillageStoreListing.objects.filter(store=self.store, item_id__in=[4110, 4112]).count(), 2)

    def test_repair_exact_cost_and_stale_quote_or_full_tool_cannot_charge(self):
        self.level("farming")
        tool = self.slot(4103, uses=10)
        wood = self.slot(4003, amount=2)
        quote = self.quote(tool, "repair")
        self.assertEqual((quote["gold_cost"], quote["material_item_id"], quote["material_quantity"]), (15, 4003, 1))
        self.act("repair_tool", tool, quote["quote_hash"])
        tool.refresh_from_db(); wood.refresh_from_db(); self.bag.refresh_from_db()
        self.assertEqual((tool.remaining_uses, wood.item_amount, self.bag.money), (50, 1, 485))
        with self.assertRaises(ToolError) as error:
            self.act("repair_tool", tool, quote["quote_hash"])
        self.assertEqual(error.exception.code, "QUOTE_CHANGED")
        full = self.quote(tool, "repair")
        with self.assertRaises(ToolError) as error:
            self.act("repair_tool", tool, full["quote_hash"])
        self.assertEqual(error.exception.code, "TOOL_ALREADY_FULL")
        self.bag.refresh_from_db()
        self.assertEqual(self.bag.money, 485)

    def test_resolve_rejects_missing_broken_low_tier_and_skill_without_wear(self):
        with self.assertRaises(ToolError):
            resolve_tool(self.player, self.bag, "forestry", minute=100)
        axe = self.slot(4104, uses=20)
        with self.assertRaises(ToolError):
            resolve_tool(self.player, self.bag, "forestry", minimum_tier=2, minute=100)
        better = self.slot(4105, uses=50)
        better.equipment_level = 3; better.save(update_fields=["equipment_level"])
        with self.assertRaises(ToolError):
            resolve_tool(self.player, self.bag, "forestry", instance_id=better.instance_id, minute=100)
        axe.refresh_from_db(); better.refresh_from_db()
        self.assertEqual((axe.remaining_uses, better.remaining_uses), (20, 50))
        axe.remaining_uses = 0; axe.save(update_fields=["remaining_uses"])
        with self.assertRaises(ToolError):
            resolve_tool(self.player, self.bag, "forestry", instance_id=axe.instance_id, minute=100)

    def test_successful_wear_splits_only_one_stack_member(self):
        tool = self.slot(4001, amount=2, uses=20)
        result = wear_tool(resolve_tool(self.player, self.bag, "farming", minute=100))
        self.assertEqual(sorted(self.bag.slots.filter(item_id=4001).values_list("usage__remaining", flat=True)), [19, 20])
        self.assertNotEqual(result["instance_id"], str(tool.instance_id))

    def test_finite_seed_and_independent_farm_tool_stock_never_regenerates(self):
        ensure_production_catalog(100)
        farm = VillageStoreListing.objects.get(store=self.store, item_id=4001)
        self.assertEqual(farm.inventory_slots.count(), 20)
        self.assertEqual({s.remaining_uses for s in farm.inventory_slots.all()}, {20})
        seeds = VillageStoreListing.objects.get(store=self.store, item_id=29304)
        self.assertEqual(sum(seeds.inventory_slots.values_list("item_amount", flat=True)), 30)
        seeds.delete()
        ensure_production_catalog(200)
        self.assertFalse(VillageStoreListing.objects.filter(store=self.store, item_id=29304).exists())
        self.assertEqual(EconomicLedgerEntry.objects.filter(entry_type="G2_INITIAL_STOCK").count(), 21)

    def remote(self, action, tool, quote, **context):
        VillageSimulationConfig.objects.update_or_create(config_key="default", defaults=dict(village_economy_enabled=True))
        payload = dict(player_id=str(self.player.pk), request_id=str(uuid.uuid4()), action=action,
            params=dict(instance_id=str(tool.instance_id), expected_quote=quote["quote_hash"]),
            current_location_code="FOREST_EDGE", **context)
        return execute_player_action(payload), payload

    def test_personal_repair_works_away_from_closed_shop_and_retry_never_charges_twice(self):
        tool = self.slot(4104, uses=3)
        wood = self.slot(1002, amount=2)
        self.store.status = "CLOSED"
        self.store.save(update_fields=["status"])
        quote = self.quote(tool, "repair")
        self.assertTrue(quote["available"])
        self.assertIsNone(quote["location_code"])
        answer, payload = self.remote("repair_tool", tool, quote)
        self.assertTrue(answer["success"], answer)
        self.assertEqual(execute_player_action(payload), dict(answer, replayed=True))
        self.assertEqual(answer["data"]["settlement_section"], "equipment")
        self.assertTrue(answer["data"]["maintenance_settlement"])
        self.assertTrue(answer["data"]["overview_partial"])
        self.assertNotIn("inventory_partial", answer["data"])
        self.assertNotIn("tutorial", answer["data"]["overview"])
        self.assertNotIn("starter", answer["data"]["overview"])
        self.assertTrue(answer["data"]["overview"]["plants"]["nodes_partial"])
        for row in answer["data"]["overview"]["inventory"]:
            self.assertTrue({"slot_index", "item_bag_name", "hide_in_shop"}.issubset(row))
        self.assertCountEqual([row["instance_id"] for row in answer["data"]["overview"]["inventory"]],
            [str(value) for value in self.bag.slots.filter(item_amount__gt=0).values_list("instance_id", flat=True)])
        tool.refresh_from_db(); wood.refresh_from_db(); self.bag.refresh_from_db(); self.account.refresh_from_db()
        self.assertEqual((tool.remaining_uses, wood.item_amount, self.bag.money, self.account.balance), (20, 1, 495, 505))

    def test_remote_upgrade_preserves_instance_and_readonly_or_foreign_instance_cannot_spend(self):
        self.level("gathering")
        tool = self.slot(4101, uses=3)
        self.slot(1002, amount=8)
        quote = self.quote(tool, "upgrade")
        answer, _ = self.remote("upgrade_tool", tool, quote, read_only=True)
        self.assertFalse(answer["success"])
        other = GameAccount.objects.create(username="other-tool-owner")
        bag = InventoryBag.objects.create(player=other, character_id=other.username)
        foreign = InventorySlot.objects.create(bag=bag, slot_index=0, item_id=4101, item_amount=1, remaining_uses=3)
        answer, _ = self.remote("upgrade_tool", foreign, quote)
        self.assertFalse(answer["success"])
        self.assertTrue(answer["data"]["maintenance_settlement"])
        self.assertNotIn(str(foreign.instance_id), [row["instance_id"] for row in answer["data"]["overview"]["inventory"]])
        self.bag.refresh_from_db()
        self.assertEqual(self.bag.money, 500)
        original_id = tool.instance_id
        answer, _ = self.remote("upgrade_tool", tool, quote)
        self.assertTrue(answer["success"], answer)
        tool.refresh_from_db(); self.bag.refresh_from_db()
        self.assertEqual((tool.instance_id, tool.item_id, tool.remaining_uses), (original_id, 4101, 3))
        self.account.refresh_from_db()
        self.assertEqual((answer["data"]["result"]["spent_gold"], self.account.balance), (20, 520))
        self.assertEqual(answer["data"]["settlement_section"], "equipment")
        self.assertNotIn("inventory_partial", answer["data"])

    def test_free_basic_weapon_repair_does_not_require_jack_or_a_workshop(self):
        NpcDefinition.objects.filter(agent_id="10012").update(is_test_only=True)
        tool = self.slot(4112, uses=0)
        quote = self.quote(tool, "repair")
        self.assertTrue(quote["available"])
        self.assertEqual(quote["action"], "repair_hunting_weapon")
        self.assertIsNone(quote["location_code"])
        answer, _ = self.remote("repair_hunting_weapon", tool, quote)
        self.assertTrue(answer["success"], answer)
        self.assertTrue(answer["data"]["maintenance_settlement"])
        tool.refresh_from_db(); self.bag.refresh_from_db()
        self.assertEqual((tool.remaining_uses, self.bag.money), (60, 500))
