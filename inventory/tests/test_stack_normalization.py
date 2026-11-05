import uuid
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase

from account.models import GameAccount
from inventory.models import InventoryBag, InventorySlot, NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot
from inventory.normalization import normalize_inventory, normalize_owner
from items.models import Item
from player.mastery import _sample_proof
from village.models import NpcCommandState, VillagePlayerActionReceipt, VillagePlayerProcessingJob


class MaterialMasteryProofTests(SimpleTestCase):
    def test_behavior_credit_does_not_require_stock_origin_or_cap_credit_at_current_stack_amount(self):
        slot = SimpleNamespace(item_id=29308, quality=4, instance_id=uuid.uuid4(), item_amount=30,
            produced_world_minute=None, expires_world_minute=None, source_request_id=None)
        requirement = dict(code="mushroom", title="采集并交样", item_ids=[29308], minimum_quality=4, minimum_tool_tier=3)
        receipt = SimpleNamespace(source_id="actual-harvest")
        facts = dict(produced_world_minute=100, tool_tier=3)
        proof = _sample_proof(slot, requirement, [(receipt, facts, dict(item_id=29308, quality=4, quantity=2))], 100, "gathering")
        self.assertEqual(proof["production_quantity"], 2)
        self.assertEqual(proof["instance_id"], str(slot.instance_id))
        self.assertIsNone(_sample_proof(slot, requirement, [], 100, "gathering"))


class StackNormalizationTests(TestCase):
    def test_active_legacy_processing_input_remains_identical_to_frozen_snapshot(self):
        from village.player_processing import _snapshot
        item = Item.objects.create(item_id=980004, item_name="旧野猪")
        player = GameAccount.objects.create(username="processing-migration")
        escrow = InventoryBag.objects.create(player=player, character_id="processing-escrow", money=10)
        held = InventorySlot.objects.create(bag=escrow, slot_index=0, item_id=item.pk, item_amount=1,
            produced_world_minute=20, source_request_id=uuid.uuid4(), quality=4)
        snapshot = _snapshot(held)
        VillagePlayerProcessingJob.objects.create(player=player, request_id=uuid.uuid4(), escrow_bag=escrow,
            input_slot=held, source_instance_id=held.instance_id, input_snapshot=snapshot, fee_gold=10,
            duration_minutes=30, started_world_minute=20, ready_world_minute=50, needs_until=20)
        normalize_owner(escrow)
        held.refresh_from_db()
        self.assertEqual(_snapshot(held), snapshot)

    def test_split_purchased_dishes_keep_verifiable_original_cost(self):
        from restaurant.player_stall import _purchase_cost
        item = Item.objects.create(item_id=980005, item_name="购入菜品", item_type="食物")
        player = GameAccount.objects.create(username="purchase-migration")
        bag = InventoryBag.objects.create(player=player, character_id=player.username)
        dish = InventorySlot.objects.create(bag=bag, slot_index=0, item_id=item.pk, item_amount=2, quality=4)
        VillagePlayerActionReceipt.objects.create(player=player, request_id=uuid.uuid4(), action="buy_store",
            payload_hash="test", world_minute=100, response={"success": True, "data": {"result": {"unit_price": 17,
                "purchased_batches": [{"instance_id": str(dish.instance_id), "item_id": item.pk, "quantity": 2}]}}})
        normalize_owner(bag)
        for portion in bag.slots.filter(item_id=item.pk, item_amount__gt=0):
            self.assertEqual(_purchase_cost(player, portion)[0], 17)
        extra = bag.slots.filter(item_id=item.pk).exclude(pk=dish.pk).get()
        extra.dish_metadata["legacy_purchase_identity"]["instance_id"] = str(uuid.uuid4())
        self.assertIsNone(_purchase_cost(player, extra)[0])

    def test_dry_run_then_apply_conserves_quality_and_splits_dishes(self):
        material = Item.objects.create(item_id=980001, item_name="材料", max_stack_size=5)
        dish = Item.objects.create(item_id=980002, item_name="菜品", item_type="食物")
        bag = InventoryBag.objects.create(character_id="normalization")
        first = InventorySlot.objects.create(bag=bag, slot_index=0, item_id=material.pk, item_amount=3, quality=4,
            source_request_id=uuid.uuid4(), produced_world_minute=1)
        second = InventorySlot.objects.create(bag=bag, slot_index=1, item_id=material.pk, item_amount=4, quality=4,
            source_request_id=uuid.uuid4(), produced_world_minute=2)
        InventorySlot.objects.create(bag=bag, slot_index=2, item_id=material.pk, item_amount=2, quality=3)
        original = InventorySlot.objects.create(bag=bag, slot_index=3, item_id=dish.pk, item_amount=2, quality=5,
            source_request_id=uuid.uuid4(), dish_metadata={"recipe_id": 123})
        normalize_inventory()
        first.refresh_from_db()
        self.assertEqual(first.item_amount, 3)
        self.assertIsNotNone(first.source_request_id)
        normalize_inventory(apply=True)
        self.assertEqual(list(bag.slots.filter(item_id=material.pk, quality=4, item_amount__gt=0)
                              .order_by("pk").values_list("item_amount", flat=True)), [5, 2])
        first.refresh_from_db(); second.refresh_from_db()
        self.assertIsNone(first.source_request_id)
        dishes = list(bag.slots.filter(item_id=dish.pk))
        self.assertEqual([slot.item_amount for slot in dishes], [1, 1])
        self.assertTrue(all(slot.source_request_id == original.source_request_id for slot in dishes))
        normalize_inventory(apply=True)
        self.assertEqual(bag.slots.filter(item_amount__gt=0).count(), 5)

    def test_active_processing_reservation_rebinds_to_surviving_material_stack(self):
        item = Item.objects.create(item_id=980003, item_name="猎物")
        npc = NpcDefinition.objects.create(agent_id="normalization-npc", name="Jack")
        bag = NpcGlobalInventory.objects.create(npc=npc)
        first = NpcGlobalInventorySlot.objects.create(bag=bag, slot_index=0, item_id=item.pk, item_amount=2)
        second = NpcGlobalInventorySlot.objects.create(bag=bag, slot_index=1, item_id=item.pk, item_amount=1)
        command = NpcCommandState.objects.create(npc=npc, kind="PROCESS_GAME", status="RUNNING", sender_id="test",
            recipient_id="test", message_id=uuid.uuid4(), request_id=uuid.uuid4(), payload_sha256="test",
            production_state={"prey_instance_id": str(second.instance_id), "rule_version": 1})
        normalize_inventory(apply=True)
        first.refresh_from_db(); second.refresh_from_db(); command.refresh_from_db()
        self.assertEqual((first.item_amount, second.item_amount), (3, 0))
        self.assertEqual(command.production_state["prey_instance_id"], str(first.instance_id))
