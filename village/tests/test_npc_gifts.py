from copy import deepcopy
import time
import uuid
from unittest.mock import patch

from django.test import TestCase

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.components import BATCH_COPY_FIELDS
from inventory.models import InventoryBag, NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot
from items.models import Item
from village.command_catalog import agent_tool
from village.models import (EconomicLedgerEntry, NpcCommandState, NpcRuntimeState,
    VillageActionReceipt, VillageDeliveryOutbox, VillageDishReview, VillageSimulationConfig, VillageWorldEvent)
from village.npc_gifts import validate_gift
from village.phase3_asset_service import commit_economy_action
from village.runtime_services import CommandService
from village.seed_data import NPC_SEEDS


class NpcGiftTests(TestCase):
    def setUp(self):
        VillageSimulationConfig.objects.update_or_create(config_key='default', defaults={
            'village_economy_enabled': True, 'village_direct_trade_enabled': True})
        self.clock = GlobalGameTimeState.get_instance()
        self.clock.world_minute = 700
        self.clock.time_revision = 700
        self.clock.save()
        self.sender, _ = NpcDefinition.objects.get_or_create(agent_id='10011', defaults={'name': 'Bill'})
        self.target, _ = NpcDefinition.objects.get_or_create(agent_id='10012', defaults={'name': 'Jack'})
        self.source, _ = NpcGlobalInventory.objects.get_or_create(npc=self.sender)
        self.source.money = 100
        self.source.save()
        self.destination, _ = NpcGlobalInventory.objects.get_or_create(npc=self.target)
        self.runtime, _ = NpcRuntimeState.objects.get_or_create(npc=self.sender)
        self.player = GameAccount.objects.create(username='gift-recipient', password='unused')
        self.player_bag = InventoryBag.objects.create(player=self.player, character_id=self.player.username)
        Item.objects.update_or_create(item_id=98765, defaults={'item_name':'测试物品', 'item_type':'Food', 'item_price':10})
        self.slot = NpcGlobalInventorySlot.objects.create(bag=self.source, slot_index=90,
            item_id=98765, item_amount=8, quality=5, produced_world_minute=500, expires_world_minute=1500,
            source_request_id=uuid.uuid4(), ingredient='玉米', dish_metadata={'recipe_id':12},
            remaining_uses=7, rated=True, rating_price=25, overall_score=8.5, review_comment='测试点评')

    def params(self, target_type='NPC'):
        return {'exchange_id':str(uuid.uuid4()), 'target_type':target_type, 'target_id':str(self.player.pk) if target_type == 'PLAYER' else '10012',
            'items':[{'instance_id':str(self.slot.instance_id), 'amount':3}]}

    def payload(self, params=None):
        params = params if params is not None else self.params()
        command = NpcCommandState.objects.create(command_id=uuid.uuid4(), message_id=uuid.uuid4(),
            request_id=uuid.uuid4(), npc=self.sender, sender_id='10011', recipient_id='env',
            schema_version='village.command.v1', kind='GIVE_ITEMS', sanitized_parameters=deepcopy(params),
            payload_sha256='test', queue_mode='STRICT', status='RUNNING', started_world_minute=699,
            duration_minutes=1, received_world_minute=699)
        self.runtime.active_command_id = command.command_id
        self.runtime.save()
        return {'schema_version':'village.economy-action.v1', 'command_id':str(command.command_id),
            'request_id':str(command.request_id), 'message_id':str(command.message_id),
            'command_name':'GIVE_ITEMS', 'npc_id':10011, 'completed_world_minute':700,
            'time_revision':700, 'current_location_code':'VILLAGE_SQUARE', 'params':params}

    def test_npc_gift_preserves_batches_and_notifies_only_receiver(self):
        response = commit_economy_action(self.payload())
        self.assertEqual(response['result_code'], 'OK', response)
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.item_amount, 5)
        received = self.destination.slots.get(item_id=98765)
        self.assertEqual(received.item_amount, 3)
        self.assertNotEqual(received.instance_id, self.slot.instance_id)
        for field in BATCH_COPY_FIELDS:
            self.assertEqual(getattr(received, field), getattr(self.slot, field), field)
        self.source.refresh_from_db(); self.destination.refresh_from_db()
        self.assertEqual((self.source.asset_version, self.destination.asset_version), (1, 1))
        self.assertEqual(response['asset_versions'], {'10011':1, '10012':1})
        event = VillageWorldEvent.objects.get(event_type='ECONOMY_GIVE_ITEMS')
        self.assertEqual(list(VillageDeliveryOutbox.objects.filter(event=event, message_kind='EVENT').values_list('recipient', flat=True)), ['10012'])
        self.assertEqual(event.private_data['items'][0]['amount'], 3)
        self.assertEqual(EconomicLedgerEntry.objects.get().external_delta, 0)

    def test_player_gift_keeps_other_players_private_and_replays_once(self):
        params = self.params('PLAYER'); params['gold_amount'] = 16
        payload = self.payload(params)
        first = commit_economy_action(payload)
        replay = commit_economy_action(payload)
        self.assertEqual(first['result_code'], 'OK', first)
        self.assertEqual(first['asset_versions'], {'10011':1})
        self.assertTrue(replay['replayed'])
        self.assertEqual(self.player_bag.slots.get().item_amount, 3)
        self.slot.refresh_from_db(); self.assertEqual(self.slot.item_amount, 5)
        self.source.refresh_from_db(); self.player_bag.refresh_from_db()
        self.assertEqual((self.source.money, self.player_bag.money), (84, 16))
        self.assertEqual(VillageActionReceipt.objects.count(), 1)
        event = VillageWorldEvent.objects.get(event_type='ECONOMY_GIVE_ITEMS')
        delivery = VillageDeliveryOutbox.objects.get(event=event, message_kind='EVENT')
        self.assertEqual(delivery.recipient, 'ENV_CLIENTS')
        self.assertEqual(delivery.payload['player_ids'], [str(self.player.pk)])
        self.assertEqual(delivery.payload['gift']['gold_amount'], 16)
        self.assertNotIn('private_data', delivery.payload)
        self.assertNotIn('inventory', str(first).lower())
        payload['params']['items'][0]['amount'] = 1
        self.assertEqual(commit_economy_action(payload)['result_code'], 'P3_REQUEST_CONFLICT')

    def test_whole_gift_rolls_back_if_any_batch_is_insufficient(self):
        second = NpcGlobalInventorySlot.objects.create(bag=self.source, slot_index=91, item_id=98765, item_amount=1)
        params = self.params()
        params['gold_amount'] = 16
        params['items'].append({'instance_id':str(second.instance_id), 'amount':2})
        payload = self.payload(params)
        result = commit_economy_action(payload)
        self.assertEqual(result['result_code'], 'GIFT_INSUFFICIENT_ITEM', result)
        self.assertIs(result['data']['tool_error']['executed'], False)
        self.slot.refresh_from_db(); second.refresh_from_db()
        self.assertEqual((self.slot.item_amount, second.item_amount), (8, 1))
        self.source.refresh_from_db(); self.assertEqual(self.source.money, 100)
        self.assertFalse(self.destination.slots.filter(item_id=98765).exists())
        self.assertFalse(EconomicLedgerEntry.objects.exists())
        self.assertFalse(VillageWorldEvent.objects.filter(event_type='ECONOMY_GIVE_ITEMS').exists())
        self.assertTrue(commit_economy_action(payload)['replayed'])

    def test_two_tasks_with_different_requests_deliver_exchange_only_once(self):
        params = self.params('PLAYER')
        first = commit_economy_action(self.payload(params))
        replay_payload = self.payload(deepcopy(params))
        replay = commit_economy_action(replay_payload)
        self.assertEqual(replay['result_code'], 'OK', replay)
        self.assertTrue(replay['data']['already_delivered'])
        self.assertEqual(replay['data']['original_request_id'], first['request_id'])
        self.assertEqual(replay['request_id'], replay_payload['request_id'])
        self.assertEqual(replay['event'], first['event'])
        self.assertEqual(replay['data']['items'], first['data']['items'])
        self.assertEqual(replay['effects'], {})
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.item_amount, 5)
        self.assertEqual(self.player_bag.slots.get().item_amount, 3)
        self.assertEqual(VillageActionReceipt.objects.filter(success=True).count(), 2)
        self.assertEqual(NpcCommandState.objects.get(pk=replay_payload['command_id']).status, 'SUCCEEDED')
        self.runtime.refresh_from_db()
        self.assertIsNone(self.runtime.active_command_id)
        self.assertEqual(EconomicLedgerEntry.objects.filter(entry_type='GIVE_ITEMS').count(), 1)
        self.assertEqual(VillageWorldEvent.objects.filter(event_type='ECONOMY_GIVE_ITEMS').count(), 1)
        self.assertEqual(VillageDeliveryOutbox.objects.filter(message_kind='EVENT').count(), 1)
        self.assertTrue(commit_economy_action(replay_payload)['replayed'])

    def test_exchange_cannot_change_target_or_amount_but_new_exchange_can_give_again(self):
        params = self.params()
        self.assertEqual(commit_economy_action(self.payload(params))['result_code'], 'OK')
        for changed in ({'target_id':'10013'}, {'items':[{'instance_id':str(self.slot.instance_id), 'amount':1}]}):
            response = commit_economy_action(self.payload({**params, **changed}))
            self.assertEqual(response['result_code'], 'GIFT_EXCHANGE_CONFLICT', response)
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.item_amount, 5)
        self.assertEqual(commit_economy_action(self.payload(self.params()))['result_code'], 'OK')
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.item_amount, 2)

    def test_failed_exchange_does_not_block_a_confirmed_retry(self):
        params = self.params()
        self.slot.item_amount = 1
        self.slot.save()
        self.assertEqual(commit_economy_action(self.payload(params))['result_code'], 'GIFT_INSUFFICIENT_ITEM')
        self.slot.item_amount = 8
        self.slot.save()
        self.assertEqual(commit_economy_action(self.payload(params))['result_code'], 'OK')
        self.assertEqual(EconomicLedgerEntry.objects.filter(entry_type='GIVE_ITEMS').count(), 1)

    def test_exchange_id_is_required_and_canonical(self):
        params = self.params()
        del params['exchange_id']
        self.assertEqual(validate_gift(params, '10011'), 'GIFT_INVALID_PARAMS')
        for value in (None, '', 'another-task', 123, str(uuid.uuid4()).upper(), uuid.uuid4().hex):
            params['exchange_id'] = value
            self.assertEqual(validate_gift(params, '10011'), 'GIFT_INVALID_PARAMS')

    def test_expired_unknown_or_reserved_items_cannot_be_given(self):
        for overrides in ({'expires_world_minute':700}, {'freshness_unknown':True}):
            with self.subTest(overrides=overrides):
                for name, value in overrides.items(): setattr(self.slot, name, value)
                self.slot.save()
                self.assertEqual(commit_economy_action(self.payload())['result_code'], 'GIFT_INSUFFICIENT_ITEM')
                self.slot.expires_world_minute = 1500; self.slot.freshness_unknown = False
                self.slot.save()
        VillageDishReview.objects.create(owner=self.sender, slot=self.slot, request_id=uuid.uuid4(),
            created_world_minute=600, expires_world_minute=1000)
        params = self.params(); params['items'][0]['amount'] = 8
        self.assertEqual(commit_economy_action(self.payload(params))['result_code'], 'GIFT_INSUFFICIENT_ITEM')
        self.slot.refresh_from_db(); self.assertEqual(self.slot.item_amount, 8)

    def test_foreign_batch_cannot_be_given(self):
        self.slot.bag = self.destination; self.slot.save()
        self.assertEqual(commit_economy_action(self.payload())['result_code'], 'GIFT_ITEM_NOT_OWNED')

    def test_invalid_target_and_parameters_never_mutate_inventory(self):
        variants = []
        for field, value in [('target_type','STORE'), ('target_id','10011'), ('target_id','10099'), ('items',[])]:
            params = self.params(); params[field] = value; variants.append(params)
        for value in (0, -1, True, '2', 1.5, 2147483648):
            params = self.params(); params['items'][0]['amount'] = value; variants.append(params)
        params = self.params(); params['items'] *= 2; variants.append(params)
        params = self.params(); params['get_items'] = []; variants.append(params)
        for params in variants:
            with self.subTest(params=params):
                self.assertIsNotNone(validate_gift(params, '10011'))
        self.slot.refresh_from_db(); self.assertEqual(self.slot.item_amount, 8)

    def test_missing_player_bag_or_invalid_player_id_is_rejected(self):
        for target in ('Player', '-1', '9'*30, str(self.player.pk + 999999)):
            params = self.params('PLAYER'); params['target_id'] = target
            self.assertEqual(commit_economy_action(self.payload(params))['result_code'], 'GIFT_TARGET_INVALID')
        self.player_bag.delete()
        self.assertEqual(commit_economy_action(self.payload(self.params('PLAYER')))['result_code'], 'GIFT_TARGET_INVALID')

    def test_settlement_requires_matching_started_command(self):
        payload = self.payload()
        payload['params']['items'][0]['amount'] = 4
        self.assertEqual(commit_economy_action(payload)['result_code'], 'P3_REQUEST_CONFLICT')
        payload = self.payload()
        NpcCommandState.objects.filter(pk=payload['command_id']).update(started_world_minute=700)
        self.assertEqual(commit_economy_action(payload)['result_code'], 'GIFT_NOT_FINISHED')
        payload = self.payload()
        NpcCommandState.objects.filter(pk=payload['command_id']).delete()
        self.assertEqual(commit_economy_action(payload)['result_code'], 'GIFT_COMMAND_REQUIRED')

    def test_feature_flag_and_sender_permissions(self):
        payload = self.payload()
        VillageSimulationConfig.objects.update(village_direct_trade_enabled=False)
        self.assertEqual(commit_economy_action(payload)['result_code'], 'P3_FEATURE_DISABLED')
        VillageSimulationConfig.objects.update(village_direct_trade_enabled=True)
        payload['npc_id'] = 10099
        self.assertEqual(commit_economy_action(payload)['result_code'], 'P3_FORBIDDEN_ACTOR')

    def test_agent_catalog_and_real_admission(self):
        self.assertEqual(agent_tool('GIVE_ITEMS')['parameters']['required'], ['exchange_id','target_type','target_id','items'])
        properties = agent_tool('GIVE_ITEMS')['parameters']['properties']
        self.assertEqual(properties['items']['minItems'], 0)
        self.assertEqual(properties['gold_amount']['minimum'], 0)
        for npc in NPC_SEEDS: self.assertIn('GIVE_ITEMS', npc['capabilities'])
        content = {'schema_version':'village.command.v1', 'id':str(uuid.uuid4()), 'request_id':str(uuid.uuid4()),
            'name':'GIVE_ITEMS', 'params':{**self.params(), 'items':[], 'gold_amount':16}, 'queue_mode':'STRICT', 'ttl_ms':86400000}
        ok, code, command = CommandService.accept_command(str(uuid.uuid4()), '10011', 'env', content, int(time.time()*1000))
        self.assertTrue(ok, code)
        self.assertEqual(command.duration_minutes, 1)
        ok, code, _ = CommandService.start_command(command.command_id)
        self.assertTrue(ok, code)

    def test_unexpected_error_rolls_back_earlier_batch_writes(self):
        payload = self.payload({**self.params('PLAYER'), 'gold_amount':16})
        with patch.object(NpcGlobalInventory, 'save', side_effect=RuntimeError('simulated storage failure')):
            with self.assertRaises(RuntimeError): commit_economy_action(payload)
        self.slot.refresh_from_db(); self.assertEqual(self.slot.item_amount, 8)
        self.assertFalse(self.player_bag.slots.exists())
        self.source.refresh_from_db(); self.player_bag.refresh_from_db()
        self.assertEqual((self.source.money, self.player_bag.money), (100, 0))

    def test_gold_only_gifts_transfer_once_to_players_and_npcs(self):
        for target_type, bag in (('PLAYER', self.player_bag), ('NPC', self.destination)):
            with self.subTest(target_type=target_type):
                bag.refresh_from_db(); before = bag.money
                self.source.refresh_from_db(); source_before = self.source.money
                params = {**self.params(target_type), 'items':[], 'gold_amount':16}
                first = commit_economy_action(self.payload(params))
                replay = commit_economy_action(self.payload(deepcopy(params)))
                self.assertEqual(first['result_code'], 'OK', first)
                self.assertEqual(first['data']['gold_amount'], 16)
                self.assertEqual(first['data']['items'], [])
                self.assertTrue(replay['data']['already_delivered'])
                self.assertEqual(first['effects']['10011'], {'money':-16})
                receiver_key = 'PLAYER:' + str(self.player.pk) if target_type == 'PLAYER' else '10012'
                self.assertEqual(first['effects'][receiver_key], {'money':16})
                changed = commit_economy_action(self.payload({**params, 'gold_amount':17}))
                self.assertEqual(changed['result_code'], 'GIFT_EXCHANGE_CONFLICT')
                bag.refresh_from_db(); self.source.refresh_from_db()
                self.assertEqual((self.source.money, bag.money), (source_before - 16, before + 16))
                self.assertFalse(bag.slots.filter(item_id=98765).exists())
        self.assertEqual(EconomicLedgerEntry.objects.filter(entry_type='GIVE_ITEMS').count(), 2)
        self.assertEqual(VillageWorldEvent.objects.filter(event_type='ECONOMY_GIVE_ITEMS').count(), 2)

    def test_money_failure_never_delivers_items_or_gold(self):
        for recipient_money, amount, code in ((0, 101, 'GIFT_INSUFFICIENT_MONEY'),
                (2147483647, 1, 'GIFT_TARGET_MONEY_LIMIT')):
            with self.subTest(code=code):
                NpcGlobalInventory.objects.filter(pk=self.destination.pk).update(money=recipient_money)
                result = commit_economy_action(self.payload({**self.params(), 'gold_amount':amount}))
                self.assertEqual(result['result_code'], code, result)
                self.source.refresh_from_db(); self.destination.refresh_from_db(); self.slot.refresh_from_db()
                self.assertEqual((self.source.money, self.destination.money, self.slot.item_amount), (100, recipient_money, 8))
                self.assertFalse(self.destination.slots.filter(item_id=98765).exists())
        self.assertFalse(EconomicLedgerEntry.objects.exists())

    def test_gold_parameters_reject_invalid_amounts_and_empty_gifts(self):
        for amount in (None, -1, True, '16', 1.5, 2147483648):
            self.assertEqual(validate_gift({**self.params(), 'gold_amount':amount}, '10011'), 'GIFT_INVALID_PARAMS')
        self.assertEqual(validate_gift({**self.params(), 'items':[], 'gold_amount':0}, '10011'), 'GIFT_INVALID_PARAMS')
        self.assertIsNone(validate_gift({**self.params(), 'gold_amount':0}, '10011'))
