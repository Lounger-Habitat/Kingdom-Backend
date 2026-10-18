import uuid
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from account.models import GameAccount
from cooking.views import cook, pity_check
from inventory.models import InventoryBag, InventorySlot
from inventory.views import (inventory_judge, inventory_judge_by_agent, inventory_add, inventory_money,
    inventory_rate, inventory_bulk_push, inventory_collect, inventory_remove)
from restaurant.views import menu_exhibit, menu_withdraw, upgrade_restaurant
from items.models import Item
from restaurant.services import RestaurantService
from village.models import VillageSimulationConfig


class PlayerProductionBoundaryTests(TestCase):
    def setUp(self):
        self.player = GameAccount.objects.create(username="production-boundary")
        self.bag = InventoryBag.objects.create(player=self.player, character_id=self.player.username)
        Item.objects.get_or_create(item_id=29201, defaults=dict(item_name="meal", item_type="食物"))
        self.slot = InventorySlot.objects.create(bag=self.bag, slot_index=0, item_id=29201,
            item_amount=2, expires_world_minute=3000, dish_metadata={"maker_player_id": str(self.player.pk)})
        VillageSimulationConfig.objects.update_or_create(config_key="default", defaults=dict(village_economy_enabled=True))

    def test_legacy_cooking_and_both_review_endpoints_reject_before_asset_writes(self):
        for view, args in ((cook, ()), (pity_check, ()), (inventory_judge, (self.player.username,)),
                           (inventory_judge_by_agent, (self.player.username,))):
            request = APIRequestFactory().post("/unused", {"instanceId": str(self.slot.instance_id)}, format="json")
            force_authenticate(request, user=self.player)
            response = view(request, *args)
            self.assertEqual((response.status_code, response.data["result_code"]), (409, "VILLAGE_ACTION_REQUIRED"))
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.item_amount, 2)

    def test_fresh_dish_cannot_enter_old_menu_that_drops_its_batch_contract(self):
        with patch.object(RestaurantService, "get_or_create_state") as state:
            ok, reason = RestaurantService.exhibit_dish(self.player, dict(instance_id=str(self.slot.instance_id),
                item_id=29201, quantity=1))
        self.assertFalse(ok)
        self.assertIn("批次", reason)
        state.assert_not_called()
        self.slot.refresh_from_db()
        self.assertEqual((self.slot.item_amount, self.slot.expires_world_minute), (2, 3000))

    def test_old_asset_adjustments_and_menu_writes_cannot_bypass_the_village_economy(self):
        for view,args in ((inventory_add,(self.player.username,)),(inventory_money,(self.player.username,)),
                          (inventory_rate,(self.player.username,)),(inventory_bulk_push,()),
                          (menu_exhibit,()),(menu_withdraw,()),(upgrade_restaurant,())):
            request=APIRequestFactory().post("/unused", {"amount":100000,"instanceId":str(self.slot.instance_id)},format="json")
            force_authenticate(request,user=self.player)
            reply=view(request,*args)
            self.assertEqual((reply.status_code,reply.data["result_code"]),(409,"VILLAGE_ACTION_REQUIRED"))
        self.bag.refresh_from_db();self.slot.refresh_from_db()
        self.assertEqual((self.bag.money,self.slot.item_amount),(0,2))

    def test_legacy_collection_cannot_skip_tool_or_shared_stock_rules(self):
        for collect_type in ("\u91c7\u96c6\u98df\u6750","\u91c7\u96c6\u6797\u4e0b\u98df\u6750"):
            request=APIRequestFactory().post("/unused",{"collectType":collect_type},format="json")
            force_authenticate(request,user=self.player)
            reply=inventory_collect(request,self.player.username)
            self.assertEqual((reply.status_code,reply.data["result_code"]),(409,"VILLAGE_ACTION_REQUIRED"))
        self.slot.refresh_from_db();self.assertEqual(self.slot.item_amount,2)

    def test_removing_a_negative_quantity_cannot_mint_an_existing_proven_batch(self):
        request=APIRequestFactory().post("/unused",{"instanceId":str(self.slot.instance_id),"amount":-5},format="json")
        force_authenticate(request,user=self.player)
        self.assertEqual(inventory_remove(request,self.player.username).status_code,400)
        self.slot.refresh_from_db();self.assertEqual(self.slot.item_amount,2)
