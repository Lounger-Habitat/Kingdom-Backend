import copy
import uuid
from unittest.mock import patch

from django.test import TestCase

from marketplace.services import create_listing
from player.models import PlayerSkillProgress
from restaurant.models import (CustomerTransaction, PlayerRestaurantState, RestaurantConfig,
                               RestaurantHourlySlot, RestaurantSaleEvent)
from village.models import EconomicLedgerEntry, NpcRuntimeState
from village.phase2_services import TickService
from village.tests.test_player_actions import PlayerFixture


class PlayerBusinessTickTests(PlayerFixture, TestCase):
    def test_market_to_stall_actual_tick_and_transport_replays_settle_once(self):
        self.clock.apply_world_minute(600)
        self.clock.time_revision = 0
        self.clock.save()
        NpcRuntimeState.objects.update(needs_world_minute=600, time_revision=0)
        RestaurantConfig.objects.update_or_create(pk=1, defaults=dict(opening_hour=10,
            closing_hour=19, base_hourly_sales=3, base_menu_capacity=5))
        source = self.slot(3, item_id=29201, bag=self.bag_b, quality=4,
            source_request_id=uuid.uuid4(), rated=True, rating_price=12)
        listing, error = create_listing(self.b, source.instance_id, 3, 7)
        self.assertIsNone(error)
        bought = self.run_action('buy_marketplace', dict(listing_id=str(listing.pk),
            quantity=3, expected_unit_price=7), location='MARKET_STREET')
        self.assertTrue(bought['success'], bought)
        batches = bought['data']['result']['purchased_batches']
        self.assertEqual(len(batches), 1)
        received = self.bag_a.slots.get(instance_id=batches[0]['instance_id'])
        self.assertEqual((received.item_amount, received.expires_world_minute), (3, 2000))
        self.assertTrue(self.run_action('open_player_stall', location='RESTAURANT')['success'])
        listed = self.run_action('list_stall_dish', dict(instance_id=str(received.instance_id),
            quantity=3), location='RESTAURANT')
        self.assertTrue(listed['success'], listed)
        hourly = RestaurantHourlySlot.objects.create(player=self.a, game_year=1,
            game_month=1, game_day=1, game_hour=10, quota=3)
        RestaurantSaleEvent.objects.bulk_create([RestaurantSaleEvent(slot=hourly,
            scheduled_minute=minute) for minute in (1, 2, 3)])
        tick = dict(world_minute=603, time_revision=1, request_id=str(uuid.uuid4()))
        with patch('requests.post', side_effect=AssertionError('Tick must not call HTTP')):
            settled = TickService.advance(copy.deepcopy(tick))
            self.assertTrue(settled['success'], settled)
            self.assertEqual(settled['restaurant_updates'], [str(self.a.pk)])
            for replay_payload in (copy.deepcopy(tick), dict(tick, request_id=str(uuid.uuid4()))):
                replay = TickService.advance(replay_payload)
                self.assertEqual(replay['result_code'], 'COMMAND_REPLAYED')
                self.assertEqual(replay['restaurant_updates'], [str(self.a.pk)])
            later = TickService.advance(dict(world_minute=604, time_revision=2,
                request_id=str(uuid.uuid4())))
            self.assertTrue(later['success'], later)
            self.assertEqual(later['restaurant_updates'], [])
        self.bag_a.refresh_from_db()
        self.assertEqual(self.bag_a.money, 100 - 21 + 36)
        self.assertEqual(CustomerTransaction.objects.filter(player=self.a).count(), 3)
        self.assertEqual(sum(CustomerTransaction.objects.filter(player=self.a)
            .values_list('unit_cost', flat=True)), 21)
        self.assertEqual(EconomicLedgerEntry.objects.filter(entry_type='PLAYER_STALL_SALE').count(), 3)
        self.assertEqual(PlayerSkillProgress.objects.get(player=self.a, skill_code='business').total_xp, 35)
        self.assertFalse(PlayerSkillProgress.objects.filter(player=self.b).exists())
        self.assertEqual(PlayerRestaurantState.objects.get(player=self.a).total_dishes_sold, 3)
        self.assertFalse(received.bag.slots.filter(item_id=29201, item_amount__gt=0).exists())
