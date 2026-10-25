"""G1 tool assets, truthful category choices and legacy upgrade retirement."""
import uuid

from django.db import transaction
from django.test import TestCase
from rest_framework.test import APIClient

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.models import (CollectionPoint, CollectionPointLevel, CollectionPointLevelItem,
                              InventoryBag, InventorySlot, NpcDefinition, PlayerCollectionOverride,
                              PlayerCollectionPoint)
from inventory.views import _build_collect_pool
from items.models import Item
from player.models import PlayerGrowthState, PlayerSkillProgress
from village.models import (EconomicLedgerEntry, VillageAccount, VillageSimulationConfig,
                            VillageStore, VillageStoreAccount, VillageStoreInventorySlot,
                            VillageStoreListing)
from village.player_gathering import (GatheringError, TOOL_ITEM_ID, TOOL_LISTING_ID,
                                     ensure_gathering_catalog, gathering_overview,
                                     prepare_targeted_collection, wear_tool)
from village.player_store import buy_from_store


class PlayerGatheringTests(TestCase):
    def setUp(self):
        VillageSimulationConfig.objects.update_or_create(config_key="default",
            defaults=dict(village_economy_enabled=True))
        self.clock = GlobalGameTimeState.get_instance()
        self.clock.world_minute = 100
        self.clock.save()
        self.player = GameAccount.objects.create(username="gatherer")
        self.other = GameAccount.objects.create(username="other-gatherer")
        self.bag = InventoryBag.objects.create(player=self.player, character_id=self.player.username, money=200)
        self.other_bag = InventoryBag.objects.create(player=self.other, character_id=self.other.username, money=200)
        self.nora, _ = NpcDefinition.objects.get_or_create(agent_id="10015", defaults=dict(name="Nora"))
        self.store, _ = VillageStore.objects.update_or_create(store_code="GENERAL_STORE", defaults=dict(
            display_name="Nora 杂货铺", location_code="GENERAL_STORE", operator_npc=self.nora, status="OPEN"))
        VillageStoreAccount.objects.update_or_create(store=self.store, defaults=dict(balance=500))
        VillageAccount.objects.update_or_create(account_code="PUBLIC_FUND", defaults=dict(balance=1000))
        self.point, _ = CollectionPoint.objects.update_or_create(collect_type="采集食材", defaults=dict(
            name="测试食材点", item_type="蔬菜", action_cost=15, collect_count=3, default_level=1))
        self.level, _ = CollectionPointLevel.objects.get_or_create(collection_point=self.point, level=1)
        self.level.items.all().delete()
        for item_id, name in [(1023, "卷心菜"), (1059, "生菜"), (1025, "胡萝卜"), (1032, "鸡蛋")]:
            item, _ = Item.objects.update_or_create(item_id=item_id,
                defaults=dict(item_name=name, item_type="蔬菜", item_price=4, is_deleted=False))
            CollectionPointLevelItem.objects.create(level=self.level, item=item,
                collect_weight=100, collect_min_amount=3, collect_max_amount=5)

    def tool(self, uses=20, amount=1, bag=None, **kwargs):
        bag = bag or self.bag
        return InventorySlot.objects.create(bag=bag, slot_index=bag.slots.count(), item_id=TOOL_ITEM_ID,
            item_amount=amount, remaining_uses=uses, produced_world_minute=90, **kwargs)

    def enable(self):
        PlayerSkillProgress.objects.update_or_create(player=self.player, skill_code="gathering",
            defaults=dict(total_xp=80))

    def prepare(self, params, bag=None):
        pool, weights, _ = _build_collect_pool(self.player, self.point)
        return prepare_targeted_collection(self.player, bag or self.bag, params, pool, weights, minute=100)

    def test_seeded_stock_is_twenty_independent_instances_and_never_replenishes(self):
        listing = ensure_gathering_catalog(100)
        rows = list(listing.inventory_slots.all())
        self.assertEqual(len(rows), 20)
        self.assertEqual({row.item_amount for row in rows}, {1})
        self.assertEqual({row.remaining_uses for row in rows}, {20})
        self.assertEqual(len({row.instance_id for row in rows}), 20)
        self.assertEqual(EconomicLedgerEntry.objects.filter(entry_type="GATHERING_TOOL_STOCK").count(), 1)
        with transaction.atomic():
            result = buy_from_store(self.player, self.bag, dict(listing_id=str(listing.pk),
                quantity=1, expected_unit_price=50), uuid.uuid4(), "GENERAL_STORE", self.clock)
        self.assertEqual(result["spent_gold"], 50)
        bought = self.bag.slots.get(item_id=TOOL_ITEM_ID)
        self.assertEqual((bought.item_amount, bought.remaining_uses), (1, 20))
        listing.inventory_slots.update(item_amount=0)
        listing.status = "SOLD_OUT"
        listing.save()
        ensure_gathering_catalog(200)
        self.assertEqual(listing.inventory_slots.count(), 20)
        self.assertEqual(sum(listing.inventory_slots.values_list("item_amount", flat=True)), 0)
        self.assertFalse(gathering_overview(self.player, self.bag, 200)["tool_shop"]["available"])
        self.assertEqual(EconomicLedgerEntry.objects.filter(entry_type="GATHERING_TOOL_STOCK").count(), 1)
        listing.delete()
        self.assertIsNone(ensure_gathering_catalog(300))
        self.assertFalse(VillageStoreListing.objects.filter(pk=TOOL_LISTING_ID).exists())

    def test_missing_store_or_account_does_not_invent_a_shop(self):
        self.store.account.delete()
        self.assertIsNone(ensure_gathering_catalog(100))
        self.assertFalse(VillageStoreListing.objects.filter(pk=TOOL_LISTING_ID).exists())
        self.assertFalse(gathering_overview(self.player, self.bag, 100)["tool_shop"]["available"])

    def test_observation_has_no_progress_point_or_stock_writes_and_projects_legacy_floor(self):
        before = (PlayerGrowthState.objects.count(), PlayerSkillProgress.objects.count(),
                  PlayerCollectionPoint.objects.count(), VillageStoreInventorySlot.objects.count())
        data = gathering_overview(self.player, self.bag, 100)
        self.assertEqual(data["level"], 1)
        self.assertFalse(data["targeted_available"])
        self.assertEqual(before, (PlayerGrowthState.objects.count(), PlayerSkillProgress.objects.count(),
            PlayerCollectionPoint.objects.count(), VillageStoreInventorySlot.objects.count()))
        PlayerCollectionPoint.objects.create(player=self.player, collection_point=self.point, current_level=5)
        self.assertEqual(gathering_overview(self.player, self.bag, 100)["level"], 7)
        self.assertFalse(PlayerGrowthState.objects.filter(player=self.player).exists())

    def test_category_intersects_effective_pool_and_old_exclusion(self):
        self.enable()
        point = PlayerCollectionPoint.objects.create(player=self.player, collection_point=self.point, current_level=1)
        PlayerCollectionOverride.objects.create(player_collection_point=point, item_id=1023, is_excluded=True)
        tool = self.tool()
        pool, weights, chosen = self.prepare(dict(category="leafy", tool_instance_id=str(tool.instance_id)))
        self.assertEqual([entry["item"].pk for entry in pool], [1059])
        self.assertEqual(weights, [100])
        self.assertEqual(chosen.pk, tool.pk)
        self.assertEqual(tool.remaining_uses, 20)
        projected = gathering_overview(self.player, self.bag, 100)
        self.assertEqual(projected["categories"][0]["item_ids"], [1059])
        self.assertNotIn(1032, [item_id for row in projected["categories"] for item_id in row["item_ids"]])
        PlayerCollectionOverride.objects.create(player_collection_point=point, item_id=1059, override_weight=0)
        with self.assertRaises(GatheringError) as error:
            self.prepare(dict(category="leafy", tool_instance_id=str(tool.instance_id)))
        self.assertEqual(error.exception.code, "COLLECTION_CATEGORY_EMPTY")

    def test_free_basic_collection_and_targeted_authority_validation(self):
        pool, weights, _ = _build_collect_pool(self.player, self.point)
        self.assertEqual(prepare_targeted_collection(self.player, self.bag, {}, pool, weights), (pool, weights, None))
        tool = self.tool()
        cases = [({"category": "leafy"}, "INVALID_PARAMS"),
                 ({"tool_instance_id": str(tool.instance_id)}, "INVALID_PARAMS"),
                 ({"category": "absent", "tool_instance_id": str(tool.instance_id)}, "INVALID_PARAMS"),
                 ({"category": "leafy", "tool_instance_id": str(tool.instance_id)}, "SKILL_LEVEL_REQUIRED")]
        for params, code in cases:
            with self.subTest(code=code), self.assertRaises(GatheringError) as error:
                self.prepare(params)
            self.assertEqual(error.exception.code, code)
        self.enable()
        foreign = self.tool(bag=self.other_bag)
        with self.assertRaises(GatheringError) as error:
            self.prepare(dict(category="leafy", tool_instance_id=str(foreign.instance_id)))
        self.assertEqual(error.exception.code, "GATHERING_TOOL_REQUIRED")
        for uses in (0, None, -1, 21):
            broken = self.tool(uses)
            with self.subTest(uses=uses), self.assertRaises(GatheringError) as error:
                self.prepare(dict(category="leafy", tool_instance_id=str(broken.instance_id)))
            self.assertEqual(error.exception.code, "GATHERING_TOOL_UNUSABLE")
        old = self.tool(expires_world_minute=100)
        self.assertEqual(self.prepare(dict(category="leafy", tool_instance_id=str(old.instance_id)))[2].pk, old.pk)

    def test_wear_splits_only_one_physical_tool_and_rolls_back_with_business(self):
        self.enable()
        original = self.tool(20, amount=2)
        _, _, tool = self.prepare(dict(category="leafy", tool_instance_id=str(original.instance_id)))
        result = wear_tool(tool)
        original.refresh_from_db()
        used = self.bag.slots.get(instance_id=result["instance_id"])
        self.assertEqual((original.item_amount, original.remaining_uses), (1, 20))
        self.assertEqual((used.item_amount, used.remaining_uses, used.produced_world_minute), (1, 19, 90))
        self.assertEqual(sum(self.bag.slots.values_list("item_amount", flat=True)), 2)
        try:
            with transaction.atomic():
                _, _, tool = self.prepare(dict(category="leafy", tool_instance_id=str(used.instance_id)))
                wear_tool(tool)
                raise ValueError("business failure")
        except ValueError:
            pass
        used.refresh_from_db()
        self.assertEqual(used.remaining_uses, 19)

    def test_retired_upgrade_and_info_do_not_create_or_change_private_assets(self):
        client = APIClient()
        client.force_authenticate(user=self.player)
        for verb, path, data in [("post", "collect-upgrade", {"collectType": "采集食材"}),
                                 ("get", "collect-upgrade-info", {"collectType": "采集食材"})]:
            result = getattr(client, verb)(f"/api/inventory/{self.player.username}/{path}", data)
            self.assertEqual(result.status_code, 403)
            self.assertEqual(result.data["code"], "COLLECTION_UPGRADE_RETIRED")
        self.assertFalse(PlayerCollectionPoint.objects.filter(player=self.player).exists())
        self.bag.refresh_from_db()
        self.assertEqual(self.bag.money, 200)
        existing = PlayerCollectionPoint.objects.create(player=self.player, collection_point=self.point, current_level=5)
        client.post(f"/api/inventory/{self.player.username}/collect-upgrade", {"collectType": "采集食材"})
        existing.refresh_from_db()
        self.assertEqual(existing.current_level, 5)
