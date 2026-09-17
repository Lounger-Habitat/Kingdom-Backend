"""Fixed-price tool buyback, using only Django's isolated test database."""
import uuid

from items.models import Item, ItemType
from village.agent_queries import _inventory
from village.models import NpcCommandState, EconomicLedgerEntry, VillageStoreInventorySlot
from village.phase3_asset_service import commit_economy_action
from village.runtime_services import _outcome_message
from village.tests.test_phase3_direct_trade import DirectTradeTestBase, _payload


class StoreToolBuybackTests(DirectTradeTestBase):
    def setUp(self):
        super().setUp()
        Item.objects.update_or_create(item_id=4002, defaults={
            'item_name':'猎具', 'item_type':ItemType.COLLECT_TOOL,
            'item_price':50, 'max_use_count':20, 'is_deleted':False,
        })
        self.store_account.balance=500
        self.store_account.save()
        self._set_location(10012,'GENERAL_STORE')
        self._set_location(10015,'GENERAL_STORE')

    def tool(self, uses, amount=1, **kwargs):
        index=self.jack_inv.slots.count()
        return self.jack_inv.slots.create(slot_index=index, item_id=4002,
            item_amount=amount, remaining_uses=uses, **kwargs)

    def sale_payload(self, amount=1, items=None):
        return _payload('PROPOSE_TRADE',10012,'GENERAL_STORE',{
            'side':'SELL','store_code':'GENERAL_STORE',
            'items':items if items is not None else [{'item_id':4002,'amount':amount}],
        })

    def balances(self):
        self.jack_inv.refresh_from_db();self.store_account.refresh_from_db()
        return (self.jack_inv.money,self.store_account.balance,
            self.jack_inv.asset_version,self.store_account.account_version)

    def test_broken_used_unknown_and_invalid_tools_rejected_with_matching_queries(self):
        for uses,reason in [(0,'TOOL_BROKEN'),(1,'TOOL_NOT_FULL_DURABILITY'),
                (18,'TOOL_NOT_FULL_DURABILITY'),(None,'TOOL_DURABILITY_UNKNOWN'),
                (-1,'TOOL_DURABILITY_INVALID'),(21,'TOOL_DURABILITY_INVALID')]:
            with self.subTest(uses=uses):
                self.jack_inv.slots.all().delete()
                tool=self.tool(uses)
                before=self.balances()
                row=_inventory(self.jack)['items'][0]
                self.assertEqual(row['sale'],{'allowed':False,'reason':reason})
                self.assertEqual(row['durability'].get('remaining_uses'),uses)
                self.assertEqual(row['durability']['max_uses'],20)
                payload=self.sale_payload()
                result=commit_economy_action(payload)
                self.assertEqual(result['result_code'],'TRADE_ITEM_NOT_FOR_SALE',result)
                self.assertEqual(result['data']['failure_kind'],reason)
                self.assertIn('满耐久',_outcome_message('PROPOSE_TRADE',result['result_code'],False,700,result['data']))
                tool.refresh_from_db();self.assertEqual(tool.item_amount,1)
                self.assertEqual(self.balances(),before)
                self.assertFalse(VillageStoreInventorySlot.objects.filter(source_slot_instance_id=tool.instance_id).exists())
                self.assertFalse(EconomicLedgerEntry.objects.filter(request_id=payload['request_id']).exists())
                replay=commit_economy_action(payload)
                self.assertEqual(replay['result_code'],result['result_code'])
                self.assertTrue(replay['replayed'])
                self.assertEqual(self.balances(),before)

    def test_mixed_bag_sells_only_full_tools_and_replay_does_not_pay_twice(self):
        bad=self.tool(0,produced_world_minute=100)
        used=self.tool(18,produced_world_minute=101)
        fresh=self.tool(20,amount=2,produced_world_minute=200)
        row=next(x for x in _inventory(self.jack)['items']
            if x.get('durability',{}).get('remaining_uses')==20)
        self.assertEqual(row['sale'],{'target':'GENERAL_STORE','unit_price':50})
        payload=self.sale_payload(items=[{'item_id':4002,'amount':1},{'item_id':4002,'amount':1}])
        result=commit_economy_action(payload)
        self.assertEqual(result['result_code'],'OK',result)
        self.assertEqual(result['data']['gold_amount'],100)
        self.assertEqual(self.balances()[:2],(340,400))
        for slot,count in [(bad,1),(used,1),(fresh,0)]:
            slot.refresh_from_db();self.assertEqual(slot.item_amount,count)
        stock=VillageStoreInventorySlot.objects.get(source_slot_instance_id=fresh.instance_id)
        self.assertEqual((stock.item_amount,stock.remaining_uses),(2,20))
        before=self.balances()
        self.assertTrue(commit_economy_action(payload)['replayed'])
        self.assertEqual(self.balances(),before)
        self.assertEqual(VillageStoreInventorySlot.objects.filter(source_slot_instance_id=fresh.instance_id).count(),1)

    def test_full_tool_shortfall_does_not_fill_with_broken_tool(self):
        full=self.tool(20);bad=self.tool(0)
        result=commit_economy_action(self.sale_payload(2))
        self.assertEqual(result['result_code'],'TRADE_ITEM_NOT_FOR_SALE')
        full.refresh_from_db();bad.refresh_from_db()
        self.assertEqual((full.item_amount,bad.item_amount),(1,1))
        self.assertEqual(self.balances()[:2],(240,500))

    def test_mixed_goods_failure_rolls_back_already_processed_food(self):
        self._give_item(self.jack_inv,1030,2)
        self.tool(0)
        result=commit_economy_action(self.sale_payload(items=[{'item_id':1030,'amount':2},{'item_id':4002,'amount':1}]))
        self.assertEqual(result['result_code'],'TRADE_ITEM_NOT_FOR_SALE',result)
        self.assertEqual(self.jack_inv.slots.get(item_id=1030).item_amount,2)
        self.assertFalse(VillageStoreInventorySlot.objects.filter(store=self.store).exists())
        self.assertEqual(self.balances()[:2],(240,500))

    def test_store_funds_shortfall_does_not_consume_full_tool(self):
        full=self.tool(20)
        self.store_account.balance=49;self.store_account.save()
        result=commit_economy_action(self.sale_payload())
        self.assertEqual(result['result_code'],'TRADE_INSUFFICIENT_MONEY')
        full.refresh_from_db();self.assertEqual(full.item_amount,1)
        self.assertEqual(self.balances()[:2],(240,49))

    def test_expired_and_work_reserved_full_tools_are_not_sold(self):
        from village.jack_production import RULE_VERSION
        expired=self.tool(20,expires_world_minute=700)
        reserved=self.tool(20)
        NpcCommandState.objects.create(npc=self.jack,kind='HUNT',status='RUNNING',
            command_id=uuid.uuid4(),request_id=uuid.uuid4(),message_id=uuid.uuid4(),
            production_state={'rule_version':RULE_VERSION,'tool_instance_id':str(reserved.instance_id)})
        result=commit_economy_action(self.sale_payload())
        self.assertEqual(result['result_code'],'TRADE_INSUFFICIENT_ITEM',result)
        expired.refresh_from_db();reserved.refresh_from_db()
        self.assertEqual((expired.item_amount,reserved.item_amount),(1,1))
        self.assertEqual(self.balances()[:2],(240,500))

    def test_negotiated_personal_used_tool_trade_preserves_uses_and_agreed_price(self):
        used=self.tool(18)
        proposed=self._propose(10012,10015,4002,1,30,location='GENERAL_STORE',side='SELL')
        self.assertEqual(proposed['result_code'],'OK',proposed)
        accepted=self._respond(10015,proposed['data']['offer_id'],'ACCEPT',location='GENERAL_STORE')
        self.assertEqual(accepted['result_code'],'OK',accepted)
        self.assertEqual(self.balances()[:2],(270,500))
        bought=self.nora_inv.slots.get(item_id=4002,item_amount=1)
        self.assertEqual(bought.remaining_uses,18)
        used.refresh_from_db();self.assertEqual(used.item_amount,0)

    def test_other_tool_types_use_item_configuration_and_non_tools_keep_price(self):
        Item.objects.update_or_create(item_id=4001,defaults={'item_type':ItemType.HOE_TOOL,'item_price':50,'max_use_count':10})
        self._give_item(self.jack_inv,4001,1)
        farm=self.jack_inv.slots.get(item_id=4001);farm.remaining_uses=9;farm.save()
        result=commit_economy_action(self.sale_payload(items=[{'item_id':4001,'amount':1}]))
        self.assertEqual(result['result_code'],'TRADE_ITEM_NOT_FOR_SALE')
        farm.remaining_uses=10;farm.save()
        self._give_item(self.jack_inv,1030,1)
        result=commit_economy_action(self.sale_payload(items=[{'item_id':4001,'amount':1},{'item_id':1030,'amount':1}]))
        self.assertEqual(result['result_code'],'OK',result)
        self.assertEqual(result['data']['gold_amount'],58)

    def test_configured_unlimited_tool_requires_explicit_unlimited_batch(self):
        Item.objects.filter(item_id=4002).update(max_use_count=-1)
        infinite=self.tool(-1)
        result=commit_economy_action(self.sale_payload())
        self.assertEqual(result['result_code'],'OK',result)
        stock=VillageStoreInventorySlot.objects.get(source_slot_instance_id=infinite.instance_id)
        self.assertEqual(stock.remaining_uses,-1)
