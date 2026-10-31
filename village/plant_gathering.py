"""Timed, shared world plants. Called inside the existing world/bag/receipt transaction."""
from datetime import timedelta
import uuid
import random

from django.db import models
from django.utils import timezone

from inventory.models import InventorySlot
from items.models import Item
from items.quality import production_quality, quality_rng
from .phase3_asset_service import deterministic_rng
from player.models import ItemGameplayDefinition, PlayerState, PlayerGrowthState, PlayerSkillProgress
from player.progression import growth_snapshot, settle_growth
from player.progression_rules import level_for_xp
from player.services import recover_action_points
from .models import VillagePlantNode, VillagePlayerGathering
from .plant_catalog import ACTIONS, NODES, PLANTS, TIME_REDUCTION, QUALITY_BONUS_CHANCE
from .production_tools import ToolError, resolve_tool, wear_tool
from player.equipment import stats_for


class PlantError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def reject(code, message):
    raise PlantError(code, message)


def identity(value):
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        reject("INVALID_PARAMS", "植物、工具或采摘记录无效。")


def level_for(player):
    if PlayerGrowthState.objects.filter(player=player, legacy_migration_version__gte=1).exists():
        return level_for_xp(PlayerSkillProgress.objects.filter(player=player, skill_code="gathering")
            .values_list("total_xp", flat=True).first() or 0)
    return next(s["level"] for s in growth_snapshot(player, read_only=True, compact=True)["skills"] if s["code"] == "gathering")


def live(attempt, now):
    return bool(attempt) and attempt.get("lease_until", 0) > now.timestamp()


def tool_data(player, bag, instance, minute):
    if not instance:
        return None, 0, {}
    try:
        slot = resolve_tool(player, bag, "targeted_gathering", instance, minute=minute)
    except ToolError as exc:
        reject(exc.code, exc.message)
    if slot.item_amount != 1:
        reject("TOOL_STACK_INVALID", "请使用独立的一件采集工具。")
    stats = stats_for(slot)
    tier = stats["tier"]
    if tier not in (1, 2, 3):
        reject("TOOL_UNAVAILABLE", "采集工具尚未配置。")
    return slot, tier, stats


def overview(player, bag, minute, *, node_id=None, level=None):
    """Read-only, including mature-state calculations; no regrowth writes."""
    now = timezone.now()
    own = VillagePlayerGathering.objects.filter(player=player).first()
    definitions = {node_id: NODES[node_id]} if node_id else NODES
    rows = {str(row.pk): row for row in VillagePlantNode.objects.filter(pk__in=definitions)}
    busy = {row.attempt.get("node_id"): row.attempt for row in VillagePlayerGathering.objects.exclude(attempt={})
            if live(row.attempt, now)}
    items = {row.pk: row for row in Item.objects.filter(
        pk__in={PLANTS[d["species"]]["item_id"] for d in definitions.values()}, is_deleted=False)}
    level = level_for(player) if level is None else level
    equipped = str(own.equipped_instance_id) if own and own.equipped_instance_id else None
    tier, tool_reason = 0, None
    equipped_stats = {}
    if equipped:
        try:
            _, tier, equipped_stats = tool_data(player, bag, equipped, minute)
        except PlantError as exc:
            tool_reason = exc.code
    nodes = []
    for current_id, definition in definitions.items():
        spec = PLANTS[definition["species"]]
        item = items.get(spec["item_id"])
        if not item:
            continue
        row = rows.get(current_id)
        ready = row is None or row.ready_world_minute <= minute
        occupied = current_id in busy
        nodes.append(dict(node_id=current_id, species=definition["species"], offset=definition["offset"],
            item_id=item.pk, title=item.item_name, model=spec["model"], location_code=definition.get("location_code", spec["location"]),
            required_level=spec["level"], quantity=spec["quantity"], quality_min=1, quality_max=6,
            action_cost=spec["action_cost"], gather_seconds=round(spec["seconds"]*(1-equipped_stats.get("gathering_time_reduction", 0)), 2),
            base_seconds=spec["seconds"], regrow_minutes=spec["regrow_minutes"],
            ready=ready, occupied=occupied, unlocked=level >= spec["level"],
            ready_world_minute=row.ready_world_minute if row else 0, revision=row.revision if row else 0))
    return dict(schema_version=1, nodes_partial=node_id is not None, revision=own.revision if own else 0, observed_at=now.timestamp(),
        observed_world_minute=minute, level=level, equipped_instance_id=equipped, tool_tier=tier,
        equipment_level=equipped_stats.get("level", 0),
        time_reduction=equipped_stats.get("gathering_time_reduction", 0), quality_bonus_chance=equipped_stats.get("quality_bonus_chance", 0), tool_reason=tool_reason,
        attempt={key: value for key, value in own.attempt.items() if key != "quality"} if own and live(own.attempt, now) else None, nodes=nodes)


def execute(player, bag, action, params, rid, location, minute, authority_session_id):
    allowed = {"node_id", "attempt_id"} if action == "collect" else ACTIONS[action]
    if set(params)-allowed:
        reject("INVALID_PARAMS", "植物采摘不能指定类别、产量或采集时间。")
    now = timezone.now()
    state, _ = VillagePlayerGathering.objects.select_for_update().get_or_create(player=player)
    if action == "equip_gathering_tool":
        if live(state.attempt, now):
            reject("GATHERING_ACTIVE", "采摘中暂不能更换工具。")
        instance = params.get("instance_id")
        if instance is not None:
            instance = identity(instance)
            tool_data(player, bag, instance, minute)
        state.equipped_instance_id = instance
        state.attempt = {}
        state.revision += 1
        state.save()
        return dict(equipped_instance_id=instance)
    if action == "plant_cancel":
        if identity(params.get("attempt_id")) != state.attempt.get("attempt_id"):
            reject("PLANT_ATTEMPT_INVALID", "这次采摘已经结束。")
        node_id = state.attempt.get("node_id")
        state.attempt = {}
        state.revision += 1
        state.save()
        return dict(cancelled=True, node_id=node_id)

    node_id = identity(params.get("node_id"))
    definition = NODES.get(node_id)
    if definition is None:
        reject("PLANT_NOT_FOUND", "没有找到这株植物。")
    spec = PLANTS[definition["species"]]
    if location != definition.get("location_code", spec["location"]):
        reject("LOCATION_INVALID", "请靠近这株植物。")
    if not isinstance(authority_session_id, str) or not authority_session_id:
        reject("SESSION_REQUIRED", "等待角色会话同步。")
    level = level_for(player)
    if level < spec["level"]:
        reject("SKILL_LEVEL_REQUIRED", f"需要采集{spec['level']}级。")
    item = Item.objects.filter(pk=spec["item_id"], is_deleted=False).first()
    if not item:
        reject("PLANT_UNAVAILABLE", "这种植物的产物尚未配置。")
    node, _ = VillagePlantNode.objects.select_for_update().get_or_create(pk=node_id)
    if node.ready_world_minute > minute:
        reject("PLANT_REGROWING", "这株植物正在生长。")
    ap, _ = PlayerState.objects.get_or_create(player_id=str(player.pk))
    ap = PlayerState.objects.select_for_update().get(pk=ap.pk)
    recover_action_points(ap)
    if ap.action_points < spec["action_cost"]:
        reject("INSUFFICIENT_ACTION_POINTS", "行动力不足。")

    if action == "plant_start":
        if live(state.attempt, now):
            reject("GATHERING_ACTIVE", "请先完成或取消当前采摘。")
        for other in VillagePlayerGathering.objects.exclude(pk=state.pk).exclude(attempt={}):
            if other.attempt.get("node_id") != node_id:
                continue
            if live(other.attempt, now):
                reject("PLANT_OCCUPIED", "这株植物正在被采摘。")
            other.attempt = {}
            other.revision += 1
            other.save(update_fields=["attempt", "revision"])
        instance = str(state.equipped_instance_id) if state.equipped_instance_id else None
        tool, tier, tool_stats = tool_data(player, bag, instance, minute)
        minimum_level = 5 if spec["level"] >= 7 else 3 if spec["level"] >= 4 else 0
        if (tool_stats or {}).get("level", 0) < minimum_level:
            reject("TOOL_LEVEL_REQUIRED", f"需要Lv.{minimum_level}采集包。")
        seconds = round(spec["seconds"]*(1-(tool_stats or {}).get("gathering_time_reduction", 0)), 2)
        state.attempt = dict(attempt_id=str(rid), node_id=node_id, node_revision=node.revision,
            authority_session_id=authority_session_id, tool_instance_id=instance, tool_tier=tier,
            equipment_level=(tool_stats or {}).get("level", 0),
            quality_bonus_chance=(tool_stats or {}).get("quality_bonus_chance", 0),
            quality_rule="quality-v2", quality=production_quality(level, quality_rng(rid, "PLANT"), (tool_stats or {}).get("quality_bonus_chance", 0)),
            started_at=now.timestamp(), ready_at=(now+timedelta(seconds=seconds)).timestamp(),
            lease_until=(now+timedelta(seconds=seconds+30)).timestamp(), gather_seconds=seconds)
        state.revision += 1
        state.save()
        return dict(attempt={key: value for key, value in state.attempt.items() if key != "quality"})

    attempt = state.attempt
    if (not live(attempt, now) or identity(params.get("attempt_id")) != attempt.get("attempt_id")
            or attempt.get("node_id") != node_id or attempt.get("node_revision") != node.revision
            or attempt.get("authority_session_id") != authority_session_id):
        reject("PLANT_ATTEMPT_INVALID", "采摘记录已失效，请重新采摘。")
    if now.timestamp() < attempt["ready_at"]:
        reject("PLANT_NOT_READY", "采摘尚未完成。")
    tool, tier, _ = tool_data(player, bag, attempt["tool_instance_id"], minute)
    if tier != attempt["tool_tier"] or (tool.equipment_level if tool else 0) != attempt.get("equipment_level", tool.equipment_level if tool else 0):
        reject("TOOL_CHANGED", "工具发生变化，请重新采摘。")
    ap.action_points -= spec["action_cost"]
    ap.action_points_recovered_at = now
    ap.save(update_fields=["action_points", "action_points_recovered_at"])
    index = (bag.slots.aggregate(n=models.Max("slot_index"))["n"] or 0)+1
    quality = attempt.get("quality", spec["quality"])
    slot = InventorySlot.objects.create(bag=bag, slot_index=index, item_id=item.pk,
        item_amount=spec["quantity"], quality=quality, produced_world_minute=minute,
        expires_world_minute=None, source_request_id=rid, item_bag_name=f"{item.item_name}_{index}")
    node.ready_world_minute = minute+spec["regrow_minutes"]
    node.revision += 1
    node.save()
    state.attempt = {}
    state.revision += 1
    state.save()
    from .player_actions import _batch_data
    collected = [_batch_data(slot, minute, {item.pk: item.item_name})]
    reward_type = "professional_collect" if spec["level"] >= 7 else "advanced_collect" if spec["level"] >= 4 else "collect"
    reward = settle_growth(player, source_type=reward_type, source_id=rid,
        facts=dict(collect_type=spec["collect_type"], node_id=node_id,
            tool_item_id=tool.item_id if tool else None, tool_tier=tier,
            source_request_id=str(rid), produced_world_minute=minute,
            outputs=[{key: batch.get(key) for key in ("instance_id", "item_id", "quantity", "quality",
                "produced_world_minute", "expires_world_minute", "source_request_id")} for batch in collected]))
    return dict(node_id=node_id, collected_items=collected, action_points=ap.action_points,
        action_cost=spec["action_cost"], tool_used=wear_tool(tool) if tool else None, growth_rewards=[reward])
