from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from game_time.models import GlobalGameTimeState
from village.models import NpcRuntimeState
from village.seed_data import FORMAL_NPC_AGENT_IDS


NEED_DEFAULTS = {
    "hunger_fp": 0,
    "energy_fp": 1000,
    "health_fp": 1000,
    "social_fp": 0,
    "stress_fp": 0,
    "hunger_remainder": 0,
    "energy_remainder": 0,
    "health_remainder": 0,
    "social_remainder": 0,
    "stress_remainder": 0,
}


class Command(BaseCommand):
    help = "将五名正式 NPC 的需求恢复到最佳状态（不修改库存、关系、剧情、位置或动作）"

    @transaction.atomic
    def handle(self, *args, **options):
        rows = list(
            NpcRuntimeState.objects.select_for_update()
            .filter(npc__agent_id__in=FORMAL_NPC_AGENT_IDS)
            .select_related("npc")
            .order_by("npc__agent_id")
        )
        found = {row.npc.agent_id for row in rows}
        missing = sorted(set(FORMAL_NPC_AGENT_IDS) - found)
        if missing:
            raise CommandError(f"正式 NPC 运行状态不完整: missing={missing}")

        clock = GlobalGameTimeState.get_instance()
        fields = [*NEED_DEFAULTS, "needs_world_minute", "time_revision", "state_version", "updated_at"]
        for row in rows:
            for field, value in NEED_DEFAULTS.items():
                setattr(row, field, value)
            row.needs_world_minute = clock.world_minute
            row.time_revision = clock.time_revision
            row.state_version += 1
            row.save(update_fields=fields)
            self.stdout.write(
                f"{row.npc.agent_id} {row.npc.name}: hunger=0 energy=100 health=100 social=0 stress=0 v{row.state_version}"
            )

        self.stdout.write(self.style.SUCCESS(f"已恢复 {len(rows)} 名 NPC；请重启 Env Server 载入新状态。"))
