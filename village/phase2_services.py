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

from game_time.models import GlobalGameTimeState
from inventory.models import NpcGlobalInventory, NpcGlobalInventorySlot
from items.models import Item, ItemType

from .models import (
    NpcActionReservation,
    NpcNeedsDaySummary,
    NpcProfile,
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
from .runtime_services import ResultCode


NEEDS_FIELDS = ("hunger", "energy", "health", "social", "stress")
NEEDS_SCALE = 10
MINUTES_PER_DAY = 1440
MAX_INTERVALS = 64
ACTIVITIES = {"IDLE", "MOVE", "WORK", "SOCIALIZE", "REST", "SLEEP"}

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


def _hash(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


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
        "social": band(runtime.social_fp, inverse=True),
        "stress": band(runtime.stress_fp),
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
        numerator = rate * minutes + getattr(runtime, remainder_name)
        delta = _trunc_div(numerator, 30)
        setattr(runtime, remainder_name, numerator - delta * 30)
        setattr(runtime, value_name, max(0, min(1000, getattr(runtime, value_name) + delta)))

    @staticmethod
    def _apply_scaled_rate(runtime: NpcRuntimeState, name: str, rate: int, minutes: int, percent: int) -> None:
        """Apply a percentage rate once, retaining sub-fixed-point residue."""
        remainder_name = f"{name}_remainder"
        value_name = f"{name}_fp"
        numerator = rate * percent * minutes + getattr(runtime, remainder_name) * 100
        delta = _trunc_div(numerator, 3000)
        residue = numerator - delta * 3000
        # Frozen action durations are 30-minute multiples, so the residue is
        # exactly representable by the existing denominator-30 remainder.
        setattr(runtime, remainder_name, _trunc_div(residue, 100))
        setattr(runtime, value_name, max(0, min(1000, getattr(runtime, value_name) + delta)))

    @classmethod
    def advance_social_scaled_locked(
        cls, runtime: NpcRuntimeState, start: int, end: int, percent: int,
    ) -> Optional[str]:
        """Advance one same-day SOCIALIZE span with exact scaled base rates.

        The severe-hunger stress rate is deliberately not scaled. A denominator
        of 3000 preserves the 25% half-fixed-point residue until the existing
        denominator-30 remainder can represent it at the end of the span.
        """
        if runtime.needs_world_minute != start or start // MINUTES_PER_DAY != (end - 1) // MINUTES_PER_DAY:
            return ResultCode.INVALID_ACTIVITY_INTERVAL
        summary = cls._summary(runtime, start // MINUTES_PER_DAY)
        social_numerator = runtime.social_remainder * 100
        stress_numerator = runtime.stress_remainder * 100
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

        runtime.social_remainder = _trunc_div(social_numerator, 100)
        runtime.stress_remainder = _trunc_div(stress_numerator, 100)
        runtime.needs_world_minute = end
        summary.save(update_fields=["severe_hunger_minutes", "updated_at"])
        return None

    @classmethod
    def _apply_segment(cls, runtime: NpcRuntimeState, activity: str, start: int, end: int) -> None:
        day_index = start // MINUTES_PER_DAY
        summary = cls._summary(runtime, day_index)
        # A one-minute loop makes the severe-hunger boundary exact even when a
        # segment crosses 90.0 halfway through a 30-minute fixed-point rate.
        # Actions are capped at 600 minutes and advance requests at 64 spans.
        for _minute in range(start, end):
            severe = runtime.hunger_fp >= 900
            if severe:
                summary.severe_hunger_minutes += 1
            if activity == "SLEEP":
                summary.sleep_minutes += 1
            for name, rate in RATES[activity].items():
                cls._apply_rate(runtime, name, rate, 1)
            if severe:
                cls._apply_rate(runtime, "stress", 20, 1)
        summary.save(update_fields=["sleep_minutes", "severe_hunger_minutes", "updated_at"])

    @classmethod
    def _finalize_day(cls, runtime: NpcRuntimeState, day_index: int) -> None:
        summary = cls._summary(runtime, day_index)
        if summary.finalized:
            return
        if summary.severe_hunger_minutes >= MINUTES_PER_DAY:
            runtime.health_fp = max(0, runtime.health_fp - 80)
        elif summary.successful_meals >= 1 and summary.sleep_minutes >= 360 and not summary.injured:
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
        if NpcActionReservation.objects.filter(
            participants=runtime.npc, action_kind="SOCIALIZE", status="HELD", active_lock_key__isnull=False
        ).exists():
            return _result(False, ResultCode.RESERVATION_CONFLICT)
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
        required = {"request_id", "command_id", "npc_id", "action", "start_world_minute", "completion_world_minute", "location_code", "expected_state_version", "params"}
        if set(payload) != required or not isinstance(payload.get("params"), dict):
            return ResultCode.INVALID_COMMAND_SCHEMA
        if payload["action"] not in LifeActionService.ACTIONS:
            return ResultCode.UNKNOWN_ACTION
        if any(isinstance(payload[name], bool) or not isinstance(payload[name], int) for name in ("start_world_minute", "completion_world_minute", "expected_state_version")):
            return ResultCode.INVALID_COMMAND_SCHEMA
        duration = payload["completion_world_minute"] - payload["start_world_minute"]
        if duration <= 0 or duration % 30:
            return ResultCode.INVALID_ACTION_DURATION
        action = payload["action"]
        limits = {"EAT": (30, 30), "REST": (30, 120), "SLEEP": (30, 600), "SOCIALIZE": (30, 120), "WORK_PLACEHOLDER": (30, 240)}
        if not limits[action][0] <= duration <= limits[action][1]:
            return ResultCode.INVALID_ACTION_DURATION
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
        if not set(params).issubset(action_params[action]):
            return ResultCode.INVALID_COMMAND_SCHEMA
        if action == "EAT":
            source = params.get("source")
            if source == "PERSONAL_INVENTORY":
                if set(params) != action_params[action] or isinstance(params.get("expected_asset_version"), bool) or not isinstance(params.get("expected_asset_version"), int):
                    return ResultCode.INVALID_COMMAND_SCHEMA
            elif source == "COMMUNITY_MEAL":
                if set(params) != {"source"}:
                    return ResultCode.INVALID_COMMAND_SCHEMA
            else:
                return ResultCode.FOOD_NOT_ALLOWED
        if action == "SOCIALIZE" and set(params) != action_params[action]:
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
    def _outbox(runtime: NpcRuntimeState, receipt: VillageActionReceipt, event: VillageWorldEvent, action: str, code: str, effects: Dict[str, Any], time_state: GlobalGameTimeState, command_id: uuid.UUID, *, success: bool = True) -> None:
        command = receipt.command
        reason_code = "" if success else code
        payload = {
            "schema_version": "village.outcome.v1", "ref_id": str(command_id),
            "original_message_id": str(command.message_id),
            "request_id": str(receipt.request_id), "npc_id": str(runtime.npc.agent_id),
            "success": success, "result_code": code, "reason_code": reason_code,
            "world_event_id": str(event.event_id), "world_minute": time_state.world_minute,
            "time_revision": time_state.time_revision, "state_version": runtime.state_version,
            "public_needs": public_needs(runtime), "public_status": runtime.action_status,
            "retryable": False, "effects": effects, "data": {},
        }
        VillageDeliveryOutbox.objects.create(
            dedupe_key=f"outcome:{receipt.request_id}", recipient=command.sender_id,
            message_kind="OUTCOME", command=receipt.command, receipt=receipt, event=event, payload=payload, priority=10, status="PENDING",
        )
        VillageDeliveryOutbox.objects.create(
            dedupe_key=f"event:{event.event_id}", recipient=command.sender_id,
            message_kind="EVENT", command=receipt.command, receipt=receipt, event=event,
            payload={
                "schema_version": "village.event.v1", "event_id": str(event.event_id),
                "event_sequence": event.event_sequence, "event_type": event.event_type,
                "world_minute": event.world_minute, "time_revision": event.time_revision,
                "actor_npc_id": str(runtime.npc.agent_id),
                "participant_npc_ids": event.participant_npc_ids,
                "location_code": event.location_code, "point_code": event.point_code,
                "command_id": str(command.command_id), "request_id": str(receipt.request_id),
                "cause_event_id": None, "visibility": event.visibility,
                "public_status_code": runtime.action_status,
                "public_data": {"public_needs": public_needs(runtime), "reason_code": reason_code},
                "summary": event.hub_summary,
            }, priority=5, status="PENDING",
        )

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
        validation = cls._validate(payload)
        if validation:
            return _result(False, validation)
        if not _config_enabled():
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
        if payload["completion_world_minute"] > time_state.world_minute:
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
        if (
            command.npc_id != runtime.npc_id or command.request_id != request_id
            or command.kind != action or command.status != "RUNNING"
            or runtime.active_command_id != command.command_id
            or command.started_world_minute != start
            or command.duration_minutes != end - start
            or runtime.action_ends_world_minute != end
        ):
            return _result(False, ResultCode.NPC_BUSY)
        if runtime.action_status not in {"ACTING", "SLEEPING"}:
            return _result(False, ResultCode.NPC_BUSY)

        def terminal(code: str) -> Dict[str, Any]:
            return cls._terminal_failure(
                runtime=runtime, command=command, request_id=request_id,
                payload_hash=payload_hash, action=action, code=code,
                time_state=time_state, location=payload.get("location_code", ""),
            )

        if runtime.state_version != payload["expected_state_version"]:
            return terminal(ResultCode.STALE_STATE_VERSION)
        profile = NpcProfile.objects.filter(npc=runtime.npc, is_active=True).first()
        if not profile:
            return terminal(ResultCode.INVALID_ACTION_LOCATION)
        location = payload["location_code"]
        if start < runtime.needs_world_minute or start > time_state.world_minute:
            return terminal(ResultCode.STALE_NEEDS_MINUTE)
        if runtime.current_location_code != location:
            return terminal(ResultCode.INVALID_ACTION_LOCATION)
        if action == "REST" and location not in {profile.home_location_code, "RESTAURANT"}:
            return terminal(ResultCode.INVALID_ACTION_LOCATION)
        if action == "REST" and location == "RESTAURANT" and not cls._location_open(location):
            return terminal(ResultCode.LOCATION_CLOSED)
        if action == "SLEEP" and location != profile.home_location_code:
            return terminal(ResultCode.INVALID_ACTION_LOCATION)
        if action == "WORK_PLACEHOLDER":
            if location != profile.work_location_code:
                return terminal(ResultCode.INVALID_ACTION_LOCATION)
            if start // MINUTES_PER_DAY != (end - 1) // MINUTES_PER_DAY:
                return terminal(ResultCode.INVALID_ACTION_DURATION)
        if action == "EAT":
            source = payload["params"].get("source")
            if source not in {"PERSONAL_INVENTORY", "COMMUNITY_MEAL"}:
                return terminal(ResultCode.FOOD_NOT_ALLOWED)
            if source == "COMMUNITY_MEAL" and (location != "RESTAURANT" or not cls._location_open(location)):
                return terminal(ResultCode.LOCATION_CLOSED)
        if action == "SOCIALIZE":
            other = runtime_by_id.get(target_id)
            if other is None or other.npc_id == runtime.npc_id:
                return terminal(ResultCode.SOCIAL_TARGET_INVALID)
            if other.action_status != "IDLE" or other.current_location_code != runtime.current_location_code or location != runtime.current_location_code:
                return terminal(ResultCode.SOCIAL_TARGET_BUSY)
            if start < other.needs_world_minute:
                return terminal(ResultCode.STALE_NEEDS_MINUTE)
        elif action != "EAT" and not cls._location_open(location):
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
                    if payload["params"]["expected_asset_version"] != inventory.asset_version:
                        return terminal(ResultCode.ASSET_VERSION_CONFLICT)
                    slot = NpcGlobalInventorySlot.objects.select_for_update().get(bag=inventory, instance_id=food_id)
                except (ValueError, TypeError, NpcGlobalInventory.DoesNotExist, NpcGlobalInventorySlot.DoesNotExist):
                    return terminal(ResultCode.FOOD_NOT_AVAILABLE)
                item = Item.objects.filter(item_id=slot.item_id, item_type=ItemType.FOOD, is_deleted=False).first()
                if item is None:
                    return terminal(ResultCode.FOOD_NOT_ALLOWED)
                if slot.item_amount < 1:
                    return terminal(ResultCode.FOOD_NOT_AVAILABLE)
                prepared.update(food_id=food_id, inventory=inventory, slot=slot)
            else:
                usage, _ = VillageCommunityMealDailyUsage.objects.select_for_update().get_or_create(day_index=(end - 1) // MINUTES_PER_DAY)
                key = str(runtime.npc.agent_id)
                if usage.total_meals >= 5 or int(usage.npc_usage.get(key, 0)) >= 1:
                    return terminal(ResultCode.FOOD_NOT_AVAILABLE)
                prepared.update(usage=usage, usage_key=key)

        # Bring the actor to the action start before evaluating need gates. This
        # prevents a stale cursor from bypassing hunger/energy/health limits.
        actor_needs_start = runtime.needs_world_minute
        if runtime.needs_world_minute < start:
            error = NeedsService.advance_locked(
                runtime, start,
                [{"interval_id": str(uuid.uuid4()), "start_world_minute": runtime.needs_world_minute,
                  "end_world_minute": start, "activity": "IDLE"}],
            )
            if error:
                return terminal(error)
        if action == "WORK_PLACEHOLDER":
            if runtime.hunger_fp >= 900:
                return terminal(ResultCode.HUNGER_TOO_HIGH)
            if runtime.energy_fp <= 100:
                return terminal(ResultCode.ENERGY_TOO_LOW)
            if runtime.health_fp <= 300:
                return terminal(ResultCode.HEALTH_TOO_LOW)
        if action == "SOCIALIZE" and runtime.health_fp <= 300:
            return terminal(ResultCode.HEALTH_TOO_LOW)
        if action == "SOCIALIZE" and start // MINUTES_PER_DAY != (end - 1) // MINUTES_PER_DAY:
            return terminal(ResultCode.INVALID_ACTION_DURATION)

        action_start_needs = public_needs(runtime)

        # EAT has no separate interval rate in the frozen activity vocabulary;
        # its only special needs effect is the committed completion effect.
        activity = "WORK" if action == "WORK_PLACEHOLDER" else "IDLE" if action == "EAT" else action
        if action != "SOCIALIZE":
            error = NeedsService.advance_locked(
                runtime, end,
                [{"interval_id": str(uuid.uuid4()), "start_world_minute": start,
                  "end_world_minute": end, "activity": activity}],
                finalize_days=False,
            )
            if error:
                return terminal(error)
        effects: Dict[str, Any] = {}
        if action == "SOCIALIZE":
            other = runtime_by_id[target_id]
            other_needs_start = other.needs_world_minute
            if other.needs_world_minute < start:
                other_error = NeedsService.advance_locked(
                    other, start,
                    [{"interval_id": str(uuid.uuid4()), "start_world_minute": other.needs_world_minute,
                      "end_world_minute": start, "activity": "IDLE"}],
                )
                if other_error:
                    return terminal(other_error)
            if other.health_fp <= 300:
                return terminal(ResultCode.HEALTH_TOO_LOW)
            low, high = sorted((runtime, other), key=lambda rt: str(rt.npc.agent_id))
            day_index = (end - 1) // MINUTES_PER_DAY
            counter, _ = NpcSocialDailyCounter.objects.select_for_update().get_or_create(low_npc=low.npc, high_npc=high.npc, day_index=day_index)
            multiplier = 100 if counter.socialize_count == 0 else 50 if counter.socialize_count == 1 else 25
            counter.socialize_count += 1; counter.save(update_fields=["socialize_count", "updated_at"])
            actor_error = NeedsService.advance_social_scaled_locked(runtime, start, end, multiplier)
            other_error = NeedsService.advance_social_scaled_locked(other, start, end, multiplier)
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
                    "items": [{"owner_id": str(runtime.npc.agent_id), "item_id": slot.item_id, "delta": -1}],
                    "money": [], "needs": {}, "relationships": [],
                    "food_instance_id": str(food_id), "asset_version": inventory.asset_version,
                }
            else:
                usage, key = prepared["usage"], prepared["usage_key"]
                usage.total_meals += 1
                usage.npc_usage = {**usage.npc_usage, key: 1}
                usage.save(update_fields=["total_meals", "npc_usage", "updated_at"])
                effects = {"items": [], "money": [], "needs": {}, "relationships": [], "community_meal": True}
            runtime.hunger_fp = max(0, runtime.hunger_fp - 450)
            runtime.energy_fp = min(1000, runtime.energy_fp + 50)
            summary = NeedsService._summary(runtime, (end - 1) // MINUTES_PER_DAY)
            summary.successful_meals += 1; summary.save(update_fields=["successful_meals", "updated_at"])
        elif action == "REST":
            effects = {"items": [], "money": [], "needs": {}, "relationships": []}
        elif action == "SLEEP":
            effects = {"items": [], "money": [], "needs": {}, "relationships": []}
        else:
            effects = {"items": [], "money": [], "needs": {}, "relationships": []}

        # A life action applies its committed completion effect before any day
        # boundary health correction, exactly once for every crossed absolute
        # day. Generic Tick advancement keeps the default eager finalization.
        participant_starts = {runtime.pk: actor_needs_start}
        if action == "SOCIALIZE":
            participant_starts[other.pk] = other_needs_start
        for participant in runtimes:
            first_day = participant_starts.get(participant.pk, participant.needs_world_minute) // MINUTES_PER_DAY
            for day_index in range(first_day, end // MINUTES_PER_DAY):
                NeedsService._finalize_day(participant, day_index)
            participant.state_version += 1
            participant.time_revision = time_state.time_revision
            if participant.pk == runtime.pk:
                participant.current_action = ""
                participant.action_status = "IDLE"
                participant.action_started_world_minute = None
                participant.action_ends_world_minute = None
                participant.active_command_id = None
            participant.save()
        actor_end_needs = public_needs(runtime)
        if action == "EAT":
            effects["needs"] = {"hunger": -45.0, "energy": 5.0}
        else:
            effects["needs"] = {
                name: round(actor_end_needs[name] - action_start_needs[name], 1)
                for name in NEEDS_FIELDS
                if actor_end_needs[name] != action_start_needs[name]
            }
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
        receipt = VillageActionReceipt.objects.create(request_id=request_id, command=command, npc=runtime.npc, action_kind=action, success=True, result_code=ResultCode.LIFE_ACTION_COMPLETED, response_data=response_data, world_minute=time_state.world_minute, time_revision=time_state.time_revision, state_version=runtime.state_version, world_event=event)
        cls._outbox(runtime, receipt, event, action, ResultCode.LIFE_ACTION_COMPLETED, effects, time_state, command_id)
        public_response = {key: value for key, value in response_data.items() if key != "request_payload_sha256"}
        return _result(True, ResultCode.LIFE_ACTION_COMPLETED, receipt=receipt, world_event=event, **public_response)


def runtime_summary(npc_id: str) -> Dict[str, Any]:
    try:
        runtime = NpcRuntimeState.objects.select_related("npc").get(npc__agent_id=str(npc_id), npc__is_test_only=False)
    except NpcRuntimeState.DoesNotExist:
        return _result(False, ResultCode.BOOTSTRAP_NPC_SET_MISMATCH)
    available = []
    if _config_enabled() and runtime.action_status == "IDLE":
        actions = set(LifeActionService.ACTIONS)
        if runtime.hunger_fp >= 900 or runtime.energy_fp <= 100 or runtime.health_fp <= 300:
            actions.discard("WORK_PLACEHOLDER")
        if runtime.health_fp <= 300:
            actions.discard("SOCIALIZE")
        available = sorted(actions)
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
        needs_world_minute=runtime.needs_world_minute, public_needs=public_needs(runtime),
        need_bands=need_bands(runtime), active_reservation=reservation_data,
        available_actions=available,
    )
