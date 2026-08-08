from django.test import TestCase

from account.models import GameAccount
from inventory.models import (
    BagTemplate,
    BagTemplateSlot,
    InventoryBag,
    InventorySlot,
    NpcDefinition,
    NpcGlobalInventory,
    NpcGlobalInventorySlot,
)
from inventory.views import _character_trade_copy_slot
from items.models import Item


class CharacterTradeNpcVisibilityTests(TestCase):
    def setUp(self):
        self.player = GameAccount.objects.create(username="trade_visibility_player", password="test")
        self.player_bag = InventoryBag.objects.create(
            player=self.player,
            character_id=self.player.username,
        )
        self.npc = NpcDefinition.objects.create(agent_id="visibility-npc", name="Visibility NPC")
        self.npc_bag = NpcGlobalInventory.objects.create(npc=self.npc)
        self.template = BagTemplate.objects.create(npc=self.npc, character_id=self.npc.agent_id)
        self.template_item = Item.objects.create(item_id=910001, item_name="Template Item")
        self.non_template_item = Item.objects.create(item_id=910002, item_name="Non-template Item")

    def create_source_slot(self, item, slot_index=0):
        return InventorySlot.objects.create(
            bag=self.player_bag,
            slot_index=slot_index,
            item_id=item.item_id,
            item_amount=10,
        )

    def npc_owner(self):
        return {"type": "npc", "bag": self.npc_bag}

    def test_template_shop_item_remains_visible_when_player_sells_it(self):
        BagTemplateSlot.objects.create(
            template=self.template,
            slot_index=0,
            item=self.template_item,
            hide_in_shop=False,
        )

        _character_trade_copy_slot(
            self.create_source_slot(self.template_item),
            self.npc_owner(),
            amount=2,
        )

        target = self.npc_bag.slots.get(item_id=self.template_item.item_id)
        self.assertEqual(target.item_amount, 2)
        self.assertFalse(target.hide_in_shop)

    def test_non_template_item_is_hidden_when_player_sells_it(self):
        _character_trade_copy_slot(
            self.create_source_slot(self.non_template_item),
            self.npc_owner(),
            amount=2,
        )

        target = self.npc_bag.slots.get(item_id=self.non_template_item.item_id)
        self.assertEqual(target.item_amount, 2)
        self.assertTrue(target.hide_in_shop)

    def test_existing_non_template_slot_is_rehidden_and_incremented(self):
        NpcGlobalInventorySlot.objects.create(
            bag=self.npc_bag,
            slot_index=0,
            item_id=self.non_template_item.item_id,
            item_amount=3,
            hide_in_shop=False,
        )

        _character_trade_copy_slot(
            self.create_source_slot(self.non_template_item),
            self.npc_owner(),
            amount=2,
        )

        target = self.npc_bag.slots.get(item_id=self.non_template_item.item_id)
        self.assertEqual(target.item_amount, 5)
        self.assertTrue(target.hide_in_shop)

    def test_hidden_template_item_stays_hidden_when_player_sells_it(self):
        BagTemplateSlot.objects.create(
            template=self.template,
            slot_index=0,
            item=self.template_item,
            hide_in_shop=True,
        )

        _character_trade_copy_slot(
            self.create_source_slot(self.template_item),
            self.npc_owner(),
            amount=2,
        )

        target = self.npc_bag.slots.get(item_id=self.template_item.item_id)
        self.assertTrue(target.hide_in_shop)
