"""Phase-two authoritative needs and life-action transactions.

The service deliberately owns every needs mutation.  Callers submit an
absolute minute and observed activity; they never submit needs values.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Sequence

from django.db import IntegrityError, transaction
from django.db.models import Q

from game_time.models import GlobalGameTimeState
from inventory.models import NpcGlobalInventory, NpcGlobalInventorySlot
from items.models import Item, ItemType

from .models import (
    NpcActionReservation,
    NpcDialogueSession,
    NpcNeedsDaySummary,
    NpcRuntimeState,
    NpcSocialDailyCounter,
    VillageActionReceipt,
    VillageCommunityMealDailyUsage,
    VillageDeliveryOutbox,
    VillageLocationDefinition,
    VillageSimulationConfig,
    VillageWorldEvent,
    NpcCommandState,
)
from .runtime_services import CommandService, ResultCode, TimeService, _fast_integration_enabled


NEEDS_FIELDS = ("hunger", "energy", "health", "social", "stress")
NEEDS_SCALE = 10
MINUTES_PER_DAY = 1440
HEALTHY_SLEEP_MINUTES = 360
SLEEP_FULL_MINUTES = 8 * 60
SLEEP_MAX_MINUTES = 10 * 60
SLEEP_CONTINUATION_GAP_MINUTES = 10
MAX_INTERVALS = 64
ACTIVITIES = {"IDLE", "MOVE", "WORK", "SOCIALIZE", "REST", "SLEEP"}
URGENT_NEEDS = {"hunger", "social", "stress", "energy"}

# Rates are fixed-point units per 30 minutes.  The activity values already
# include the awake base values from the frozen contract.
RATES = {
    "IDLE": {"hunger": 20, "energy": 0, "social": 10, "stress": 0},
    "MOVE": {"hunger": 20, "energy": -10, "social": 10, "stress": 5},
    "WORK": {"hunger": 30, "energy": -40, "social": 10, "stress": 20},
    "SOCIALIZE": {"hunger": 20, "energy": 0, "social": -80, "stress": -30},
    "REST": {"hunger": 20, "energy": 60, "social": 10, "stress": -40},
    "SLEEP": {"hunger": 10, "energy": 100, "social": 0, "stress": -20},
}

# RATES is the legacy total effect for an activity.  The authoritative split
# is a fixed IDLE-like base applied by the world Tick plus an action delta
# applied once when the action settles.
BASE_RATES = dict(RATES["IDLE"])
ACTION_DELTAS = {
    activity: {
        name: rate - BASE_RATES.get(name, 0)
        for name, rate in rates.items()
        if rate - BASE_RATES.get(name, 0)
    }
    for activity, rates in RATES.items()
}
ACTION_DELTA_ACTIVITIES = {
    "MOVE": "MOVE", "GO_HOME": "MOVE",
    "WORK_PLACEHOLDER": "WORK",
    "PLANT": "WORK", "HARVEST": "WORK", "HUNT": "WORK", "CHOP_TREE": "WORK", "COOK": "WORK",
    "REPAIR_LABOR": "WORK",
    "PROCESS_GAME": "WORK",
}


def settle_action_needs_locked(
    runtime: NpcRuntimeState, action: str, started: Optional[int], ended: Optional[int],
    *, base_until: Optional[int] = None, changed_fields: Optional[set] = None,
) -> Optional[str]:
    """Catch up fixed base and append one action delta for a settled action."""
    if action != "SLEEP" and started is not None and ended is not None and ended > started:
        NeedsService._reset_sleep_state(runtime)
    activity = ACTION_DELTA_ACTIVITIES.get(action)
    if not activity or started is None or ended is None or ended <= started:
        return None
    if base_until is not None and runtime.needs_world_minute < base_until:
        error = NeedsService.advance_base_locked(runtime, base_until, finalize_days=False)
        if error:
            return error
    before = public_needs(runtime) if changed_fields is not None else None
    error = NeedsService.apply_behavior_delta_locked(runtime, started, ended, activity)
    if not error and changed_fields is not None:
        changed_fields.update(name for name, value in public_needs(runtime).items() if value != before[name])
    return error


def sleep_receipt_details(start: int, end: int, planned_end: int, reason: str) -> dict:
    """Only this sleep session; calendar conversion belongs to the wire projection."""
    days = []
    cursor = start
    while cursor < end:
        day_end = min(end, (cursor // MINUTES_PER_DAY + 1) * MINUTES_PER_DAY)
        days.append({"day_world_minute": cursor // MINUTES_PER_DAY * MINUTES_PER_DAY,
                     "minutes": day_end - cursor})
        cursor = day_end
    return {"slept_minutes": end - start, "planned_duration_minutes": planned_end - start,
            "remaining_minutes": max(0, planned_end - end), "end_reason": reason,
            "sleep_status": "INTERRUPTED" if reason == "VOLUNTARY_WAKE" else "COMPLETED",
            "sleep_minutes_by_day": days}


def _hash(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def settle_repair_needs_locked(runtime, command, world_minute):
    """Persist a server-owned cursor; ticks, cancellation and retries share it."""
    from .affair_commands import labor_unavailable as repair_labor_unavailable
    started = command.started_world_minute
    if started is None:
        return None
    params = dict(command.sanitized_parameters)
    params.pop("expected_story_version", None)
    cursor = int(params.get("_repair_needs_until", started))
    error = repair_labor_unavailable(runtime, world_minute, params)
    if runtime.active_command_id != command.command_id or runtime.current_action != "CONTRIBUTE_TO_AFFAIR":
        error = error or ResultCode.ACTION_NOT_ALLOWED
    end = min(world_minute, started + command.duration_minutes)
    # Stop at the first work threshold, even after reconnect.
    while cursor < end and not error:
        error = settle_action_needs_locked(runtime, "REPAIR_LABOR", cursor, cursor + 1, base_until=cursor + 1)
        cursor += 1
        error = error or repair_labor_unavailable(runtime, cursor, params)
    params["_repair_needs_until"] = cursor
    command.sanitized_parameters = params
    command.save(update_fields=["sanitized_parameters", "updated_at"])
    runtime.save()
    return error


def _trunc_div(numerator: int, denominator: int) -> int:
    return (abs(numerator) // denominator) * (1 if numerator >= 0 else -1)


def public_needs(runtime: NpcRuntimeState) -> Dict[str, float]:
    return {name: getattr(runtime, f"{name}_fp") / NEEDS_SCALE for name in NEEDS_FIELDS}


def need_bands(runtime: NpcRuntimeState) -> Dict[str, str]:
    def band(value: int, *, inverse: bool = False) -> str:
        if inverse:
            return "CRITICAL" if value <= 100 else "LOW" if value <= 300 else "NORMAL"
        return "CRITICAL" if value >= 900 else "HIGH" if value >= 700 else "NORMAL"
    return {
        "hunger": band(runtime.hunger_fp),
        "energy": band(runtime.energy_fp, inverse=True),
        "health": band(runtime.health_fp, inverse=True),
        "social": band(runtime.social_fp),
        "stress": band(runtime.stress_fp),
    }


def _critical_band_crossed(before: Dict[str, str], after: Dict[str, str]) -> Iterable[str]:
    """Return needs that entered CRITICAL during one authoritative update."""
    return (
        name for name in ("hunger", "social", "stress", "energy")
        if before.get(name) != "CRITICAL" and after.get(name) == "CRITICAL"
    )


def needs_rules() -> Dict[str, Any]:
    """Return the authoritative Agent-facing interpretation of public needs."""
    return {
        "scale": {"minimum": 0, "maximum": 100},
        "directions": {
            "hunger": "LOWER_IS_BETTER",
            "energy": "HIGHER_IS_BETTER",
            "health": "HIGHER_IS_BETTER",
            "social": "LOWER_IS_BETTER",
            "stress": "LOWER_IS_BETTER",
        },
        "bands": {
            "higher_is_worse": {"high_at": 70, "critical_at": 90},
            "lower_is_worse": {"low_at": 30, "critical_at": 10},
        },
        "action_gates": {
            action: {
                **({"hunger_below": CommandService.NEED_GATE_LIMITS["hunger"] / NEEDS_SCALE,
                    "energy_above": CommandService.NEED_GATE_LIMITS["energy"] / NEEDS_SCALE}
                   if action in CommandService.WORK_ACTIONS else {}),
                "health_above": CommandService.NEED_GATE_LIMITS["health"] / NEEDS_SCALE,
            }
            for action in sorted(CommandService.ALLOWED_ACTIONS - CommandService.LOW_HEALTH_ACTIONS - {"SOCIALIZE"})
        },
        "effects_per_30_minutes": {
            activity: {name: rate / NEEDS_SCALE for name, rate in rates.items()}
            for activity, rates in RATES.items()
        },
        "base_effects_per_30_minutes": {
            name: rate / NEEDS_SCALE for name, rate in BASE_RATES.items()
        },
        "behavior_delta_effects_per_30_minutes": {
            activity: {name: rate / NEEDS_SCALE for name, rate in rates.items()}
            for activity, rates in ACTION_DELTAS.items()
        },
        "eat_completion_effect": {"hunger": -45, "energy": 5},
        "socialize_daily_effect_percent": [100, 50, 25],
        "daily_health": {
            "minimum_sleep_minutes": HEALTHY_SLEEP_MINUTES,
            "minimum_continuous_sleep_minutes": HEALTHY_SLEEP_MINUTES,
            "severe_hunger_all_day": -8,
            "ate_and_slept_at_least_360_minutes_and_not_injured": 2,
        },
        "sleep": {
            "full_effect_minutes": SLEEP_FULL_MINUTES,
            "low_gain_after_minutes": SLEEP_FULL_MINUTES,
            "no_gain_after_minutes": SLEEP_MAX_MINUTES,
            "continuation_gap_minutes": SLEEP_CONTINUATION_GAP_MINUTES,
            "curve": "F(t)=(t/480)^2 for t<=480; then +10% through 600; capped at 110%",
            "core_night_window": {"start": "23:00", "end": "04:00"},
            "core_night_penalty_if_zero_minutes": {"health": -5},
        },
    }


def _config_enabled() -> bool:
    return bool(VillageSimulationConfig.objects.filter(config_key="default", village_needs_enabled=True).exists())


def _result(success: bool, code: str, **data: Any) -> Dict[str, Any]:
    return {"success": success, "result_code": code, **data}


class NeedsService:
    """Fixed-point advancement under the authority time -> runtime lock order."""

    @staticmethod
    def validate_intervals(start: int, end: int, intervals: Any) -> Optional[str]:
        if not isinstance(intervals, list) or not intervals or len(intervals) > MAX_INTERVALS:
            return ResultCode.INVALID_ACTIVITY_INTERVAL
        cursor = start
        seen = set()
        for interval in intervals:
            if not isinstance(interval, dict) or set(interval) != {
                "interval_id", "start_world_minute", "end_world_minute", "activity"
            }:
                return ResultCode.INVALID_ACTIVITY_INTERVAL
            try:
                interval_id = uuid.UUID(str(interval["interval_id"]))
            except (ValueError, TypeError):
                return ResultCode.INVALID_ACTIVITY_INTERVAL
            if interval_id in seen:
                return ResultCode.INVALID_ACTIVITY_INTERVAL
            seen.add(interval_id)
            interval_start = interval["start_world_minute"]
            interval_end = interval["end_world_minute"]
            if (
                isinstance(interval_start, bool) or isinstance(interval_end, bool)
                or not isinstance(interval_start, int) or not isinstance(interval_end, int)
                or interval_start != cursor or interval_end <= interval_start
                or interval["activity"] not in ACTIVITIES
            ):
                return ResultCode.INVALID_ACTIVITY_INTERVAL
            cursor = interval_end
        return None if cursor == end else ResultCode.INVALID_ACTIVITY_INTERVAL

    @staticmethod
    def _summary(runtime: NpcRuntimeState, day_index: int) -> NpcNeedsDaySummary:
        summary, _ = NpcNeedsDaySummary.objects.select_for_update().get_or_create(
            npc=runtime.npc,
            day_index=day_index,
        )
        return summary

    @staticmethod
    def _apply_rate(runtime: NpcRuntimeState, name: str, rate: int, minutes: int) -> None:
        remainder_name = f"{name}_remainder"
        value_name = f"{name}_fp"
        numerator = rate * 100 * minutes + getattr(runtime, remainder_name)
        delta = _trunc_div(numerator, 3000)
        setattr(runtime, remainder_name, numerator - delta * 3000)
        setattr(runtime, value_name, max(0, min(1000, getattr(runtime, value_name) + delta)))

    @staticmethod
    def _apply_scaled_rate(runtime: NpcRuntimeState, name: str, rate: int, minutes: int, percent: int) -> None:
        """Apply a percentage rate once, retaining sub-fixed-point residue."""
        remainder_name = f"{name}_remainder"
        value_name = f"{name}_fp"
        numerator = rate * percent * minutes + getattr(runtime, remainder_name)
        delta = _trunc_div(numerator, 3000)
        residue = numerator - delta * 3000
        setattr(runtime, remainder_name, residue)
        setattr(runtime, value_name, max(0, min(1000, getattr(runtime, value_name) + delta)))

    @staticmethod
    def _sleep_curve_percent(minutes: int) -> int:
        """Cumulative sleep effect, rounded to whole percentage points."""
        minutes = max(0, int(minutes))
        if minutes <= SLEEP_FULL_MINUTES:
            return (minutes * minutes * 100 + (SLEEP_FULL_MINUTES ** 2) // 2) // (SLEEP_FULL_MINUTES ** 2)
        if minutes <= SLEEP_MAX_MINUTES:
            return 100 + ((minutes - SLEEP_FULL_MINUTES) * 10 + 60) // 120
        return 110

    @staticmethod
    def _reset_sleep_state(runtime: NpcRuntimeState) -> None:
        runtime.continuous_sleep_minutes = 0
        runtime.last_sleep_end_world_minute = None

    @staticmethod
    def _summary_update_fields() -> list[str]:
        return [
            "sleep_minutes", "severe_hunger_minutes", "longest_continuous_sleep_minutes",
            "core_night_sleep_minutes", "core_night_evaluated", "core_night_penalized", "updated_at",
        ]

    @classmethod
    def _emit_needs_urgent(cls, runtime: NpcRuntimeState, need: str, world_minute: int) -> None:
        """Wake one NPC once when a need crosses into CRITICAL."""
        dedupe_key = f"needs-urgent:{runtime.npc.agent_id}:{need}:{world_minute}"[:128]
        event, created = VillageWorldEvent.objects.get_or_create(
            dedupe_key=dedupe_key,
            defaults={
                "event_type": "NPC_NEEDS_URGENT",
                "world_minute": world_minute,
                "time_revision": GlobalGameTimeState.objects.filter(pk=1).values_list(
                    "time_revision", flat=True
                ).first() or 0,
                "actor_npc": runtime.npc,
                "participant_npc_ids": [str(runtime.npc.agent_id)],
                "location_code": runtime.current_location_code,
                "request_id": uuid.uuid5(uuid.NAMESPACE_URL, dedupe_key),
                "effects": {},
                "visibility": "PARTICIPANTS",
                "public_data": {
                    "need": need,
                    "value": getattr(runtime, f"{need}_fp") / NEEDS_SCALE,
                    "reason": "ENTERED_CRITICAL",
                },
                "player_summary": f"{runtime.npc.name}的{need}需求已进入危急状态。",
                "hub_summary": f"你的{need}需求已进入危急状态，请优先处理。",
            },
        )
        if created:
            from .phase4_visibility import enqueue_event_deliveries
            enqueue_event_deliveries(event)

    @classmethod
    def _notify_critical_transitions(
        cls, runtime: NpcRuntimeState, before: Dict[str, str], world_minute: int,
    ) -> None:
        after = need_bands(runtime)
        for need in _critical_band_crossed(before, after):
            cls._emit_needs_urgent(runtime, need, world_minute)

    @staticmethod
    def _night_index_for_minute(world_minute: int) -> Optional[int]:
        minute_of_day = world_minute % MINUTES_PER_DAY
        day_index = world_minute // MINUTES_PER_DAY
        if minute_of_day >= 23 * 60:
            return day_index + 1
        if minute_of_day < 4 * 60:
            return day_index
        return None

    @classmethod
    def _pending_sleep_overlaps_night(
        cls, runtime: NpcRuntimeState, night_index: int, through_minute: int,
    ) -> bool:
        if runtime.action_status != "SLEEPING" or runtime.current_action != "SLEEP":
            return False
        start = runtime.action_started_world_minute
        end = runtime.action_ends_world_minute
        if start is None or end is None:
            return False
        night_start = night_index * MINUTES_PER_DAY - 60
        night_end = night_index * MINUTES_PER_DAY + 4 * 60
        return max(start, night_start) < min(end, night_end, through_minute)

    @classmethod
    def evaluate_core_nights_locked(cls, runtime: NpcRuntimeState, through_minute: int) -> None:
        """Apply the 23:00-04:00 penalty for each night reached by a cursor."""
        last_night = through_minute // MINUTES_PER_DAY
        existing_indices = NpcNeedsDaySummary.objects.filter(
            npc=runtime.npc, day_index__gte=0, day_index__lte=last_night,
            core_night_evaluated=False,
        ).values_list("day_index", flat=True)
        for night_index in existing_indices:
            night_end = night_index * MINUTES_PER_DAY + 4 * 60
            if night_end > through_minute:
                continue
            summary = cls._summary(runtime, night_index)
            if summary.core_night_evaluated:
                continue
            slept = summary.core_night_sleep_minutes > 0 or cls._pending_sleep_overlaps_night(
                runtime, night_index, through_minute
            )
            if not slept:
                runtime.health_fp = max(0, runtime.health_fp - 50)
            summary.core_night_evaluated = True
            summary.core_night_penalized = not slept
            summary.save(update_fields=cls._summary_update_fields())

    @classmethod
    def advance_social_scaled_locked(
        cls, runtime: NpcRuntimeState, start: int, end: int, percent: int,
    ) -> Optional[str]:
        """Advance one same-day SOCIALIZE span with exact scaled base rates.

        The severe-hunger stress rate is deliberately not scaled. A denominator
        of 3000 preserves percentage-rate residue even for one-minute spans.
        """
        if runtime.needs_world_minute != start or start // MINUTES_PER_DAY != (end - 1) // MINUTES_PER_DAY:
            return ResultCode.INVALID_ACTIVITY_INTERVAL
        summary = cls._summary(runtime, start // MINUTES_PER_DAY)
        social_numerator = runtime.social_remainder
        stress_numerator = runtime.stress_remainder
        for _minute in range(start, end):
            severe = runtime.hunger_fp >= 900
            if severe:
                summary.severe_hunger_minutes += 1
            cls._apply_rate(runtime, "hunger", 20, 1)

            social_numerator += -80 * percent
            social_delta = _trunc_div(social_numerator, 3000)
            social_numerator -= social_delta * 3000
            runtime.social_fp = max(0, min(1000, runtime.social_fp + social_delta))

            stress_numerator += -30 * percent + (2000 if severe else 0)
            stress_delta = _trunc_div(stress_numerator, 3000)
            stress_numerator -= stress_delta * 3000
            runtime.stress_fp = max(0, min(1000, runtime.stress_fp + stress_delta))

        runtime.social_remainder = social_numerator
        runtime.stress_remainder = stress_numerator
        runtime.needs_world_minute = end
        summary.save(update_fields=["severe_hunger_minutes", "updated_at"])
        return None

    @classmethod
    def _apply_segment(cls, runtime: NpcRuntimeState, activity: str, start: int, end: int) -> None:
        if activity == "SLEEP":
            cls._apply_sleep_segment(runtime, start, end, RATES["SLEEP"])
            return
        cls._reset_sleep_state(runtime)
        day_index = start // MINUTES_PER_DAY
        summary = cls._summary(runtime, day_index)
        # A one-minute loop makes the severe-hunger boundary exact even when a
        # segment crosses 90.0 halfway through a 30-minute fixed-point rate.
        # ponytail: one-minute iteration; batch at hunger thresholds if long spans become costly.
        for _minute in range(start, end):
            before_bands = need_bands(runtime)
            severe = runtime.hunger_fp >= 900
            if severe:
                summary.severe_hunger_minutes += 1
            if activity == "SLEEP":
                summary.sleep_minutes += 1
            for name, rate in RATES[activity].items():
                cls._apply_rate(runtime, name, rate, 1)
            if severe:
                cls._apply_rate(runtime, "stress", 20, 1)
            cls._notify_critical_transitions(runtime, before_bands, _minute + 1)
        summary.save(update_fields=["sleep_minutes", "severe_hunger_minutes", "updated_at"])

    @classmethod
    def _apply_base_segment(cls, runtime: NpcRuntimeState, start: int, end: int) -> None:
        """Apply only the fixed world-time Needs base for one day segment."""
        last_sleep_end = runtime.last_sleep_end_world_minute
        if (
            runtime.action_status != "SLEEPING"
            and last_sleep_end is not None
            and end - last_sleep_end > SLEEP_CONTINUATION_GAP_MINUTES
        ):
            cls._reset_sleep_state(runtime)
        summary = cls._summary(runtime, start // MINUTES_PER_DAY)
        for _minute in range(start, end):
            before_bands = need_bands(runtime)
            severe = runtime.hunger_fp >= 900
            if severe:
                summary.severe_hunger_minutes += 1
                cls._apply_rate(runtime, "stress", 20, 1)
            for name, rate in BASE_RATES.items():
                cls._apply_rate(runtime, name, rate, 1)
            cls._notify_critical_transitions(runtime, before_bands, _minute + 1)
        summary.save(update_fields=["severe_hunger_minutes", "updated_at"])

    @classmethod
    def advance_base_locked(
        cls, runtime: NpcRuntimeState, target: int, *, finalize_days: bool = True,
        evaluate_core_nights: bool = True,
    ) -> Optional[str]:
        """Advance a runtime to target using only the fixed base rates."""
        if target < runtime.needs_world_minute:
            return ResultCode.STALE_NEEDS_MINUTE
        if target == runtime.needs_world_minute:
            return None
        cursor = runtime.needs_world_minute
        while cursor < target:
            next_day = ((cursor // MINUTES_PER_DAY) + 1) * MINUTES_PER_DAY
            segment_end = min(target, next_day)
            cls._apply_base_segment(runtime, cursor, segment_end)
            sleep_still_running = (
                runtime.action_status == "SLEEPING"
                and runtime.action_ends_world_minute is not None
                and runtime.action_ends_world_minute >= segment_end
            )
            if finalize_days and segment_end == next_day and not sleep_still_running:
                cls._finalize_day(runtime, cursor // MINUTES_PER_DAY)
            cursor = segment_end
        runtime.needs_world_minute = target
        if evaluate_core_nights:
            cls.evaluate_core_nights_locked(runtime, target)
        return None

    @classmethod
    def _apply_extra_segment(
        cls, runtime: NpcRuntimeState, activity: str, start: int, end: int,
        multiplier_percent: int = 100,
    ) -> None:
        if activity == "SLEEP":
            cls._apply_sleep_segment(runtime, start, end, ACTION_DELTAS["SLEEP"])
            return
        cls._reset_sleep_state(runtime)
        summary = cls._summary(runtime, start // MINUTES_PER_DAY)
        deltas = ACTION_DELTAS[activity]
        for _minute in range(start, end):
            before_bands = need_bands(runtime)
            if activity == "SLEEP":
                summary.sleep_minutes += 1
            for name, rate in deltas.items():
                if multiplier_percent == 100:
                    cls._apply_rate(runtime, name, rate, 1)
                else:
                    cls._apply_scaled_rate(runtime, name, rate, 1, multiplier_percent)
            cls._notify_critical_transitions(runtime, before_bands, _minute + 1)
        summary.save(update_fields=["sleep_minutes", "updated_at"])

    @classmethod
    def _apply_sleep_segment(
        cls, runtime: NpcRuntimeState, start: int, end: int, rates: Dict[str, int],
    ) -> None:
        """Apply a sleep span using one cumulative curve.

        ``rates`` is either the full activity rate (Needs.advance) or only the
        action delta (life-action settlement, whose base was already applied).
        """
        if end <= start:
            return
        last_end = runtime.last_sleep_end_world_minute
        continuous = runtime.continuous_sleep_minutes
        if last_end is None or start < last_end or start - last_end > SLEEP_CONTINUATION_GAP_MINUTES:
            continuous = 0
        summaries: Dict[int, NpcNeedsDaySummary] = {}
        for world_minute in range(start, end):
            before_bands = need_bands(runtime)
            old_percent = cls._sleep_curve_percent(continuous)
            continuous += 1
            new_percent = cls._sleep_curve_percent(continuous)
            percent_delta = new_percent - old_percent
            for name, rate in rates.items():
                # 480 minutes at 100% represents one complete sleep effect;
                # the existing 3000-denominator remainder keeps this additive.
                if percent_delta:
                    cls._apply_scaled_rate(runtime, name, rate, SLEEP_FULL_MINUTES, percent_delta)

            day_index = world_minute // MINUTES_PER_DAY
            summary = summaries.setdefault(day_index, cls._summary(runtime, day_index))
            summary.sleep_minutes += 1
            summary.longest_continuous_sleep_minutes = max(
                summary.longest_continuous_sleep_minutes, continuous
            )
            night_index = cls._night_index_for_minute(world_minute)
            if night_index is not None:
                night_summary = summaries.setdefault(night_index, cls._summary(runtime, night_index))
                night_summary.core_night_sleep_minutes += 1
            cls._notify_critical_transitions(runtime, before_bands, world_minute + 1)

        runtime.continuous_sleep_minutes = continuous
        runtime.last_sleep_end_world_minute = end
        for summary in summaries.values():
            summary.save(update_fields=cls._summary_update_fields())

    @classmethod
    def apply_behavior_delta_locked(
        cls, runtime: NpcRuntimeState, start: int, end: int, activity: str,
        multiplier_percent: int = 100,
    ) -> Optional[str]:
        """Apply one action's incremental effect without moving the Needs cursor."""
        if activity not in ACTION_DELTAS or end <= start:
            return ResultCode.INVALID_ACTIVITY_INTERVAL if end <= start else ResultCode.UNKNOWN_ACTION
        cursor = start
        while cursor < end:
            segment_end = min(end, ((cursor // MINUTES_PER_DAY) + 1) * MINUTES_PER_DAY)
            cls._apply_extra_segment(runtime, activity, cursor, segment_end, multiplier_percent)
            cursor = segment_end
        return None

    @classmethod
    def _finalize_day(cls, runtime: NpcRuntimeState, day_index: int) -> None:
        summary = cls._summary(runtime, day_index)
        if summary.finalized:
            return
        if summary.severe_hunger_minutes >= MINUTES_PER_DAY:
            runtime.health_fp = max(0, runtime.health_fp - 80)
        elif (
            summary.successful_meals >= 1
            and summary.longest_continuous_sleep_minutes >= HEALTHY_SLEEP_MINUTES
            and not summary.injured
        ):
            runtime.health_fp = min(1000, runtime.health_fp + 20)
        summary.finalized = True
        summary.save(update_fields=["finalized", "updated_at"])

    @classmethod
    def advance_locked(
        cls,
        runtime: NpcRuntimeState,
        target: int,
        intervals: Sequence[Dict[str, Any]],
        *,
        finalize_days: bool = True,
    ) -> Optional[str]:
        if target < runtime.needs_world_minute:
            return ResultCode.STALE_NEEDS_MINUTE
        if target == runtime.needs_world_minute:
            return None
        validation = cls.validate_intervals(runtime.needs_world_minute, target, intervals)
        if validation:
            return validation
        for interval in intervals:
            cursor = interval["start_world_minute"]
            interval_end = interval["end_world_minute"]
            while cursor < interval_end:
                next_day = ((cursor // MINUTES_PER_DAY) + 1) * MINUTES_PER_DAY
                segment_end = min(interval_end, next_day)
                cls._apply_segment(runtime, interval["activity"], cursor, segment_end)
                if finalize_days and segment_end == next_day:
                    cls._finalize_day(runtime, cursor // MINUTES_PER_DAY)
                cursor = segment_end
        runtime.needs_world_minute = target
        cls.evaluate_core_nights_locked(runtime, target)
        return None

    @classmethod
    @transaction.atomic
    def advance(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        forbidden = {key for key in payload if key in NEEDS_FIELDS or key.endswith("_fp") or key.endswith("_remainder")}
        allowed = {"npc_id", "advance_to_world_minute", "activity_intervals", "expected_state_version", "tick_id"}
        if forbidden:
            return _result(False, ResultCode.NEEDS_FIELDS_NOT_ALLOWED)
        if set(payload) != allowed:
            return _result(False, ResultCode.INVALID_COMMAND_SCHEMA)
        try:
            tick_id = uuid.UUID(str(payload["tick_id"]))
        except (ValueError, TypeError):
            return _result(False, ResultCode.INVALID_COMMAND_SCHEMA)
        if not _config_enabled():
            return _result(False, ResultCode.VILLAGE_NEEDS_DISABLED)
        existing = VillageActionReceipt.objects.filter(request_id=tick_id).first()
        payload_hash = _hash(payload)
        if existing:
            if existing.action_kind == "ADVANCE_NEEDS" and existing.response_data.get("request_payload_sha256") == payload_hash:
                public_response = {key: value for key, value in existing.response_data.items() if key != "request_payload_sha256"}
                return _result(True, ResultCode.COMMAND_REPLAYED, receipt=existing, **public_response)
            return _result(False, ResultCode.REQUEST_ID_CONFLICT)
        if not isinstance(payload["advance_to_world_minute"], int) or not isinstance(payload["expected_state_version"], int):
            return _result(False, ResultCode.INVALID_COMMAND_SCHEMA)
        try:
            time_state = GlobalGameTimeState.objects.select_for_update().get(pk=1)
        except GlobalGameTimeState.DoesNotExist:
            return _result(False, ResultCode.BACKEND_UNAVAILABLE)
        # The global authority lock serializes receipt creation. Re-check under
        # that lock so concurrent delivery of the same tick is a true replay.
        existing = VillageActionReceipt.objects.filter(request_id=tick_id).first()
        if existing:
            if existing.action_kind == "ADVANCE_NEEDS" and existing.response_data.get("request_payload_sha256") == payload_hash:
                public_response = {key: value for key, value in existing.response_data.items() if key != "request_payload_sha256"}
                return _result(True, ResultCode.COMMAND_REPLAYED, receipt=existing, **public_response)
            return _result(False, ResultCode.REQUEST_ID_CONFLICT)
        try:
            runtime = NpcRuntimeState.objects.select_for_update().select_related("npc").get(
                npc__agent_id=str(payload["npc_id"]), npc__is_test_only=False
            )
        except NpcRuntimeState.DoesNotExist:
            return _result(False, ResultCode.BACKEND_UNAVAILABLE)
        if payload["advance_to_world_minute"] > time_state.world_minute:
            return _result(False, ResultCode.STALE_NEEDS_MINUTE)
        if runtime.state_version != payload["expected_state_version"]:
            return _result(False, ResultCode.STALE_STATE_VERSION, state_version=runtime.state_version)
        config = VillageSimulationConfig.objects.filter(config_key="default").first()
        max_catchup_days = max(1, config.max_catchup_days if config else 3)
        if payload["advance_to_world_minute"] - runtime.needs_world_minute > max_catchup_days * MINUTES_PER_DAY:
            return _result(False, ResultCode.STALE_NEEDS_MINUTE)
        changed = payload["advance_to_world_minute"] != runtime.needs_world_minute
        code = cls.advance_locked(runtime, payload["advance_to_world_minute"], payload["activity_intervals"])
        if code:
            return _result(False, code)
        if changed:
            runtime.state_version += 1
            runtime.time_revision = time_state.time_revision
            runtime.save()
        data = {"request_payload_sha256": payload_hash, "public_needs": public_needs(runtime), "needs_world_minute": runtime.needs_world_minute, "state_version": runtime.state_version}
        receipt = VillageActionReceipt.objects.create(
            request_id=tick_id, npc=runtime.npc, action_kind="ADVANCE_NEEDS", success=True,
            result_code=ResultCode.LIFE_ACTION_COMPLETED, response_data=data,
            world_minute=time_state.world_minute, time_revision=time_state.time_revision,
            state_version=runtime.state_version,
        )
        public_response = {key: value for key, value in data.items() if key != "request_payload_sha256"}
        return _result(True, ResultCode.LIFE_ACTION_COMPLETED, receipt=receipt, **public_response)


class _TickRejected(Exception):
    def __init__(self, result: Dict[str, Any]):
        self.result = result


class TickService:
    """Persist Unity's clock and apply elapsed-time Needs for formal NPCs."""

    @classmethod
    def advance(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {"world_minute", "time_revision", "request_id"}
        legacy_allowed = {"world_minute", "expected_time_revision", "request_id"}
        optional = {"needs_requests", "deferred_hunt_command_ids"}
        if not any(set(payload) - optional == candidate for candidate in (allowed, legacy_allowed)):
            return _result(False, ResultCode.INVALID_COMMAND_SCHEMA)
        deferred = payload.get("deferred_hunt_command_ids", [])
        if not isinstance(deferred, list) or len(deferred) > 32:
            return _result(False, ResultCode.INVALID_COMMAND_SCHEMA)
        try:
            parsed_deferred = [uuid.UUID(value) for value in deferred if isinstance(value, str)]
            if len(parsed_deferred) != len(deferred) or len(set(parsed_deferred)) != len(deferred):
                return _result(False, ResultCode.INVALID_COMMAND_SCHEMA)
        except (ValueError, TypeError, AttributeError):
            return _result(False, ResultCode.INVALID_COMMAND_SCHEMA)
        try:
            request_id = uuid.UUID(str(payload["request_id"]))
        except (ValueError, TypeError):
            return _result(False, ResultCode.INVALID_COMMAND_SCHEMA)
        if (isinstance(payload["world_minute"], bool)
                or isinstance(payload.get("time_revision", payload.get("expected_time_revision")), bool)
                or not isinstance(payload["world_minute"], int)
                or not isinstance(payload.get("time_revision", payload.get("expected_time_revision")), int)):
            return _result(False, ResultCode.INVALID_COMMAND_SCHEMA)
        if "time_revision" not in payload:
            # Legacy field was the pre-Unity CAS revision; Unity's resulting
            # revision is one greater.  New callers must send time_revision.
            payload["time_revision"] = payload.pop("expected_time_revision") + 1
        # Accept the old envelope during rollout, but never trust its activity
        # intervals: the Tick has one authoritative base rate for every NPC.
        legacy_requests = payload.get("needs_requests")
        if legacy_requests is not None:
            if not isinstance(legacy_requests, list):
                return _result(False, ResultCode.INVALID_COMMAND_SCHEMA)
            if any(not isinstance(item, dict) or "npc_id" not in item for item in legacy_requests):
                return _result(False, ResultCode.INVALID_COMMAND_SCHEMA)
            npc_ids = [str(item["npc_id"]) for item in legacy_requests]
            if len(npc_ids) != len(legacy_requests) or len(npc_ids) != len(set(npc_ids)):
                return _result(False, ResultCode.INVALID_COMMAND_SCHEMA)
            if any(item.get("advance_to_world_minute") != payload["world_minute"] for item in legacy_requests):
                return _result(False, ResultCode.INVALID_COMMAND_SCHEMA)

        payload_hash = _hash(payload)
        existing = VillageActionReceipt.objects.filter(request_id=request_id).first()
        if existing:
            if (existing.action_kind == "VILLAGE_TICK"
                    and existing.response_data.get("request_payload_sha256") == payload_hash):
                data = {key: value for key, value in existing.response_data.items()
                        if key != "request_payload_sha256"}
                return _result(True, ResultCode.COMMAND_REPLAYED, receipt=existing, **data)
            return _result(False, ResultCode.REQUEST_ID_CONFLICT)
        # A second delivery of the same Unity clock observation is a replay
        # even if transport assigned a new request id.
        existing_tick = VillageActionReceipt.objects.filter(
            action_kind="VILLAGE_TICK", world_minute=payload["world_minute"]
        ).first()
        if existing_tick:
            data = {key: value for key, value in existing_tick.response_data.items()
                    if key != "request_payload_sha256"}
            return _result(True, ResultCode.COMMAND_REPLAYED, receipt=existing_tick, **data)
        try:
            return cls._advance_atomic(payload, request_id, payload_hash)
        except _TickRejected as rejected:
            return rejected.result

    @staticmethod
    @transaction.atomic
    def _advance_atomic(payload: Dict[str, Any], request_id: uuid.UUID, payload_hash: str) -> Dict[str, Any]:
        # Unity already advanced its authoritative clock.  Backend only
        # accepts the monotonic observation, then applies elapsed-time
        # projections atomically with the NPC updates.
        success, code, state = TimeService.receive_time(payload["world_minute"], payload["time_revision"])
        if not success:
            raise _TickRejected(_result(False, code))

        from .caravan import sync_visit
        sync_visit(state.world_minute)

        receipt = VillageActionReceipt.objects.create(
            request_id=request_id,
            action_kind="VILLAGE_TICK",
            success=True,
            result_code=ResultCode.COMMAND_ACCEPTED,
            world_minute=state.world_minute,
            time_revision=state.time_revision,
        )

        legacy_versions = {
            str(item["npc_id"]): item.get("expected_state_version")
            for item in payload.get("needs_requests", [])
        }
        runtimes = list(
            NpcRuntimeState.objects.select_for_update().select_related("npc")
            .filter(npc__is_test_only=False)
            .order_by("npc__agent_id")
        )
        npc_results = {}
        deferred_hunts = {uuid.UUID(value) for value in payload.get("deferred_hunt_command_ids", [])}
        for runtime in runtimes:
            npc_id = str(runtime.npc.agent_id)
            interrupted_command_id = None
            if runtime.active_command_id:
                from .phase5_contract import is_repair_labor
                from .runtime_services import ActionService
                command = NpcCommandState.objects.select_for_update().filter(
                    command_id=runtime.active_command_id, status="RUNNING").first()
                if command and command.kind == "HUNT" and command.pk in deferred_hunts:
                    from .models import VillageHuntAttempt
                    if VillageHuntAttempt.objects.filter(command=command, status="ACTIVE", prey__status="CLAIMED").exists():
                        # Env has a durable death waiting for clock/settlement.
                        # Keep this NPC's exact needs cursor until complete;
                        # advance world time and every other NPC normally.
                        npc_results[npc_id] = {
                            "interrupted_command_id": None, "deferred_hunt": True,
                            "public_needs": public_needs(runtime),
                            "needs_world_minute": runtime.needs_world_minute,
                            "state_version": runtime.state_version,
                        }
                        continue
                if command and command.kind == "CHOP_TREE" and command.pk in deferred_hunts:
                    from .models import VillageChopAttempt
                    if VillageChopAttempt.objects.filter(command=command, status="ACTIVE", node__status="CLAIMED").exists():
                        npc_results[npc_id] = {"interrupted_command_id": None, "deferred_hunt": True,
                            "public_needs": public_needs(runtime), "needs_world_minute": runtime.needs_world_minute,
                            "state_version": runtime.state_version}
                        continue
                from .jack_production import is_jack_work, settle_work_needs
                if command and (is_repair_labor(command.kind, command.sanitized_parameters) or is_jack_work(command)):
                    settle = settle_work_needs if is_jack_work(command) else settle_repair_needs_locked
                    error = settle(runtime, command, state.world_minute)
                    if error:
                        was_away = runtime.action_status == "AWAY" or (runtime.away_until_world_minute is not None and runtime.away_until_world_minute > state.world_minute)
                        ok, _, _ = ActionService.complete_action(command.command_id, command.request_id, False, error)
                        if ok:
                            interrupted_command_id = str(command.command_id)
                            runtime.refresh_from_db()
                            if was_away:
                                runtime.action_status = "AWAY"
                                runtime.save(update_fields=["action_status"])
            if npc_id in legacy_versions and runtime.state_version != legacy_versions[npc_id]:
                raise _TickRejected(_result(
                    False, ResultCode.STALE_STATE_VERSION, npc_id=npc_id,
                    state_version=runtime.state_version,
                ))
            if runtime.needs_world_minute >= payload["world_minute"]:
                npc_results[npc_id] = {
                    "interrupted_command_id": interrupted_command_id,
                    "public_needs": public_needs(runtime),
                    "needs_world_minute": runtime.needs_world_minute,
                    "state_version": runtime.state_version,
                }
                continue
            error = NeedsService.advance_base_locked(runtime, payload["world_minute"])
            if error:
                raise _TickRejected(_result(False, error, npc_id=npc_id))
            # A clock-only needs update is expected between accept and start.
            # Rebase only commands validated against exactly this prior version;
            # intervening action/checkpoint mutations still fail STRICT checks.
            NpcCommandState.objects.filter(npc=runtime.npc, status__in=["RECEIVED", "QUEUED"],
                validated_at_state_version=runtime.state_version).update(validated_at_state_version=runtime.state_version + 1)
            runtime.state_version += 1
            runtime.time_revision = state.time_revision
            runtime.save()
            npc_results[npc_id] = {
                "interrupted_command_id": interrupted_command_id,
                "public_needs": public_needs(runtime),
                "needs_world_minute": runtime.needs_world_minute,
                "state_version": runtime.state_version,
            }

        from .caravan import projection
        caravan = projection(state.world_minute)
        from .player_farm import project_plot
        from .models import VillageFarmPlot
        farm_plots = [project_plot(plot, state.world_minute) for plot in
            VillageFarmPlot.objects.select_related("lessee_player").order_by("plot_code")]
        data = {
            "caravan": caravan,
            "farm_plots": farm_plots,
            "request_payload_sha256": payload_hash,
            "world_minute": receipt.world_minute,
            "time_revision": receipt.time_revision,
            "npc_results": npc_results,
        }
        receipt.action_kind = "VILLAGE_TICK"
        receipt.response_data = data
        receipt.save(update_fields=["action_kind", "response_data"])
        from .daily_events import evaluate_daily_events
        evaluate_daily_events(state.world_minute)
        return _result(True, ResultCode.COMMAND_ACCEPTED, receipt=receipt,
                       world_minute=receipt.world_minute, time_revision=receipt.time_revision,
                       npc_results=npc_results, caravan=caravan, farm_plots=farm_plots)


class LifeActionService:
    """Complete life actions in one receipt/event/outbox transaction."""

    ACTIONS = {"EAT", "REST", "SLEEP", "SOCIALIZE", "WORK_PLACEHOLDER"}

    @staticmethod
    def _location_open(code: str) -> bool:
        location = VillageLocationDefinition.objects.filter(location_code=code, is_active=True).first()
        if not location:
            return False
        return location.opening_hours.get("alwaysOpen", True) is not False

    @staticmethod
    def _intervals(runtime: NpcRuntimeState, start: int, end: int, activity: str) -> list[Dict[str, Any]]:
        rows = []
        if runtime.needs_world_minute < start:
            rows.append({"interval_id": str(uuid.uuid4()), "start_world_minute": runtime.needs_world_minute, "end_world_minute": start, "activity": "IDLE"})
        rows.append({"interval_id": str(uuid.uuid4()), "start_world_minute": start, "end_world_minute": end, "activity": activity})
        return rows

    @staticmethod
    def _validate(payload: Dict[str, Any]) -> Optional[str]:
        fast_integration = _fast_integration_enabled()
        required = {"request_id", "command_id", "npc_id", "action", "start_world_minute", "completion_world_minute", "location_code", "expected_state_version", "params"}
        if ((not fast_integration and set(payload) != required) or
                (fast_integration and not required.issubset(payload)) or
                not isinstance(payload.get("params"), dict)):
            return ResultCode.INVALID_COMMAND_SCHEMA
        if payload["action"] in {"WORK_PLACEHOLDER", "SOCIALIZE"}:
            return "ACTION_RETIRED"
        if payload["action"] not in LifeActionService.ACTIONS:
            return ResultCode.UNKNOWN_ACTION
        if any(isinstance(payload[name], bool) or not isinstance(payload[name], int) for name in ("start_world_minute", "completion_world_minute", "expected_state_version")):
            return ResultCode.INVALID_COMMAND_SCHEMA
        duration = payload["completion_world_minute"] - payload["start_world_minute"]
        if duration <= 0:
            return ResultCode.INVALID_ACTION_DURATION
        action = payload["action"]
        try:
            uuid.UUID(str(payload["request_id"])); uuid.UUID(str(payload["command_id"]))
        except (ValueError, TypeError):
            return ResultCode.INVALID_COMMAND_SCHEMA
        params = payload["params"]
        action_params = {
            "EAT": {"source", "food_instance_id", "expected_asset_version"},
            "REST": set(), "SLEEP": set(), "WORK_PLACEHOLDER": set(),
            "SOCIALIZE": {"target_npc_id", "reservation_id"},
        }
        if not fast_integration and not set(params).issubset(action_params[action]):
            return ResultCode.INVALID_COMMAND_SCHEMA
        if action == "EAT":
            source = params.get("source")
            if source == "PERSONAL_INVENTORY":
                version = params.get("expected_asset_version")
                if ((not fast_integration and not {"source", "food_instance_id"}.issubset(params)) or
                        (not fast_integration and version is not None and
                         (isinstance(version, bool) or not isinstance(version, int)))):
                    return ResultCode.INVALID_COMMAND_SCHEMA
            elif source == "COMMUNITY_MEAL":
                if not fast_integration and set(params) != {"source"}:
                    return ResultCode.INVALID_COMMAND_SCHEMA
            else:
                return ResultCode.FOOD_NOT_ALLOWED
        if action == "SOCIALIZE" and not fast_integration and set(params) != action_params[action]:
            return ResultCode.INVALID_COMMAND_SCHEMA
        return None

    @staticmethod
    def _lock_runtimes(npc_ids: Iterable[str]) -> list[NpcRuntimeState]:
        # Sorting agent ids before issuing the locking query is the stable
        # protocol order; order_by keeps PostgreSQL lock acquisition aligned.
        ids = sorted({str(npc_id) for npc_id in npc_ids})
        runtimes = list(NpcRuntimeState.objects.select_for_update().select_related("npc").filter(npc__agent_id__in=ids, npc__is_test_only=False).order_by("npc__agent_id"))
        return runtimes

    @staticmethod
    def _outbox(runtime: NpcRuntimeState, receipt: VillageActionReceipt, event: VillageWorldEvent, action: str, code: str, effects: Dict[str, Any], time_state: GlobalGameTimeState, command_id: uuid.UUID, *, success: bool = True, data: Optional[Dict[str, Any]] = None) -> None:
        from .runtime_services import _with_command_timing
        command = receipt.command
        reason_code = "" if success else code
        payload = {
            "schema_version": "village.outcome.v1", "ref_id": str(command_id),
            "original_message_id": str(command.message_id),
            "request_id": str(receipt.request_id), "npc_id": str(runtime.npc.agent_id),
            "action": action,
            "success": success, "result_code": code, "reason_code": reason_code,
            "world_event_id": str(event.event_id), "world_minute": time_state.world_minute,
            "time_revision": time_state.time_revision, "state_version": runtime.state_version,
            "current_location_code": runtime.current_location_code,
            "duration_minutes": command.duration_minutes,
            "public_needs": public_needs(runtime), "public_status": runtime.action_status,
            "retryable": False, "effects": effects, "data": _with_command_timing(data, command),
        }
        VillageDeliveryOutbox.objects.create(
            dedupe_key=f"outcome:{receipt.request_id}", recipient=command.sender_id,
            message_kind="OUTCOME", command=receipt.command, receipt=receipt, event=event, payload=payload, priority=10, status="PENDING",
        )
        # 二跑复盘（R1）：不再为生命动作事件手建 EVENT 投递行；事件已落库存档，
        # 动作类事件的广播投递由 phase4_visibility.SUPPRESS_ACTION_EVENT_DELIVERY 控制。

    @classmethod
    def _terminal_failure(cls, *, runtime: NpcRuntimeState, command: NpcCommandState, request_id: uuid.UUID, payload_hash: str, action: str, code: str, time_state: GlobalGameTimeState, location: str = "") -> Dict[str, Any]:
        """Persist one terminal Backend fact without applying action effects."""
        runtime.state_version += 1
        runtime.time_revision = time_state.time_revision
        runtime.current_action = ""
        runtime.action_status = "IDLE"
        runtime.action_started_world_minute = None
        runtime.action_ends_world_minute = None
        runtime.active_command_id = None
        runtime.save()
        if action == "SOCIALIZE":
            NpcActionReservation.objects.select_for_update().filter(
                command_id=command.command_id, status="HELD"
            ).update(status="RELEASED", active_lock_key=None, released_world_minute=time_state.world_minute)
        event = VillageWorldEvent.objects.create(
            dedupe_key=f"life-failed:{request_id}", event_type="npc_action_failed",
            world_minute=time_state.world_minute, time_revision=time_state.time_revision,
            actor_npc=runtime.npc, participant_npc_ids=[str(runtime.npc.agent_id)],
            location_code=location, command_id=command.command_id, request_id=request_id,
            effects={}, visibility="PUBLIC", public_data={"public_needs": public_needs(runtime), "reason_code": code},
        )
        command.status = "FAILED"
        command.result_code = code
        command.completed_world_minute = time_state.world_minute
        command.state_version = runtime.state_version
        command.save()
        effects = {"items": [], "money": [], "needs": {}, "relationships": []}
        response_data = {
            "request_payload_sha256": payload_hash,
            "request_id": str(request_id), "command_id": str(command.command_id),
            "reason_code": code,
            "world_event_id": str(event.event_id), "world_minute": time_state.world_minute,
            "time_revision": time_state.time_revision, "state_version": runtime.state_version,
            "public_needs": public_needs(runtime), "public_status": runtime.action_status,
            "effects": effects,
        }
        receipt = VillageActionReceipt.objects.create(
            request_id=request_id, command=command, npc=runtime.npc, action_kind=action,
            success=False, result_code=code, response_data=response_data,
            world_minute=time_state.world_minute, time_revision=time_state.time_revision,
            state_version=runtime.state_version, world_event=event,
        )
        cls._outbox(runtime, receipt, event, action, code, effects, time_state, command.command_id, success=False)
        public_response = {key: value for key, value in response_data.items() if key != "request_payload_sha256"}
        return _result(False, code, receipt=receipt, world_event=event, **public_response)

    @classmethod
    @transaction.atomic
    def execute(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        fast_integration = _fast_integration_enabled()
        validation = cls._validate(payload)
        if validation:
            return _result(False, validation)
        if not fast_integration and not _config_enabled():
            return _result(False, ResultCode.VILLAGE_NEEDS_DISABLED)
        request_id, command_id = uuid.UUID(str(payload["request_id"])), uuid.UUID(str(payload["command_id"]))
        payload_hash = _hash(payload)
        existing = VillageActionReceipt.objects.filter(request_id=request_id).first()
        if existing:
            if existing.response_data.get("request_payload_sha256") != payload_hash:
                return _result(False, ResultCode.REQUEST_ID_CONFLICT)
            public_response = {key: value for key, value in existing.response_data.items() if key != "request_payload_sha256"}
            return _result(existing.success, ResultCode.COMMAND_REPLAYED if existing.success else existing.result_code, receipt=existing, **public_response)
        try:
            time_state = GlobalGameTimeState.objects.select_for_update().get(pk=1)
        except GlobalGameTimeState.DoesNotExist:
            return _result(False, ResultCode.BACKEND_UNAVAILABLE)
        existing = VillageActionReceipt.objects.filter(request_id=request_id).first()
        if existing:
            if existing.response_data.get("request_payload_sha256") != payload_hash:
                return _result(False, ResultCode.REQUEST_ID_CONFLICT)
            public_response = {key: value for key, value in existing.response_data.items() if key != "request_payload_sha256"}
            return _result(existing.success, ResultCode.COMMAND_REPLAYED if existing.success else existing.result_code, receipt=existing, **public_response)
        if not fast_integration and payload["completion_world_minute"] > time_state.world_minute:
            return _result(False, ResultCode.STALE_NEEDS_MINUTE)

        action = payload["action"]
        target_id = str(payload["params"].get("target_npc_id", ""))
        runtime_ids = [str(payload["npc_id"])] + ([target_id] if action == "SOCIALIZE" else [])
        runtimes = cls._lock_runtimes(runtime_ids)
        if len(runtimes) != len(set(runtime_ids)):
            return _result(False, ResultCode.SOCIAL_TARGET_INVALID if action == "SOCIALIZE" else ResultCode.INVALID_COMMAND_SCHEMA)
        runtime_by_id = {str(rt.npc.agent_id): rt for rt in runtimes}
        runtime = runtime_by_id.get(str(payload["npc_id"]))
        start, end = payload["start_world_minute"], payload["completion_world_minute"]
        if runtime is None:
            return _result(False, ResultCode.INVALID_COMMAND_SCHEMA)
        try:
            command = NpcCommandState.objects.select_for_update().get(command_id=command_id)
        except NpcCommandState.DoesNotExist:
            return _result(False, ResultCode.INVALID_COMMAND_SCHEMA)

        def terminal(code: str) -> Dict[str, Any]:
            return cls._terminal_failure(
                runtime=runtime, command=command, request_id=request_id,
                payload_hash=payload_hash, action=action, code=code,
                time_state=time_state, location=payload.get("location_code", ""),
            )

        if (
            command.npc_id != runtime.npc_id or command.request_id != request_id
            or command.kind != action or command.status != "RUNNING"
            or runtime.active_command_id != command.command_id
            or command.started_world_minute != start
            or command.duration_minutes != end - start
            or runtime.action_ends_world_minute != end
        ):
            # 身份/区间不一致是终态拒绝：Unity 不会重放该动作，命令若保持 RUNNING
            # 会成僵尸且发送方永远等不到 OUTCOME。走终态失败落库并投递失败结果。
            return terminal(ResultCode.NPC_BUSY)
        if runtime.action_status not in {"ACTING", "SLEEPING"}:
            return terminal(ResultCode.NPC_BUSY)

        # A world Tick may advance this row while the action is running.  The
        # command identity checks above remain authoritative; its start-time
        # state_version is only a snapshot, not a settlement lock.
        if not fast_integration and runtime.state_version < payload["expected_state_version"]:
            return terminal(ResultCode.STALE_STATE_VERSION)
        location = payload["location_code"]
        if not fast_integration and start > time_state.world_minute:
            return terminal(ResultCode.STALE_NEEDS_MINUTE)
        if not fast_integration and action == "REST" and location == "RESTAURANT" and not cls._location_open(location):
            return terminal(ResultCode.LOCATION_CLOSED)
        if action == "WORK_PLACEHOLDER" and not fast_integration:
            if start // MINUTES_PER_DAY != (end - 1) // MINUTES_PER_DAY:
                return terminal(ResultCode.INVALID_ACTION_DURATION)
        if action == "EAT":
            source = payload["params"].get("source")
            if source == "COMMUNITY_MEAL":
                return terminal("PUBLIC_MEAL_USE_STORE")
            if source not in {"PERSONAL_INVENTORY", "COMMUNITY_MEAL"}:
                return terminal(ResultCode.FOOD_NOT_ALLOWED)
            if not fast_integration and source == "COMMUNITY_MEAL" and not cls._location_open(location):
                return terminal(ResultCode.LOCATION_CLOSED)
        if action == "SOCIALIZE":
            other = runtime_by_id.get(target_id)
            if other is None or other.npc_id == runtime.npc_id:
                return terminal(ResultCode.SOCIAL_TARGET_INVALID)
            if not fast_integration and other.action_status != "IDLE":
                return terminal(ResultCode.SOCIAL_TARGET_BUSY)
        elif not fast_integration and action != "EAT" and not cls._location_open(location):
            return terminal(ResultCode.LOCATION_CLOSED)

        # Preflight every business resource before advancing needs.  A terminal
        # failure below must therefore persist only its failure fact, never a
        # partial summary, needs cursor, inventory decrement, or reservation.
        prepared: Dict[str, Any] = {}
        if action == "SOCIALIZE":
            try:
                reservation_uuid = uuid.UUID(str(payload["params"]["reservation_id"]))
                reservation = NpcActionReservation.objects.select_for_update().get(
                    reservation_id=reservation_uuid, action_kind="SOCIALIZE",
                    command_id=command_id, request_id=request_id, status="HELD",
                )
            except (ValueError, TypeError, NpcActionReservation.DoesNotExist):
                return terminal(ResultCode.RESERVATION_CONFLICT)
            if set(reservation.participants.values_list("id", flat=True)) != {runtime.npc_id, other.npc_id}:
                return terminal(ResultCode.RESERVATION_CONFLICT)
            prepared["reservation"] = reservation
        elif action == "EAT":
            source = payload["params"]["source"]
            if source == "PERSONAL_INVENTORY":
                try:
                    food_id = uuid.UUID(str(payload["params"]["food_instance_id"]))
                    inventory = NpcGlobalInventory.objects.select_for_update().get(npc=runtime.npc)
                    expected_version = payload["params"].get("expected_asset_version")
                    if (not fast_integration and expected_version is not None and
                            expected_version != inventory.asset_version):
                        return terminal(ResultCode.ASSET_VERSION_CONFLICT)
                    slot = NpcGlobalInventorySlot.objects.select_for_update().get(bag=inventory, instance_id=food_id)
                except (ValueError, TypeError, NpcGlobalInventory.DoesNotExist, NpcGlobalInventorySlot.DoesNotExist):
                    return terminal(ResultCode.FOOD_NOT_AVAILABLE)
                item = Item.objects.filter(item_id=slot.item_id, item_type=ItemType.FOOD, is_deleted=False).first()
                if item is None:
                    return terminal(ResultCode.FOOD_NOT_ALLOWED)
                from .phase3_asset_service import get_available_slot_amount
                if get_available_slot_amount(slot, time_state.world_minute) < 1:
                    return terminal(ResultCode.FOOD_NOT_AVAILABLE)
                prepared.update(food_id=food_id, inventory=inventory, slot=slot)
            else:
                usage, _ = VillageCommunityMealDailyUsage.objects.select_for_update().get_or_create(day_index=(end - 1) // MINUTES_PER_DAY)
                key = str(runtime.npc.agent_id)
                if not fast_integration and (usage.total_meals >= 5 or int(usage.npc_usage.get(key, 0)) >= 1):
                    return terminal(ResultCode.FOOD_NOT_AVAILABLE)
                prepared.update(usage=usage, usage_key=key)

        # Bring the actor to the action start before evaluating need gates. This
        # prevents a stale cursor from bypassing hunger/energy/health limits.
        actor_needs_start = runtime.needs_world_minute
        if runtime.needs_world_minute < start:
            error = NeedsService.advance_base_locked(runtime, start)
            if error:
                return terminal(error)
        if action == "WORK_PLACEHOLDER" and not fast_integration:
            if runtime.hunger_fp >= CommandService.NEED_GATE_LIMITS["hunger"]:
                return terminal(ResultCode.HUNGER_TOO_HIGH)
            if runtime.energy_fp <= CommandService.NEED_GATE_LIMITS["energy"]:
                return terminal(ResultCode.ENERGY_TOO_LOW)
            if runtime.health_fp <= CommandService.NEED_GATE_LIMITS["health"]:
                return terminal(ResultCode.HEALTH_TOO_LOW)
        if action == "SOCIALIZE" and not fast_integration and runtime.health_fp <= CommandService.NEED_GATE_LIMITS["health"]:
            return terminal(ResultCode.HEALTH_TOO_LOW)
        if (action == "SOCIALIZE" and not fast_integration and
                start // MINUTES_PER_DAY != (end - 1) // MINUTES_PER_DAY):
            return terminal(ResultCode.INVALID_ACTION_DURATION)

        action_start_needs = public_needs(runtime)

        # EAT has no separate interval rate in the frozen activity vocabulary;
        # its only special needs effect is the committed completion effect.
        activity = "WORK" if action == "WORK_PLACEHOLDER" else "IDLE" if action == "EAT" else action
        if action != "SOCIALIZE" and runtime.needs_world_minute < end:
            error = NeedsService.advance_base_locked(
                runtime, end, finalize_days=False, evaluate_core_nights=action != "SLEEP"
            )
            if error:
                return terminal(error)
        if action != "SOCIALIZE":
            direct_needs_before = public_needs(runtime)
            error = NeedsService.apply_behavior_delta_locked(runtime, start, end, activity)
            if error:
                return terminal(error)
            if action == "SLEEP":
                NeedsService.evaluate_core_nights_locked(runtime, end)
        effects: Dict[str, Any] = {}
        if action == "SOCIALIZE":
            other = runtime_by_id[target_id]
            other_needs_start = other.needs_world_minute
            if other.needs_world_minute < start:
                other_error = NeedsService.advance_base_locked(other, start)
                if other_error:
                    return terminal(other_error)
            if not fast_integration and other.health_fp <= CommandService.NEED_GATE_LIMITS["health"]:
                return terminal(ResultCode.HEALTH_TOO_LOW)
            if runtime.needs_world_minute < end:
                actor_error = NeedsService.advance_base_locked(runtime, end, finalize_days=False)
            else:
                actor_error = None
            if other.needs_world_minute < end:
                other_error = NeedsService.advance_base_locked(other, end, finalize_days=False)
            else:
                other_error = None
            if actor_error or other_error:
                return terminal(actor_error or other_error)
            low, high = sorted((runtime, other), key=lambda rt: str(rt.npc.agent_id))
            day_index = (end - 1) // MINUTES_PER_DAY
            counter, _ = NpcSocialDailyCounter.objects.select_for_update().get_or_create(low_npc=low.npc, high_npc=high.npc, day_index=day_index)
            multiplier = 100 if counter.socialize_count == 0 else 50 if counter.socialize_count == 1 else 25
            counter.socialize_count += 1; counter.save(update_fields=["socialize_count", "updated_at"])
            direct_needs_before = public_needs(runtime)
            actor_error = NeedsService.apply_behavior_delta_locked(runtime, start, end, "SOCIALIZE", multiplier)
            other_error = NeedsService.apply_behavior_delta_locked(other, start, end, "SOCIALIZE", multiplier)
            if actor_error or other_error:
                return terminal(actor_error or other_error)
            reservation = prepared["reservation"]
            reservation.status = "COMMITTED"
            reservation.released_world_minute = end
            reservation.active_lock_key = None
            reservation.save(update_fields=["status", "released_world_minute", "active_lock_key", "updated_at"])
            effects = {"items": [], "money": [], "needs": {}, "relationships": [], "social_multiplier_percent": multiplier}
        elif action == "EAT":
            source = payload["params"]["source"]
            if source == "PERSONAL_INVENTORY":
                food_id, inventory, slot = prepared["food_id"], prepared["inventory"], prepared["slot"]
                slot.item_amount -= 1; slot.save(update_fields=["item_amount"])
                inventory.asset_version += 1; inventory.save(update_fields=["asset_version", "updated_at"])
                effects = {
                    "items": [{"owner_id": str(runtime.npc.agent_id), "item_id": slot.item_id,
                               "food_instance_id": str(food_id), "amount": 1, "delta": -1}],
                    "money": [], "needs": {}, "relationships": [],
                    "food_instance_id": str(food_id), "asset_version": inventory.asset_version,
                }
            else:
                usage, key = prepared["usage"], prepared["usage_key"]
                usage.total_meals += 1
                usage.npc_usage = {**usage.npc_usage, key: 1}
                usage.save(update_fields=["total_meals", "npc_usage", "updated_at"])
                effects = {"items": [], "money": [], "needs": {}, "relationships": [], "community_meal": True}
            from .lucy_catalog import food_recovery
            recovery = food_recovery(prepared["slot"].item_id)
            runtime.hunger_fp = max(0, runtime.hunger_fp + recovery["hunger_fp"])
            runtime.energy_fp = min(1000, runtime.energy_fp + recovery["energy_fp"])
            summary = NeedsService._summary(runtime, (end - 1) // MINUTES_PER_DAY)
            summary.successful_meals += 1; summary.save(update_fields=["successful_meals", "updated_at"])
        elif action == "REST":
            effects = {"items": [], "money": [], "needs": {}, "relationships": []}
        elif action == "SLEEP":
            effects = {"items": [], "money": [], "needs": {}, "relationships": []}
        else:
            effects = {"items": [], "money": [], "needs": {}, "relationships": []}
        action_end_needs = public_needs(runtime)

        # A life action applies its committed completion effect before any day
        # boundary health correction, exactly once for every crossed absolute
        # day. Generic Tick advancement keeps the default eager finalization.
        participant_starts = {runtime.pk: actor_needs_start}
        if action == "SOCIALIZE":
            participant_starts[other.pk] = other_needs_start
        for participant in runtimes:
            if participant.needs_world_minute < time_state.world_minute:
                error = NeedsService.advance_base_locked(
                    participant, time_state.world_minute, finalize_days=False
                )
                if error:
                    return terminal(error)
            first_day = participant_starts.get(participant.pk, participant.needs_world_minute) // MINUTES_PER_DAY
            for day_index in range(first_day, time_state.world_minute // MINUTES_PER_DAY):
                NeedsService._finalize_day(participant, day_index)
            participant.state_version += 1
            participant.time_revision = time_state.time_revision
            if participant.pk == runtime.pk:
                if action == "SLEEP" and time_state.world_minute - end > SLEEP_CONTINUATION_GAP_MINUTES:
                    NeedsService._reset_sleep_state(participant)
                participant.current_action = ""
                participant.action_status = "IDLE"
                participant.action_started_world_minute = None
                participant.action_ends_world_minute = None
                participant.active_command_id = None
            participant.save()
        actor_end_needs = action_end_needs
        if action == "EAT":
            effects["needs"] = {"hunger": recovery["hunger_fp"] / 10, "energy": recovery["energy_fp"] / 10}
        else:
            effects["needs"] = {
                name: round(actor_end_needs[name] - action_start_needs[name], 1)
                for name in NEEDS_FIELDS
                if actor_end_needs[name] != action_start_needs[name]
            }
        from .agent_feedback import add_item_names
        add_item_names(effects)
        event = VillageWorldEvent.objects.create(
            dedupe_key=f"life:{request_id}", event_type="npc_action_completed", world_minute=time_state.world_minute,
            time_revision=time_state.time_revision, actor_npc=runtime.npc,
            participant_npc_ids=[str(rt.npc.agent_id) for rt in runtimes], location_code=location,
            command_id=command_id, request_id=request_id, effects=effects, visibility="PUBLIC",
            public_data={"public_needs": public_needs(runtime), "reason_code": ""},
        )
        command.status = "SUCCEEDED"; command.result_code = ResultCode.LIFE_ACTION_COMPLETED; command.completed_world_minute = end; command.state_version = runtime.state_version; command.save()
        response_data = {
            "request_payload_sha256": payload_hash,
            "request_id": str(request_id), "command_id": str(command_id),
            "reason_code": "",
            "world_event_id": str(event.event_id), "world_minute": time_state.world_minute,
            "time_revision": time_state.time_revision, "state_version": runtime.state_version,
            "public_needs": public_needs(runtime), "public_status": runtime.action_status,
            "effects": effects,
        }
        sleep_details = sleep_receipt_details(start, end, end, "PLANNED_END") if action == "SLEEP" else {}
        sleep_details["needs_after"] = {
            name: response_data["public_needs"][name] for name in NEEDS_FIELDS
            if action_end_needs[name] != direct_needs_before[name]
        }
        response_data.update(sleep_details)
        receipt = VillageActionReceipt.objects.create(request_id=request_id, command=command, npc=runtime.npc, action_kind=action, success=True, result_code=ResultCode.LIFE_ACTION_COMPLETED, response_data=response_data, world_minute=time_state.world_minute, time_revision=time_state.time_revision, state_version=runtime.state_version, world_event=event)
        cls._outbox(runtime, receipt, event, action, ResultCode.LIFE_ACTION_COMPLETED, effects, time_state, command_id, data=sleep_details)
        from .daily_events import evaluate_daily_events
        evaluate_daily_events(time_state.world_minute)
        public_response = {key: value for key, value in response_data.items() if key != "request_payload_sha256"}
        return _result(True, ResultCode.LIFE_ACTION_COMPLETED, receipt=receipt, world_event=event, **public_response)

    @classmethod
    @transaction.atomic
    def interrupt_sleep(cls, npc_id: str, command_id: str) -> Dict[str, Any]:
        """Stop one running sleep and settle only the world minutes already slept."""
        try:
            command_uuid = uuid.UUID(str(command_id))
        except (TypeError, ValueError):
            return _result(False, ResultCode.INVALID_COMMAND_SCHEMA)
        try:
            time_state = GlobalGameTimeState.objects.select_for_update().get(pk=1)
            command = NpcCommandState.objects.select_for_update().select_related("npc").get(
                command_id=command_uuid,
            )
        except GlobalGameTimeState.DoesNotExist:
            return _result(False, ResultCode.BACKEND_UNAVAILABLE)
        except NpcCommandState.DoesNotExist:
            return _result(False, ResultCode.SLEEP_COMMAND_MISMATCH)

        existing = VillageActionReceipt.objects.filter(command=command).first()
        if existing and existing.result_code == ResultCode.SLEEP_INTERRUPTED:
            data = dict(existing.response_data or {})
            data.pop("request_payload_sha256", None)
            return _result(True, ResultCode.COMMAND_REPLAYED, replayed=True, **data)
        if str(command.npc.agent_id) != str(npc_id) or command.kind != "SLEEP":
            return _result(False, ResultCode.SLEEP_COMMAND_MISMATCH)
        if command.status != "RUNNING":
            return _result(False, ResultCode.NPC_NOT_SLEEPING)

        runtime = NpcRuntimeState.objects.select_for_update().get(npc=command.npc)
        if runtime.action_status != "SLEEPING" or runtime.active_command_id != command.command_id:
            return _result(False, ResultCode.NPC_NOT_SLEEPING)

        start = command.started_world_minute
        planned_end = runtime.action_ends_world_minute
        if start is None or planned_end is None:
            return _result(False, ResultCode.SLEEP_COMMAND_MISMATCH)
        end = max(start, min(time_state.world_minute, planned_end))
        before = public_needs(runtime)
        if runtime.needs_world_minute < end:
            error = NeedsService.advance_base_locked(
                runtime, end, finalize_days=False, evaluate_core_nights=False
            )
            if error:
                return _result(False, error)
        direct_needs_before = public_needs(runtime)
        error = NeedsService.apply_behavior_delta_locked(runtime, start, end, "SLEEP")
        if error:
            return _result(False, error)
        direct_needs_after = public_needs(runtime)
        NeedsService.evaluate_core_nights_locked(runtime, end)
        for day_index in range(start // MINUTES_PER_DAY, end // MINUTES_PER_DAY):
            NeedsService._finalize_day(runtime, day_index)

        after = public_needs(runtime)
        effects = {
            "items": [], "money": [], "relationships": [],
            "needs": {
                name: round(after[name] - before[name], 1)
                for name in NEEDS_FIELDS if after[name] != before[name]
            },
        }
        runtime.state_version += 1
        runtime.time_revision = time_state.time_revision
        runtime.current_action = ""
        runtime.action_status = "IDLE"
        runtime.action_started_world_minute = None
        runtime.action_ends_world_minute = None
        runtime.active_command_id = None
        if time_state.world_minute - end > SLEEP_CONTINUATION_GAP_MINUTES:
            NeedsService._reset_sleep_state(runtime)
        runtime.save()

        command.status = "CANCELLED"
        command.result_code = ResultCode.SLEEP_INTERRUPTED
        command.completed_world_minute = end
        command.state_version = runtime.state_version
        command.save()
        details = sleep_receipt_details(start, end, planned_end, "VOLUNTARY_WAKE")
        details["needs_after"] = {
            name: after[name] for name in NEEDS_FIELDS
            if direct_needs_after[name] != direct_needs_before[name]
        }
        event = VillageWorldEvent.objects.create(
            dedupe_key=f"sleep-interrupted:{command.request_id}",
            event_type="npc_action_interrupted", world_minute=time_state.world_minute,
            time_revision=time_state.time_revision, actor_npc=runtime.npc,
            participant_npc_ids=[str(runtime.npc.agent_id)],
            location_code=runtime.current_location_code, command_id=command.command_id,
            request_id=command.request_id, effects=effects, visibility="PUBLIC",
            public_data={
                "public_needs": after,
                "action": "SLEEP",
                "interruption_reason": "ACTOR_REQUESTED_WAKE_UP",
                **details,
            },
            player_summary=(
                f"{runtime.npc.name}提前起床，实际睡了{end - start}分钟。"
            )[:256],
            hub_summary=(
                f"{runtime.npc.name}主动结束睡眠，实际执行{end - start}分钟。"
            )[:256],
        )
        response_data = {
            "request_id": str(command.request_id), "command_id": str(command.command_id),
            "reason_code": ResultCode.SLEEP_INTERRUPTED,
            "world_event_id": str(event.event_id), "world_minute": time_state.world_minute,
            "time_revision": time_state.time_revision, "state_version": runtime.state_version,
            "public_needs": after, "public_status": runtime.action_status,
            "effects": effects, **details,
        }
        receipt = VillageActionReceipt.objects.create(
            request_id=command.request_id, command=command, npc=runtime.npc, action_kind="SLEEP",
            success=False, result_code=ResultCode.SLEEP_INTERRUPTED, response_data=response_data,
            world_minute=time_state.world_minute, time_revision=time_state.time_revision,
            state_version=runtime.state_version, world_event=event,
        )
        cls._outbox(
            runtime, receipt, event, "SLEEP", ResultCode.SLEEP_INTERRUPTED, effects,
            time_state, command.command_id, success=False, data=details,
        )
        return _result(True, ResultCode.SLEEP_INTERRUPTED, receipt=receipt, **response_data)


def runtime_summary(npc_id: str) -> Dict[str, Any]:
    try:
        runtime = NpcRuntimeState.objects.select_related("npc").get(npc__agent_id=str(npc_id), npc__is_test_only=False)
    except NpcRuntimeState.DoesNotExist:
        return _result(False, ResultCode.BOOTSTRAP_NPC_SET_MISMATCH)
    world_minute = GlobalGameTimeState.objects.filter(pk=1).values_list(
        "world_minute", flat=True
    ).first() or 0
    actions = CommandService.allowed_actions_for(runtime.npc)
    actions.discard("SOCIALIZE")
    if not _fast_integration_enabled():
        config = VillageSimulationConfig.objects.filter(config_key="default").first()
        if not config or not config.village_needs_enabled:
            actions -= CommandService.PHASE2_ACTIONS
        if not config or not config.village_economy_enabled:
            actions -= CommandService.PHASE3_ACTIONS
        elif not config.village_direct_trade_enabled:
            actions -= CommandService.DIRECT_TRADE_ACTIONS
        if not config or not config.village_social_enabled:
            actions -= CommandService.PHASE4_ACTIONS
        if not config or not config.village_story_enabled:
            actions -= CommandService.STORY_ACTIONS
    active_dialogue = NpcDialogueSession.objects.filter(
        Q(initiator=runtime.npc) | Q(target=runtime.npc), status="ACTIVE"
    ).first()
    unavailable_reasons = {}
    available = []
    for action in sorted(actions):
        if action in CommandService.READ_QUERY_ACTIONS or action in {"WRITE_SCHEDULE", "UPDATE_SCHEDULE", "WRITE_DIARY", "UPDATE_DIARY"}:
            available.append(action)
            continue
        if active_dialogue:
            reason = ResultCode.NPC_BUSY
        else:
            reason = ResultCode.NPC_BUSY if runtime.action_status != "IDLE" else CommandService.unavailable_reason(action, runtime)
        if reason:
            unavailable_reasons[action] = reason
        else:
            available.append(action)
    active_reservation = (
        NpcActionReservation.objects.filter(
            participants=runtime.npc, status="HELD", active_lock_key__isnull=False,
        )
        .prefetch_related("participants")
        .order_by("acquired_world_minute", "reservation_id")
        .first()
    )
    reservation_data = None
    if active_reservation:
        reservation_data = {
            "reservation_id": str(active_reservation.reservation_id),
            "action_kind": active_reservation.action_kind,
            "participant_npc_ids": sorted(str(npc.agent_id) for npc in active_reservation.participants.all()),
            "location_code": active_reservation.location_code,
            "acquired_world_minute": active_reservation.acquired_world_minute,
            "expires_world_minute": active_reservation.expires_world_minute,
            "command_id": str(active_reservation.command_id) if active_reservation.command_id else None,
            "request_id": str(active_reservation.request_id) if active_reservation.request_id else None,
            "status": active_reservation.status,
        }
    return _result(
        True, ResultCode.LIFE_ACTION_COMPLETED,
        npc_id=str(runtime.npc.agent_id), state_version=runtime.state_version,
        world_minute=world_minute,
        needs_world_minute=runtime.needs_world_minute, public_needs=public_needs(runtime),
        action_status="DIALOGUING" if active_dialogue else runtime.action_status,
        current_action="DIALOGUE" if active_dialogue else runtime.current_action or None,
        action_started_world_minute=active_dialogue.started_world_minute if active_dialogue else runtime.action_started_world_minute,
        active_command_id=str(runtime.active_command_id) if runtime.active_command_id else None,
        action_ends_world_minute=active_dialogue.expires_world_minute if active_dialogue else runtime.action_ends_world_minute,
        remaining_minutes=max(0, active_dialogue.expires_world_minute - world_minute) if active_dialogue else
        (max(0, runtime.action_ends_world_minute - world_minute)
         if runtime.action_ends_world_minute is not None else None),
        need_bands=need_bands(runtime), active_reservation=reservation_data,
        available_actions=available, unavailable_reasons=unavailable_reasons,
    )
