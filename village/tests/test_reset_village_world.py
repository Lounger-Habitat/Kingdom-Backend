from io import StringIO
import uuid

from account.models import GameAccount
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone

from game_time.models import GlobalGameTimeState
from inventory.models import (BagTemplate, BagTemplateSlot, EnvActionReceipt, InventoryBag, InventorySlot,
                              NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot, PlayerCollectionPoint,
                              CollectionPoint)
from cooking.models import PityCounter
from leaderboard.models import LeaderboardEntry, PendingReward, ReportEvent
from player.models import DefaultRecipe, PlayerRecipe, PlayerState, PlayerTitle
from restaurant.models import (PlayerRestaurantState, MenuItem, DailyBusinessLog,
                               CustomerTransaction, RestaurantHourlySlot, RestaurantSaleEvent)
from items.models import Item
from marketplace.models import MarketListing, MarketListingSource, MarketTransaction
from village.models import (
    NpcActionReservation,
    NpcCommandState,
    NpcCookingCounter,
    NpcDialogueSession,
    NpcRelationship,
    NpcRuntimeState,
    NpcSubjectiveGoalState,
    VillageAccount,
    VillageCaravanRequest,
    VillageCaravanVisit,
    VillageDeliveryOutbox,
    VillageDishReview,
    VillageFarmPlot,
    VillagePublicDecision,
    VillageSimulationConfig,
    VillageStoryArc,
    VillageStoryMeeting,
    VillageStoreAccount,
    VillageStoreInventorySlot,
    VillageStoreListing,
    VillageWorldEvent,
    VillageAffair,
    VillagePlayerActionReceipt,
    VillagePlayerContribution,
    VillagePlayerAffairLabor,
    VillagePersonalCommission,
    VillagePersonalCommissionEscrow,
    VillagePersonalCommissionReceipt,
    VillageWildlife,
    VillageHuntAttempt,
    VillageWildlifeReceipt,
)
from village.management.commands.reset_village_world import (
    FORMAL_NPC_INITIAL_BALANCE,
    GENERAL_STORE_INITIAL_BALANCE,
    GENERAL_STORE_INITIAL_STOCK,
    PUBLIC_FUND_INITIAL_BALANCE,
)
from village.phase3_catalog import BASE_PRICES, FARM_CROP_RULES
from village.seed_data import (
    FARM_PLOT_SEEDS,
    INITIAL_SUPPLEMENTARY_GRANTS,
    NPC_SEEDS,
    TOOL_ITEM_SEEDS,
)


@override_settings(DEBUG=True, DEPLOYMENT_ENV="development")
class ResetVillageWorldTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        required = {item_id for seed in NPC_SEEDS for item_id, _ in seed["items"]}
        required.update(item_id for _, item_id, _ in INITIAL_SUPPLEMENTARY_GRANTS)
        required.update(item_id for item_id, _, _ in GENERAL_STORE_INITIAL_STOCK)
        for item_id in required:
            Item.objects.get_or_create(
                item_id=item_id,
                defaults={"item_name": f"item-{item_id}", "item_type": "Commodity", "item_price": 10},
            )
        for seed in TOOL_ITEM_SEEDS:
            Item.objects.update_or_create(
                item_id=seed["item_id"],
                defaults={
                    "item_name": seed["item_name"],
                    "item_type": seed["item_type"],
                    "item_description": seed["item_description"],
                    "item_price": seed["item_price"],
                    "collectable": seed["collectable"],
                    "max_use_count": seed.get("max_use_count", 1),
                },
            )
        call_command("seed_village_v1", stdout=StringIO())
        template = BagTemplate.objects.create(character_id="player", money=123)
        BagTemplateSlot.objects.create(template=template, slot_index=0, item_id=4001, item_amount=1, hide_in_shop=True)
        DefaultRecipe.objects.create(recipe_name="reset-default-recipe")

    def test_reset_restores_world_and_preserves_feature_flags(self):
        config = VillageSimulationConfig.objects.get(config_key="default")
        config.village_needs_enabled = True
        config.village_economy_enabled = True
        config.village_social_enabled = True
        config.village_story_enabled = True
        config.save()

        bill_bag = NpcGlobalInventory.objects.get(npc__agent_id="10011")
        bill_bag.money = 999
        bill_bag.asset_version = 7
        bill_bag.save()
        bill_runtime = NpcRuntimeState.objects.get(npc__agent_id="10011")
        bill_runtime.current_location_code = "GENERAL_STORE"
        bill_runtime.state_version = 9
        bill_runtime.save()
        NpcSubjectiveGoalState.objects.create(npc=bill_runtime.npc, summary="旧目标", status="ACTIVE")
        reservation = NpcActionReservation.objects.create(
            action_kind="DIALOGUE", acquired_world_minute=0, expires_world_minute=1
        )
        NpcDialogueSession.objects.create(
            conversation_id="reset-dialogue",
            initiator=bill_runtime.npc,
            target=NpcRuntimeState.objects.get(npc__agent_id="10012").npc,
            reservation=reservation,
            started_world_minute=0,
            expires_world_minute=1,
        )
        VillageWorldEvent.objects.create(
            dedupe_key="reset-test-event",
            event_type="TEST",
            world_minute=1,
            time_revision=1,
        )
        VillageCaravanRequest.objects.create(
            day_index=1,
            item_ids=[1049],
            submitted_world_minute=1,
        )
        VillageCaravanVisit.objects.create(
            day_index=1,
            route="NORMAL",
            requested_items=[1049],
        )

        lucy = NpcDefinition.objects.get(agent_id="10014")
        lucy_slot = NpcGlobalInventory.objects.get(npc=lucy).slots.first()
        NpcCookingCounter.objects.create(npc=lucy, count_4=3, count_5=7, count_6=27)
        VillageDishReview.objects.create(
            owner=lucy,
            slot=lucy_slot,
            request_id=uuid.uuid4(),
            created_world_minute=1,
            expires_world_minute=181,
        )

        test_npc = NpcDefinition.objects.create(
            agent_id="RESET_TEST_NPC_A",
            name="reset_test_a",
            is_test_only=True,
        )
        test_buyer_npc = NpcDefinition.objects.create(
            agent_id="RESET_TEST_NPC_B",
            name="reset_test_b",
            is_test_only=True,
        )
        test_bag = NpcGlobalInventory.objects.create(npc=test_npc, money=9876, asset_version=8)
        test_slot = NpcGlobalInventorySlot.objects.create(
            bag=test_bag,
            slot_index=0,
            item_id=4001,
            item_amount=9,
        )
        test_counter = NpcCookingCounter.objects.create(
            npc=test_npc,
            count_4=4,
            count_5=5,
            count_6=6,
        )
        test_review = VillageDishReview.objects.create(
            owner=test_npc,
            slot=test_slot,
            request_id=uuid.uuid4(),
            created_world_minute=1,
            expires_world_minute=181,
        )

        bill = NpcDefinition.objects.get(agent_id="10011")
        jack = NpcDefinition.objects.get(agent_id="10012")
        formal_listing = MarketListing.objects.create(
            source_type=MarketListingSource.NPC,
            seller_npc=bill,
            item_id=4001,
            total_amount=1,
            remaining_amount=1,
            price=10,
        )
        formal_transaction = MarketTransaction.objects.create(
            listing=formal_listing,
            source_type=MarketListingSource.NPC,
            seller_npc=bill,
            buyer_npc=jack,
            item_id=4001,
            amount=1,
            unit_price=10,
            total_price=10,
        )
        test_listing = MarketListing.objects.create(
            source_type=MarketListingSource.NPC,
            seller_npc=test_npc,
            item_id=4001,
            total_amount=1,
            remaining_amount=1,
            price=11,
        )
        test_transaction = MarketTransaction.objects.create(
            listing=test_listing,
            source_type=MarketListingSource.NPC,
            seller_npc=test_npc,
            buyer_npc=test_buyer_npc,
            item_id=4001,
            amount=1,
            unit_price=11,
            total_price=11,
        )

        player_seller = GameAccount.objects.create(username="reset-player-seller")
        player_buyer = GameAccount.objects.create(username="reset-player-buyer")
        player_listing = MarketListing.objects.create(
            source_type=MarketListingSource.PLAYER,
            seller=player_seller,
            item_id=4001,
            total_amount=1,
            remaining_amount=1,
            price=12,
        )
        player_transaction = MarketTransaction.objects.create(
            listing=player_listing,
            source_type=MarketListingSource.PLAYER,
            seller=player_seller,
            buyer=player_buyer,
            item_id=4001,
            amount=1,
            unit_price=12,
            total_price=12,
        )

        arc = VillageStoryArc.objects.create(
            arc_code="RESET_DELETE_ORDER",
            current_phase="AFTERMATH",
        )
        meeting = VillageStoryMeeting.objects.create(
            arc=arc,
            opened_day=0,
            closes_day=1,
        )
        decision = VillagePublicDecision.objects.create(
            meeting=meeting,
            plan_code="RATION_AND_WAIT",
            approved_world_minute=1,
        )
        protected_store_listing = VillageStoreListing.objects.create(
            store_id="GENERAL_STORE",
            item_id=4001,
            unit_price=0,
            public_meal_code="C1",
            subsidy_decision=decision,
            listed_by_npc=bill,
            created_request_id=uuid.uuid4(),
            listed_world_minute=1,
        )
        VillageAccount.objects.update(balance=123, asset_version=4)
        Item.objects.filter(item_id=4003).delete()

        call_command(
            "reset_village_world",
            "--confirm",
            "RESET_WUXI_VILLAGE",
            stdout=StringIO(),
        )

        time_state = GlobalGameTimeState.objects.get(pk=1)
        self.assertEqual(
            (time_state.game_year, time_state.game_month, time_state.game_day,
             time_state.game_hour, time_state.game_minute, time_state.time_revision),
            (2026, 3, 8, 7, 0, 0),
        )
        plots = list(VillageFarmPlot.objects.order_by("plot_code"))
        empty_plots = [plot for plot in plots if plot.status == "EMPTY"]
        half_grown_plots = [
            plot for plot in plots
            if plot.status == "GROWING" and plot.mature_world_minute > time_state.world_minute
        ]
        mature_plots = [
            plot for plot in plots
            if plot.status == "GROWING" and plot.mature_world_minute <= time_state.world_minute
        ]
        self.assertEqual(len(plots), len(FARM_PLOT_SEEDS))
        self.assertEqual(
            (len(empty_plots), len(half_grown_plots), len(mature_plots)),
            (18, 9, 9),
        )
        for plot in empty_plots:
            self.assertEqual(plot.crop_code, "")
            self.assertIsNone(plot.batch_id)
            self.assertIsNone(plot.planted_world_minute)
            self.assertIsNone(plot.mature_world_minute)
            self.assertIsNone(plot.yield_seed)
            self.assertIsNone(plot.source_request_id)
        for plot in half_grown_plots + mature_plots:
            rule = FARM_CROP_RULES[plot.crop_code]
            self.assertIsNotNone(plot.batch_id)
            self.assertIsNotNone(plot.source_request_id)
            self.assertGreaterEqual(plot.yield_seed, rule["yield_min"])
            self.assertLessEqual(plot.yield_seed, rule["yield_max"])
            self.assertEqual(
                plot.mature_world_minute - plot.planted_world_minute,
                rule["growth_minutes"],
            )
        for plot in half_grown_plots:
            self.assertEqual(
                time_state.world_minute - plot.planted_world_minute,
                FARM_CROP_RULES[plot.crop_code]["growth_minutes"] // 2,
            )
        for plot in mature_plots:
            self.assertEqual(plot.mature_world_minute, time_state.world_minute)
        bill_bag.refresh_from_db()
        bill_runtime = NpcRuntimeState.objects.get(npc__agent_id="10011")
        config.refresh_from_db()
        public_fund = VillageAccount.objects.get(account_code="PUBLIC_FUND")
        self.assertEqual(
            (bill_bag.money, bill_bag.asset_version),
            (FORMAL_NPC_INITIAL_BALANCE, 0),
        )
        self.assertEqual(
            (
                public_fund.balance,
                public_fund.audit_opening_balance,
                public_fund.asset_version,
                public_fund.audit_baseline_kind,
            ),
            (PUBLIC_FUND_INITIAL_BALANCE, PUBLIC_FUND_INITIAL_BALANCE, 0, "INITIAL"),
        )
        self.assertEqual(
            (bill_runtime.current_location_code, bill_runtime.action_status, bill_runtime.state_version),
            ("BILL_HOME", "IDLE", 0),
        )
        self.assertEqual(
            dict(NpcRuntimeState.objects.values_list("npc__agent_id", "current_location_code")),
            {
                "10011": "BILL_HOME",
                "10012": "JACK_HOME",
                "10013": "RECORD_HALL",
                "10014": "RESTAURANT",
                "10015": "GENERAL_STORE",
            },
        )
        self.assertFalse(NpcSubjectiveGoalState.objects.exists())
        self.assertFalse(NpcDialogueSession.objects.exists())
        self.assertFalse(VillageCaravanRequest.objects.exists())
        self.assertFalse(VillageCaravanVisit.objects.exists())
        self.assertFalse(NpcCookingCounter.objects.filter(npc=lucy).exists())
        self.assertFalse(VillageDishReview.objects.filter(owner=lucy).exists())
        self.assertFalse(MarketListing.objects.filter(pk=formal_listing.pk).exists())
        self.assertFalse(MarketTransaction.objects.filter(pk=formal_transaction.pk).exists())
        self.assertFalse(VillageStoreListing.objects.filter(pk=protected_store_listing.pk).exists())
        self.assertFalse(VillagePublicDecision.objects.filter(pk=decision.pk).exists())

        test_bag.refresh_from_db()
        self.assertEqual((test_bag.money, test_bag.asset_version), (9876, 8))
        self.assertTrue(NpcCookingCounter.objects.filter(pk=test_counter.pk).exists())
        self.assertTrue(VillageDishReview.objects.filter(pk=test_review.pk).exists())
        self.assertTrue(MarketListing.objects.filter(pk=test_listing.pk).exists())
        self.assertTrue(MarketTransaction.objects.filter(pk=test_transaction.pk).exists())
        self.assertFalse(MarketListing.objects.filter(pk=player_listing.pk).exists())
        self.assertFalse(MarketTransaction.objects.filter(pk=player_transaction.pk).exists())

        self.assertEqual(
            VillageStoreAccount.objects.get(store_id="GENERAL_STORE").balance,
            GENERAL_STORE_INITIAL_BALANCE,
        )
        self.assertEqual(
            VillageStoreAccount.objects.get(store_id="RESTAURANT_STORE").balance,
            0,
        )
        expected_stock = {
            item_id: (amount, remaining_uses)
            for item_id, amount, remaining_uses in GENERAL_STORE_INITIAL_STOCK
        }
        actual_stock = {
            slot.item_id: (slot.item_amount, slot.remaining_uses)
            for slot in VillageStoreInventorySlot.objects.filter(
                store_id="GENERAL_STORE"
            ).order_by("item_id")
        }
        self.assertEqual(actual_stock, expected_stock)
        self.assertFalse(
            VillageStoreInventorySlot.objects.filter(
                store_id="GENERAL_STORE", listing__isnull=True
            ).exists()
        )
        self.assertEqual(
            {
                listing.item_id: (
                    listing.unit_price,
                    listing.price_mode,
                    listing.status,
                )
                for listing in VillageStoreListing.objects.filter(
                    store_id="GENERAL_STORE"
                )
            },
            {
                item_id: (BASE_PRICES[item_id], "ORIGINAL", "ACTIVE")
                for item_id, _, _ in GENERAL_STORE_INITIAL_STOCK
            },
        )
        reset_event = VillageWorldEvent.objects.get(event_type="VILLAGE_WORLD_RESET")
        self.assertEqual(reset_event.public_data["story_id"], "BROKEN_TRADE_ROUTE_V1")
        self.assertTrue(VillageDeliveryOutbox.objects.filter(event=reset_event).exists())
        self.assertFalse(VillageStoryArc.objects.exists())
        # 二跑复盘 R6：摘要只写重置时刻真实存在的事实。
        self.assertIn("2026-03-08 07:00", reset_event.hub_summary)
        self.assertIn("FARM_PLOT_A", reset_event.hub_summary)
        self.assertIn("18 块空置（其中 9 块可租）", reset_event.hub_summary)
        self.assertIn("9 块生长至一半", reset_event.hub_summary)
        self.assertIn("9 块可收获", reset_event.hub_summary)
        self.assertIn("Bill 2000", reset_event.hub_summary)
        self.assertIn("Nora 2000", reset_event.hub_summary)
        self.assertEqual(
            set(
                NpcGlobalInventory.objects.filter(
                    npc__agent_id__in={seed["agent_id"] for seed in NPC_SEEDS}
                ).values_list("money", flat=True)
            ),
            {FORMAL_NPC_INITIAL_BALANCE},
        )
        self.assertIn(f"公共基金 {PUBLIC_FUND_INITIAL_BALANCE}", reset_event.hub_summary)
        # 剧情 arc 此刻已删，不得在摘要里声称具体剧情阶段。
        self.assertNotIn("AFTERMATH", reset_event.hub_summary)
        self.assertEqual(reset_event.hub_summary, reset_event.player_summary)
        self.assertEqual(reset_event.public_data["summary"], reset_event.hub_summary)
        self.assertEqual(NpcRelationship.objects.count(), 20)
        nora_bag = NpcGlobalInventory.objects.get(npc__agent_id="10015")
        self.assertEqual(
            dict(nora_bag.slots.filter(item_id__in=(4001, 4002)).values_list("item_id", "item_amount")),
            {4001: 3, 4002: 3},
        )
        self.assertTrue(config.village_needs_enabled)
        self.assertTrue(config.village_economy_enabled)
        self.assertTrue(config.village_social_enabled)
        self.assertTrue(config.village_story_enabled)

    def _create_current_progress(self):
        player = GameAccount.objects.create(username="full-reset-player", password="keep-password",
                                            appearance={"test": "keep-appearance"}, appearance_revision=4)
        state = PlayerState.objects.create(player_id=str(player.pk), action_points=7, action_points_max=2000,
                                           current_season_id="old-season")
        PlayerRecipe.objects.create(player=state, recipe_name="old-recipe")
        PlayerTitle.objects.create(player=state, title_id="old-title", title_name="old")
        bag = InventoryBag.objects.create(player=player, character_id=player.username)
        InventoryBag.objects.filter(pk=bag.pk).update(money=9999)
        InventorySlot.objects.create(bag=bag, slot_index=0, item_id=4001, item_amount=3, remaining_uses=1)
        EnvActionReceipt.objects.create(player=player, request_id="old-gift", action="character_trade",
                                        response_data={"success": True})
        VillagePlayerActionReceipt.objects.create(player=player, request_id=uuid.uuid4(), payload_hash="a"*64,
            action="buy_store", response={"success": True}, world_minute=100)
        VillagePlayerContribution.objects.create(player=player, request_id=uuid.uuid4(), kind="material",
            item_id=1002, quantity=3, earned_gold=20, world_minute=100)

        bill = NpcDefinition.objects.get(agent_id="10011")
        command = NpcCommandState.objects.create(npc=bill, message_id=uuid.uuid4(), request_id=uuid.uuid4(),
            sender_id="test", recipient_id="10011", kind="HUNT", payload_sha256="a"*64, status="RUNNING")
        prey = VillageWildlife.objects.create(prey_id=uuid.uuid4(), species="PIG", region_id="HUNTING_GROUNDS",
            position={"x": 1}, status="CLAIMED", claim_version=1, born_world_minute=100)
        VillageHuntAttempt.objects.create(attempt_id=uuid.uuid4(), prey=prey, actor_type="NPC", actor_id="10011",
            command=command, claim_version=1, started_world_minute=100)
        VillageWildlifeReceipt.objects.create(request_id=uuid.uuid4(), payload_hash="b"*64,
            response={"success": True}, world_minute=100)
        commission = VillagePersonalCommission.objects.create(publisher=bill, title="old commission", status="ACTIVE",
            actor_type="PLAYER", actor_id=str(player.pk), reward_gold=80, reserved_reward_gold=80, escrow_gold=30,
            opened_world_minute=100, expires_world_minute=200, created_request_id=uuid.uuid4())
        VillagePersonalCommissionEscrow.objects.create(commission=commission, condition_id="wood", item_id=1002,
            quantity=2, source_instance_id=uuid.uuid4(), batch_snapshot={"quality": 4})
        VillagePersonalCommissionReceipt.objects.create(commission=commission, actor_type="PLAYER", actor_id=str(player.pk),
            request_id=uuid.uuid4(), action="CONTRIBUTE", payload_hash="c"*64, response={}, world_minute=100)
        VillagePlayerAffairLabor.objects.create(player=player, commission=commission, authority_session_id=uuid.uuid4(),
            location_code="BILL_FARM", accumulated_minutes=12, last_world_minute=100, last_time_revision=2,
            last_seen_at=timezone.now())

        clock = GlobalGameTimeState.get_instance()
        clock.apply_world_minute(clock.world_minute + 14400)
        clock.time_revision = 123
        clock.save()
        VillageFarmPlot.objects.filter(plot_code="FARM_PLOT_D_01").update(lessee_player=player,
            leased_world_minute=clock.world_minute, lease_ends_world_minute=clock.world_minute+1440,
            lease_price_paid=50, lease_grace_minutes=2, lease_expiry_notified=True, plot_version=7,
            status="GROWING", crop_code="CORN", batch_id=uuid.uuid4(),
            planted_world_minute=clock.world_minute, mature_world_minute=clock.world_minute+500)
        VillageAffair.objects.filter(pk="FARM_IRRIGATION").update(status="COMPLETED", version=12)
        return player

    def test_full_reset_clears_protected_history_and_recreates_initial_world(self):
        player = self._create_current_progress()
        call_command("reset_village_world", "--confirm", "RESET_WUXI_VILLAGE", stdout=StringIO())
        for model in (VillageHuntAttempt, VillageWildlife, VillageWildlifeReceipt, NpcCommandState,
                      VillagePersonalCommission, VillagePersonalCommissionEscrow, VillagePersonalCommissionReceipt,
                      VillagePlayerAffairLabor, VillagePlayerActionReceipt, VillagePlayerContribution, EnvActionReceipt):
            self.assertFalse(model.objects.exists(), model.__name__)
        for code in ("BRIDGE_REPAIR", "FARM_IRRIGATION"):
            affair = VillageAffair.objects.get(pk=code)
            self.assertEqual(affair.status, "ACTIVE")
            self.assertFalse(any(affair.progress["conditions"].values()))
        clock = GlobalGameTimeState.get_instance()
        self.assertEqual((clock.game_year, clock.game_month, clock.game_day, clock.game_hour, clock.game_minute,
                          clock.time_revision), (2026, 3, 8, 7, 0, 0))
        plot = VillageFarmPlot.objects.get(plot_code="FARM_PLOT_D_01")
        self.assertEqual((plot.status, plot.crop_code, plot.plot_version, plot.lease_price_paid), ("EMPTY", "", 0, 0))
        self.assertIsNone(plot.lessee_player_id)
        self.assertIsNone(plot.leased_world_minute)
        self.assertIsNone(plot.lease_ends_world_minute)
        self.assertIsNone(plot.batch_id)
        self.assertFalse(plot.lease_expiry_notified)
        self.assertEqual(plot.lease_grace_minutes, VillageSimulationConfig.objects.get(config_key="default").farm_grace_minutes)
        player.refresh_from_db()
        self.assertEqual((player.password, player.appearance_revision), ("keep-password", 4))
        self.assertEqual(player.appearance, {"test": "keep-appearance"})
        self.assertEqual(
            NpcGlobalInventory.objects.get(npc__agent_id="10011").money,
            FORMAL_NPC_INITIAL_BALANCE,
        )
        # The whole reset is repeatable after the bridge and water canal have been rebuilt.
        call_command("reset_village_world", "--confirm", "RESET_WUXI_VILLAGE", stdout=StringIO())
        self.assertEqual(VillageAffair.objects.filter(pk__in=("BRIDGE_REPAIR", "FARM_IRRIGATION")).count(), 2)

    def test_player_assets_progress_and_pending_income_return_to_defaults(self):
        player = self._create_current_progress()
        point = CollectionPoint.objects.create(collect_type="reset-point", name="test")
        PlayerCollectionPoint.objects.create(player=player, collection_point=point, current_level=8)
        PityCounter.objects.create(player=player, count_6=90)
        PlayerRestaurantState.objects.create(player=player, current_level=8, total_gold_earned=400)
        MenuItem.objects.create(player=player, instance_id=str(uuid.uuid4()), item_id=1002, item_name="old", quantity=20)
        daily = DailyBusinessLog.objects.create(player=player)
        sale = CustomerTransaction.objects.create(player=player, daily_log=daily, game_day=3, customer_name="old",
                                                   item_id=1002, item_name="old")
        hour = RestaurantHourlySlot.objects.create(player=player, game_year=2026, game_month=3, game_day=9, game_hour=10)
        RestaurantSaleEvent.objects.create(slot=hour, scheduled_minute=20, transaction=sale)
        PendingReward.objects.create(player_id=str(player.pk), reward_id="old-reward", rewards=[{"gold": 999}])
        ReportEvent.objects.create(player_id=str(player.pk), event_type="old")
        LeaderboardEntry.objects.create(character_id=str(player.pk), board_type="total_wealth", season_id="old", score=9999)

        call_command("reset_village_world", "--confirm", "RESET_WUXI_VILLAGE", stdout=StringIO())
        state = PlayerState.objects.get(player_id=str(player.pk))
        self.assertEqual((state.action_points, state.action_points_max, state.current_season_id), (200, 1000, ""))
        self.assertEqual(list(state.recipes.values_list("recipe_name", flat=True)), ["reset-default-recipe"])
        self.assertFalse(state.titles.exists())
        bag = InventoryBag.objects.get(player=player, character_id=player.username)
        self.assertEqual(bag.money, 123)
        slot = bag.slots.get()
        self.assertEqual((slot.item_id, slot.item_amount, slot.remaining_uses, slot.hide_in_shop), (4001, 1, 20, True))
        for model in (PlayerCollectionPoint, PityCounter, PlayerRestaurantState, MenuItem, DailyBusinessLog,
                      CustomerTransaction, RestaurantHourlySlot, RestaurantSaleEvent, PendingReward, ReportEvent, LeaderboardEntry):
            self.assertFalse(model.objects.exists(), model.__name__)

    def test_dry_run_rolls_back_all_new_state_and_player_assets(self):
        self._create_current_progress()

        def snapshot():
            with connection.cursor() as cursor:
                result = {}
                for table in connection.introspection.table_names():
                    cursor.execute(f"SELECT * FROM {connection.ops.quote_name(table)}")
                    result[table] = sorted(cursor.fetchall(), key=repr)
                return result

        before = snapshot()
        call_command("reset_village_world", "--dry-run", stdout=StringIO())
        self.assertEqual(snapshot(), before)

    def test_missing_player_template_fails_before_mutating_progress(self):
        player = self._create_current_progress()
        BagTemplate.objects.filter(character_id="player").delete()
        with self.assertRaisesMessage(CommandError, "player 背包模板"):
            call_command("reset_village_world", "--confirm", "RESET_WUXI_VILLAGE", stdout=StringIO())
        self.assertEqual(InventoryBag.objects.get(player=player).money, 9999)
        self.assertTrue(VillageHuntAttempt.objects.exists())
        self.assertTrue(VillageFarmPlot.objects.filter(lessee_player=player).exists())

    @override_settings(DEPLOYMENT_ENV="production")
    def test_production_rejects_apply_and_dry_run(self):
        for arguments in (("--confirm", "RESET_WUXI_VILLAGE"), ("--dry-run",)):
            with self.assertRaisesMessage(CommandError, "正式环境禁止"):
                call_command("reset_village_world", *arguments, stdout=StringIO())

    def test_confirmation_and_dry_run_are_safe(self):
        with self.assertRaises(CommandError):
            call_command("reset_village_world", stdout=StringIO())

        bag = NpcGlobalInventory.objects.get(npc__agent_id="10011")
        bag.money = 777
        bag.save(update_fields=["money"])
        call_command("reset_village_world", "--dry-run", stdout=StringIO())
        bag.refresh_from_db()
        self.assertEqual(bag.money, 777)

    def test_restore_npc_needs_only_updates_the_five_runtime_rows(self):
        bill = NpcRuntimeState.objects.get(npc__agent_id="10011")
        bill.current_location_code = "GENERAL_STORE"
        bill.hunger_fp = 999
        bill.energy_fp = 1
        bill.health_fp = 2
        bill.social_fp = 998
        bill.stress_fp = 997
        bill.state_version = 7
        bill.save()

        call_command("restore_npc_needs", stdout=StringIO())

        bill.refresh_from_db()
        clock = GlobalGameTimeState.get_instance()
        self.assertEqual(
            (bill.hunger_fp, bill.energy_fp, bill.health_fp, bill.social_fp, bill.stress_fp),
            (0, 1000, 1000, 0, 0),
        )
        self.assertEqual((bill.current_location_code, bill.state_version), ("GENERAL_STORE", 8))
        self.assertEqual((bill.needs_world_minute, bill.time_revision), (clock.world_minute, clock.time_revision))
