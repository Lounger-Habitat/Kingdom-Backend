from io import StringIO
import uuid

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition, NpcGlobalInventory
from items.models import Item
from village.management.commands.seed_village_v1 import Command as SeedVillageCommand
from village.models import (
    DailyPriceSnapshot,
    EconomicLedgerEntry,
    NpcActionReservation,
    NpcCommandState,
    NpcCommitment,
    NpcNeedsDaySummary,
    NpcRelationship,
    NpcRelationshipChange,
    NpcRuntimeState,
    NpcSocialDailyCounter,
    NpcStoryFactAccess,
    NpcSubjectiveGoalState,
    NpcTradeDelivery,
    NpcTradeOrder,
    PlayerStoryFactAccess,
    VillageAccount,
    VillageActionReceipt,
    VillageCommunityMealDailyUsage,
    VillageDeliveryCursor,
    VillageDeliveryOutbox,
    VillageFarmPlot,
    VillageRestaurantState,
    VillageRouteAttempt,
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


class Command(BaseCommand):
    help = "仅开发环境：把雾溪村运行状态恢复到五名正式 NPC 的初始 Seed"

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
        missing = required_items - set(Item.objects.filter(item_id__in=required_items).values_list("item_id", flat=True))
        if missing:
            raise CommandError(f"缺少正式 Seed Item: {sorted(missing)}")

    def _reset(self):
        cleared = 0

        # Clear children that protect or feed the event/command history first.
        for model in (
            VillageDeliveryOutbox,
            VillageDeliveryCursor,
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
            VillageStoreInventorySlot,
            VillageStoreListing,
            DailyPriceSnapshot,
            VillageRestaurantState,
            VillageCommunityMealDailyUsage,
            NpcSocialDailyCounter,
            NpcNeedsDaySummary,
            NpcActionReservation,
            VillageActionReceipt,
            NpcCommandState,
            VillageWorldEvent,
            NpcSubjectiveGoalState,
            NpcRuntimeState,
        ):
            count = model.objects.count()
            model.objects.all().delete()
            cleared += count

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

        VillageAccount.objects.update(
            balance=0,
            asset_version=0,
            last_caravan_day=None,
            last_closed_day_index=time_state.world_minute // 1440 - 1,
        )
        VillageFarmPlot.objects.update(
            status="EMPTY",
            batch_id=None,
            crop_code="",
            planted_world_minute=None,
            mature_world_minute=None,
            yield_seed=None,
            source_request_id=None,
        )
        VillageStore.objects.update(status="OPEN", inventory_version=0)
        VillageStoreAccount.objects.update(balance=0, account_version=0)

        seed_command = SeedVillageCommand(stdout=StringIO())
        items = Item.objects.in_bulk({item_id for seed in NPC_SEEDS for item_id, _ in seed["items"]})
        for seed in NPC_SEEDS:
            npc = NpcDefinition.objects.get(agent_id=seed["agent_id"])
            bag = NpcGlobalInventory.objects.get(npc=npc)
            bag.money = seed["money"]
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
                "summary": "村庄世界已重置，请刷新相关剧情目录",
            },
            hub_summary="村庄世界已重置",
            player_summary="村庄世界已重置",
        )
        enqueue_event_deliveries(reset_event)
        return cleared
