"""Normalized storage, compatibility writes and batch lifecycle regressions."""
import copy
import uuid
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.db import IntegrityError, transaction
from django.forms import modelform_factory
from django.test import RequestFactory, TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from account.models import GameAccount
from inventory.components import COMPONENT_FIELDS, copy_batch_attributes, same_batch_attributes
from inventory.forms import BatchComponentForm
from inventory.models import (InventoryBag, InventorySlot, InventorySlotDish, InventorySlotUsage,
                              InventorySlotReview, NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot)
from inventory.views import inventory_compact, inventory_swap, inventory_rate, inventory_transfer, clear_slot_if_empty
from items.models import Item
from marketplace.models import MarketListing
from marketplace.services import _listing_batch, _add_snapshot_item_to_bag
from village.models import VillageStore, VillageStoreInventorySlot


class ComponentStorageTests(TestCase):
    def setUp(self):
        self.player = GameAccount.objects.create(username="component-test", password="test")
        self.bag = InventoryBag.objects.create(player=self.player, character_id=self.player.username)
        self.item = Item.objects.create(item_id=989991, item_name="测试菜", item_type="Food")
        self.attributes = dict(rated=True, rating_price=73, overall_score=91.5, ingredient="玉米×2",
                               cook_time=60, dish_quality=6, review_comment="香甜", remaining_uses=7,
                               dish_metadata={"recipe_id": 5, "scores": {"taste": 90}})

    def make_slot(self, **kw):
        return InventorySlot.objects.create(bag=self.bag, slot_index=kw.pop("slot_index", 0),
            item_id=self.item.pk, item_amount=kw.pop("item_amount", 2), **(self.attributes | kw))

    def api(self, view, data):
        request = APIRequestFactory().post("/", data, format="json")
        force_authenticate(request, user=self.player)
        response = view(request, "player")
        self.assertEqual(response.status_code, 200, response.data)
        return response

    def test_columns_are_physically_separate_and_quality_is_common(self):
        for model in (InventorySlot, NpcGlobalInventorySlot, MarketListing, VillageStoreInventorySlot):
            fields = {field.name for field in model._meta.concrete_fields}
            self.assertTrue(COMPONENT_FIELDS.keys().isdisjoint(fields))
            self.assertIn("quality", fields)
            self.assertNotIn("dish_quality", fields)
        slot = self.make_slot()
        slot = InventorySlot.objects.get(pk=slot.pk)
        self.assertEqual(slot.usage.remaining, 7)
        self.assertEqual(slot.dish.ingredients, "玉米×2")
        self.assertEqual(slot.dish.metadata["recipe_id"], 5)
        self.assertEqual(slot.review.score, 91.5)
        self.assertEqual(slot.quality, 6)

    def test_ordinary_and_empty_slots_have_no_detail_rows(self):
        row = InventorySlot.objects.create(bag=self.bag, slot_index=0, item_id=1030, item_amount=38,
                                          rated=False, ingredient="", dish_metadata={}, remaining_uses=None)
        self.assertFalse(InventorySlotDish.objects.filter(owner=row).exists())
        self.assertFalse(InventorySlotUsage.objects.filter(owner=row).exists())
        self.assertFalse(InventorySlotReview.objects.filter(owner=row).exists())
        self.assertFalse(row.rated)
        self.assertIsNone(row.remaining_uses)

    def test_partial_save_does_not_commit_other_pending_component_changes(self):
        row = self.make_slot()
        row.remaining_uses = 3
        row.review_comment = "pending"
        row.item_amount = 10
        row.save(update_fields=["remaining_uses"])
        stored = InventorySlot.objects.get(pk=row.pk)
        self.assertEqual((stored.item_amount, stored.remaining_uses, stored.review_comment), (2, 3, "香甜"))
        row.save(update_fields=["review_comment", "dish_quality"])
        row.refresh_from_db()
        self.assertEqual(row.review_comment, "pending")

    def test_json_in_place_changes_are_persisted_and_copies_are_independent(self):
        row = self.make_slot()
        row.refresh_from_db()
        row.dish_metadata["scores"]["taste"] = 99
        row.save()
        row.refresh_from_db()
        self.assertEqual(row.dish_metadata["scores"]["taste"], 99)
        target = InventorySlot(bag=self.bag, slot_index=1, item_id=self.item.pk, item_amount=1)
        copy_batch_attributes(row, target)
        target.save()
        target.dish_metadata["scores"]["taste"] = 2
        target.remaining_uses = 1
        target.save()
        row.refresh_from_db()
        self.assertEqual(row.dish_metadata["scores"]["taste"], 99)
        self.assertEqual(row.remaining_uses, 7)

    def test_all_usage_sentinels_round_trip_without_coercion(self):
        for index, remaining in enumerate((None, -1, 0, 3, -2)):
            row = InventorySlot.objects.create(bag=self.bag, slot_index=index, item_id=4001,
                                              item_amount=1, remaining_uses=remaining)
            self.assertEqual(InventorySlot.objects.get(pk=row.pk).remaining_uses, remaining)

    def test_clear_and_reuse_cannot_inherit_dish_review_usage_or_freshness(self):
        row = self.make_slot(produced_world_minute=100, expires_world_minute=200,
                             source_request_id=uuid.uuid4(), freshness_unknown=True)
        row.item_amount = 0
        clear_slot_if_empty(row)
        row.refresh_from_db()
        self.assertEqual((row.item_id, row.item_amount, row.quality, row.dish_metadata), (0, 0, 3, {}))
        self.assertFalse(row.rated)
        self.assertIsNone(row.remaining_uses)
        self.assertIsNone(row.expires_world_minute)
        self.assertIsNone(row.source_request_id)
        self.assertFalse(row.freshness_unknown)
        for model in (InventorySlotDish, InventorySlotReview, InventorySlotUsage):
            self.assertFalse(model.objects.filter(owner=row).exists())

    def test_failed_component_save_rolls_back_parent_and_other_details(self):
        row = self.make_slot()
        row.item_amount = 100
        row.remaining_uses = 1
        row.review_comment = "changed"
        with patch.object(InventorySlotReview, "save", side_effect=IntegrityError("test failure")):
            with self.assertRaises(IntegrityError):
                row.save()
        row.refresh_from_db()
        self.assertEqual((row.item_amount, row.remaining_uses, row.review_comment), (2, 7, "香甜"))

    def test_parent_delete_cascades_and_one_to_one_prevents_shared_detail(self):
        row = self.make_slot()
        with self.assertRaises(IntegrityError), transaction.atomic():
            InventorySlotUsage.objects.create(owner=row, remaining=9)
        row.delete()
        self.assertEqual(InventorySlotUsage.objects.count(), 0)
        self.assertEqual(InventorySlotDish.objects.count(), 0)
        self.assertEqual(InventorySlotReview.objects.count(), 0)

    def test_bulk_create_cannot_silently_drop_components(self):
        with self.assertRaisesMessage(ValueError, "save()"):
            InventorySlot.objects.bulk_create([InventorySlot(bag=self.bag, slot_index=0, remaining_uses=4)])
        self.assertEqual(self.bag.slots.count(), 0)

    def test_reads_use_one_joined_query_for_all_component_rows(self):
        self.make_slot()
        self.make_slot(slot_index=1)
        with self.assertNumQueries(1):
            rows = list(self.bag.slots.all())
            self.assertEqual([row.remaining_uses for row in rows], [7, 7])
            self.assertEqual([row.rated for row in rows], [True, True])
            self.assertEqual([row.dish_metadata["recipe_id"] for row in rows], [5, 5])

    def test_refresh_specific_component_and_partial_common_save(self):
        row = self.make_slot()
        other = InventorySlot.objects.get(pk=row.pk)
        other.remaining_uses = 2
        other.save(update_fields=["remaining_uses"])
        row.refresh_from_db(fields=["remaining_uses"])
        self.assertEqual(row.remaining_uses, 2)
        row.item_amount = 3
        row.save(update_fields=["item_amount"])
        row.refresh_from_db()
        self.assertEqual(row.remaining_uses, 2)

    def test_npc_store_market_and_player_transfer_preserves_typed_state(self):
        npc = NpcDefinition.objects.create(agent_id="component-npc", name="test")
        bag = NpcGlobalInventory.objects.create(npc=npc)
        source = NpcGlobalInventorySlot.objects.create(bag=bag, slot_index=0, item_id=self.item.pk,
                                                      item_amount=2, **self.attributes)
        store = VillageStore.objects.create(store_code="COMPONENT_STORE", display_name="test", location_code="RESTAURANT")
        attrs = {name: getattr(source, name) for name in COMPONENT_FIELDS if name != "remaining_uses"}
        attrs.update(dish_quality=6, unknown_extension={"keep": True})
        stock = VillageStoreInventorySlot.objects.create(store=store, item_id=self.item.pk, item_amount=2,
            remaining_uses=source.remaining_uses, item_attributes=attrs)
        stock.refresh_from_db()
        self.assertTrue(COMPONENT_FIELDS.keys().isdisjoint(stock.common_attributes))
        self.assertNotIn("dish_quality", stock.common_attributes)
        self.assertEqual(stock.item_attributes["unknown_extension"], {"keep": True})
        self.assertEqual(stock.item_attributes["dish_metadata"], source.dish_metadata)
        self.assertEqual(stock.quality, 6)
        listing = MarketListing.objects.create(source_type="system", item_id=self.item.pk, price=73,
                                               **self.attributes, item_snapshot={"dishMetadata": {"old": True}})
        listing.remaining_uses = 4
        listing.save(update_fields=["remaining_uses"])
        restored = _add_snapshot_item_to_bag(self.bag, dict(_listing_batch(listing), itemID=self.item.pk), 1)
        self.assertEqual(restored.remaining_uses, 4)
        self.assertEqual(restored.dish_metadata, source.dish_metadata)
        self.assertEqual(restored.review_comment, "香甜")
        self.assertEqual(listing.item_snapshot, {"dishMetadata": {"old": True}})

    def test_store_legacy_attribute_save_does_not_drop_usage_or_unknown_keys(self):
        store = VillageStore.objects.create(store_code="COMPONENT_STORE", display_name="test", location_code="RESTAURANT")
        row = VillageStoreInventorySlot.objects.create(store=store, item_id=self.item.pk, item_amount=1,
            remaining_uses=-1, item_attributes={"rated": True, "dish_quality": 5, "opaque": 2})
        row.item_attributes = dict(row.item_attributes, rating_price=70)
        row.save(update_fields=["item_attributes"])
        row.refresh_from_db()
        self.assertEqual((row.remaining_uses, row.rating_price, row.quality), (-1, 70, 5))
        self.assertEqual(row.item_attributes["opaque"], 2)

    def test_actual_market_purchase_uses_details_not_stale_audit_snapshot(self):
        from game_time.models import GlobalGameTimeState
        from marketplace.services import buy_listing
        GlobalGameTimeState.get_instance()
        self.bag.money = 1000
        self.bag.save()
        listing = MarketListing.objects.create(source_type="system", item_id=self.item.pk, price=73,
            total_amount=1, remaining_amount=1, **self.attributes,
            item_snapshot={"itemID": self.item.pk, "rated": False, "remainingUses": 100,
                           "dishMetadata": {"old": True}, "reviewComment": "old"})
        result, error = buy_listing(self.player, listing.pk, 1)
        self.assertIsNone(error)
        received = self.bag.slots.get()
        self.assertEqual((received.remaining_uses, received.review_comment, received.quality), (7, "香甜", 6))
        self.assertEqual(received.dish_metadata, self.attributes["dish_metadata"])

    def test_system_creation_imports_optional_snapshot_inputs_once(self):
        listing = MarketListing.objects.create(source_type="system", item_id=4001, price=50,
            item_snapshot={"remainingUses": 20})
        listing.refresh_from_db()
        self.assertEqual(listing.remaining_uses, 20)
        listing.item_snapshot = {"remainingUses": 99}
        listing.save()
        listing.refresh_from_db()
        self.assertEqual(listing.remaining_uses, 20)

    def test_compact_preserves_primary_key_instance_and_all_components(self):
        row = self.make_slot(slot_index=8, produced_world_minute=100, expires_world_minute=200)
        pk, instance = row.pk, row.instance_id
        InventorySlot.objects.create(bag=self.bag, slot_index=2)
        self.api(inventory_compact, {})
        row.refresh_from_db()
        self.assertEqual((row.pk, row.instance_id, row.slot_index), (pk, instance, 0))
        self.assertEqual((row.remaining_uses, row.dish_metadata, row.expires_world_minute), (7, self.attributes["dish_metadata"], 200))

    def test_swap_moves_all_batch_attributes_together(self):
        row = self.make_slot(slot_index=0, produced_world_minute=100, expires_world_minute=200)
        instance = row.instance_id
        plain = InventorySlot.objects.create(bag=self.bag, slot_index=1, item_id=1030, item_amount=38)
        self.api(inventory_swap, {"fromIndex": 0, "toIndex": 1})
        destination = InventorySlot.objects.get(instance_id=instance)
        self.assertEqual((destination.slot_index, destination.expires_world_minute, destination.overall_score), (1, 200, 91.5))
        self.assertEqual(destination.dish_metadata, self.attributes["dish_metadata"])
        row.refresh_from_db()
        self.assertEqual((row.item_id, row.dish_metadata, row.remaining_uses), (1030, {}, None))

    def test_review_split_copies_metadata_but_does_not_mutate_source_review(self):
        row = self.make_slot(rated=False, review_comment="", produced_world_minute=100, expires_world_minute=200)
        self.api(inventory_rate, {"instanceId": str(row.instance_id), "ratingPrice": 90, "overallScore": 99, "reviewComment": "new"})
        row.refresh_from_db()
        rated = self.bag.slots.exclude(pk=row.pk).get()
        self.assertEqual((row.item_amount, row.rated, rated.item_amount, rated.review_comment), (1, False, 1, "new"))
        self.assertEqual(rated.dish_metadata, row.dish_metadata)
        self.assertEqual(rated.expires_world_minute, 200)

    def test_self_transfer_cannot_change_quantity_or_components(self):
        row = self.make_slot()
        request = APIRequestFactory().post("/", {"instanceId": str(row.instance_id), "amount": 1,
                                                  "targetCharacterId": "player"}, format="json")
        force_authenticate(request, user=self.player)
        self.assertEqual(inventory_transfer(request, "player").status_code, 400)
        row.refresh_from_db()
        self.assertEqual((row.item_amount, row.remaining_uses), (2, 7))

    def test_admin_form_writes_components_and_can_build_bag_inline(self):
        row = self.make_slot()
        form_class = modelform_factory(InventorySlot, form=BatchComponentForm, fields=["item_amount", "remaining_uses", "review_comment"])
        form = form_class(data={"item_amount": 3, "remaining_uses": 2, "review_comment": "admin"}, instance=row)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        row.refresh_from_db()
        self.assertEqual((row.item_amount, row.remaining_uses, row.review_comment), (3, 2, "admin"))
        from inventory.admin import InventorySlotInline
        request = RequestFactory().get("/")
        request.user = type("Admin", (), {"has_perm": lambda *args: True})()
        InventorySlotInline(InventoryBag, AdminSite()).get_formset(request)
