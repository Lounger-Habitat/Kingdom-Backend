from io import StringIO
import random
import uuid

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from account.models import GameAccount
from autobuy.models import AutoBuyConfig, AutoBuyPurchase
from cooking.models import PityCounter
from game_time.models import GlobalGameTimeState
from inventory.models import (
    BagTemplate, EnvActionReceipt, InventoryBag, InventorySlot,
    NpcDefinition, NpcGlobalInventory, PlayerCollectionPoint,
)
from items.models import Item
from leaderboard.models import LeaderboardEntry, PendingReward, ReportEvent
from marketplace.models import MarketListing, MarketTransaction
from player.models import DefaultRecipe, PlayerRecipe, PlayerState
from recipes.models import Recipe
from restaurant.models import (
    CustomerTransaction, DailyBusinessLog, MenuItem, PlayerRestaurantState,
    RestaurantHourlySlot,
)
from village.management.commands.seed_village_v1 import Command as SeedVillageCommand
from village.models import (
    DailyPriceSnapshot,
    EconomicLedgerEntry,
    MarkDailySalary,
    VillageProjectReview,
    VillagePublicDecision,
    NpcActionReservation,
    NpcCommandState,
    NpcCommitment,
    NpcCookingCounter,
    NpcDialogueSession,
    NpcNeedsDaySummary,
    NpcRelationship,
    NpcRelationshipChange,
    NpcRuntimeState,
    NpcSocialDailyCounter,
    NpcStoryFactAccess,
    NpcSubjectiveGoalState,
    NpcDailyRecord,
    NpcTradeDelivery,
    NpcTradeOrder,
    PlayerStoryFactAccess,
    VillageAccount,
    VillageAffair,
    VillageAffairContribution,
    VillagePlayerAffairLabor,
    VillagePlayerActionReceipt,
    VillagePlayerContribution,
    VillagePersonalCommission,
    VillagePersonalCommissionEscrow,
    VillagePersonalCommissionReceipt,
    VillageHuntAttempt,
    VillageWildlife,
    VillageWildlifeReceipt,
    VillageActionReceipt,
    VillageCommunityMealDailyUsage,
    VillageDeliveryCursor,
    VillageDeliveryOutbox,
    VillageDishReview,
    VillageFarmPlot,
    VillageRestaurantState,
    VillageRouteAttempt,
    VillageCaravanRequest,
    VillageCaravanVisit,
    VillageSimulationConfig,
    VillageStore,
    VillageStoreAccount,
    VillageStoreInventorySlot,
    VillageStoreListing,
    VillageStoryArc,
    VillageStoryFact,
    VillageStoryMeeting,
    VillageStoryTransitionReceipt,
    VillageStoryVote,
    VillageTradeOffer,
    VillageWorldEvent,
)
from village.phase4_visibility import enqueue_event_deliveries
from village.phase3_catalog import BASE_PRICES, FARM_CROP_RULES
from village.phase3_contract import ListingStatus, PriceMode
from village.phase5_contract import ARC_CODE
from village.seed_data import (
    FARM_PLOT_SEEDS,
    FORMAL_NPC_AGENT_IDS,
    INITIAL_SUPPLEMENTARY_GRANTS,
    INITIAL_TOOL_GRANTS,
    NPC_SEEDS,
)


CONFIRMATION = "RESET_WUXI_VILLAGE"
START_TIME = (2026, 3, 8, 7, 0)
FORMAL_NPC_INITIAL_BALANCE = 2000
GENERAL_STORE_INITIAL_BALANCE = 10000
PUBLIC_FUND_INITIAL_BALANCE = 2000
GENERAL_STORE_INITIAL_STOCK = (
    (1049, 5, None),  # 盐
    (1050, 5, None),  # 糖
    (1099, 5, None),  # 醋
    (4001, 2, 20),    # 农具（满耐久）
    (4002, 2, 20),    # 猎具（满耐久）
)


class Command(BaseCommand):
    help = "仅开发环境：重置雾溪村、租约、时间及全部玩家进度；保留账号、外观和配置"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="执行完整重置但回滚所有写入")
        parser.add_argument("--confirm", default="", help=f"实际执行必须传入 {CONFIRMATION}")

    def handle(self, *args, **options):
        if settings.DEPLOYMENT_ENV in {"production", "prod"}:
            raise CommandError("正式环境禁止重置村庄")
        if not options["dry_run"] and options["confirm"] != CONFIRMATION:
            raise CommandError(f"实际执行必须传入 --confirm {CONFIRMATION}")

        self._preflight()
        with transaction.atomic():
            cleared = self._reset()
            if options["dry_run"]:
                transaction.set_rollback(True)

        mode = "DRY-RUN（已回滚）" if options["dry_run"] else "APPLIED"
        self.stdout.write(self.style.SUCCESS(
            f"reset_village_world {mode}: cleared={cleared}, npcs={len(FORMAL_NPC_AGENT_IDS)}"
        ))

    @staticmethod
    def _preflight():
        expected = set(FORMAL_NPC_AGENT_IDS)
        actual = set(NpcDefinition.objects.filter(agent_id__in=expected).values_list("agent_id", flat=True))
        if actual != expected:
            raise CommandError(f"正式 NPC 不完整: missing={sorted(expected - actual)}")
        if NpcGlobalInventory.objects.filter(npc__agent_id__in=expected).count() != len(expected):
            raise CommandError("五名正式 NPC 的全局库存不完整，请先修复 Bootstrap")
        if not VillageSimulationConfig.objects.filter(config_key="default").exists():
            raise CommandError("缺少 default VillageSimulationConfig")
        if VillageStore.objects.filter(store_code__in={"GENERAL_STORE", "RESTAURANT_STORE"}).count() != 2:
            raise CommandError("正式商店定义不完整")
        if VillageStoreAccount.objects.filter(store_id__in={"GENERAL_STORE", "RESTAURANT_STORE"}).count() != 2:
            raise CommandError("正式商店账户不完整")
        if VillageFarmPlot.objects.filter(
            plot_code__in={seed["plot_code"] for seed in FARM_PLOT_SEEDS}
        ).count() != len(FARM_PLOT_SEEDS):
            raise CommandError("正式农田定义不完整")

        required_items = {
            item_id for seed in NPC_SEEDS for item_id, _ in seed["items"]
        }
        required_items.update(item_id for _, item_id, _, _ in INITIAL_TOOL_GRANTS)
        required_items.update(item_id for _, item_id, _ in INITIAL_SUPPLEMENTARY_GRANTS)
        required_items.update(item_id for item_id, _, _ in GENERAL_STORE_INITIAL_STOCK)
        missing = required_items - set(Item.objects.filter(item_id__in=required_items).values_list("item_id", flat=True))
        if missing:
            raise CommandError(f"缺少正式 Seed Item: {sorted(missing)}")
        if GameAccount.objects.exists() and BagTemplate.objects.filter(character_id="player", npc__isnull=True).count() != 1:
            raise CommandError("完整重置玩家需要唯一的 player 背包模板")

    def _reset(self):
        GlobalGameTimeState.get_instance()
        GlobalGameTimeState.objects.select_for_update().get(pk=1)
        cleared = 0
        formal_npc_ids = tuple(FORMAL_NPC_AGENT_IDS)

        # Player inventories are reset too. Keep only unrelated test-NPC/system listings.
        for queryset in (
            MarketTransaction.objects.filter(
                Q(seller_npc__agent_id__in=formal_npc_ids)
                | Q(buyer_npc__agent_id__in=formal_npc_ids)
                | Q(seller__isnull=False) | Q(buyer__isnull=False)
            ),
            MarketListing.objects.filter(
                Q(seller_npc__agent_id__in=formal_npc_ids) | Q(seller__isnull=False)),
            VillageDishReview.objects.filter(owner__agent_id__in=formal_npc_ids),
            NpcCookingCounter.objects.filter(npc__agent_id__in=formal_npc_ids),
        ):
            count = queryset.count()
            queryset.delete()
            cleared += count

        # Clear children that protect or feed the event/command history first.
        for model in (
            VillageWildlifeReceipt,
            VillageHuntAttempt,  # PROTECTs both wildlife and NPC command rows.
            VillageWildlife,
            VillagePlayerAffairLabor,
            VillagePersonalCommissionReceipt,
            VillagePersonalCommissionEscrow,
            VillagePersonalCommission,
            VillagePlayerActionReceipt,
            VillagePlayerContribution,
            VillageAffairContribution,
            VillageAffair,
            VillageCaravanRequest,
            VillageCaravanVisit,
            VillageDeliveryOutbox,
            VillageDeliveryCursor,
            VillageProjectReview,
            VillageStoreInventorySlot,
            VillageStoreListing,
            VillagePublicDecision,
            MarkDailySalary,
            VillageStoryVote,
            VillageStoryTransitionReceipt,
            VillageRouteAttempt,
            VillageStoryMeeting,
            VillageStoryFact,
            VillageStoryArc,
            VillageTradeOffer,
            NpcCommitment,
            NpcRelationshipChange,
            NpcRelationship,
            NpcStoryFactAccess,
            PlayerStoryFactAccess,
            EconomicLedgerEntry,
            NpcTradeDelivery,
            NpcTradeOrder,
            DailyPriceSnapshot,
            VillageRestaurantState,
            VillageCommunityMealDailyUsage,
            NpcSocialDailyCounter,
            NpcNeedsDaySummary,
            NpcDialogueSession,
            NpcActionReservation,
            VillageActionReceipt,
            NpcCommandState,
            VillageWorldEvent,
            NpcSubjectiveGoalState,
            NpcDailyRecord,
            NpcRuntimeState,
        ):
            count = model.objects.count()
            model.objects.all().delete()
            cleared += count

        # Escrow is discarded, not refunded: both sides receive fresh starting assets.
        cleared += self._reset_players()

        year, month, day, hour, minute = START_TIME
        time_state = GlobalGameTimeState.get_instance()
        time_state.game_year = year
        time_state.game_month = month
        time_state.game_day = day
        time_state.game_hour = hour
        time_state.game_minute = minute
        time_state.season = 0
        time_state.current_season_id = ""
        time_state.recalculate_world_minute()
        time_state.time_revision = 0
        time_state.save()

        VillageSimulationConfig.objects.filter(config_key="default").update(
            mark_daily_salary=100, mark_employed_from_minute=time_state.world_minute,
            action_epoch=uuid.uuid4())
        VillageAccount.objects.update(
            balance=0,
            asset_version=0,
            audit_opening_balance=0,
            audit_after_ledger_id=0,
            audit_baseline_kind="INITIAL",
            last_caravan_day=None,
            last_closed_day_index=time_state.world_minute // 1440 - 1,
        )
        VillageAccount.objects.filter(account_code="PUBLIC_FUND").update(
            balance=PUBLIC_FUND_INITIAL_BALANCE,
            audit_opening_balance=PUBLIC_FUND_INITIAL_BALANCE,
        )
        VillageFarmPlot.objects.update(
            lessee_player=None,
            leased_world_minute=None,
            lease_ends_world_minute=None,
            lease_price_paid=0,
            lease_expiry_notified=False,
            lease_grace_minutes=VillageSimulationConfig.objects.get(config_key="default").farm_grace_minutes,
            plot_version=0,
            status="EMPTY",
            batch_id=None,
            crop_code="",
            planted_world_minute=None,
            mature_world_minute=None,
            yield_seed=None,
            source_request_id=None,
        )
        farm_counts = self._seed_farm_plot_states(time_state.world_minute)
        VillageStore.objects.update(status="OPEN", inventory_version=0)
        VillageStoreAccount.objects.update(balance=0, account_version=0, daily_limits={}, limits_until_world_minute=None)
        VillageStoreAccount.objects.filter(store_id="GENERAL_STORE").update(
            balance=GENERAL_STORE_INITIAL_BALANCE
        )
        self._seed_general_store(time_state.world_minute)

        seed_command = SeedVillageCommand(stdout=StringIO())
        items = Item.objects.in_bulk({item_id for seed in NPC_SEEDS for item_id, _ in seed["items"]})
        for seed in NPC_SEEDS:
            npc = NpcDefinition.objects.get(agent_id=seed["agent_id"])
            bag = NpcGlobalInventory.objects.get(npc=npc)
            bag.money = FORMAL_NPC_INITIAL_BALANCE
            bag.asset_version = 0
            bag.save(update_fields=["money", "asset_version", "updated_at"])
            seed_command._replace_inventory_slots(bag, seed["items"], items)
            seed_command._ensure_phase_two_base_rations(npc, bag)
            seed_command._ensure_phase_two_runtime(npc)

        summary = {
            "tool_grants": 0,
            "supplement_grants": 0,
            "relationships": 0,
            "story_fact_access": 0,
            "phase4_action_configs": 0,
        }
        seed_command._seed_phase3_grants(summary)
        seed_command._seed_phase4_social(summary)
        from village.affairs import ensure_bridge_affair
        from village.farm_irrigation import ensure_irrigation_affair
        ensure_bridge_affair(time_state.world_minute)
        ensure_irrigation_affair(time_state.world_minute)
        # 二跑复盘（R6）：摘要只写重置那一刻真实存在的事实。
        # 剧情已改为独立事务，bootstrap_story 只读；不声称存在旧剧情阶段。
        reset_wallets = "、".join(
            f"{seed['display_name']} {FORMAL_NPC_INITIAL_BALANCE}" for seed in NPC_SEEDS
        )
        farm_codes = [seed["plot_code"] for seed in FARM_PLOT_SEEDS]
        reset_summary = (
            f"村庄世界已重置。当前日历 {time_state.game_year:04d}-{time_state.game_month:02d}-"
            f"{time_state.game_day:02d} {time_state.game_hour:02d}:{time_state.game_minute:02d}。"
            f"农田 {len(farm_codes)} 块（含 {farm_codes[0]}）："
            f"{farm_counts['empty']} 块空置（其中 {farm_counts['leasable']} 块可租）、{farm_counts['half_grown']} 块生长至一半、"
            f"{farm_counts['mature']} 块可收获，已种作物随机；"
            f"GENERAL_STORE 与 RESTAURANT_STORE 营业中；"
            f"公共基金 {PUBLIC_FUND_INITIAL_BALANCE}；个人钱包恢复初始值：{reset_wallets}。"
            f"交易、关系、剧情进度已回初始状态；以当前世界状态为准，勿沿用旧记忆中的日期与库存。"
        )[:256]
        reset_event = VillageWorldEvent.objects.create(
            dedupe_key="village-world-reset",
            event_type="VILLAGE_WORLD_RESET",
            world_minute=time_state.world_minute,
            time_revision=time_state.time_revision,
            participant_npc_ids=list(FORMAL_NPC_AGENT_IDS),
            location_code="VILLAGE_SQUARE",
            request_id=uuid.uuid5(uuid.NAMESPACE_URL, "village-world-reset"),
            effects={},
            visibility="PUBLIC",
            public_data={
                "story_id": ARC_CODE,
                "story_version": 0,
                "change_type": "RESET",
                "summary": reset_summary,
            },
            hub_summary=reset_summary,
            player_summary=reset_summary,
        )
        enqueue_event_deliveries(reset_event)
        return cleared

    @staticmethod
    def _reset_players():
        """Reset progress through current templates, without minting money-change reports."""
        cleared = 0
        for model in (
            EnvActionReceipt, AutoBuyPurchase, RestaurantHourlySlot,
            CustomerTransaction, DailyBusinessLog, MenuItem, PlayerRestaurantState,
            PlayerCollectionPoint, PityCounter, InventoryBag, PlayerState,
            PendingReward, ReportEvent, LeaderboardEntry,
        ):
            cleared += model.objects.count()
            model.objects.all().delete()
        AutoBuyConfig.objects.update(fund=0)

        template = BagTemplate.objects.filter(character_id="player", npc__isnull=True).first()
        template_slots = list(template.slots.select_related("item")) if template else []
        default_recipes = list(DefaultRecipe.objects.values_list("recipe_name", flat=True))
        catalogs = dict(Recipe.objects.filter(recipe_name__in=default_recipes).values_list("recipe_name", "pk"))
        for account in GameAccount.objects.all():
            state = PlayerState.objects.create(player_id=str(account.pk))
            PlayerRecipe.objects.bulk_create([
                PlayerRecipe(player=state, recipe_name=name, catalog_recipe_id=catalogs.get(name))
                for name in default_recipes
            ])
            # bulk_create deliberately bypasses InventoryBag.save's leaderboard side effects.
            bag = InventoryBag(player=account, character_id=account.username, money=template.money)
            InventoryBag.objects.bulk_create([bag])
            for slot in template_slots:
                InventorySlot.objects.create(
                    bag=bag, slot_index=slot.slot_index,
                    item_id=slot.item_id or 0, item_amount=slot.item_amount,
                    item_bag_name=f"{slot.item.item_name}_{slot.slot_index}" if slot.item else "",
                    rated=slot.rated, rating_price=slot.rating_price, overall_score=slot.overall_score,
                    ingredient=slot.ingredient, cook_time=slot.cook_time, dish_quality=slot.dish_quality,
                    review_comment=slot.review_comment, hide_in_shop=slot.hide_in_shop,
                    remaining_uses=slot.item.max_use_count if slot.item and (
                        slot.item.item_type == "Usable" or slot.item.item_type.endswith("Tool")) else None,
                )
        return cleared

    @staticmethod
    def _seed_general_store(world_minute):
        store = VillageStore.objects.get(store_code="GENERAL_STORE")
        operator = NpcDefinition.objects.get(agent_id="10015")

        for item_id, amount, remaining_uses in GENERAL_STORE_INITIAL_STOCK:
            listing = VillageStoreListing.objects.create(
                store=store,
                item_id=item_id,
                price_mode=PriceMode.ORIGINAL.value,
                unit_price=BASE_PRICES[item_id],
                status=ListingStatus.ACTIVE.value,
                listed_by_npc=operator,
                created_request_id=uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"village-reset:general-store-listing:{item_id}",
                ),
                listed_world_minute=world_minute,
            )
            slot = VillageStoreInventorySlot(
                store=store,
                listing=listing,
                item_id=item_id,
                item_amount=amount,
                source_npc=operator,
            )
            if remaining_uses is not None:
                slot.remaining_uses = remaining_uses
            slot.save()

    @staticmethod
    def _seed_farm_plot_states(world_minute):
        """Randomly split formal plots between empty, half-grown, and harvestable."""
        farm_codes = [seed["plot_code"] for seed in FARM_PLOT_SEEDS]
        plots = list(
            VillageFarmPlot.objects.filter(plot_code__in=farm_codes, lessee_player__isnull=True, lease_enabled=False).order_by("plot_code")
        )
        random.shuffle(plots)

        empty_count = len(plots) // 3
        half_grown_count = len(plots) // 3
        for index, plot in enumerate(plots[empty_count:], start=empty_count):
            crop_code = random.choice(tuple(FARM_CROP_RULES))
            crop_rule = FARM_CROP_RULES[crop_code]
            growth_minutes = crop_rule["growth_minutes"]
            elapsed_minutes = (
                growth_minutes // 2
                if index < empty_count + half_grown_count
                else growth_minutes
            )

            plot.status = "GROWING"
            plot.batch_id = uuid.uuid4()
            plot.crop_code = crop_code
            plot.planted_world_minute = world_minute - elapsed_minutes
            plot.mature_world_minute = plot.planted_world_minute + growth_minutes
            plot.yield_seed = random.randint(
                crop_rule["yield_min"], crop_rule["yield_max"]
            )
            plot.source_request_id = uuid.uuid4()

        planted_plots = plots[empty_count:]
        VillageFarmPlot.objects.bulk_update(
            planted_plots,
            [
                "status",
                "batch_id",
                "crop_code",
                "planted_world_minute",
                "mature_world_minute",
                "yield_seed",
                "source_request_id",
            ],
        )
        leasable_count = VillageFarmPlot.objects.filter(plot_code__in=farm_codes, lease_enabled=True).count()
        return {
            "empty": empty_count + leasable_count,
            "leasable": leasable_count,
            "half_grown": half_grown_count,
            "mature": len(plots) - empty_count - half_grown_count,
        }
