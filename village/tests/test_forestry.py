import uuid
from unittest.mock import patch
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken
from player.models import PlayerState, PlayerGrowthState, PlayerSkillProgress, SkillRewardReceipt
from village.tests.test_player_actions import PlayerFixture
from village.forestry import execute, bootstrap, RULES
from village.wildlife import execute as hunt
from village.models import VillageTreeNode, VillageChopAttempt, VillageForestryReceipt
from inventory.models import InventorySlot
from village.tests.test_phase3_production import ProductionTestBase, _payload
from village.models import VillageActionDurationConfig, NpcProfile, VillageActionReceipt
from village.runtime_services import CommandService, ActionService
from village.phase3_asset_service import commit_economy_action
from items.models import Item


class ForestryTests(PlayerFixture, TestCase):
    def call(self, action, node=None, **kwargs):
        return execute(dict(action=action, node_id=str(node or self.node), request_id=str(uuid.uuid4()), **kwargs))

    def setUp(self):
        super().setUp()
        self.axe_a = InventorySlot.objects.create(bag=self.bag_a, slot_index=0, item_id=4104,
            item_amount=1, remaining_uses=20)
        self.axe_b = InventorySlot.objects.create(bag=self.bag_b, slot_index=0, item_id=4104,
            item_amount=1, remaining_uses=20)
        self.node = uuid.uuid4()
        self.assertTrue(self.call('register', species='BIRCH', position={'x':-120,'y':2.4,'z':30})['success'])

    def begin(self, actor=None):
        attempt=str(uuid.uuid4())
        result=self.call('begin', generation=1, attempt_id=attempt, actor_type='PLAYER', actor_id=str((actor or self.a).pk))
        self.assertTrue(result['success'],result)
        return attempt, result['data']['attempt']['claim_version']

    def payload(self, action='complete', **extra):
        attempt, version=self.begin()
        return dict(action=action, node_id=str(self.node), request_id=str(uuid.uuid4()), generation=1,
                    attempt_id=attempt, claim_version=version, felled_world_minute=100, **extra)

    def test_wood_exactly_once_and_no_action_points(self):
        state=PlayerState.objects.create(player_id=str(self.a.pk),action_points=0)
        before=PlayerState.objects.filter(pk=state.pk).values().get()
        payload=self.payload()
        result=execute(payload)
        self.assertTrue(result['success'],result)
        self.assertEqual(execute(payload),result)
        self.assertEqual(execute(dict(payload,request_id=str(uuid.uuid4())))['data'],result['data'])
        self.assertEqual(self.bag_a.slots.get(item_id=1002).item_amount,4)
        self.assertIsNone(self.bag_a.slots.get(item_id=1002).expires_world_minute)
        self.assertEqual(PlayerState.objects.filter(pk=state.pk).values().get(),before)
        self.assertEqual(VillageChopAttempt.objects.filter(status='SUCCEEDED').count(),1)

    def test_batch_registration_is_atomic_idempotent_and_supports_every_tree_species(self):
        rows=[dict(node_id=str(uuid.uuid4()),species=species,position={'x':index,'y':0,'z':2})
              for index,species in enumerate(RULES['species'])]
        payload=dict(action='register_batch',request_id=str(uuid.uuid4()),trees=rows)
        result=execute(payload)
        self.assertTrue(result['success'],result)
        self.assertEqual(len(result['data']['trees']),len(RULES['species']))
        self.assertEqual(execute(payload),result)
        before=VillageTreeNode.objects.count()
        invalid=[dict(rows[0],node_id=str(uuid.uuid4())),dict(rows[1],node_id=str(uuid.uuid4()),species='INVALID')]
        self.assertEqual(execute(dict(action='register_batch',request_id=str(uuid.uuid4()),trees=invalid))['result_code'],'INVALID_PARAMS')
        self.assertEqual(VillageTreeNode.objects.count(),before)
        self.assertEqual(execute(dict(action='register_batch',request_id=str(uuid.uuid4()),trees=rows*20))['result_code'],'INVALID_PARAMS')

    def test_claim_race_cancel_and_stale_completion(self):
        first,version=self.begin()
        other=self.call('begin',generation=1,attempt_id=str(uuid.uuid4()),actor_type='PLAYER',actor_id=str(self.b.pk))
        self.assertEqual(other['result_code'],'TREE_BUSY')
        self.assertTrue(self.call('cancel',generation=1,attempt_id=first,claim_version=version)['success'])
        second,new_version=self.begin(self.b)
        self.assertEqual(self.call('complete',generation=1,attempt_id=first,claim_version=version,felled_world_minute=100)['result_code'],'ATTEMPT_NOT_ACTIVE')
        self.assertEqual(self.call('complete',generation=1,attempt_id=second,claim_version=version,felled_world_minute=100)['result_code'],'STALE_CLAIM')
        self.assertTrue(self.call('complete',generation=1,attempt_id=second,claim_version=new_version,felled_world_minute=100)['success'])
        self.assertFalse(self.bag_a.slots.filter(item_id=1002).exists())

    def test_regrowth_requires_shared_clock_and_old_round_cannot_reward_again(self):
        payload=self.payload();self.assertTrue(execute(payload)['success'])
        self.assertEqual(bootstrap()['data']['rules']['regrow_minutes'],7200)
        self.assertEqual(VillageTreeNode.objects.get(pk=self.node).regrow_world_minute,7300)
        self.assertEqual(self.call('respawn',generation=1)['result_code'],'TREE_NOT_READY')
        for elapsed in (1440, 7199):
            self.clock.world_minute=100+elapsed;self.clock.save()
            self.assertEqual(self.call('respawn',generation=1)['result_code'],'TREE_NOT_READY')
        self.clock.world_minute=7300;self.clock.save()
        self.assertTrue(self.call('respawn',generation=1)['success'])
        self.assertEqual(VillageTreeNode.objects.get(pk=self.node).generation,2)
        self.assertEqual(self.call('begin',generation=1,attempt_id=str(uuid.uuid4()),actor_type='PLAYER',actor_id=str(self.a.pk))['result_code'],'STALE_GENERATION')
        self.assertTrue(execute(dict(payload,request_id=str(uuid.uuid4())))['success'])
        self.assertEqual(self.bag_a.slots.filter(item_id=1002).count(),1)
        self.assertEqual(VillageTreeNode.objects.get(pk=self.node).status,'STANDING')

    def test_reward_failure_rolls_back_and_can_retry(self):
        payload=self.payload()
        with patch('village.forestry.VillageWorldEvent.objects.create',side_effect=RuntimeError('database unavailable')):
            with self.assertRaises(RuntimeError): execute(payload)
        self.assertFalse(self.bag_a.slots.filter(item_id=1002).exists())
        self.assertEqual(VillageTreeNode.objects.get(pk=self.node).status,'CLAIMED')
        self.assertTrue(execute(payload)['success'])

    def test_identity_payload_and_reward_boundaries(self):
        payload=self.payload()
        self.assertEqual(execute(dict(payload,amount=1000))['result_code'],'INVALID_PARAMS')
        self.assertEqual(execute(dict(payload,felled_world_minute=101))['result_code'],'INVALID_FELL_TIME')
        self.assertEqual(execute(payload)['result_code'],'REQUEST_CONFLICT')
        payload['request_id']=str(uuid.uuid4())
        self.assertTrue(execute(payload)['success'])
        self.assertEqual(execute(dict(payload,action='cancel'))['result_code'],'INVALID_PARAMS')
        self.assertEqual(self.call('register',species='PINE',position={'x':-120,'y':2.4,'z':30})['result_code'],'TREE_ID_CONFLICT')

    def test_bootstrap_and_registration_do_not_revive_stumps(self):
        self.assertTrue(execute(self.payload())['success'])
        self.assertTrue(self.call('register',species='BIRCH',position={'x':-120,'y':2.4,'z':30})['success'])
        snapshot=bootstrap()['data']
        self.assertEqual(snapshot['trees'][0]['status'],'STUMP')
        self.assertEqual(snapshot['attempts'],[])

    def test_player_cannot_assert_world_facts(self):
        client=APIClient();token=AccessToken.for_user(self.a)
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(client.get('/api/village/forestry/bootstrap').status_code,401)
        token['scope'],token['aud']='env_server','kingdom-env'
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(client.get('/api/village/forestry/bootstrap').status_code,200)

    def test_partial_targets_do_not_lock_player_to_one_activity(self):
        prey=str(uuid.uuid4())
        def h(action,**params):return hunt(dict(action=action,request_id=str(uuid.uuid4()),prey_id=prey,**params))
        self.assertTrue(h('register',species='PIG',region_id='HUNTING_GROUNDS',position={'x':-120,'y':2.4,'z':100})['success'])
        attempt,version=self.begin()
        self.assertTrue(h('begin',attempt_id=str(uuid.uuid4()),actor_type='PLAYER',actor_id=str(self.a.pk))['success'])
        other = uuid.uuid4()
        self.assertTrue(self.call('register', node=other, species='BIRCH', position={'x':0,'y':0,'z':0})['success'])
        self.assertTrue(self.call('begin', node=other, generation=1, attempt_id=str(uuid.uuid4()), actor_type='PLAYER', actor_id=str(self.a.pk))['success'])

    def test_final_hit_replaces_legacy_owner_and_awards_only_last_hitter_once(self):
        old, _ = self.begin(self.a)
        payload = dict(action='finish', node_id=str(self.node), request_id=str(uuid.uuid4()),
                       generation=1, attempt_id=str(uuid.uuid4()), actor_type='PLAYER',
                       actor_id=str(self.b.pk), felled_world_minute=100)
        self.clock.world_minute = 105
        self.clock.save()
        result = execute(payload)
        self.assertTrue(result['success'], result)
        self.assertEqual(execute(payload), result)
        self.assertEqual(VillageChopAttempt.objects.get(pk=old).status, 'CANCELLED')
        self.assertFalse(self.bag_a.slots.filter(item_id=1002).exists())
        self.assertEqual(self.bag_b.slots.get(item_id=1002).item_amount, 4)
        late = execute(dict(payload, request_id=str(uuid.uuid4()), attempt_id=str(uuid.uuid4()), actor_id=str(self.a.pk)))
        self.assertFalse(late['success'])
        self.assertFalse(self.bag_a.slots.filter(item_id=1002).exists())
        self.assertEqual(VillageChopAttempt.objects.filter(status='SUCCEEDED').count(), 1)

    def test_rejected_final_hit_preserves_legacy_claim_and_mints_nothing(self):
        old, _ = self.begin()
        result = self.call('finish', generation=1, attempt_id=str(uuid.uuid4()), actor_type='NPC',
                           actor_id='10012', command_id=str(uuid.uuid4()), felled_world_minute=100)
        self.assertFalse(result['success'])
        self.assertEqual(VillageChopAttempt.objects.get(pk=old).status, 'ACTIVE')
        self.assertFalse(self.bag_a.slots.filter(item_id=1002).exists())

    def test_preflight_is_read_only_rejects_missing_tools_and_reports_cooldown(self):
        before = (PlayerGrowthState.objects.count(), PlayerSkillProgress.objects.count(),
                  VillageForestryReceipt.objects.count(), VillageChopAttempt.objects.count())
        result = self.call('preflight', actor_type='PLAYER', actor_id=str(self.a.pk))
        self.assertTrue(result['success'], result)
        self.assertEqual(result['data']['forestry']['tool_instance_id'], str(self.axe_a.instance_id))
        self.assertEqual(result['data']['forestry']['cooldown_seconds'], 1)
        self.assertEqual(before, (PlayerGrowthState.objects.count(), PlayerSkillProgress.objects.count(),
            VillageForestryReceipt.objects.count(), VillageChopAttempt.objects.count()))
        PlayerSkillProgress.objects.create(player=self.a, skill_code='forestry', total_xp=80)
        skilled = self.call('preflight', actor_type='PLAYER', actor_id=str(self.a.pk))
        self.assertEqual(skilled['data']['forestry']['cooldown_seconds'], .95)
        self.axe_a.delete()
        failed = self.call('preflight', actor_type='PLAYER', actor_id=str(self.a.pk))
        self.assertEqual(failed['result_code'], 'TOOL_MISSING')
        self.assertFalse(PlayerGrowthState.objects.filter(player=self.a).exists())

    def test_hardwood_requires_skill_and_real_improved_axe_then_settles_once(self):
        node = uuid.uuid4()
        self.assertTrue(self.call('register', node=node, species='HARDWOOD', position={'x':1,'y':0,'z':1})['success'])
        self.assertEqual(self.call('preflight', node=node, actor_type='PLAYER', actor_id=str(self.a.pk))['result_code'], 'SKILL_LEVEL_REQUIRED')
        PlayerSkillProgress.objects.create(player=self.a, skill_code='forestry', total_xp=440)
        self.assertEqual(self.call('preflight', node=node, actor_type='PLAYER', actor_id=str(self.a.pk))['result_code'], 'TOOL_TIER_REQUIRED')
        self.axe_a.item_id, self.axe_a.remaining_uses, self.axe_a.equipment_level = 4105, 50, 3
        self.axe_a.save(update_fields=['item_id', 'remaining_uses', 'equipment_level'])
        ready = self.call('preflight', node=node, actor_type='PLAYER', actor_id=str(self.a.pk))
        self.assertTrue(ready['success'], ready)
        self.assertEqual(ready['data']['forestry']['cooldown_seconds'], 1)
        payload = dict(action='finish', node_id=str(node), request_id=str(uuid.uuid4()), generation=1,
            attempt_id=str(uuid.uuid4()), actor_type='PLAYER', actor_id=str(self.a.pk),
            tool_instance_id=str(self.axe_a.instance_id), felled_world_minute=100)
        result = execute(payload)
        self.assertTrue(result['success'], result)
        self.assertEqual(result['data']['items_added'][0]['item_id'], 29306)
        self.assertEqual(result['data']['items_added'][0]['amount'], 4)
        self.assertEqual(result['data']['tool_used']['remaining_uses'], 49)
        self.assertEqual(result['data']['growth_rewards'][0]['xp_gained'], 24)
        self.assertEqual(execute(payload), result)
        self.assertEqual(SkillRewardReceipt.objects.filter(player=self.a, source_type='forestry').count(), 1)
        self.axe_a.refresh_from_db()
        self.assertEqual(self.axe_a.remaining_uses, 49)
        event = result['data']['growth_rewards'][0]
        self.assertEqual(event['total_xp'], 464)

    def test_final_settlement_rechecks_original_tool_and_failure_rolls_back(self):
        payload = self.payload()
        self.axe_a.bag, self.axe_a.slot_index = self.bag_b, 10
        self.axe_a.save(update_fields=['bag', 'slot_index'])
        result = execute(payload)
        self.assertEqual(result['result_code'], 'TOOL_MISSING')
        self.assertFalse(self.bag_a.slots.filter(item_id=1002).exists())
        self.assertFalse(SkillRewardReceipt.objects.filter(player=self.a).exists())
        self.assertEqual(VillageTreeNode.objects.get(pk=self.node).status, 'CLAIMED')

    def test_successful_tree_gives_only_one_use_and_exact_growth_on_replays(self):
        payload = self.payload()
        result = execute(payload)
        self.assertTrue(result['success'], result)
        self.assertEqual(result['data']['growth_rewards'][0]['xp_gained'], 22)
        self.assertEqual(execute(dict(payload, request_id=str(uuid.uuid4())))['data'], result['data'])
        self.axe_a.refresh_from_db()
        self.assertEqual(self.axe_a.remaining_uses, 19)
        self.assertEqual(SkillRewardReceipt.objects.filter(player=self.a, source_type='forestry').count(), 1)


class NpcForestryTests(ProductionTestBase):
    def setUp(self):
        super().setUp()
        self.jack_rt.needs_world_minute=700
        self.jack_rt.hunger_fp=200;self.jack_rt.energy_fp=1000;self.jack_rt.save()
        profile=NpcProfile.objects.filter(npc=self.jack).first()
        if profile:
            profile.capabilities=list(set(profile.capabilities+['CHOP_TREE']));profile.save()
        VillageActionDurationConfig.objects.update_or_create(action_name='CHOP_TREE',defaults={'duration_minutes':120})
        Item.objects.get_or_create(pk=1002,defaults={'item_name':'木材','item_price':5})
        self.node=str(uuid.uuid4())
        self.assertTrue(self.call('register',species='PINE',position={'x':-130,'y':3,'z':35})['success'])

    def call(self,action,**params):
        return execute(dict(action=action,node_id=self.node,request_id=str(uuid.uuid4()),**params))

    def start(self):
        import time
        content=dict(schema_version='village.command.v1',id=str(uuid.uuid4()),request_id=str(uuid.uuid4()),
            name='CHOP_TREE',params={},queue_mode='STRICT',expected_state_version=self.jack_rt.state_version,ttl_ms=86400000)
        ok,code,command=CommandService.accept_command(str(uuid.uuid4()),'10012','env',content,int(time.time()*1000))
        self.assertTrue(ok,code)
        ok,code,command=CommandService.start_command(command.pk)
        self.assertTrue(ok,code)
        attempt=str(uuid.uuid4())
        result=self.call('begin',generation=1,attempt_id=attempt,actor_type='NPC',actor_id='10012',command_id=str(command.pk))
        self.assertTrue(result['success'],result)
        return command,attempt

    def economy_payload(self,command):
        value=_payload('CHOP_TREE',10012,'FOREST_LOGGING',command.sanitized_parameters,request_id=command.request_id)
        value.update(command_id=str(command.pk),message_id=str(command.message_id))
        return value

    def test_actual_felling_required_and_replay_uses_existing_economy_receipt(self):
        command,attempt=self.start()
        self.assertEqual(commit_economy_action(self.economy_payload(command))['result_code'],'TREE_FELL_REQUIRED')
        self.assertFalse(ActionService.complete_action(command.pk,command.request_id,True,'OK')[0])
        self.time.world_minute=701;self.time.save()
        result=self.call('complete',generation=1,attempt_id=attempt,claim_version=1,felled_world_minute=701)
        self.assertTrue(result['success'],result)
        self.assertEqual(self.jack_inv.slots.get(item_id=1002).item_amount,6)
        self.assertIsNone(self.jack_inv.slots.get(item_id=1002).expires_world_minute)
        self.assertEqual(VillageActionReceipt.objects.filter(command=command).count(),1)
        self.assertEqual(commit_economy_action(self.economy_payload(command))['result_code'],'OK')
        command.refresh_from_db();self.assertEqual(command.status,'SUCCEEDED')

    def test_npc_final_hit_settles_atomically_with_its_command(self):
        command, old = self.start()
        result = self.call('finish', generation=1, attempt_id=str(uuid.uuid4()), actor_type='NPC',
                           actor_id='10012', command_id=str(command.pk), felled_world_minute=700)
        self.assertTrue(result['success'], result)
        self.assertEqual(VillageChopAttempt.objects.get(pk=old).status, 'CANCELLED')
        self.assertEqual(self.jack_inv.slots.get(item_id=1002).item_amount, 6)
        command.refresh_from_db()
        self.assertEqual(command.status, 'SUCCEEDED')

    def test_cancel_releases_tree_and_late_felling_gives_nothing(self):
        command,attempt=self.start()
        result=ActionService.complete_action(command.pk,command.request_id,False,'ACTION_CANCELLED')
        self.assertTrue(result[0],result)
        self.assertEqual(VillageTreeNode.objects.get(pk=self.node).status,'STANDING')
        self.assertEqual(self.call('complete',generation=1,attempt_id=attempt,claim_version=1,felled_world_minute=700)['result_code'],'ATTEMPT_NOT_ACTIVE')
        self.assertFalse(self.jack_inv.slots.filter(item_id=1002).exists())

    def test_deadline_does_not_mint_wood(self):
        command,attempt=self.start()
        self.time.world_minute=821;self.time.save()
        result=self.call('complete',generation=1,attempt_id=attempt,claim_version=1,felled_world_minute=821)
        self.assertEqual(result['result_code'],'CHOP_TIMEOUT')
        self.assertFalse(self.jack_inv.slots.filter(item_id=1002).exists())
