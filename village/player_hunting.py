"""Persistent hunting equipment and health. Combat entry points are Env-only."""
from copy import deepcopy
import hashlib
import hmac
import json
import uuid

from django.conf import settings
from django.db import transaction
from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.models import InventoryBag, InventorySlot, NpcDefinition
from items.models import Item
from player.models import (PlayerCombatReceipt, PlayerGrowthState, PlayerHuntingState,
                           PlayerMilestoneProgress)
from player.progression import ensure_growth, growth_snapshot
from .models import EconomicLedgerEntry, VillageHuntAttempt, VillageSimulationConfig, VillageWildlife
from .phase3_catalog import HUNTING_WEAPONS, HUNTING_KILL_XP
from .production_tools import ToolError, resolve_tool, wear_tool, _index, _single_in_place

KIT_MILESTONE = "starter_hunting_kit_claimed"
SAFE_LOCATION, REST_MINUTES = "VILLAGE_SQUARE", 30
CAPABILITIES = {0: "hunting_spear", 1: "hunting_sword", 2: "hunting_bow", 3: "forestry"}
REGIONS = [
    dict(code="NORTHWEST_HUNTING_GROUNDS", title="旧猎场", center=dict(x=-120, y=2.4, z=98),
        radius=35, recommended_level=1, recommended_tier=1, species=["CHICKEN", "PIG", "CATTLE"]),
    dict(code="DEEP_FOREST", title="深林", center=dict(x=-145, y=6.35, z=125), radius=18,
        recommended_level=4, recommended_tier=2, species=["DEER"]),
]


class HuntingError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def reject(code, message):
    raise HuntingError(code, message)


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        reject("INVALID_PARAMS", "请求或实例标识无效。")


def _level(player):
    return next(row["level"] for row in growth_snapshot(player, read_only=True)["skills"] if row["code"] == "hunting")


def _health(state):
    return dict(current=state.current_health if state else 100, maximum=100,
        downed=bool(state and state.current_health == 0), revision=state.revision if state else 0,
        recovery_ready_world_minute=state.recovery_ready_world_minute if state else None, safe_location_code=SAFE_LOCATION)


def health_snapshot(player):
    return _health(PlayerHuntingState.objects.filter(player=player).first())


def _bump(player, state):
    state.revision += 1
    state.save()
    growth = ensure_growth(player)
    growth.revision += 1
    growth.save(update_fields=["revision"])


def _combat_ready(state):
    if state and state.current_health <= 0:
        reject("PLAYER_DOWNED", "倒地后请到广场免费休整。")
    if state and state.recovery_ready_world_minute is not None:
        reject("PLAYER_RESTING", "休整期间不能进行战斗。")


def _weapon_data(slot, weapon, level):
    spec = HUNTING_WEAPONS.get(slot.item_id)
    tier = spec["tier"] if spec else 2 if slot.item_id == 4105 else 1
    item = Item.objects.filter(pk=slot.item_id).first()
    return dict(weapon=weapon, instance_id=str(slot.instance_id), item_id=slot.item_id, name=item.item_name if item else str(slot.item_id),
        tier=tier, remaining_uses=slot.remaining_uses, max_uses=item.max_use_count if item else 0,
        damage_multiplier=1.2 if tier == 2 and spec else 1.0,
        draw_duration=1.2 * (.95 if level >= 2 else 1))


def resolve_player_weapon(player, bag, weapon, instance_id, minute):
    if type(weapon) is not int or weapon not in CAPABILITIES:
        reject("INVALID_WEAPON", "武器类型无效。")
    if not bag.pk:
        reject("TOOL_MISSING", "请先领取并持有真实狩猎武器。")
    try:
        slot = resolve_tool(player, bag, CAPABILITIES[weapon], instance_id=instance_id, minute=minute)
    except ToolError as exc:
        reject(exc.code, exc.message)
    if (weapon != 3 and (slot.item_id not in HUNTING_WEAPONS or HUNTING_WEAPONS[slot.item_id]["weapon"] != weapon)
            or weapon == 3 and slot.item_id not in (4104, 4105)):
        reject("TOOL_UNAVAILABLE", "该物品不是当前开放的真实武器。")
    return slot, _weapon_data(slot, weapon, _level(player))


def weapon_repair_quote(player, bag, slot, minute):
    spec = HUNTING_WEAPONS.get(slot.item_id)
    item = Item.objects.filter(pk=slot.item_id, is_deleted=False).first()
    reason = ("TOOL_NOT_OWNED" if not bag.pk or slot.bag_id != bag.pk or bag.player_id != player.pk or bag.character_id != player.username else
        "FREE_REPAIR_T1_ONLY" if not spec or spec["tier"] != 1 else
        "TOOL_UNAVAILABLE" if not item else
        "TOOL_DURABILITY_UNKNOWN" if type(slot.remaining_uses) is not int or not 0 <= slot.remaining_uses <= item.max_use_count else
        "TOOL_EXPIRED" if slot.freshness_unknown or slot.expires_world_minute is not None and slot.expires_world_minute <= minute else
        "TOOL_ALREADY_FULL" if slot.remaining_uses == item.max_use_count else
        "WORKSHOP_UNAVAILABLE" if not NpcDefinition.objects.filter(agent_id="10012", is_test_only=False).exists() else "")
    payload = dict(player_id=player.pk, instance_id=str(slot.instance_id), item_id=slot.item_id,
        amount=slot.item_amount, remaining_uses=slot.remaining_uses, maximum=item.max_use_count if item else None)
    return dict(available=not reason, reason=reason, quote_hash=hmac.new(settings.SECRET_KEY.encode(),
        json.dumps(payload, sort_keys=True).encode(), hashlib.sha256).hexdigest(), gold_cost=0, materials=[],
        material_item_id=None, material_quantity=0, target_item_id=slot.item_id, required_level=1,
        location_code="BUTCHER_YARD", action="repair_hunting_weapon")


def hunting_overview(player, bag, minute, read_only=False):
    state = PlayerHuntingState.objects.filter(player=player).first()
    level, weapons = _level(player), []
    for slot in bag.slots.filter(item_amount__gt=0, item_id__in=[*HUNTING_WEAPONS, 4104, 4105]).order_by("slot_index") if bag.pk else []:
        weapon = HUNTING_WEAPONS[slot.item_id]["weapon"] if slot.item_id in HUNTING_WEAPONS else 3
        data = _weapon_data(slot, weapon, level)
        try:
            _combat_ready(state)
            resolve_player_weapon(player, bag, weapon, slot.instance_id, minute)
            reason = ""
        except HuntingError as exc:
            reason = exc.code
        repair = weapon_repair_quote(player, bag, slot, minute)
        if read_only:
            repair.update(available=False, reason="MODE_READ_ONLY")
        data.update(usable=not reason, reason=reason, repair=repair)
        weapons.append(data)
    claimed = PlayerMilestoneProgress.objects.filter(player=player, code=KIT_MILESTONE).exists()
    kit_ready = Item.objects.filter(pk__in=[4110, 4112, 4114], is_deleted=False).count() == 3
    claim_reason = "MODE_READ_ONLY" if read_only else "ALREADY_CLAIMED" if claimed else "WEAPON_CATALOG_UNAVAILABLE" if not kit_ready else ""
    health = _health(state)
    ready = health["recovery_ready_world_minute"]
    reason = "MODE_READ_ONLY" if read_only else "ALREADY_HEALTHY" if health["current"] >= 100 else ""
    return dict(revision=PlayerGrowthState.objects.filter(player=player).values_list("revision", flat=True).first() or 0,
        observed_world_minute=minute, level=level, health=health, weapons=weapons,
        can_claim_starter_kit=not claim_reason, claim_reason=claim_reason,
        rest=dict(can_start=not reason and ready is None, can_finish=not read_only and ready is not None and minute >= ready,
            reason=reason or "REST_IN_PROGRESS" if ready is not None and minute < ready else reason,
            ready_world_minute=ready, remaining_minutes=max(0, ready-minute) if ready is not None else 0,
            duration_minutes=REST_MINUTES, location_code=SAFE_LOCATION),
        rules=dict(kill_xp=deepcopy(HUNTING_KILL_XP), first_kill_bonus=20, attack_wear=1,
            starter_kit_location="HUNTING_GATE", t1_repair_location="BUTCHER_YARD", t1_repair_gold=0,
            t2_minimum_level=4, t2_damage_multiplier=1.2, health_maximum=100, safe_rest_minutes=REST_MINUTES),
        regions=deepcopy(REGIONS))


@transaction.atomic
def execute_hunting_action(player, bag, action, params, rid, clock):
    GlobalGameTimeState.objects.select_for_update().get(pk=1)
    if not bag.pk or bag.player_id != player.pk or bag.character_id != player.username or not isinstance(params, dict):
        reject("PLAYER_BAG_REQUIRED", "请使用本人主背包。")
    state, _ = PlayerHuntingState.objects.select_for_update().get_or_create(player=player)
    if action == "claim_starter_hunting_kit":
        if params:
            reject("INVALID_PARAMS", "领取基础狩猎装备不需要额外参数。")
        if PlayerMilestoneProgress.objects.filter(player=player, code=KIT_MILESTONE).exists():
            reject("ALREADY_CLAIMED", "每人只能领取一次基础狩猎装备，现有武器可免费整修。")
        items = Item.objects.in_bulk([4110, 4112, 4114])
        if len(items) != 3 or any(i.is_deleted for i in items.values()):
            reject("WEAPON_CATALOG_UNAVAILABLE", "基础狩猎装备目录尚未就绪。")
        added = []
        for item in items.values():
            slot = InventorySlot.objects.create(bag=bag, slot_index=_index(bag), item_id=item.pk,
                item_amount=1, quality=3, remaining_uses=item.max_use_count, produced_world_minute=clock.world_minute, source_request_id=rid)
            added.append(dict(_weapon_data(slot, HUNTING_WEAPONS[item.pk]["weapon"], _level(player)), quantity=1))
        PlayerMilestoneProgress.objects.create(player=player, code=KIT_MILESTONE, title="领取基础狩猎装备",
            source_type=action, source_id=str(rid))
        result = dict(items_added=added)
    elif action == "repair_hunting_weapon":
        if set(params) != {"instance_id", "expected_quote"}:
            reject("INVALID_PARAMS", "请确认本人武器整修报价。")
        slot = bag.slots.select_for_update().filter(instance_id=_uuid(params["instance_id"]), item_amount__gt=0).first()
        if not slot:
            reject("TOOL_MISSING", "本人背包中没有这件武器。")
        quote = weapon_repair_quote(player, bag, slot, clock.world_minute)
        if params["expected_quote"] != quote["quote_hash"]:
            reject("QUOTE_CHANGED", "武器状态已变化，请重新确认免费整修。")
        if not quote["available"]:
            reject(quote["reason"], "当前不能整修这件武器。")
        _single_in_place(slot)
        slot.remaining_uses = Item.objects.get(pk=slot.item_id).max_use_count
        slot.save(update_fields=["remaining_uses"])
        result = dict(weapon=_weapon_data(slot, HUNTING_WEAPONS[slot.item_id]["weapon"], _level(player)), spent_gold=0)
    elif action in {"start_safe_rest", "finish_safe_rest"}:
        if params:
            reject("INVALID_PARAMS", "安全点休整不接受额外参数。")
        if action == "start_safe_rest":
            if state.current_health >= 100:
                reject("ALREADY_HEALTHY", "当前生命已满。")
            if state.recovery_ready_world_minute is None:
                state.recovery_ready_world_minute = clock.world_minute + REST_MINUTES
        else:
            if state.recovery_ready_world_minute is None:
                reject("REST_NOT_STARTED", "请先在广场开始免费休整。")
            if clock.world_minute < state.recovery_ready_world_minute:
                reject("REST_NOT_READY", "休整尚未完成。")
            state.current_health, state.recovery_ready_world_minute = 100, None
        result = dict(health=None)
    else:
        reject("INVALID_ACTION", "未开放的狩猎动作。")
    _bump(player, state)
    result["health"] = _health(state)
    EconomicLedgerEntry.objects.create(request_id=uuid.uuid5(uuid.NAMESPACE_URL, f"hunting-action:{player.pk}:{rid}"),
        entry_type=action.upper(), world_minute=clock.world_minute, changes=dict(player_id=str(player.pk), result=deepcopy(result)))
    return result


def execute_combat(payload):
    """Trusted service-only endpoint; never register it as a public player action."""
    try:
        if not isinstance(payload, dict):
            reject("INVALID_PARAMS", "战斗请求无效。")
        action = payload.get("action")
        common = {"action", "player_id", "request_id", "authority_session_id"}
        extra = {"weapon", "instance_id"} if action in {"resolve_weapon", "authorize_attack"} else {"damage", "source_kind", "prey_id"} if action == "damage" else set()
        if action not in {"inspect_state", "resolve_weapon", "authorize_attack", "damage"} or set(payload) - common - extra:
            reject("INVALID_PARAMS", "战斗请求字段无效。")
        rid = _uuid(payload.get("request_id"))
        session = payload.get("authority_session_id")
        if not isinstance(session, str) or not 0 < len(session) <= 128:
            reject("INVALID_PARAMS", "战斗许可需要当前Env会话。")
        identifier = payload.get("player_id")
        if (type(identifier) not in (int, str) or not str(identifier).isascii()
                or not str(identifier).isdigit() or len(str(identifier)) > 18):
            reject("PLAYER_NOT_FOUND", "玩家身份无效。")
        player = GameAccount.objects.filter(pk=int(identifier), is_active=True, is_ai=False).first()
        if not player:
            reject("PLAYER_NOT_FOUND", "玩家身份无效。")
        if not VillageSimulationConfig.objects.filter(config_key="default", village_economy_enabled=True).exists():
            reject("FEATURE_DISABLED", "村庄战斗尚未启用。")
        clock = GlobalGameTimeState.objects.filter(pk=1).first()
        if not clock:
            reject("WORLD_NOT_READY", "世界时间尚未就绪。")
        if action in {"inspect_state", "resolve_weapon"}:
            state = PlayerHuntingState.objects.filter(player=player).first()
            _combat_ready(state)
            data = dict(health=_health(state))
            if action == "resolve_weapon":
                bag = InventoryBag.objects.filter(player=player, character_id=player.username).first()
                if not bag:
                    reject("TOOL_MISSING", "本人主背包中没有武器。")
                _, data["weapon"] = resolve_player_weapon(player, bag, payload.get("weapon"), payload.get("instance_id"), clock.world_minute)
            return dict(success=True, result_code="OK", data=data)
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with transaction.atomic():
            clock = GlobalGameTimeState.objects.select_for_update().get(pk=1)
            state, _ = PlayerHuntingState.objects.select_for_update().get_or_create(player=player)
            old = PlayerCombatReceipt.objects.filter(player=player, request_id=rid).first()
            if old:
                if old.authority_session_id != session or state.authority_session_id != session:
                    reject("STALE_AUTHORITY_SESSION", "旧Env会话的攻击许可不能重放。")
                if old.payload_hash != digest:
                    reject("REQUEST_CONFLICT", "同一战斗请求编号不能用于不同动作。")
                answer = deepcopy(old.response)
                answer["data"].update(health=_health(state), replayed=True)
                return answer
            if action == "authorize_attack":
                _combat_ready(state)
                if payload.get("weapon") == 3:
                    reject("WRONG_WEAPON", "斧子继续使用真实伐木作业规则。")
                bag = InventoryBag.objects.select_for_update().filter(player=player, character_id=player.username).first()
                if not bag:
                    reject("TOOL_MISSING", "本人主背包中没有武器。")
                slot, weapon = resolve_player_weapon(player, bag, payload.get("weapon"), payload.get("instance_id"), clock.world_minute)
                used = wear_tool(slot)
                weapon.update(instance_id=used["instance_id"], remaining_uses=used["remaining_uses"])
                result = dict(weapon=weapon)
            else:
                damage = payload.get("damage")
                prey_id = _uuid(payload.get("prey_id"))
                if payload.get("source_kind") != "BOAR_CHARGE" or type(damage) is not int or not 1 <= damage <= 100:
                    reject("INVALID_DAMAGE", "可信伤害事件无效。")
                if not VillageWildlife.objects.filter(pk=prey_id, species="BOAR", status__in=["ALIVE", "CLAIMED"]).exists():
                    reject("DAMAGE_SOURCE_UNAVAILABLE", "该真实野猪攻击来源尚不可用。")
                state.current_health = max(0, state.current_health-damage)
                state.recovery_ready_world_minute = None
                if state.current_health == 0:
                    from .wildlife import _cancel
                    for attempt in VillageHuntAttempt.objects.select_for_update().filter(actor_type="PLAYER", actor_id=str(player.pk), status="ACTIVE"):
                        prey = VillageWildlife.objects.select_for_update().get(pk=attempt.prey_id)
                        _cancel(prey, attempt)
                result = dict(damage=damage, source_kind="BOAR_CHARGE", prey_id=str(prey_id))
            state.authority_session_id = session
            _bump(player, state)
            result["health"] = _health(state)
            answer = dict(success=True, result_code="OK", data=result)
            PlayerCombatReceipt.objects.create(player=player, request_id=rid, authority_session_id=session,
                payload_hash=digest, action=action, response=deepcopy(answer), world_minute=clock.world_minute)
            return answer
    except HuntingError as exc:
        data = dict(health=health_snapshot(player)) if "player" in locals() and player else {}
        return dict(success=False, result_code=exc.code, message=exc.message, data=data)
