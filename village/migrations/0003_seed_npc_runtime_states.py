# Data migration: seed default IDLE runtime states for 5 formal NPCs

from django.db import migrations


def seed_npc_runtime_states(apps, schema_editor):
    """为五名正式 NPC 创建默认 IDLE 运行时状态。"""
    NpcDefinition = apps.get_model("inventory", "NpcDefinition")
    NpcRuntimeState = apps.get_model("village", "NpcRuntimeState")

    # 正式 NPC ID
    formal_npc_ids = [10011, 10012, 10013, 10014, 10015]

    for npc_id in formal_npc_ids:
        try:
            npc = NpcDefinition.objects.get(agent_id=npc_id, is_test_only=False)
            NpcRuntimeState.objects.get_or_create(
                npc=npc,
                defaults={
                    "current_location_code": "VILLAGE_SQUARE",
                    "current_point_code": "VILLAGE_SQUARE",
                    "current_goal": "NONE",
                    "current_action": "",
                    "action_status": "IDLE",
                    "state_version": 0,
                    "last_simulated_world_minute": 0,
                    "time_revision": 0,
                },
            )
        except NpcDefinition.DoesNotExist:
            pass  # NPC 不存在时跳过


def reverse_seed(apps, schema_editor):
    """反向迁移：删除五名正式 NPC 的运行时状态。"""
    NpcDefinition = apps.get_model("inventory", "NpcDefinition")
    NpcRuntimeState = apps.get_model("village", "NpcRuntimeState")

    formal_npc_ids = [10011, 10012, 10013, 10014, 10015]
    NpcRuntimeState.objects.filter(npc__agent_id__in=formal_npc_ids).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("village", "0002_npcactionreservation_npccommandstate_npcruntimestate_and_more"),
        ("inventory", "0022_phase0_village_inventory_guards"),
    ]

    operations = [
        migrations.RunPython(seed_npc_runtime_states, reverse_seed),
    ]
