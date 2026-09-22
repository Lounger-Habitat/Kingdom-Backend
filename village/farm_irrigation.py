"""Permanent farm improvement, using the shared resource-affair reducer."""
from django.db import transaction

from .models import VillageAffair, VillageLocationDefinition, VillagePlayerAffairLabor

IRRIGATION_AFFAIR_CODE = "FARM_IRRIGATION"
GROWTH_REDUCTION_PERCENT = 10
COMPLETION_EFFECT = "农田水渠恢复通水；Bill 和玩家新播种的作物生长时间缩短 10%，已播种作物不变。"
WORKSITES = (
    ("FARM_CANAL_WEST", "水渠·西渠清淤点", (-46.7, 0, -71.0)),
    ("FARM_CANAL_SOUTH_WEST", "水渠·南渠西段清淤点", (-38.5, 0, -74.2)),
    ("FARM_CANAL_SOUTH_EAST", "水渠·南渠东段清淤点", (-22.5, 0, -74.2)),
    ("FARM_CANAL_EAST", "水渠·东渠清淤点", (-16.5, 0, -61.0)),
    ("FARM_CANAL_MATERIALS", "水渠·材料堆", (-47.2, 0, -61.3)),
    ("FARM_CANAL_GATE", "水渠·闸门操作点", (-47.0, 0, -66.3)),
)
CLEAR_CONDITIONS = ("clear_west", "clear_south_west", "clear_south_east", "clear_east")
REQUIREMENTS = {"stages": [
    dict(id="clear_silt", title="分段清理淤泥", conditions=[
        dict(id=condition, type="labor", label=name.replace("水渠·", "").replace("点", ""), target=1,
             unit_minutes=30, location_code=location)
        for condition, (location, name, _) in zip(CLEAR_CONDITIONS, WORKSITES[:4])]),
    dict(id="reinforce", title="加固渠岸", location_code="FARM_CANAL_MATERIALS", conditions=[
        dict(id="canal_wood", type="item", label="加固木材", item_ids=[1002], target=30),
        dict(id="canal_gold", type="gold", label="水渠建设资金", target=100)]),
    dict(id="open_water", title="开闸通水", location_code="FARM_CANAL_GATE", conditions=[
        dict(id="water_labor", type="labor", label="操作闸门通水", target=1, unit_minutes=30)]),
]}


@transaction.atomic
def ensure_irrigation_affair(world_minute):
    from .affairs import create_resource_affair, normalize_definition, condition_progress, _legacy_totals
    from game_time.models import GlobalGameTimeState
    # Match settlement's clock -> affair/labor lock order during a live upgrade.
    GlobalGameTimeState.objects.select_for_update().filter(pk=1).first()
    for code, name, position in WORKSITES:
        VillageLocationDefinition.objects.update_or_create(location_code=code, defaults=dict(
            display_name=name, center_anchor_code="LOC_"+code, location_type="work",
            bounds={"center": list(position)}, opening_hours={"alwaysOpen": True},
            supported_action_types=["CONTRIBUTE_TO_AFFAIR"], capacity=4, is_public=True, is_active=True))
    locations = [site[0] for site in WORKSITES]
    row = create_resource_affair(IRRIGATION_AFFAIR_CODE, "疏通农田水渠", REQUIREMENTS, locations, world_minute)
    requirements = normalize_definition(REQUIREMENTS, locations)
    if row.requirements == requirements and row.locations == locations:
        return row
    # Upgrade the shipped aggregate labor without erasing earned contributions.
    progress = condition_progress(row)
    old_clear = min(4, max(0, progress.pop("clear_labor", 0)))
    for index, condition in enumerate(CLEAR_CONDITIONS):
        progress[condition] = min(1, max(progress.get(condition, 0), int(index < old_clear)))
    progress["water_labor"] = min(1, progress.get("water_labor", 0))
    for stage in requirements["stages"]:
        for condition in stage["conditions"]:
            if row.status == "COMPLETED":
                progress[condition["id"]] = condition["target"]
    completed = all(progress.get(c["id"], 0) >= c["target"] for s in requirements["stages"] for c in s["conditions"])
    was_completed = row.status == "COMPLETED"
    row.requirements, row.locations = requirements, locations
    row.progress = {"conditions": progress, **_legacy_totals(requirements, progress)}
    row.status = "COMPLETED" if completed else "ACTIVE"
    if completed and row.completed_world_minute is None:
        row.completed_world_minute = world_minute
    row.version += 1
    row.save(update_fields=["requirements", "locations", "progress", "status", "completed_world_minute", "version"])
    VillagePlayerAffairLabor.objects.filter(affair=row, status="ACTIVE").update(
        status="CANCELLED", stop_reason="CONDITION_CHANGED", accumulated_minutes=0)
    if completed and not was_completed:
        import uuid
        from .affairs import _event
        _event(row, world_minute, uuid.uuid4())
    return row


def project_irrigation():
    """Read only; safe for both public world snapshots and private farm views."""
    from .affairs import active_stage, condition_progress
    row = VillageAffair.objects.filter(pk=IRRIGATION_AFFAIR_CODE).first()
    completed = row is not None and row.status == "COMPLETED"
    stage = active_stage(row) if row else None
    stage_id = stage["id"] if stage else None
    progress = condition_progress(row) if row else {}
    return dict(affair_id=IRRIGATION_AFFAIR_CODE, status=row.status if row else "UNAVAILABLE",
                active_stage_id=stage_id, current_stage_title=stage["title"] if stage else "已通水" if completed else "尚未开放",
                visual_state="FLOWING" if completed else {"reinforce": "CLEARED", "open_water": "CONSTRUCTION"}.get(stage_id, "SILTED"),
                growth_reduction_percent=GROWTH_REDUCTION_PERCENT if completed else 0,
                cleared_sections=[key for key in CLEAR_CONDITIONS if completed or progress.get(key, 0) >= 1],
                completion_effect=COMPLETION_EFFECT, version=row.version if row else 0)


def effective_growth_minutes(base_minutes, irrigation=None):
    if irrigation is None:
        completed = VillageAffair.objects.filter(pk=IRRIGATION_AFFAIR_CODE, status="COMPLETED").exists()
    else:
        completed = irrigation.get("status") == "COMPLETED"
    # Round up to a whole game minute and never accelerate an existing batch.
    return max(1, (base_minutes * (100 - GROWTH_REDUCTION_PERCENT) + 99) // 100) if completed else base_minutes
