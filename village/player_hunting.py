"""Persistent hunting equipment and health. Combat entry points are Env-only."""
from copy import deepcopy
import hashlib
import hmac
import json
import uuid
import secrets

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
from player.models import EquipmentDefinition, WeaponMove
from player.equipment import stats_for, weapon_moves, WEAPONS, equipment_catalog

KIT_MILESTONE = "starter_hunting_kit_claimed"
SAFE_LOCATION, REST_MINUTES = "VILLAGE_SQUARE", 30
CAPABILITIES = {0: "hunting_spear", 1: "hunting_sword", 2: "hunting_bow", 3: "forestry"}
REGIONS = [
    dict(code="NORTHWEST_HUNTING_GROUNDS", title="旧猎场", center=dict(x=-120, y=2.4, z=98),
        radius=35, recommended_level=1, recommended_tier=1, species=["CHICKEN", "PIG", "CATTLE"]),
    dict(code="DEEP_FOREST", title="深林", center=dict(x=-145, y=6.35, z=125), radius=18,
        recommended_level=4, recommended_tier=2, species=["DEER"]),
    dict(code="NORTH_SLOPE", title="北坡", center=dict(x=-115, y=7.56, z=148), radius=15,
        recommended_level=7, recommended_tier=3, species=["BOAR"]),
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


def _weapon_data(slot, weapon, level, *, context=None):
    stats = stats_for(slot, catalog=context["catalog"] if context is not None else None)
    if not stats:
        item = Item.objects.filter(pk=slot.item_id).first()
        return dict(weapon=weapon, instance_id=str(slot.instance_id), item_id=slot.item_id,
            name=item.item_name if item else str(slot.item_id), equipment_level=slot.equipment_level,
            remaining_uses=slot.remaining_uses, max_uses=0, usable=False)
    return dict(weapon=weapon, instance_id=str(slot.instance_id), item_id=slot.item_id,
        name=stats["item_name"], category=stats["category"], equipment_level=stats["level"], tier=stats["tier"],
        remaining_uses=slot.remaining_uses, max_uses=stats["max_durability"],
        base_attack_min=stats["attack_min"], base_attack_max=stats["attack_max"],
        moves=weapon_moves(stats["category"], moves=context["moves"] if context is not None else None), draw_duration=1.2 * (.95 if level >= 2 else 1))


def resolve_player_weapon(player, bag, weapon, instance_id, minute, *, levels=None, context=None):
    if type(weapon) is not int or weapon not in CAPABILITIES:
        reject("INVALID_WEAPON", "武器类型无效。")
    if not bag.pk:
        reject("TOOL_MISSING", "请先持有真实武器。")
    try:
        slot = resolve_tool(player, bag, CAPABILITIES[weapon], instance_id=instance_id, minute=minute, levels=levels, context=context)
    except ToolError as exc:
        reject(exc.code, exc.message)
    stats = stats_for(slot, catalog=context["catalog"] if context is not None else None)
    if not stats or WEAPONS.get(stats["category"]) != weapon:
        reject("TOOL_UNAVAILABLE", "该物品不是指定类型的武器。")
    if stats["attack_min"] < 1 or stats["attack_max"] < stats["attack_min"]:
        reject("WEAPON_CONFIGURATION_UNAVAILABLE", "武器基础攻击配置无效。")
    moves = weapon_moves(stats["category"], moves=context["moves"] if context is not None else None)
    expected = {0: {"spear_thrust"}, 1: {"sword_light_0", "sword_light_1", "sword_light_2", "sword_heavy"},
                2: {"bow_shot"}, 3: {"axe_chop"}}[weapon]
    if not expected.issubset(moves):
        reject("WEAPON_MOVE_UNAVAILABLE", "武器招式配置不完整。")
    return slot, _weapon_data(slot, weapon, levels.get("hunting", 1) if levels is not None else _level(player), context=context)


def weapon_repair_quote(player, bag, slot, minute, *, context=None):
    stats = context["catalog"].get((slot.item_id, slot.equipment_level)) if context is not None else stats_for(slot)
    item = context["items"].get(slot.item_id) if context is not None else Item.objects.filter(pk=slot.item_id, is_deleted=False).first()
    reason = ("TOOL_NOT_OWNED" if not bag.pk or slot.bag_id != bag.pk or bag.player_id != player.pk or bag.character_id != player.username else
        "PAID_REPAIR_REQUIRED" if not stats or stats["repair_gold"] or stats["repair_materials"] else
        "TOOL_UNAVAILABLE" if not item else
        "TOOL_DURABILITY_UNKNOWN" if type(slot.remaining_uses) is not int or not 0 <= slot.remaining_uses <= stats["max_durability"] else
        "TOOL_EXPIRED" if False else
        "TOOL_ALREADY_FULL" if slot.remaining_uses == stats["max_durability"] else "")
    payload = dict(player_id=player.pk, instance_id=str(slot.instance_id), item_id=slot.item_id,
        amount=slot.item_amount, remaining_uses=slot.remaining_uses, maximum=stats["max_durability"] if stats else None, configuration=stats)
    return dict(available=not reason, reason=reason, quote_hash=hmac.new(settings.SECRET_KEY.encode(),
        json.dumps(payload, sort_keys=True).encode(), hashlib.sha256).hexdigest(), gold_cost=0, materials=[],
        material_item_id=None, material_quantity=0, target_item_id=slot.item_id, required_level=1,
        location_code=None, action="repair_hunting_weapon")


def hunting_overview(player, bag, minute, read_only=False, *, tool_state=None, levels=None, slots=None, context=None):
    state = PlayerHuntingState.objects.filter(player=player).first()
    context = {} if context is None else context
    levels = context.get("levels") if levels is None else levels
    if levels is None:
        levels = {row["code"]: row["level"] for row in growth_snapshot(player, read_only=True, compact=True)["skills"]}
    level, weapons = levels.get("hunting", 1), []
    if "profiles" not in context:
        context["profiles"] = {row.item_id: row for row in EquipmentDefinition.objects.select_related("item")}
    profiles = context["profiles"]
    weapon_ids = {*HUNTING_WEAPONS, 4104, 4105, *(item_id for item_id, row in profiles.items() if row.category in WEAPONS)}
    repairs = {row["instance_id"]: row["repair"] for row in (tool_state or {}).get("tools", []) if "repair" in row}
    slots = context.get("slots") if slots is None else slots
    slots = list(bag.slots.filter(item_amount__gt=0, item_id__in=weapon_ids).select_related("usage").order_by("slot_index")) if slots is None and bag.pk else sorted(slots or [], key=lambda s: s.slot_index)
    context.update(levels=levels, slots=slots)
    if "catalog" not in context:
        context["catalog"] = equipment_catalog()
    if "moves" not in context:
        context["moves"] = list(WeaponMove.objects.all())
    for slot in slots:
        if slot.item_amount <= 0 or slot.item_id not in weapon_ids:
            continue
        profile = profiles.get(slot.item_id)
        weapon = WEAPONS[profile.category] if profile else HUNTING_WEAPONS.get(slot.item_id, {}).get("weapon", 3)
        try:
            _combat_ready(state)
            _, data = resolve_player_weapon(player, bag, weapon, slot.instance_id, minute, levels=levels, context=context)
            reason = ""
        except HuntingError as exc:
            data = _weapon_data(slot, weapon, level, context=context)
            reason = exc.code
        from .production_tools import _quote
        repair = deepcopy(repairs[str(slot.instance_id)]) if str(slot.instance_id) in repairs else _quote(player, bag, slot, "repair", minute)
        if read_only:
            repair.update(available=False, reason="MODE_READ_ONLY")
        data.update(usable=not reason, reason=reason, repair=repair)
        weapons.append(data)
    claim_reason = "MODE_READ_ONLY" if read_only else "STARTER_EQUIPMENT_AUTOMATIC"
    health = _health(state)
    ready = health["recovery_ready_world_minute"]
    reason = "MODE_READ_ONLY" if read_only else "ALREADY_HEALTHY" if health["current"] >= 100 else ""
    return dict(revision=PlayerGrowthState.objects.filter(player=player).values_list("revision", flat=True).first() or 0,
        observed_world_minute=minute, level=level, health=health, weapons=weapons,
        can_claim_starter_kit=False, claim_reason=claim_reason,
        rest=dict(can_start=not reason and ready is None, can_finish=not read_only and ready is not None and minute >= ready,
            reason=reason or "REST_IN_PROGRESS" if ready is not None and minute < ready else reason,
            ready_world_minute=ready, remaining_minutes=max(0, ready-minute) if ready is not None else 0,
            duration_minutes=REST_MINUTES, location_code=SAFE_LOCATION),
        rules=dict(kill_xp=deepcopy(HUNTING_KILL_XP), first_kill_bonus=20, attack_wear=1,
            starter_kit_location=None, initial_equipment_item_ids=[4114, 4104], weapon_shop_location="GENERAL_STORE", t1_repair_location=None, t1_repair_gold=0,
            equipment_max_level=5, damage_formula="base_attack * move_multiplier",
            boar_health=180, boar_hit_damage=30, boar_defense_outcomes=["BLOCKED", "DODGED"], health_maximum=100, safe_rest_minutes=REST_MINUTES),
        regions=deepcopy(REGIONS))


@transaction.atomic
def execute_hunting_action(player, bag, action, params, rid, clock):
    GlobalGameTimeState.objects.select_for_update().get(pk=1)
    if not bag.pk or bag.player_id != player.pk or bag.character_id != player.username or not isinstance(params, dict):
        reject("PLAYER_BAG_REQUIRED", "请使用本人主背包。")
    state, _ = PlayerHuntingState.objects.select_for_update().get_or_create(player=player)
    if action == "claim_starter_hunting_kit":
        reject("STARTER_EQUIPMENT_AUTOMATIC", "初始剑和斧头已随背包发放，其他武器请到商店购买。")
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
        slot.remaining_uses = stats_for(slot)["max_durability"]
        slot.save(update_fields=["remaining_uses"])
        result = dict(weapon=_weapon_data(slot, WEAPONS[stats_for(slot)["category"]], _level(player)), spent_gold=0)
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
        extra = {"weapon", "instance_id"} if action in {"resolve_weapon", "authorize_attack"} else {"damage", "source_kind", "prey_id", "outcome", "impact_world_minute"} if action == "damage" else set()
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
                if action != "damage" and (old.authority_session_id != session or state.authority_session_id != session):
                    reject("STALE_AUTHORITY_SESSION", "旧Env会话的攻击许可不能重放。")
                if old.payload_hash != digest:
                    reject("REQUEST_CONFLICT", "同一战斗请求编号不能用于不同动作。")
                answer = deepcopy(old.response)
                answer["data"].update(health=_health(state), replayed=True)
                return answer
            if action == "authorize_attack":
                _combat_ready(state)
                bag = InventoryBag.objects.select_for_update().filter(player=player, character_id=player.username).first()
                if not bag:
                    reject("TOOL_MISSING", "本人主背包中没有武器。")
                slot, weapon = resolve_player_weapon(player, bag, payload.get("weapon"), payload.get("instance_id"), clock.world_minute)
                used = wear_tool(slot)
                weapon.update(instance_id=used["instance_id"], remaining_uses=used["remaining_uses"],
                    attack_base=secrets.randbelow(weapon["base_attack_max"] - weapon["base_attack_min"] + 1) + weapon["base_attack_min"],
                    critical=secrets.randbelow(100) < 10, critical_chance=0.1, critical_multiplier=1.5)
                result = dict(weapon=weapon)
            else:
                damage = payload.get("damage")
                prey_id = _uuid(payload.get("prey_id"))
                outcome = payload.get("outcome", "HIT")
                impact = payload.get("impact_world_minute", clock.world_minute)
                legacy = "outcome" not in payload
                valid_damage = (type(damage) is int and
                    (1 <= damage <= 100 if legacy else damage == (30 if outcome == "HIT" else 0)))
                if payload.get("source_kind") != "BOAR_CHARGE" or outcome not in {"HIT", "BLOCKED", "DODGED"} or not valid_damage:
                    reject("INVALID_DAMAGE", "可信伤害或防御事件无效。")
                prey = VillageWildlife.objects.select_for_update().filter(pk=prey_id, species="BOAR").first()
                if (not prey or type(impact) is not int or not prey.born_world_minute <= impact <= clock.world_minute
                        or prey.death_world_minute is not None and impact > prey.death_world_minute):
                    reject("DAMAGE_SOURCE_UNAVAILABLE", "事件不在该真实野猪的生命时间内。")
                state.current_health = max(0, state.current_health-damage)
                if damage:
                    state.recovery_ready_world_minute = None
                if state.current_health == 0:
                    from .wildlife import _cancel
                    for attempt in VillageHuntAttempt.objects.select_for_update().filter(actor_type="PLAYER", actor_id=str(player.pk), status="ACTIVE"):
                        prey = VillageWildlife.objects.select_for_update().get(pk=attempt.prey_id)
                        _cancel(prey, attempt)
                result = dict(damage=damage, source_kind="BOAR_CHARGE", prey_id=str(prey_id),
                              outcome=outcome, impact_world_minute=impact)
            if action == "authorize_attack" or not state.authority_session_id:
                state.authority_session_id = session
            _bump(player, state)
            if action == "authorize_attack":
                from player.tutorials import observe_weapon
                observe_weapon(player, weapon["category"], rid)
            result["health"] = _health(state)
            answer = dict(success=True, result_code="OK", data=result)
            PlayerCombatReceipt.objects.create(player=player, request_id=rid, authority_session_id=session,
                payload_hash=digest, action=action, response=deepcopy(answer), world_minute=clock.world_minute)
            return answer
    except HuntingError as exc:
        data = dict(health=health_snapshot(player)) if "player" in locals() and player else {}
        return dict(success=False, result_code=exc.code, message=exc.message, data=data)
