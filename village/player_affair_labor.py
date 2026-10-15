"""Continuous player labor. Only trusted Env presence ticks advance this clock.

The public action protocol has start/cancel, never tick or complete. Each tick
uses the database clock, checks the Env connection nonce and expires a missing
heartbeat instead of crediting an offline interval.
"""
import uuid

from django.db import transaction
from django.utils import timezone

from game_time.models import GlobalGameTimeState
from .models import VillageAffair, VillagePlayerAffairLabor, VillageSimulationConfig

LEASE_SECONDS = 10
MAX_TICK_MINUTES = 30


def labor_data(labor):
    if not labor:
        return None
    return dict(labor_id=str(labor.pk), affair_id=labor.affair_id,
                commission_id=str(labor.commission_id) if labor.commission_id else None,
                condition_id=labor.condition_id, status=labor.status,
                location_code=labor.location_code, accumulated_minutes=labor.accumulated_minutes,
                remaining_minutes=max(0, labor.unit_minutes-labor.accumulated_minutes), unit_minutes=labor.unit_minutes,
                completed_units=labor.completed_units, stop_reason=labor.stop_reason,
                authority_session_id=str(labor.authority_session_id))


def current_labor(player):
    labor = VillagePlayerAffairLabor.objects.filter(player=player, status="ACTIVE").first()
    data = labor_data(labor)
    # An expired lease is visibly stopped even before the next trusted tick.
    if data and (timezone.now()-labor.last_seen_at).total_seconds() > LEASE_SECONDS:
        data.update(status="CANCELLED", stop_reason="PRESENCE_EXPIRED", accumulated_minutes=0,
                    remaining_minutes=labor.unit_minutes)
    return data


def _stop(labor, reason, status="CANCELLED"):
    labor.status = status
    labor.stop_reason = reason
    labor.accumulated_minutes = 0
    labor.save(update_fields=["status", "stop_reason", "accumulated_minutes"])
    return labor_data(labor)


def start_labor(player, affair, rid, location, clock, authority_session_id, condition_id=None):
    from .affairs import AffairError, resolve_condition
    from .player_actions import fail, key
    try:
        condition = resolve_condition(affair, condition_id=condition_id, kind="labor")
    except AffairError as exc:
        fail(exc.code, exc.message)
    if condition["locations"] and location not in condition["locations"]:
        fail("LOCATION_INVALID", "请到该条件指定的位置开始劳动。")
    return _start_session(player, condition, rid, location, clock, authority_session_id, affair=affair)


def start_commission_labor(player, commission_id, rid, location, clock, authority_session_id, condition_id):
    from .personal_commissions import validate_commission_condition
    row, condition = validate_commission_condition(commission_id, "PLAYER", str(player.pk), condition_id,
                                                  clock.world_minute, location=location, kind="labor")
    return _start_session(player, condition, rid, location, clock, authority_session_id, commission=row)


def _start_session(player, condition, rid, location, clock, authority_session_id, *, affair=None, commission=None):
    from .player_actions import fail, key
    nonce = key(authority_session_id)
    existing = VillagePlayerAffairLabor.objects.select_for_update().filter(player=player, status="ACTIVE").first()
    if existing:
        if (existing.authority_session_id == nonce
                and (timezone.now()-existing.last_seen_at).total_seconds() <= LEASE_SECONDS):
            if (existing.affair_id != (affair.pk if affair else None)
                    or existing.commission_id != (commission.pk if commission else None)
                    or existing.condition_id != condition["id"]):
                fail("LABOR_ALREADY_ACTIVE", "请先停止正在进行的劳动。")
            return labor_data(existing)
        _stop(existing, "RESTARTED")
    labor_id = uuid.uuid5(rid, str(player.pk))
    labor = VillagePlayerAffairLabor.objects.create(labor_id=labor_id, player=player, affair=affair, commission=commission,
        condition_id=condition["id"], unit_minutes=condition["unit_minutes"],
        authority_session_id=nonce, location_code=location, last_world_minute=clock.world_minute,
        last_time_revision=clock.time_revision, last_seen_at=timezone.now())
    return dict(labor_data(labor), locations=condition["locations"])


def cancel_labor(player, reason="CANCELLED_BY_PLAYER"):
    labor = VillagePlayerAffairLabor.objects.select_for_update().filter(player=player, status="ACTIVE").first()
    return _stop(labor, reason) if labor else None


@transaction.atomic
def tick_player_labor(payload):
    """Endpoint called only by Env's own presence loop, not its client router."""
    from .affairs import AffairError, complete_labor, labor_unavailable, resolve_condition
    from .player_actions import ActionError, fail, key, response
    from .personal_commissions import (CommissionError, expire_commissions, validate_commission_condition,
                                       complete_commission_labor)
    try:
        if not isinstance(payload, dict) or set(payload) != {
                "player_id", "labor_id", "authority_session_id", "current_location_code", "present", "observed_world_minute"}:
            fail("INVALID_PARAMS", "劳动同步参数无效。")
        if type(payload["present"]) is not bool or not isinstance(payload["current_location_code"], str):
            fail("INVALID_PARAMS", "劳动在场信息无效。")
        if type(payload["observed_world_minute"]) is not int or payload["observed_world_minute"] < 0:
            fail("INVALID_PARAMS", "劳动时间观察无效。")
        labor_id, nonce = key(payload["labor_id"]), key(payload["authority_session_id"])
        player_id = payload["player_id"]
        if (type(player_id) not in (str, int) or not str(player_id).isascii()
                or not str(player_id).isdigit() or len(str(player_id)) > 18):
            fail("PLAYER_NOT_FOUND", "玩家身份无效。")
        clock = GlobalGameTimeState.objects.select_for_update().filter(pk=1).first()
        if not clock:
            fail("WORLD_NOT_READY", "村庄时间尚未初始化。")
        expire_commissions(clock.world_minute)
        labor = VillagePlayerAffairLabor.objects.select_for_update().filter(pk=labor_id, player_id=player_id).first()
        if not labor or labor.authority_session_id != nonce:
            fail("LABOR_SESSION_INVALID", "施工连接已失效，请重新开始劳动。")
        if labor.status != "ACTIVE":
            return response(labor_id, data=dict(labor=labor_data(labor), credited_units=0))
        affair = VillageAffair.objects.select_for_update().get(pk=labor.affair_id) if labor.affair_id else None
        now = timezone.now()
        # A delayed HTTP observation cannot credit time after the player left.
        # Env cannot advance the database clock through this endpoint either.
        observed_minute = min(payload["observed_world_minute"], clock.world_minute)
        delta = observed_minute-labor.last_world_minute
        try:
            if labor.commission_id:
                _, condition = validate_commission_condition(labor.commission_id, "PLAYER", str(labor.player_id),
                    labor.condition_id, clock.world_minute, location=payload["current_location_code"], kind="labor")
            else:
                condition = resolve_condition(affair, condition_id=labor.condition_id, kind="labor")
        except (AffairError, CommissionError):
            condition = None
        reason = None
        enabled = VillageSimulationConfig.objects.filter(config_key="default", village_economy_enabled=True)
        if not labor.commission_id:
            enabled = enabled.filter(village_story_enabled=True)
        if not enabled.exists():
            reason = "FEATURE_DISABLED"
        elif not condition:
            reason = "CONDITION_UNAVAILABLE"
        elif (not payload["present"] or (condition["locations"]
                and payload["current_location_code"] not in condition["locations"])):
            reason = "LEFT_WORKSITE"
        elif (now-labor.last_seen_at).total_seconds() > LEASE_SECONDS:
            reason = "PRESENCE_EXPIRED"
        elif delta < 0 or delta > MAX_TICK_MINUTES or clock.time_revision < labor.last_time_revision:
            reason = "CLOCK_DISCONTINUITY"
        elif condition["unit_minutes"] != labor.unit_minutes:
            reason = "CONDITION_CHANGED"
        if reason:
            return response(labor_id, data=dict(labor=_stop(labor, reason), credited_units=0))
        labor.accumulated_minutes += delta
        labor.last_world_minute = observed_minute
        labor.last_time_revision = clock.time_revision
        labor.last_seen_at = now
        labor.location_code = payload["current_location_code"]
        credited = 0
        affected_npcs, earned_gold = [], 0
        while labor.accumulated_minutes >= labor.unit_minutes:
            # Stable per-unit key makes a response loss/retry harmless. The clock
            # lock serializes the final unit against every player and NPC.
            receipt = uuid.uuid5(labor.pk, str(labor.completed_units+1))
            try:
                if labor.commission_id:
                    result = complete_commission_labor(labor.commission_id, "PLAYER", str(labor.player_id),
                        labor.condition_id, receipt, clock.world_minute, location=payload["current_location_code"])
                    affected_npcs = result.get("affected_npc_ids", [])
                    finished = result.get("status") == "COMPLETED"
                    if finished:
                        earned_gold = result.get("reward_gold", 0)
                    condition_closed = finished or not any(c.get("id") == labor.condition_id and c.get("available")
                                                          for c in result.get("conditions", []))
                else:
                    complete_labor(affair, world_minute=clock.world_minute, request_id=receipt,
                                   condition_id=labor.condition_id, player_id=str(labor.player_id))
                    finished = affair.status == "COMPLETED"
                    condition_closed = labor_unavailable(affair, condition_id=labor.condition_id)
            except (AffairError, CommissionError) as exc:
                return response(labor_id, data=dict(labor=_stop(labor, exc.code), credited_units=0))
            labor.accumulated_minutes -= labor.unit_minutes
            labor.completed_units += 1
            credited += 1
            if condition_closed:
                labor.status = "COMPLETED"
                labor.stop_reason = ("COMMISSION_COMPLETED" if labor.commission_id else "AFFAIR_COMPLETED") if finished else "CONDITION_COMPLETED"
                labor.accumulated_minutes = 0
                break
        labor.save()
        if credited:
            from .daily_events import evaluate_daily_events
            evaluate_daily_events(clock.world_minute)
        return response(labor_id, data=dict(labor=labor_data(labor), credited_units=credited,
                                           earned_gold=earned_gold, affected_npc_ids=affected_npcs))
    except ActionError as exc:
        return response(payload.get("labor_id", "") if isinstance(payload, dict) else "", exc.code, exc.message)
