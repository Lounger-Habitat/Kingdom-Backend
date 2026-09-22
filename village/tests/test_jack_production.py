"""Formal lifecycle tests, not live-world simulations."""
import copy
import time
import uuid
from unittest.mock import patch

from django.db import transaction
from django.db.models import Max
from inventory.models import NpcGlobalInventory, NpcGlobalInventorySlot
from items.models import Item
from village.tests.test_phase3_production import ProductionTestBase, _payload
from village.models import (NpcCommandState, NpcRuntimeState, NpcProfile, VillageActionDurationConfig,
    VillageActionReceipt, VillageStoryArc, VillageStoryFact, VillageStoreInventorySlot)
from village.runtime_services import CommandService, ActionService
from village.phase3_asset_service import commit_economy_action, get_available_slot_amount
from village.phase3_catalog import PREY_RECIPES, PREY_ITEM_IDS, BYPRODUCT_ITEM_IDS
from village.jack_production import production_info, settle_work_needs


def bind_wildlife(case, cmd):
    from village.wildlife import execute
    prey_id, attempt_id = str(uuid.uuid4()), str(uuid.uuid4())
    registered=execute(dict(action='register',request_id=str(uuid.uuid4()),prey_id=prey_id,
        species='CHICKEN',region_id='HUNTING_GROUNDS',position=dict(x=0,y=0,z=0)))
    case.assertTrue(registered['success'], registered)
    begun=execute(dict(action='begin',request_id=str(uuid.uuid4()),prey_id=prey_id,
        attempt_id=attempt_id,actor_type='NPC',actor_id='10012',command_id=str(cmd.pk)))
    case.assertTrue(begun['success'], begun)


class JackProductionTests(ProductionTestBase):
    def setUp(self):
        super().setUp()
        self.jack_rt.needs_world_minute = 700
        self.jack_rt.energy_fp = 1000
        self.jack_rt.hunger_fp = 200
        self.jack_rt.save()
        self.jack_hunt_tool.item_amount = 1
        self.jack_hunt_tool.remaining_uses = 20
        self.jack_hunt_tool.save()
        for action in ('HUNT','PROCESS_GAME'):
            VillageActionDurationConfig.objects.update_or_create(action_name=action,defaults={'duration_minutes':5})

    def accept(self, action='HUNT', params=None, content=None):
        self.jack_rt.refresh_from_db()
        content = content or {
            'schema_version':'village.command.v1', 'id':str(uuid.uuid4()), 'request_id':str(uuid.uuid4()),
            'name':action, 'params':params or {'hunt_code':'HUNT_ANIMALS'},
            'queue_mode':'STRICT','expected_state_version':self.jack_rt.state_version,'ttl_ms':86400000,
        }
        outcome = CommandService.accept_command(str(uuid.uuid4()),'10012','env',copy.deepcopy(content),int(time.time()*1000))
        return outcome, content

    def start(self, action='HUNT', params=None):
        result,_=self.accept(action,params)
        self.assertTrue(result[0],result[1])
        cmd=result[2]
        self.jack_rt.refresh_from_db()
        ok,code,cmd=CommandService.start_command(cmd.command_id)
        self.assertTrue(ok,code)
        if action == 'HUNT':
            bind_wildlife(self, cmd)
        return cmd

    def finish(self,cmd,minute=None):
        from village.models import VillageHuntAttempt
        if cmd.kind == 'HUNT' and cmd.sanitized_parameters.get('hunt_code') == 'HUNT_ANIMALS':
            if not VillageHuntAttempt.objects.filter(command=cmd).exists():
                bind_wildlife(self, cmd)
        self.time.world_minute = minute if minute is not None else cmd.started_world_minute+cmd.duration_minutes
        self.time.save()
        if cmd.kind == 'HUNT' and cmd.sanitized_parameters.get('hunt_code') == 'HUNT_ANIMALS':
            from village.wildlife import execute
            from village.models import VillageHuntAttempt
            attempt=VillageHuntAttempt.objects.get(command=cmd,status='ACTIVE')
            result=execute(dict(action='complete',request_id=str(uuid.uuid4()),prey_id=str(attempt.prey_id),
                attempt_id=str(attempt.pk),death_world_minute=self.time.world_minute))
            if not result['success']:
                ActionService.complete_action(cmd.pk,cmd.request_id,False,result['result_code'])
                return result
            return result['data']['economy_result']
        return commit_economy_action(self.payload(cmd))

    def payload(self,cmd):
        p=_payload(cmd.kind,10012,'BUTCHER_YARD' if cmd.kind=='PROCESS_GAME' else 'HUNTING_GATE',cmd.sanitized_parameters,request_id=cmd.request_id)
        p.update(command_id=str(cmd.command_id),message_id=str(cmd.message_id))
        return p

    def prey(self,item=29101,expires=3585):
        index=(self.jack_inv.slots.aggregate(n=Max('slot_index'))['n'] or 0)+1
        return NpcGlobalInventorySlot.objects.create(bag=self.jack_inv,slot_index=index,
            item_id=item,item_amount=1,produced_world_minute=705,expires_world_minute=expires)

    def test_full_hunt_process_and_replay_conserve_inputs(self):
        hunt=self.start()
        self.assertEqual(get_available_slot_amount(self.jack_hunt_tool,700),0)
        early=commit_economy_action(self.payload(hunt))
        self.assertEqual(early['result_code'],'HUNT_KILL_REQUIRED')
        self.assertFalse(VillageActionReceipt.objects.filter(request_id=hunt.request_id).exists())
        result=self.finish(hunt)
        self.assertEqual(result['result_code'],'OK',result)
        self.assertTrue(commit_economy_action(self.payload(hunt))['replayed'])
        prey=self.jack_inv.slots.get(item_id__in=PREY_ITEM_IDS,item_amount__gt=0)
        self.assertEqual((prey.item_amount,prey.produced_world_minute,prey.expires_world_minute),(1,705,3585))
        cmd=self.start('PROCESS_GAME',{'prey_instance_id':str(prey.instance_id)})
        self.assertEqual(get_available_slot_amount(prey,705),0)
        result=self.finish(cmd)
        self.assertEqual(result['result_code'],'OK',result)
        self.assertTrue(commit_economy_action(self.payload(cmd))['replayed'])
        prey.refresh_from_db();self.assertEqual(prey.item_amount,0)
        for item_id,amount in PREY_RECIPES[prey.item_id]['outputs'].items():
            slot=self.jack_inv.slots.get(item_id=item_id,item_amount__gt=0)
            self.assertEqual(slot.item_amount,amount)
            self.assertEqual(slot.expires_world_minute,None if item_id in BYPRODUCT_ITEM_IDS else 3585)
        self.jack_hunt_tool.refresh_from_db();self.assertEqual(self.jack_hunt_tool.remaining_uses,18)

    def test_all_animals_processing_and_no_freshness_reset(self):
        for item_id,recipe in PREY_RECIPES.items():
            with self.subTest(item_id=item_id):
                p=self.prey(item_id,expires=900)
                result=self.finish(self.start('PROCESS_GAME',{'prey_instance_id':str(p.instance_id)}))
                self.assertEqual(result['result_code'],'OK',result)
                outputs=result['data']['production']['outputs']
                self.assertEqual({x['item_id']:x['amount'] for x in outputs},recipe['outputs'])
                self.assertTrue(all(x['expires_world_minute'] in (None,900) for x in outputs))

    def test_db_duration_missing_freeze_and_client_cannot_override(self):
        (ok,code,first),content=self.accept();self.assertTrue(ok,code)
        VillageActionDurationConfig.objects.filter(action_name='HUNT').update(duration_minutes=7)
        replay,_=self.accept(content=content)
        self.assertEqual(replay[2].duration_minutes,5)
        second,_=self.accept();self.assertEqual(second[2].duration_minutes,7)
        VillageActionDurationConfig.objects.filter(action_name='HUNT').delete()
        missing,_=self.accept();self.assertEqual(missing[1],'INVALID_ACTION_DURATION')
        self.assertNotIn('HUNT',VillageActionDurationConfig.default_durations())
        ok,code,cmd=CommandService.start_command(first.command_id)
        self.assertTrue(ok,code);self.assertEqual(cmd.duration_minutes,5)
        self.assertEqual(self.finish(cmd)['result_code'],'OK')
        VillageActionDurationConfig.objects.create(action_name='HUNT',duration_minutes=7)
        current,_=self.accept()
        VillageActionDurationConfig.objects.filter(action_name='HUNT').delete()
        ok,code,cmd=CommandService.start_command(current[2].command_id)
        self.assertTrue(ok,code);self.assertEqual(cmd.duration_minutes,7)
        self.assertEqual(self.finish(cmd,cmd.started_world_minute+6)['result_code'],'OK')
        self.assertTrue(commit_economy_action(self.payload(cmd))['replayed'])
        VillageActionDurationConfig.objects.filter(action_name='PROCESS_GAME').delete()
        missing,_=self.accept('PROCESS_GAME',{'prey_instance_id':str(uuid.uuid4())})
        self.assertEqual(missing[1],'INVALID_ACTION_DURATION')
        old,_=self.accept(params={'hunt_code':'HUNT_CHICKEN'})
        self.assertEqual(old[1],'HUNT_CODE_RETIRED')

    def test_client_start_clock_skew_uses_committed_tick_and_same_day_fatigue(self):
        result,_=self.accept()
        ok,code,first=CommandService.start_command(result[2].command_id,699)
        self.assertTrue(ok,code);self.assertEqual(first.started_world_minute,700)
        ActionService.complete_action(first.command_id,first.request_id,False,'ACTION_CANCELLED')
        result,_=self.accept()
        ok,code,second=CommandService.start_command(result[2].command_id,99999)
        self.assertTrue(ok,code);self.assertEqual(second.started_world_minute,700)
        self.assertEqual(second.production_state['hunt_ordinal'],2)
        self.assertEqual(second.production_state['extra_energy_fp'],40)

    def test_second_hunt_fatigue_once_and_cancel_still_counts(self):
        first=self.start()
        ActionService.complete_action(first.command_id,first.request_id,False,'ACTION_CANCELLED')
        self.jack_rt.refresh_from_db(); before=self.jack_rt.energy_fp
        second=self.start()
        self.jack_rt.refresh_from_db();self.assertEqual(self.jack_rt.energy_fp,before-40)
        CommandService.start_command(second.command_id)
        self.jack_rt.refresh_from_db();self.assertEqual(self.jack_rt.energy_fp,before-40)
        result=self.finish(second)
        self.assertEqual(result['result_code'],'OK',result)
        self.jack_rt.refresh_from_db();self.assertLessEqual(abs(self.jack_rt.energy_fp-(before-40-7)),1)
        self.assertEqual(production_info(self.jack,705)['hunts_started_today'],2)

    def test_failed_start_never_counts_or_reserves(self):
        self.jack_hunt_tool.remaining_uses=0;self.jack_hunt_tool.save()
        result,_=self.accept();cmd=result[2]
        outcome=CommandService.start_command(cmd.command_id)
        self.assertFalse(outcome[0]);cmd.refresh_from_db()
        self.assertIsNone(cmd.started_world_minute)
        self.assertEqual(production_info(self.jack,700)['hunts_started_today'],0)
        self.assertEqual(NpcCommandState.objects.filter(status='RUNNING').count(),0)

    def test_resources_reserved_across_reload_cancel_releases_and_needs_once(self):
        prey=self.prey();cmd=self.start('PROCESS_GAME',{'prey_instance_id':str(prey.instance_id)})
        cmd=NpcCommandState.objects.get(pk=cmd.pk)
        self.jack_rt.refresh_from_db()
        with transaction.atomic():
            self.assertIsNone(settle_work_needs(self.jack_rt,cmd,702))
        self.jack_rt.refresh_from_db();energy=self.jack_rt.energy_fp
        with transaction.atomic():
            self.assertIsNone(settle_work_needs(self.jack_rt,cmd,702))
        self.jack_rt.refresh_from_db();self.assertEqual(self.jack_rt.energy_fp,energy)
        self.time.world_minute=702;self.time.save()
        self.assertTrue(ActionService.complete_action(cmd.command_id,cmd.request_id,False,'ACTION_CANCELLED')[0])
        self.assertEqual(get_available_slot_amount(prey,702),1)
        self.assertEqual(get_available_slot_amount(self.jack_hunt_tool,702),1)

    def test_expiry_mid_process_and_invalid_prey_cannot_produce_byproduct(self):
        prey=self.prey(expires=703)
        cmd=self.start('PROCESS_GAME',{'prey_instance_id':str(prey.instance_id)})
        result=self.finish(cmd)
        self.assertNotEqual(result['result_code'],'OK')
        prey.refresh_from_db();self.assertEqual(prey.item_amount,1)
        self.assertFalse(self.jack_inv.slots.filter(item_id__in=BYPRODUCT_ITEM_IDS).exists())
        self.jack_hunt_tool.refresh_from_db();self.assertEqual(self.jack_hunt_tool.remaining_uses,20)

    def test_atomic_failure_after_first_output_rolls_back_every_asset(self):
        prey=self.prey();cmd=self.start('PROCESS_GAME',{'prey_instance_id':str(prey.instance_id)})
        from village.phase3_asset_service import add_batch
        def fail_second(*args,**kwargs):
            if args[1] in BYPRODUCT_ITEM_IDS:raise ValueError('injected failure')
            return add_batch(*args,**kwargs)
        with patch('village.phase3_asset_service.add_batch',side_effect=fail_second):
            with self.assertRaises(ValueError):self.finish(cmd)
        prey.refresh_from_db();self.jack_hunt_tool.refresh_from_db()
        self.assertEqual((prey.item_amount,self.jack_hunt_tool.remaining_uses),(1,20))
        self.assertFalse(self.jack_inv.slots.filter(item_id=1056).exists())
        self.assertEqual(self.finish(cmd)['result_code'],'OK')

    def test_health_hunger_energy_and_away_stop_both_actions(self):
        for action in ('HUNT','PROCESS_GAME'):
            for field,value in [('health_fp',300),('hunger_fp',900),('energy_fp',100),('action_status','AWAY')]:
                with self.subTest(action=action,field=field):
                    self.jack_rt.refresh_from_db()
                    self.jack_rt.health_fp=1000;self.jack_rt.hunger_fp=200;self.jack_rt.energy_fp=1000
                    self.jack_rt.action_status='IDLE';self.jack_rt.active_command_id=None;self.jack_rt.save()
                    p=self.prey() if action=='PROCESS_GAME' else None
                    cmd=self.start(action,{'prey_instance_id':str(p.instance_id)} if p else None)
                    self.jack_rt.refresh_from_db();setattr(self.jack_rt,field,value);self.jack_rt.save()
                    self.assertNotEqual(self.finish(cmd)['result_code'],'OK')
                    if p:p.delete()

    def test_no_formal_command_no_reward_or_generic_success(self):
        self.assertEqual(commit_economy_action(_payload('HUNT',10012,'HUNTING_GATE',{'hunt_code':'HUNT_ANIMALS'}))['result_code'],'PRODUCTION_COMMAND_REQUIRED')
        cmd=self.start()
        self.assertEqual(ActionService.complete_action(cmd.command_id,cmd.request_id,True,'OK')[1],'PRODUCTION_SETTLEMENT_REQUIRED')
        spoof=self.payload(cmd);spoof['params']={'hunt_code':'HUNT_CHICKEN'}
        self.assertEqual(commit_economy_action(spoof)['result_code'],'P3_REQUEST_CONFLICT')

    def test_midnight_uses_start_day_and_next_day_resets(self):
        self.time.world_minute=1438;self.time.save()
        self.jack_rt.needs_world_minute=1438;self.jack_rt.save()
        cmd=self.start();self.assertEqual(cmd.production_state['hunt_day'],0)
        self.assertEqual(self.finish(cmd)['result_code'],'OK')
        self.assertEqual(production_info(self.jack,1443)['hunts_started_today'],0)
        self.assertEqual(self.start().production_state['extra_energy_fp'],0)

    def test_authoritative_tick_stops_work_at_energy_threshold(self):
        from village.phase2_services import TickService
        from village.models import VillageSimulationConfig
        VillageSimulationConfig.objects.filter(config_key='default').update(village_needs_enabled=True)
        NpcRuntimeState.objects.all().update(needs_world_minute=700)
        self.jack_rt.refresh_from_db();self.jack_rt.energy_fp=102;self.jack_rt.save()
        cmd=self.start()
        result=TickService.advance({'request_id':str(uuid.uuid4()),'world_minute':705,'time_revision':701})
        self.assertTrue(result['success'],result)
        cmd.refresh_from_db();self.jack_rt.refresh_from_db()
        self.assertEqual(cmd.status,'FAILED')
        self.assertEqual(cmd.result_code,'ENERGY_TOO_LOW')
        self.assertEqual(cmd.production_state['needs_until'],702)
        self.assertFalse(self.jack_inv.slots.filter(item_id__in=PREY_ITEM_IDS,item_amount__gt=0).exists())
        self.jack_hunt_tool.refresh_from_db();self.assertEqual(self.jack_hunt_tool.remaining_uses,20)

    def test_processing_information_explains_unknown_and_reserved_prey(self):
        p=self.prey();p.produced_world_minute=None;p.save()
        data=production_info(self.jack,700)
        self.assertFalse(data['prey'][0]['can_process'])
        self.assertIn('PREY_FRESHNESS_UNKNOWN',data['prey'][0]['blocked_reasons'])

    def test_invalid_processing_params_and_wrong_role_are_rejected_at_accept(self):
        for params in ({'prey_instance_id':'29101'},{'prey_instance_id':str(uuid.uuid4()),'amount':99}):
            result,_=self.accept('PROCESS_GAME',params)
            self.assertFalse(result[0])
        content={'schema_version':'village.command.v1','id':str(uuid.uuid4()),'request_id':str(uuid.uuid4()),
            'name':'PROCESS_GAME','params':{'prey_instance_id':str(uuid.uuid4())},'queue_mode':'STRICT','ttl_ms':30000}
        result=CommandService.accept_command(str(uuid.uuid4()),'10014','env',content,int(time.time()*1000))
        self.assertFalse(result[0])

    def test_prey_market_round_trip_retains_expiry_and_unknown_old_snapshot_blocks(self):
        from marketplace import services as market
        p=self.prey(expires=800)
        listing,error=market.create_npc_listing(self.jack,p.instance_id,1,12)
        self.assertIsNone(error)
        _,error=market.delist_npc_listing(self.jack,listing.pk)
        self.assertIsNone(error)
        returned=self.jack_inv.slots.get(item_id=29101,item_amount=1)
        self.assertEqual(returned.expires_world_minute,800)
        listing,error=market.create_npc_listing(self.jack,returned.instance_id,1,12)
        self.assertIsNone(error)
        listing.item_snapshot={'itemID':29101,'itemAmount':1};listing.save()
        _,error=market.delist_npc_listing(self.jack,listing.pk)
        self.assertIsNone(error)
        returned=self.jack_inv.slots.get(item_id=29101,item_amount=1)
        self.assertTrue(returned.freshness_unknown)

    def test_legacy_accepted_hunt_cannot_mint_timer_meat(self):
        cmd=self.start()
        cmd.production_state={};cmd.sanitized_parameters={'hunt_code':'HUNT_CHICKEN'};cmd.save()
        result=self.finish(cmd)
        self.assertEqual(result['result_code'],'HUNT_KILL_REQUIRED',result)
        self.assertFalse(self.jack_inv.slots.filter(item_id__in=PREY_ITEM_IDS,item_amount__gt=0).exists())
        self.assertFalse(self.jack_inv.slots.filter(item_id=1056).exists())
