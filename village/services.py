from inventory.village_policy import FORMAL_VILLAGE_NPC_IDS

from .models import NpcProfile, VillageLocationDefinition, VillageSimulationConfig


FLAG_FIELDS = (
    "village_simulation_enabled",
    "village_needs_enabled",
    "village_economy_enabled",
    "village_social_enabled",
    "village_story_enabled",
    "village_hub_command_adapter_enabled",
    "village_offline_catchup_enabled",
    "village_debug_overlay_enabled",
)


def validate_bootstrap():
    errors = []
    profiles = list(NpcProfile.objects.select_related("npc").filter(is_active=True))
    profile_ids = {profile.npc.agent_id for profile in profiles}
    if profile_ids != FORMAL_VILLAGE_NPC_IDS:
        errors.append(
            "正式 NPC 集合不匹配: expected="
            + ",".join(sorted(FORMAL_VILLAGE_NPC_IDS))
            + " actual="
            + ",".join(sorted(profile_ids))
        )

    active_locations = set(
        VillageLocationDefinition.objects.filter(is_active=True).values_list(
            "location_code", flat=True
        )
    )
    for profile in profiles:
        for field_name in (
            "home_location_code",
            "work_location_code",
            "social_location_code",
        ):
            code = getattr(profile, field_name)
            if code not in active_locations:
                errors.append(f"{profile.npc.agent_id}.{field_name}={code} 不存在")
        if not hasattr(profile.npc, "global_inventory"):
            errors.append(f"{profile.npc.agent_id} 缺少全局库存")
        template = profile.npc.templates.first()
        if template is None or not template.seed_only:
            errors.append(f"{profile.npc.agent_id} 缺少 seed_only 模板")

    config = VillageSimulationConfig.objects.filter(config_key="default").first()
    if config is None:
        errors.append("缺少 default VillageSimulationConfig")
    elif {
        field for field in FLAG_FIELDS if getattr(config, field)
    } != {"village_simulation_enabled", "village_hub_command_adapter_enabled"}:
        errors.append("阶段零要求所有自治 Feature Flag 保持关闭")

    return {
        "valid": not errors,
        "resultCode": "VILLAGE_BOOTSTRAP_VALID" if not errors else "VILLAGE_BOOTSTRAP_INVALID",
        "errors": errors,
        "npcCount": len(profile_ids),
        "locationCount": len(active_locations),
    }
