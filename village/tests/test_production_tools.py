import uuid

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.models import InventoryBag, InventorySlot, NpcDefinition
from items.models import Item
from player.models import PlayerSkillProgress, SkillRewardReceipt
from village.models import VillageStore, VillageStoreAccount, VillageStoreListing, EconomicLedgerEntry
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
        for item_id in (1002, 1051):
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

    def test_readonly_tool_validation_and_once_only_free_axe(self):
        with CaptureQueriesContext(connection) as queries:
            self.assertTrue(tools_overview(self.player, self.bag, 100)["can_claim_starter_axe"])
        self.assertFalse([q for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))])
        first = self.act("claim_starter_axe")
        axe = resolve_tool(self.player, self.bag, "forestry", minute=100)
        self.assertEqual((axe.item_id, axe.remaining_uses, str(axe.instance_id)), (4104, 20, first["instance_id"]))
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
        self.assertEqual((tool.instance_id, tool.item_id, tool.item_amount, tool.remaining_uses), (original, 4102, 1, 50))
        other = self.bag.slots.get(item_id=4101)
        self.assertEqual((other.item_amount, other.remaining_uses), (1, 5))
        self.assertEqual((material.item_amount, self.bag.money, self.account.balance), (2, 400, 600))

    def test_repair_exact_cost_and_stale_quote_or_full_tool_cannot_charge(self):
        self.level("farming")
        tool = self.slot(4103, uses=10)
        wood = self.slot(29306, amount=2)
        quote = self.quote(tool, "repair")
        self.assertEqual((quote["gold_cost"], quote["material_item_id"], quote["material_quantity"]), (15, 29306, 1))
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
        self.assertEqual(EconomicLedgerEntry.objects.filter(entry_type="G2_INITIAL_STOCK").count(), 7)
