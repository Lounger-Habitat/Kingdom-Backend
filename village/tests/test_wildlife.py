"""Isolated real-hunt ownership, interruption and economy regression tests."""
import copy
import uuid
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken
from account.models import GameAccount
from inventory.models import InventorySlot
from player.models import PlayerState
from village.models import VillageWildlife, VillageHuntAttempt, VillageActionReceipt, VillageActionDurationConfig
from village.tests import test_jack_production as jack_tests
from village.tests.test_player_actions import PlayerFixture
from village.tests.test_phase3_production import ProductionTestBase
from village.phase3_asset_service import commit_economy_action
from village.runtime_services import ActionService
from village.wildlife import execute, bootstrap


class WildlifeFixture:
    def call(self, action, prey_id, **kwargs):
        return execute(dict(action=action, request_id=str(uuid.uuid4()), prey_id=str(prey_id), **kwargs))

    def register(self, species="PIG", prey_id=None):
        prey_id = prey_id or uuid.uuid4()
        result = self.call("register", prey_id, species=species, region_id="HUNTING_GROUNDS",
                           position={"x": -100, "y": 0, "z": 60})
        self.assertTrue(result["success"], result)
        return prey_id

    def begin(self, prey_id, actor_id, command=None):
        attempt_id = str(uuid.uuid4())
        args = dict(attempt_id=attempt_id, actor_type="NPC" if command else "PLAYER", actor_id=str(actor_id))
        if command:
            args["command_id"] = str(command.pk)
        result = self.call("begin", prey_id, **args)
        self.assertTrue(result["success"], result)
        return attempt_id


class PlayerWildlifeTests(WildlifeFixture, PlayerFixture, TestCase):
    def test_ap_zero_never_read_or_changed_and_exactly_one_batch_per_life(self):
        state = PlayerState.objects.create(player_id=str(self.a.pk), action_points=0)
        before = PlayerState.objects.filter(pk=state.pk).values().get()
        prey = self.register()
        attempt = self.begin(prey, self.a.pk)
        payload = dict(action="complete", request_id=str(uuid.uuid4()), prey_id=str(prey), attempt_id=attempt)
        with patch('player.services.recover_action_points', side_effect=AssertionError("hunting must not touch AP")):
            result = execute(payload)
            self.assertTrue(result['success'], result)
            self.assertEqual(result, execute(payload))
            another_id = dict(payload, request_id=str(uuid.uuid4()))
            self.assertEqual(result['data'], execute(another_id)['data'])
        self.assertEqual(PlayerState.objects.filter(pk=state.pk).values().get(), before)
        slot = self.bag_a.slots.get(item_id=29102)
        self.assertEqual((slot.item_amount, slot.produced_world_minute, slot.expires_world_minute), (1, 100, 2980))
        self.assertEqual(VillageHuntAttempt.objects.filter(prey_id=prey, status='SUCCEEDED').count(), 1)
        # Re-registering an old UUID cannot resurrect it; respawns need a fresh life.
        self.register(prey_id=prey)
        self.assertEqual(VillageWildlife.objects.get(pk=prey).status, 'CLAIMED_REWARD')
        result = self.call('begin', prey, attempt_id=str(uuid.uuid4()), actor_type='PLAYER', actor_id=str(self.a.pk))
        self.assertEqual(result['result_code'], 'PREY_NOT_ALIVE')
        self.begin(self.register(), self.a.pk)

    def test_first_claim_wins_cancel_releases_and_old_attempt_cannot_kill(self):
        prey = self.register()
        first = self.begin(prey, self.a.pk)
        lost = self.call('begin', prey, attempt_id=str(uuid.uuid4()), actor_type='PLAYER', actor_id=str(self.b.pk))
        self.assertEqual(lost['result_code'], 'PREY_BUSY')
        self.assertTrue(self.call('cancel', prey, attempt_id=first)['success'])
        second = self.begin(prey, self.b.pk)
        self.assertEqual(self.call('complete', prey, attempt_id=first)['result_code'], 'ATTEMPT_NOT_ACTIVE')
        self.assertEqual(self.call('complete', prey, attempt_id=second, claim_version=1)['result_code'], 'STALE_CLAIM')
        self.assertTrue(self.call('complete', prey, attempt_id=second, claim_version=2)['success'])
        self.assertFalse(self.bag_a.slots.exists())
        self.assertEqual(self.bag_b.slots.get().item_amount, 1)

    def test_request_conflict_invalid_death_and_caller_cannot_choose_reward(self):
        prey = self.register()
        invalid = self.call('begin', prey, attempt_id=str(uuid.uuid4()), actor_type=[], actor_id=str(self.a.pk))
        self.assertEqual(invalid['result_code'], 'INVALID_PARAMS')
        attempt = self.begin(prey, self.a.pk)
        alias = self.call('begin', self.register(), attempt_id=str(uuid.uuid4()), actor_type='PLAYER', actor_id='0'+str(self.a.pk))
        self.assertTrue(alias['success'], alias)
        self.assertEqual(alias['data']['attempt']['actor_id'], str(self.a.pk))
        self.assertEqual(self.call('complete', prey, attempt_id=attempt, death_world_minute=101)['result_code'], 'INVALID_DEATH_TIME')
        self.assertEqual(self.call('complete', prey, attempt_id=attempt, death_world_minute=99)['result_code'], 'INVALID_DEATH_TIME')
        payload = dict(action='complete', request_id=str(uuid.uuid4()), prey_id=str(prey), attempt_id=attempt)
        self.assertEqual(execute(dict(payload, item_id=29103))['result_code'], 'INVALID_PARAMS')
        self.assertTrue(execute(payload)['success'])
        self.assertEqual(execute(dict(payload, action='cancel'))['result_code'], 'REQUEST_CONFLICT')

    def test_final_hit_replaces_legacy_owner_and_only_last_hitter_receives_meat(self):
        prey = self.register()
        old = self.begin(prey, self.a.pk)
        payload = dict(action='finish', request_id=str(uuid.uuid4()), prey_id=str(prey),
                       attempt_id=str(uuid.uuid4()), actor_type='PLAYER', actor_id=str(self.b.pk),
                       death_world_minute=100)
        result = execute(payload)
        self.assertTrue(result['success'], result)
        self.assertEqual(execute(payload), result)
        self.assertEqual(VillageHuntAttempt.objects.get(pk=old).status, 'CANCELLED')
        self.assertFalse(self.bag_a.slots.exists())
        self.assertEqual(self.bag_b.slots.get(item_id=29102).item_amount, 1)
        late = execute(dict(payload, request_id=str(uuid.uuid4()), attempt_id=str(uuid.uuid4()), actor_id=str(self.a.pk)))
        self.assertFalse(late['success'])
        self.assertFalse(self.bag_a.slots.exists())
        self.assertEqual(VillageHuntAttempt.objects.filter(status='SUCCEEDED').count(), 1)

    def test_failure_rolls_back_death_and_asset_then_same_request_can_retry(self):
        prey = self.register()
        attempt = self.begin(prey, self.a.pk)
        payload = dict(action='complete', request_id=str(uuid.uuid4()), prey_id=str(prey), attempt_id=attempt)
        with patch('village.wildlife.VillageWorldEvent.objects.create', side_effect=RuntimeError('lost database')):
            with self.assertRaises(RuntimeError):
                execute(payload)
        self.assertFalse(self.bag_a.slots.exists())
        self.assertEqual(VillageWildlife.objects.get(pk=prey).status, 'CLAIMED')
        self.assertTrue(execute(payload)['success'])

    def test_bootstrap_retains_life_identity_claims_and_dead_tombstones(self):
        prey = self.register()
        attempt = self.begin(prey, self.a.pk)
        snapshot = bootstrap()['data']
        self.assertEqual(snapshot['prey'][0]['attempt_id'], attempt)
        self.assertEqual(snapshot['attempts'][0]['actor_id'], str(self.a.pk))
        self.call('complete', prey, attempt_id=attempt)
        self.assertEqual(bootstrap()['data']['prey'][0]['status'], 'CLAIMED_REWARD')
        self.assertEqual(bootstrap()['data']['attempts'], [])

    def test_old_meat_collect_is_closed_without_touching_ap(self):
        before = list(PlayerState.objects.values())
        result = self.run_action('collect', {'collect_type':'采集肉类'}, location='HUNTING_GATE')
        self.assertEqual(result['result_code'], 'REAL_HUNT_REQUIRED')
        self.assertEqual(list(PlayerState.objects.values()), before)
        client = APIClient()
        client.force_authenticate(user=self.a)
        response = client.post(f'/api/inventory/{self.a.username}/collect', {'collectType':'采集肉类'}, format='json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['code'], 'REAL_HUNT_REQUIRED')
        self.assertEqual(list(PlayerState.objects.values()), before)

    def test_player_jwt_cannot_assert_hits_but_env_jwt_can_register(self):
        client = APIClient()
        token = AccessToken.for_user(self.a)
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(client.get('/api/village/wildlife/bootstrap').status_code, 401)
        token['scope'], token['aud'] = 'env_server', 'kingdom-env'
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(client.get('/api/village/wildlife/bootstrap').status_code, 200)


class JackWildlifeTests(WildlifeFixture, ProductionTestBase):
    # Reuse fixture helpers without inheriting the timer-era test cases.
    def setUp(self):
        super().setUp()
        self.jack_rt.needs_world_minute = 700
        self.jack_rt.energy_fp, self.jack_rt.hunger_fp = 1000, 200
        self.jack_rt.save()
        self.jack_hunt_tool.item_amount, self.jack_hunt_tool.remaining_uses = 1, 20
        self.jack_hunt_tool.save()
        for action in ('HUNT', 'PROCESS_GAME'):
            VillageActionDurationConfig.objects.update_or_create(action_name=action, defaults={'duration_minutes':5})
    accept = jack_tests.JackProductionTests.accept
    payload = jack_tests.JackProductionTests.payload

    def start(self):
        from village.runtime_services import CommandService
        outcome, _ = self.accept()
        self.assertTrue(outcome[0], outcome[1])
        ok, code, command = CommandService.start_command(outcome[2].pk)
        self.assertTrue(ok, code)
        return command

    def test_timer_cannot_grant_reward_but_real_kill_finishes_early_and_replays(self):
        VillageActionDurationConfig.objects.filter(action_name="HUNT").update(duration_minutes=120)
        command = self.start()
        self.assertNotIn('prey_item_id', command.production_state)
        prey = self.register('CATTLE')
        attempt = self.begin(prey, '10012', command)
        self.time.world_minute = 701
        self.time.save()
        self.assertEqual(commit_economy_action(self.payload(command))['result_code'], 'HUNT_KILL_REQUIRED')
        result = self.call('complete', prey, attempt_id=attempt, death_world_minute=701)
        self.assertTrue(result['success'], result)
        self.assertEqual(result['data']['items_added'][0]['item_id'], 29103)
        self.assertEqual(result['data']['items_added'][0]['produced_world_minute'], 701)
        replay = commit_economy_action(self.payload(command))
        self.assertTrue(replay['replayed'], replay)
        self.jack_hunt_tool.refresh_from_db()
        self.assertEqual(self.jack_hunt_tool.remaining_uses, 19)
        command.refresh_from_db()
        self.assertEqual(command.status, 'SUCCEEDED')
        from village.models import VillageDeliveryOutbox
        timing = VillageDeliveryOutbox.objects.get(command=command, message_kind="OUTCOME").payload["data"]
        self.assertEqual(timing["max_duration_minutes"], 120)
        self.assertEqual(timing["actual_duration_minutes"], 1)
        self.assertEqual(VillageActionReceipt.objects.filter(command=command).count(), 1)
        settled = bootstrap()['data']['settled_attempts']
        self.assertEqual(settled[0]['attempt']['command_id'], str(command.pk))
        self.assertEqual(settled[0]['economy_result']['request_id'], str(command.request_id))

    def test_npc_final_hit_settles_atomically_at_the_authoritative_death_time(self):
        command = self.start()
        prey = self.register()
        self.time.world_minute = 720
        self.time.save()
        result = self.call('finish', prey, attempt_id=str(uuid.uuid4()), actor_type='NPC',
                           actor_id='10012', command_id=str(command.pk), death_world_minute=704)
        self.assertTrue(result['success'], result)
        self.assertEqual(self.jack_inv.slots.get(item_id=29102).expires_world_minute, 3584)
        command.refresh_from_db()
        self.assertEqual(command.status, 'SUCCEEDED')

    def test_elapsed_budget_alone_never_mints_and_retarget_does_not_repeat_fatigue(self):
        command = self.start()
        prey = self.register()
        attempt = self.begin(prey, '10012', command)
        self.jack_rt.refresh_from_db()
        energy = self.jack_rt.energy_fp
        self.call('cancel', prey, attempt_id=attempt)
        self.begin(self.register(), '10012', command)
        self.jack_rt.refresh_from_db()
        self.assertEqual(self.jack_rt.energy_fp, energy)
        self.time.world_minute = 800
        self.time.save()
        self.assertEqual(commit_economy_action(self.payload(command))['result_code'], 'HUNT_KILL_REQUIRED')
        self.assertFalse(self.jack_inv.slots.filter(item_id__in=(29101,29102,29103)).exists())
        self.jack_hunt_tool.refresh_from_db()
        self.assertEqual(self.jack_hunt_tool.remaining_uses, 20)

    def test_migration_changes_only_legacy_five_minute_budget(self):
        import importlib
        from types import SimpleNamespace
        from django.apps import apps
        from django.db import connection
        migration = importlib.import_module('village.migrations.0054_real_wildlife_hunting')
        migration.expand_legacy_hunt_budget(apps, SimpleNamespace(connection=connection))
        self.assertEqual(VillageActionDurationConfig.configured_duration('HUNT'), 120)
        VillageActionDurationConfig.objects.filter(action_name='HUNT').update(duration_minutes=37)
        migration.expand_legacy_hunt_budget(apps, SimpleNamespace(connection=connection))
        self.assertEqual(VillageActionDurationConfig.configured_duration('HUNT'), 37)
        VillageActionDurationConfig.objects.filter(action_name='HUNT').delete()
        migration.expand_legacy_hunt_budget(apps, SimpleNamespace(connection=connection))
        self.assertIsNone(VillageActionDurationConfig.configured_duration('HUNT'))

    def test_late_delivery_uses_death_time_not_request_arrival_deadline(self):
        command = self.start()
        prey = self.register()
        attempt = self.begin(prey, '10012', command)
        self.time.world_minute = 720
        self.time.save()
        result = self.call('complete', prey, attempt_id=attempt, death_world_minute=704)
        self.assertTrue(result['success'], result)
        self.assertEqual(self.jack_inv.slots.get(item_id=29102).expires_world_minute, 3584)
        command.refresh_from_db()
        self.assertEqual(command.production_state['needs_until'], 704)

    def test_death_after_deadline_and_body_failure_never_reward(self):
        command = self.start()
        prey = self.register()
        attempt = self.begin(prey, '10012', command)
        self.time.world_minute = 706
        self.time.save()
        self.assertEqual(self.call('complete', prey, attempt_id=attempt)['result_code'], 'HUNT_TIMEOUT')
        self.assertFalse(self.jack_inv.slots.filter(item_id=29102).exists())
        self.assertEqual(VillageWildlife.objects.get(pk=prey).status, 'CLAIMED')

    def test_command_cancel_releases_prey_and_stale_confirmation_cannot_mint(self):
        command = self.start()
        prey = self.register()
        attempt = self.begin(prey, '10012', command)
        ActionService.complete_action(command.pk, command.request_id, False, 'ACTION_CANCELLED')
        self.assertEqual(VillageWildlife.objects.get(pk=prey).status, 'ALIVE')
        self.assertEqual(self.call('complete', prey, attempt_id=attempt)['result_code'], 'ATTEMPT_NOT_ACTIVE')
        self.jack_hunt_tool.refresh_from_db()
        self.assertEqual(self.jack_hunt_tool.remaining_uses, 20)

    def test_pending_death_defers_only_its_npc_needs_while_authority_clock_advances(self):
        from village.phase2_services import TickService
        from village.models import VillageSimulationConfig, NpcRuntimeState
        VillageSimulationConfig.objects.filter(config_key='default').update(village_needs_enabled=True)
        NpcRuntimeState.objects.all().update(needs_world_minute=700)
        self.jack_rt.refresh_from_db()
        self.jack_rt.energy_fp = 102
        self.jack_rt.save()
        command = self.start()
        prey = self.register()
        attempt = self.begin(prey, '10012', command)
        tick = dict(request_id=str(uuid.uuid4()), world_minute=705, time_revision=701,
                    deferred_hunt_command_ids=[str(command.pk)])
        result = TickService.advance(copy.deepcopy(tick))
        self.assertTrue(result['success'], result)
        self.assertTrue(result['npc_results']['10012']['deferred_hunt'])
        self.jack_rt.refresh_from_db()
        self.bill_rt.refresh_from_db()
        self.time.refresh_from_db()
        self.assertEqual(self.time.world_minute, 705)
        self.assertEqual(self.bill_rt.needs_world_minute, 705)
        self.assertEqual((self.jack_rt.energy_fp, self.jack_rt.needs_world_minute), (102, 700))
        self.assertTrue(TickService.advance(copy.deepcopy(tick))['success'])
        result = self.call('complete', prey, attempt_id=attempt, death_world_minute=701)
        self.assertTrue(result['success'], result)
        command.refresh_from_db()
        self.assertEqual((command.status, command.production_state['needs_until']), ('SUCCEEDED', 701))

    def test_unrelated_deferred_id_does_not_bypass_health_interruption(self):
        from village.phase2_services import TickService
        from village.models import VillageSimulationConfig, NpcRuntimeState
        VillageSimulationConfig.objects.filter(config_key='default').update(village_needs_enabled=True)
        NpcRuntimeState.objects.all().update(needs_world_minute=700)
        self.jack_rt.refresh_from_db()
        self.jack_rt.energy_fp = 102
        self.jack_rt.save()
        command = self.start()
        self.begin(self.register(), '10012', command)
        result = TickService.advance(dict(request_id=str(uuid.uuid4()),world_minute=705,time_revision=701,
                    deferred_hunt_command_ids=[str(uuid.uuid4())]))
        self.assertTrue(result['success'], result)
        command.refresh_from_db()
        self.assertEqual((command.status, command.result_code), ('FAILED','ENERGY_TOO_LOW'))
