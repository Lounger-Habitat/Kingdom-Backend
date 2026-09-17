import uuid
from django.db.models import Max
from village.tests.test_phase3_direct_trade import DirectTradeTestBase, _payload
from village.models import (VillageStoryArc, VillageStoryFact, VillageSimulationConfig,
    VillageStoryMeeting, VillageStoreInventorySlot, NpcCommandState, NpcRuntimeState)
from village.phase5_story import complete_story_action, _candidate_plans, submit_story_vote
from village.phase3_asset_service import commit_economy_action
from village import caravan
from village.phase5_contract import ALL_STORY_ACTIONS
from inventory.models import NpcGlobalInventorySlot


class JackSupplyTests(DirectTradeTestBase):
    def setUp(self):
        super().setUp()
        VillageSimulationConfig.objects.filter(config_key='default').update(village_story_enabled=True)
        self.arc=VillageStoryArc.objects.create(arc_code='BROKEN_TRADE_ROUTE_V1',current_phase='AFTERMATH')
        self.fact=VillageStoryFact.objects.create(arc=self.arc)

    def contact(self, actor='10015',request=None,attempt=None):
        return complete_story_action({'request_id':str(request or uuid.uuid4()),'npc_id':actor,
            'current_location_code':'GENERAL_STORE','params':{'action_code':'CONTACT_EMERGENCY_CARAVAN',
                'route_attempt_id':str(attempt or uuid.uuid4())}})

    def test_emergency_fee_only_before_meeting_and_after_story_completion(self):
        self.arc.is_completed=True;self.arc.save()
        self.fact.forest_route_scouted=0;self.fact.forest_route_supply=0;self.fact.save()
        request,attempt=uuid.uuid4(),uuid.uuid4()
        result=self.contact(request=request,attempt=attempt)
        self.assertEqual(result['result_code'],'OK',result)
        self.nora_inv.refresh_from_db();self.assertEqual(self.nora_inv.money,350)
        self.assertEqual(self.contact(request=request,attempt=attempt)['result_code'],'OK')
        self.assertEqual(self.contact()['result_code'],'OK')
        self.nora_inv.refresh_from_db();self.fact.refresh_from_db()
        self.assertEqual(self.nora_inv.money,350)
        self.assertEqual((self.fact.forest_route_scouted,self.fact.forest_route_supply),(0,0))
        self.assertTrue(self.fact.emergency_caravan_succeeded)
        self.assertEqual(caravan.route_readiness(10012)['route'],'EMERGENCY')

    def test_wrong_actor_and_insufficient_personal_funds(self):
        self.assertNotEqual(self.contact('10012')['result_code'],'OK')
        self.nora_inv.money=99;self.nora_inv.save()
        self.store_account.balance=10000;self.store_account.save()
        self.assertNotEqual(self.contact()['result_code'],'OK')
        self.fact.refresh_from_db();self.assertFalse(self.fact.emergency_caravan_succeeded)
        self.nora_inv.refresh_from_db();self.assertEqual(self.nora_inv.money,99)

    def test_old_forest_commands_and_proposals_are_retired(self):
        self.assertNotIn('FOREST_ROUTE',_candidate_plans(self.fact))
        for action in ('SCOUT_FOREST_ROUTE','SUPPLY_FOREST_ROUTE'):
            self.assertNotIn(action,ALL_STORY_ACTIONS)
            result=complete_story_action({'request_id':str(uuid.uuid4()),'npc_id':'10012',
                'current_location_code':'FOREST_EDGE','params':{'action_code':action}})
            self.assertEqual(result['result_code'],'STORY_ACTION_RETIRED')

    def test_route_freezes_at_arrival_and_repaired_bridge_switches_next_visit(self):
        frozen=caravan.sync_visit(700)
        self.assertEqual(frozen.route,'NONE')
        self.assertEqual(self.contact()['result_code'],'OK')
        self.assertEqual(caravan.sync_visit(710).route,'NONE')
        next_visit=caravan.sync_visit(1440+660)
        self.assertEqual(next_visit.route,'EMERGENCY')
        self.fact.refresh_from_db();self.fact.bridge_condition='REPAIRED';self.fact.save()
        self.assertEqual(caravan.sync_visit(1440+700).route,'EMERGENCY')
        self.assertEqual(caravan.sync_visit(2880+660).route,'NORMAL')

    def test_byproduct_store_purchase_withdraw_export_deposit(self):
        self.store_account.balance=200;self.store_account.save()
        self._give_item(self.jack_inv,29104,2)
        before_jack=self.jack_inv.money
        sale=_payload('PROPOSE_TRADE',10012,'MARKET_STREET',{'side':'SELL','target_npc_id':'10015',
            'store_code':'GENERAL_STORE','item_id':29104,'amount':2,'gold_amount':999,'expires_world_minute':900})
        result=commit_economy_action(sale)
        self.assertEqual(result['result_code'],'OK',result)
        self.assertTrue(commit_economy_action(sale)['replayed'])
        self.jack_inv.refresh_from_db();self.store_account.refresh_from_db()
        self.assertEqual((self.jack_inv.money,self.store_account.balance),(before_jack+6,194))
        self.assertEqual(sum(VillageStoreInventorySlot.objects.filter(store=self.store,item_id=29104).values_list('item_amount',flat=True)),2)
        self.fact.emergency_caravan_succeeded=True;self.fact.save()
        visit=caravan.sync_visit(700);stock=dict(visit.stock)
        result=commit_economy_action(_payload('WITHDRAW_STORE_ITEM',10015,'GENERAL_STORE',{'store_code':'GENERAL_STORE','item_id':29104,'amount':2}))
        self.assertEqual(result['result_code'],'OK',result)
        result=commit_economy_action(_payload('EXPORT_GOODS',10015,'EMERGENCY_TRADE_POST',{'item_id':29104,'amount':2}))
        self.assertEqual(result['result_code'],'OK',result)
        visit.refresh_from_db();self.assertEqual(visit.stock,stock)
        result=commit_economy_action(_payload('DEPOSIT_STORE_FUNDS',10015,'GENERAL_STORE',{'store_code':'GENERAL_STORE','amount':6}))
        self.assertEqual(result['result_code'],'OK',result)
        self.store_account.refresh_from_db();self.assertEqual(self.store_account.balance,200)

    def test_work_reservation_blocks_immediate_store_sale(self):
        self._give_item(self.jack_inv,29102,1,produced=600,expires=900)
        prey=self.jack_inv.slots.get(item_id=29102)
        command=NpcCommandState.objects.create(npc=self.jack,kind='PROCESS_GAME',status='RUNNING',
            command_id=uuid.uuid4(),request_id=uuid.uuid4(),message_id=uuid.uuid4(),
            production_state={'rule_version':1,'prey_instance_id':str(prey.instance_id)})
        self.store_account.balance=200;self.store_account.save()
        result=commit_economy_action(_payload('PROPOSE_TRADE',10012,'MARKET_STREET',{'side':'SELL','target_npc_id':'10015',
            'store_code':'GENERAL_STORE','item_id':29102,'amount':1,'gold_amount':30,'expires_world_minute':900}))
        self.assertNotEqual(result['result_code'],'OK')
        prey.refresh_from_db();self.assertEqual(prey.item_amount,1)
