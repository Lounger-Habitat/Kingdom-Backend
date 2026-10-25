import copy
import uuid

from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from inventory.models import NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot, InventorySlot
from items.models import Item, ItemType
from player.models import PlayerState, PlayerSkillProgress, PlayerCombatReceipt
from village.models import NpcRuntimeState, VillageActionDurationConfig, VillagePlayerProcessingJob
from village.phase2_services import TickService
from village.player_hunting import execute_combat
from village.tests.test_player_actions import PlayerFixture
from village.wildlife import execute as wildlife


class PlayerHuntingLoopTests(PlayerFixture, TestCase):
    def setUp(self):
        super().setUp()
        Item.objects.update_or_create(pk=1049, defaults=dict(item_name="salt", item_type=ItemType.SEASONING))
        Item.objects.update_or_create(pk=29203, defaults=dict(item_name="meat soup", item_type=ItemType.FOOD))
        self.clock.apply_world_minute(100)
        self.clock.time_revision = 0
        self.clock.save()
        jack, _ = NpcDefinition.objects.get_or_create(agent_id='10012', defaults=dict(name='Jack'))
        self.jack, _ = NpcGlobalInventory.objects.update_or_create(npc=jack, defaults=dict(money=240))
        self.jack.slots.all().delete()
        NpcGlobalInventorySlot.objects.create(bag=self.jack, slot_index=0, item_id=4002,
            item_amount=1, remaining_uses=20)
        NpcRuntimeState.objects.update_or_create(npc=jack, defaults=dict(current_location_code='BUTCHER_YARD',
            health_fp=1000, energy_fp=800, hunger_fp=200, needs_world_minute=100,
            active_command_id=None, action_status='IDLE'))
        NpcRuntimeState.objects.update(needs_world_minute=100, time_revision=0)
        VillageActionDurationConfig.objects.update_or_create(action_name='PROCESS_GAME', defaults=dict(duration_minutes=5))

    def test_real_permit_deer_to_processing_tick_and_existing_meat_soup(self):
        from inventory.models import InventorySlot
        InventorySlot.objects.create(bag=self.bag_a, slot_index=self.bag_a.slots.count(), item_id=4110, item_amount=1, remaining_uses=60)
        attack_id = str(uuid.uuid4())
        permit = execute_combat(dict(action='authorize_attack', player_id=str(self.a.pk),
            request_id=attack_id, authority_session_id='loop-env-session', weapon=0))
        self.assertTrue(permit['success'], permit)
        prey_id = str(uuid.uuid4())
        registered = wildlife(dict(action='register', request_id=str(uuid.uuid4()), prey_id=prey_id,
            species='DEER', region_id='DEEP_FOREST', position=dict(x=-145, y=6.35, z=125)))
        self.assertTrue(registered['success'], registered)
        killed = wildlife(dict(action='finish', request_id=str(uuid.uuid4()), prey_id=prey_id,
            attempt_id=str(uuid.uuid4()), actor_type='PLAYER', actor_id=str(self.a.pk), command_id=None,
            death_world_minute=100, attack_id=attack_id))
        self.assertTrue(killed['success'], killed)
        prey = self.bag_a.slots.get(item_id=29106, item_amount=1)
        preview = self.run_action('preview_game_processing', dict(prey_instance_id=str(prey.instance_id)))
        self.assertTrue(preview['success'], preview)
        quote = preview['data']['result']['preview']
        started = self.run_action('start_game_processing', dict(prey_instance_id=str(prey.instance_id),
            expected_quote=quote['quote_hash']), location='BUTCHER_YARD')
        self.assertTrue(started['success'], started)
        job_id = started['data']['result']['job']['job_id']
        tick = dict(world_minute=105, time_revision=1, request_id=str(uuid.uuid4()))
        settled = TickService.advance(copy.deepcopy(tick))
        self.assertTrue(settled['success'], settled)
        self.assertEqual(settled['restaurant_updates'], [str(self.a.pk)])
        self.assertEqual(TickService.advance(copy.deepcopy(tick))['result_code'], 'COMMAND_REPLAYED')
        self.assertEqual(VillagePlayerProcessingJob.objects.get(pk=job_id).status, 'READY')
        claimed = self.run_action('claim_game_processing', dict(job_id=job_id), location='BUTCHER_YARD')
        self.assertTrue(claimed['success'], claimed)
        meat = self.bag_a.slots.get(item_id=29107, item_amount=4)
        self.assertEqual((meat.produced_world_minute, meat.expires_world_minute), (100, None))
        self.assertFalse(meat.dish_metadata)
        self.bag_a.refresh_from_db(); self.jack.refresh_from_db()
        self.assertEqual((self.bag_a.money, self.jack.money), (95, 245))
        PlayerState.objects.update_or_create(player_id=str(self.a.pk), defaults=dict(action_points=200))
        PlayerSkillProgress.objects.update_or_create(player=self.a, skill_code='cooking', defaults=dict(total_xp=440))
        tool = InventorySlot.objects.create(bag=self.bag_a, slot_index=self.bag_a.slots.count(),
            item_id=4107, item_amount=1, remaining_uses=50, equipment_level=3)
        salt = self.slot(1, item_id=1049, quality=3, expires_world_minute=None)
        params = dict(recipe_id=29226, batches=1, tool_instance_id=str(tool.instance_id), selections=[
            dict(instance_id=str(meat.instance_id), quantity=2), dict(instance_id=str(salt.instance_id), quantity=1)])
        cooking = self.run_action('preview_cooking', params)
        self.assertTrue(cooking['success'], cooking)
        self.assertTrue(cooking['data']['result']['preview']['available'], cooking)
        cooked = self.run_action('cook_dish', dict(params,
            expected_quote=cooking['data']['result']['preview']['quote_hash']), location='RESTAURANT')
        self.assertTrue(cooked['success'], cooked)
        dish = cooked['data']['result']['dish']
        self.assertEqual((dish['item_id'], dish['quantity'], dish['expires_world_minute']), (29203, 3, None))
        self.assertEqual(PlayerSkillProgress.objects.get(player=self.a, skill_code='hunting').total_xp, 32)

    def test_ordinary_player_token_cannot_reach_trusted_combat_or_report_damage(self):
        client = APIClient()
        token = AccessToken.for_user(self.a)
        client.credentials(HTTP_AUTHORIZATION='Bearer ' + str(token))
        payload = dict(action='inspect_state', player_id=str(self.a.pk), request_id=str(uuid.uuid4()),
            authority_session_id='api-boundary')
        denied = client.post('/api/village/player/combat/', payload, format='json')
        self.assertEqual(denied.status_code, 401)
        self.assertFalse(PlayerCombatReceipt.objects.filter(player=self.a).exists())
        token['scope'] = 'env_server'; token['aud'] = 'kingdom-env'
        client.credentials(HTTP_AUTHORIZATION='Bearer ' + str(token))
        accepted = client.post('/api/village/player/combat/', payload, format='json')
        self.assertEqual(accepted.status_code, 200)
        self.assertTrue(accepted.data['success'], accepted.data)
        self.assertEqual(accepted.data['data']['health']['current'], 100)
