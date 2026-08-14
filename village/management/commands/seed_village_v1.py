import uuid

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from game_time.models import GlobalGameTimeState
from inventory.models import (
    BagTemplate,
    BagTemplateSlot,
    NpcDefinition,
    NpcGlobalInventory,
    NpcGlobalInventorySlot,
)
from items.models import Item
from village.models import (
    NpcProfile,
    NpcRuntimeState,
    VillageAccount,
    VillageFarmPlot,
    VillageLocationDefinition,
    VillageRestaurantState,
    VillageSimulationConfig,
)
from village.seed_data import (
    FARM_PLOT_SEEDS,
    INITIAL_SUPPLEMENTARY_GRANTS,
    INITIAL_TOOL_GRANTS,
    LOCATION_SEEDS,
    NPC_SEEDS,
    SIMULATION_DEFAULTS,
    TEST_NPC_ISOLATION,
    TOOL_ITEM_SEEDS,
    VILLAGE_ACCOUNT_SEEDS,
)


BASE_RATION_ITEM_ID = 29001
BASE_RATION_ITEM_TYPE = "食物"
BASE_RATION_INSTANCE_NAMESPACE = uuid.UUID("c1f4c9ec-b93a-4df3-8b0f-021c3c0f159f")


class Command(BaseCommand):
    help = "幂等初始化雾溪村阶段零配置、五名正式 NPC 与 seed_only 库存"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="执行完整校验但回滚所有写入")
        parser.add_argument(
            "--reset-demo-balances",
            action="store_true",
            help="仅本地开发允许：用正式 Seed 覆盖五名 NPC 的测试余额和槽位",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        reset_demo = options["reset_demo_balances"]
        if reset_demo and (
            not settings.DEBUG
            or settings.DEPLOYMENT_ENV in {"production", "prod"}
        ):
            raise CommandError("正式环境禁止 --reset-demo-balances")

        summary = {
            "locations": 0,
            "npcs": 0,
            "profiles": 0,
            "templates": 0,
            "inventories": 0,
            "preserved_nonempty": 0,
            "test_npcs": 0,
            "farm_plots": 0,
            "village_accounts": 0,
            "tool_items": 0,
            "tool_grants": 0,
            "supplement_grants": 0,
        }

        with transaction.atomic():
            self._seed_locations(summary)
            self._seed_config()
            self._seed_test_npc_isolation(summary)
            self._seed_npcs(summary, reset_demo=reset_demo)
            self._seed_test_npc_isolation(summary)
            summary["test_npcs"] = NpcDefinition.objects.filter(is_test_only=True).count()
            # Phase 3 seeds
            self._seed_tool_items(summary)
            self._seed_farm_plots(summary)
            self._seed_village_accounts(summary)
            self._seed_phase3_grants(summary)
            if dry_run:
                transaction.set_rollback(True)

        mode = "DRY-RUN（已回滚）" if dry_run else "APPLIED"
        self.stdout.write(
            self.style.SUCCESS(
                f"seed_village_v1 {mode}: "
                + ", ".join(f"{key}={value}" for key, value in summary.items())
            )
        )

    def _seed_locations(self, summary):
        for code, display_name, center, location_type, capacity in LOCATION_SEEDS:
            location, _ = VillageLocationDefinition.objects.update_or_create(
                location_code=code,
                defaults={
                    "display_name": display_name,
                    "scene_name": "WuxiVillage",
                    "center_anchor_code": f"LOC_{code}",
                    "location_type": location_type,
                    "bounds": {"center": list(center)},
                    "location_tags": [location_type],
                    "opening_hours": {"alwaysOpen": True},
                    "capacity": capacity,
                    "supported_action_types": [],
                    "allowed_occupations": [],
                    "required_story_states": [],
                    "is_public": location_type not in {"home"},
                    "is_active": True,
                },
            )
            location.full_clean()
            summary["locations"] += 1

    def _seed_config(self):
        VillageSimulationConfig.objects.update_or_create(
            config_key="default",
            defaults=SIMULATION_DEFAULTS,
        )

    def _seed_npcs(self, summary, reset_demo):
        required_item_ids = {
            item_id
            for seed in NPC_SEEDS
            for item_id, _amount in seed["items"]
        }
        items = Item.objects.in_bulk(required_item_ids)
        missing = sorted(required_item_ids - set(items))
        if missing:
            raise CommandError(f"缺少正式 Seed 依赖的 Item 配置: {missing}")

        for seed in NPC_SEEDS:
            npc, _ = NpcDefinition.objects.update_or_create(
                agent_id=seed["agent_id"],
                defaults={"name": seed["name"], "is_test_only": False},
            )
            summary["npcs"] += 1

            profile_defaults = {
                field: seed[field]
                for field in (
                    "display_name",
                    "occupation",
                    "home_location_code",
                    "work_location_code",
                    "social_location_code",
                    "personality_traits",
                    "personal_goal",
                    "capabilities",
                )
            }
            profile_defaults.update({"need_rates": {}, "is_active": True})
            profile, _ = NpcProfile.objects.update_or_create(
                npc=npc,
                defaults=profile_defaults,
            )
            profile.full_clean()
            profile.save()
            summary["profiles"] += 1

            template, template_created = BagTemplate.objects.get_or_create(
                npc=npc,
                defaults={
                    "character_id": seed["agent_id"],
                    "money": seed["money"],
                    "seed_only": True,
                    "is_test_only": False,
                },
            )
            template.character_id = seed["agent_id"]
            template.seed_only = True
            template.is_test_only = False
            template_has_assets = template.money != 0 or template.slots.filter(item_amount__gt=0).exists()
            if template_created or reset_demo or not template_has_assets:
                template.money = seed["money"]
                self._replace_template_slots(template, seed["items"], items)
            else:
                summary["preserved_nonempty"] += 1
            template.save()
            summary["templates"] += 1

            bag, bag_created = NpcGlobalInventory.objects.get_or_create(
                npc=npc,
                defaults={"money": seed["money"]},
            )
            bag_has_assets = bag.money != 0 or bag.slots.filter(item_amount__gt=0).exists()
            if bag_created or reset_demo or not bag_has_assets:
                bag.money = seed["money"]
                bag.save(update_fields=["money"])
                self._replace_inventory_slots(bag, seed["items"], items)
            else:
                summary["preserved_nonempty"] += 1
            self._ensure_phase_two_runtime(npc)
            self._ensure_phase_two_base_rations(npc, bag)
            summary["inventories"] += 1

    @staticmethod
    def _ensure_phase_two_runtime(npc):
        """Seed only a missing runtime; existing phase-one/phase-two state wins."""
        time_state = GlobalGameTimeState.get_instance()
        runtime, created = NpcRuntimeState.objects.get_or_create(
            npc=npc,
            defaults={
                "current_location_code": "VILLAGE_SQUARE",
                "current_point_code": "VILLAGE_SQUARE",
                "current_goal": "NONE",
                "current_action": "",
                "action_status": "IDLE",
                "state_version": 0,
                "last_simulated_world_minute": time_state.world_minute,
                "time_revision": time_state.time_revision,
                "hunger_fp": 200,
                "energy_fp": 800,
                "health_fp": 1000,
                "social_fp": 200,
                "stress_fp": 100,
                "hunger_remainder": 0,
                "energy_remainder": 0,
                "health_remainder": 0,
                "social_remainder": 0,
                "stress_remainder": 0,
                "needs_world_minute": time_state.world_minute,
            },
        )
        # A runtime may have been created after the phase-two cutover migration
        # but before the formal seed.  Cursor zero would replay the entire world
        # history minute-by-minute.  Align only that untouched sentinel; never
        # overwrite a non-zero phase-two cursor or any need value.
        if not created and runtime.needs_world_minute == 0 and time_state.world_minute > 0:
            runtime.needs_world_minute = time_state.world_minute
            runtime.time_revision = time_state.time_revision
            runtime.save(update_fields=["needs_world_minute", "time_revision", "updated_at"])

    @staticmethod
    def _base_ration_instance_id(agent_id, ordinal):
        return uuid.uuid5(
            BASE_RATION_INSTANCE_NAMESPACE,
            f"village.phase2.base-ration:{agent_id}:{ordinal}",
        )

    def _ensure_phase_two_base_rations(self, npc, bag):
        """Append, never replace, the four deterministic phase-two rations."""
        ration = Item.objects.filter(item_id=BASE_RATION_ITEM_ID).first()
        if ration is None:
            ration = Item.objects.create(
                item_id=BASE_RATION_ITEM_ID,
                item_name="村庄基础口粮",
                item_type=BASE_RATION_ITEM_TYPE,
                item_description="阶段二基础生活动作口粮",
                item_price=0,
                collectable=False,
            )
        elif ration.item_type != BASE_RATION_ITEM_TYPE or ration.is_deleted:
            raise CommandError("阶段二基础口粮 item 29001 定义不兼容")

        next_slot_index = bag.slots.order_by("-slot_index").values_list("slot_index", flat=True).first()
        next_slot_index = 0 if next_slot_index is None else next_slot_index + 1
        for ordinal in range(4):
            instance_id = self._base_ration_instance_id(npc.agent_id, ordinal)
            existing = NpcGlobalInventorySlot.objects.filter(instance_id=instance_id).first()
            if existing is not None:
                if existing.bag_id != bag.id:
                    raise CommandError("阶段二基础口粮确定性实例已属于其他库存")
                continue
            NpcGlobalInventorySlot.objects.create(
                bag=bag,
                slot_index=next_slot_index,
                instance_id=instance_id,
                item_id=BASE_RATION_ITEM_ID,
                item_amount=1,
                item_bag_name="村庄基础口粮",
                hide_in_shop=True,
            )
            next_slot_index += 1

    def _seed_test_npc_isolation(self, summary):
        """Clone legacy TestScent assets away from the five formal IDs once."""
        for source_id, test_id, test_name, fallback_money in TEST_NPC_ISOLATION:
            source_npc = NpcDefinition.objects.filter(agent_id=source_id).first()
            if source_npc is None:
                continue
            test_npc, _ = NpcDefinition.objects.update_or_create(
                agent_id=test_id,
                defaults={"name": test_name, "is_test_only": True},
            )
            source_template = source_npc.templates.first()
            target_template, _ = BagTemplate.objects.get_or_create(
                npc=test_npc,
                defaults={
                    "character_id": test_id,
                    "money": source_template.money if source_template else fallback_money,
                    "seed_only": False,
                    "is_test_only": True,
                },
            )
            target_template.character_id = test_id
            target_template.seed_only = False
            target_template.is_test_only = True
            target_template.save()
            if source_template and not target_template.slots.exists():
                for slot in source_template.slots.all():
                    BagTemplateSlot.objects.create(
                        template=target_template,
                        slot_index=slot.slot_index,
                        item=slot.item,
                        item_amount=slot.item_amount,
                        rated=slot.rated,
                        rating_price=slot.rating_price,
                        overall_score=slot.overall_score,
                        ingredient=slot.ingredient,
                        cook_time=slot.cook_time,
                        dish_quality=slot.dish_quality,
                        hide_in_shop=slot.hide_in_shop,
                        review_comment=slot.review_comment,
                    )

            source_bag = (
                NpcGlobalInventory.objects.filter(npc=source_npc).first()
            )
            target_bag, _ = NpcGlobalInventory.objects.get_or_create(
                npc=test_npc,
                defaults={"money": source_bag.money if source_bag else fallback_money},
            )
            if source_bag and not target_bag.slots.exists():
                for slot in source_bag.slots.all():
                    NpcGlobalInventorySlot.objects.create(
                        bag=target_bag,
                        slot_index=slot.slot_index,
                        item_id=slot.item_id,
                        item_amount=slot.item_amount,
                        rated=slot.rated,
                        rating_price=slot.rating_price,
                        overall_score=slot.overall_score,
                        ingredient=slot.ingredient,
                        item_bag_name=slot.item_bag_name,
                        cook_time=slot.cook_time,
                        dish_quality=slot.dish_quality,
                        hide_in_shop=slot.hide_in_shop,
                        remaining_uses=slot.remaining_uses,
                        review_comment=slot.review_comment,
                    )

    @staticmethod
    def _replace_template_slots(template, seed_items, items):
        template.save()
        template.slots.all().delete()
        BagTemplateSlot.objects.bulk_create(
            [
                BagTemplateSlot(
                    template=template,
                    slot_index=index,
                    item=items[item_id],
                    item_amount=amount,
                    hide_in_shop=False,
                )
                for index, (item_id, amount) in enumerate(seed_items)
            ]
        )

    @staticmethod
    def _replace_inventory_slots(bag, seed_items, items):
        bag.slots.all().delete()
        NpcGlobalInventorySlot.objects.bulk_create(
            [
                NpcGlobalInventorySlot(
                    bag=bag,
                    slot_index=index,
                    item_id=item_id,
                    item_amount=amount,
                    item_bag_name=f"{items[item_id].item_name}_{index}",
                    hide_in_shop=False,
                )
                for index, (item_id, amount) in enumerate(seed_items)
            ]
        )

    # ── Phase 3 seed methods ───────────────────────────────────────

    @staticmethod
    def _seed_tool_items(summary):
        """Idempotently create Phase 3 tool items (4001/4002).

        If the item already exists with different properties, raise
        CommandError to avoid silent overwrites.
        """
        for seed in TOOL_ITEM_SEEDS:
            item, created = Item.objects.get_or_create(
                item_id=seed["item_id"],
                defaults={
                    "item_name": seed["item_name"],
                    "item_type": seed["item_type"],
                    "item_description": seed["item_description"],
                    "item_price": seed["item_price"],
                    "collectable": seed["collectable"],
                },
            )
            if not created:
                # Validate existing item is compatible
                if item.item_type != seed["item_type"]:
                    raise CommandError(
                        f"工具 item {seed['item_id']} 已存在但类型不匹配: "
                        f"expected {seed['item_type']}, got {item.item_type}"
                    )
            summary["tool_items"] += 1

    @staticmethod
    def _seed_farm_plots(summary):
        """Idempotently create the three farm plots, all EMPTY."""
        for seed in FARM_PLOT_SEEDS:
            VillageFarmPlot.objects.get_or_create(
                plot_code=seed["plot_code"],
                defaults={"status": "EMPTY"},
            )
            summary["farm_plots"] += 1

    @staticmethod
    def _seed_village_accounts(summary):
        """Idempotently create village accounts (PUBLIC_FUND)."""
        time_state = GlobalGameTimeState.objects.filter(pk=1).first()
        current_day = max(0, int(time_state.world_minute) // 1440) if time_state else 0
        # A newly enabled economy starts at the current day boundary; it must not
        # replay every historical game-time day that existed before Phase 3.
        initial_last_closed_day = current_day - 1
        for seed in VILLAGE_ACCOUNT_SEEDS:
            VillageAccount.objects.get_or_create(
                account_code=seed["account_code"],
                defaults={
                    "balance": seed["balance"],
                    "last_closed_day_index": initial_last_closed_day,
                },
            )
            summary["village_accounts"] += 1

    def _seed_phase3_grants(self, summary):
        """Grant initial tools and supplementary items to NPCs.

        Only adds items if the NPC doesn't already have that tool/supplement.
        Uses a deterministic instance_id to ensure idempotency.
        """
        tool_items = Item.objects.in_bulk([s["item_id"] for s in TOOL_ITEM_SEEDS])
        grant_namespace = uuid.UUID("a3f7c9ec-b93a-4df3-8b0f-021c3c0f159f")

        # Tool grants
        for agent_id, tool_item_id, amount, uses in INITIAL_TOOL_GRANTS:
            if tool_item_id not in tool_items:
                continue
            npc = NpcDefinition.objects.filter(agent_id=agent_id).first()
            if npc is None:
                continue
            bag = NpcGlobalInventory.objects.filter(npc=npc).first()
            if bag is None:
                continue
            instance_id = uuid.uuid5(
                grant_namespace,
                f"village.phase3.tool:{agent_id}:{tool_item_id}",
            )
            existing = NpcGlobalInventorySlot.objects.filter(instance_id=instance_id).first()
            if existing is not None:
                summary["tool_grants"] += 1
                continue
            next_slot = bag.slots.order_by("-slot_index").values_list("slot_index", flat=True).first()
            next_slot = 0 if next_slot is None else next_slot + 1
            NpcGlobalInventorySlot.objects.create(
                bag=bag,
                slot_index=next_slot,
                instance_id=instance_id,
                item_id=tool_item_id,
                item_amount=amount,
                remaining_uses=uses,
                item_bag_name=f"{tool_items[tool_item_id].item_name}_tool",
                hide_in_shop=True,
            )
            summary["tool_grants"] += 1

        # Supplementary grants (seeds, ingredients)
        # Skip gracefully if items don't exist in the database (e.g. test env)
        sup_items_needed = {iid for _, iid, _ in INITIAL_SUPPLEMENTARY_GRANTS}
        sup_items = Item.objects.in_bulk(sup_items_needed)
        for agent_id, item_id, amount in INITIAL_SUPPLEMENTARY_GRANTS:
            if item_id not in sup_items:
                continue
            npc = NpcDefinition.objects.filter(agent_id=agent_id).first()
            if npc is None:
                continue
            bag = NpcGlobalInventory.objects.filter(npc=npc).first()
            if bag is None:
                continue
            instance_id = uuid.uuid5(
                grant_namespace,
                f"village.phase3.supplement:{agent_id}:{item_id}",
            )
            existing = NpcGlobalInventorySlot.objects.filter(instance_id=instance_id).first()
            if existing is not None:
                summary["supplement_grants"] += 1
                continue
            next_slot = bag.slots.order_by("-slot_index").values_list("slot_index", flat=True).first()
            next_slot = 0 if next_slot is None else next_slot + 1
            NpcGlobalInventorySlot.objects.create(
                bag=bag,
                slot_index=next_slot,
                instance_id=instance_id,
                item_id=item_id,
                item_amount=amount,
                item_bag_name=f"{sup_items[item_id].item_name}_supplement",
                hide_in_shop=True,
            )
            summary["supplement_grants"] += 1
