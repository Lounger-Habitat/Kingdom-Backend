"""Two deterministic production/sale/food cycles including tool replacement.

Commands are scripted test decisions, not an autonomous remote Agent run.
"""
from unittest.mock import patch
from items.models import Item
from recipes.models import Recipe, RecipeIngredient
from village.tests.test_phase3_direct_trade import DirectTradeTestBase, _payload
from village.tests import test_jack_production as production_tests
from village.models import NpcRuntimeState, VillageActionDurationConfig
from village.phase3_asset_service import commit_economy_action
from village.phase3_catalog import PREY_ITEM_IDS
from inventory.models import NpcGlobalInventorySlot


class JackCycleTests(DirectTradeTestBase):
    accept=production_tests.JackProductionTests.accept
    start=production_tests.JackProductionTests.start
    finish=production_tests.JackProductionTests.finish
    payload=production_tests.JackProductionTests.payload

    def setUp(self):
        super().setUp()
        self.jack_rt=NpcRuntimeState.objects.get(npc=self.jack)
        self.jack_rt.needs_world_minute=700;self.jack_rt.energy_fp=1000;self.jack_rt.hunger_fp=600;self.jack_rt.save()
        NpcRuntimeState.objects.all().update(needs_world_minute=700)
        for action in ('HUNT','PROCESS_GAME'):
            VillageActionDurationConfig.objects.update_or_create(action_name=action,defaults={'duration_minutes':5})
        for item,name,price,kind in [(4002,'猎具',50,'CollectTool'),(1053,'辣子鸡丁',25,'食物'),(1040,'红辣椒',3,'调料类')]:
            Item.objects.update_or_create(item_id=item,defaults={'item_name':name,'item_price':price,'item_type':kind})
        self.tool=NpcGlobalInventorySlot.objects.create(bag=self.jack_inv,slot_index=0,item_id=4002,item_amount=1,remaining_uses=2)
        self._give_item(self.nora_inv,4002,1)
        for tool in self.nora_inv.slots.filter(item_id=4002):
            tool.remaining_uses = 20
            tool.save(update_fields=["remaining_uses"])
        self.store_account.balance=200;self.store_account.save()
        listing=self._list(10015,'GENERAL_STORE',4002,1,'ORIGINAL')
        self.assertEqual(listing['result_code'],'OK',listing)
        self.tool_listing=listing['data']['listing_id']
        self._give_item(self.lucy_inv,1040,2)
        recipe,_=Recipe.objects.update_or_create(pk=2,defaults={'recipe_name':'辣子鸡丁','item_id':1053,'output_quantity':1})
        recipe.ingredients.all().delete()
        for item,name,n in [(1056,'鸡肉',2),(1040,'红辣椒',1)]:
            RecipeIngredient.objects.create(recipe=recipe,item_id=item,item_name=name,count=n)

    def trade(self,seller,buyer,item,amount,gold):
        proposed=commit_economy_action(_payload('PROPOSE_TRADE',seller,'MARKET_STREET',{
            'target_npc_id':str(buyer),'side':'SELL','item_id':item,'amount':amount,'gold_amount':gold,
            'expires_world_minute':self.time.world_minute+100}))
        self.assertEqual(proposed['result_code'],'OK',proposed)
        accepted=commit_economy_action(_payload('RESPOND_TRADE',buyer,'MARKET_STREET',{
            'offer_id':proposed['data']['offer_id'],'decision':'ACCEPT'}))
        self.assertEqual(accepted['result_code'],'OK',accepted)

    def test_two_cycles_meat_to_lucy_food_to_jack_and_new_tool(self):
        for cycle in range(2):
            with patch('village.phase3_asset_service.deterministic_rng') as rng:
                rng.return_value.choices.return_value=[29101]
                hunt=self.start()
            result=self.finish(hunt);self.assertEqual(result['result_code'],'OK',result)
            prey=self.jack_inv.slots.get(item_id__in=PREY_ITEM_IDS,item_amount__gt=0)
            result=self.finish(self.start('PROCESS_GAME',{'prey_instance_id':str(prey.instance_id)}))
            self.assertEqual(result['result_code'],'OK',result)
            self.trade(10012,10014,1056,3,36)
            result=commit_economy_action(_payload('PROPOSE_TRADE',10012,'GENERAL_STORE',{
                'target_npc_id':'10015','store_code':'GENERAL_STORE','side':'SELL','item_id':29104,'amount':1}))
            self.assertEqual(result['result_code'],'OK',result)
            cooked=commit_economy_action(_payload('COOK',10014,'RESTAURANT',{'recipe_id':2}))
            self.assertEqual(cooked['result_code'],'OK',cooked)
            self.trade(10014,10012,1053,1,20)
            food=self.jack_inv.slots.get(item_id=1053,item_amount__gt=0)
            eaten=commit_economy_action(_payload('EAT',10012,'MARKET_STREET',{
                'source':'PERSONAL_INVENTORY','food_instance_id':str(food.instance_id)}))
            self.assertEqual(eaten['result_code'],'OK',eaten)
            if cycle==0:
                self.tool.refresh_from_db();self.assertEqual(self.tool.remaining_uses,0)
                result,_=self.accept();blocked=__import__('village.runtime_services',fromlist=['CommandService']).CommandService.start_command(result[2].command_id)
                self.assertFalse(blocked[0])
                bought=self._buy(10012,self.tool_listing,1)
                self.assertEqual(bought['result_code'],'OK',bought)
        self.jack_inv.refresh_from_db();self.jack_rt.refresh_from_db();self.store_account.refresh_from_db()
        # 2*(meat36 + feather3 - meal20) - new tool50.
        self.assertEqual(self.jack_inv.money,240+2*(36+3-20)-50)
        self.assertEqual(self.jack_inv.slots.get(item_id=4002,usage__remaining__gt=0).remaining_uses,18)
        self.assertEqual(self.jack_inv.slots.filter(item_id=1053,item_amount__gt=0).count(),0)
        self.assertLess(self.jack_rt.hunger_fp,600)
