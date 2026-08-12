"""Passive persistence for Unity-authoritative village simulation.

Unity is the only runtime authority.  These helpers deliberately do not apply
queue, busy, version, time, location, need, or action-policy checks.  They only
resolve the referenced NPC and persist the snapshot/result supplied by the
authenticated environment server.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict

from django.db import transaction
from django.utils import timezone

from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition

from .models import (
    NpcCommandState,
    NpcRuntimeState,
    VillageActionReceipt,
    VillageWorldEvent,
)
from .runtime_services import ResultCode


NEED_FIELDS = ("hunger", "energy", "health", "social", "stress")


def _result(success: bool, code: str, **data: Any) -> Dict[str, Any]:
    return {"success": success, "result_code": code, **data}


def _payload_hash(payload: Dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _uuid(value: Any) -> uuid.UUID:
    return uuid.UUID(str(value))


def _save_time(world_minute: int, time_revision: int) -> GlobalGameTimeState:
    state = GlobalGameTimeState.get_instance()
    state.apply_world_minute(int(world_minute))
    state.time_revision = int(time_revision)
    state.save()
    return state


def _apply_snapshot(runtime: NpcRuntimeState, snapshot: Dict[str, Any]) -> None:
    """Last-write-wins persistence; values are trusted as reported by Unity."""
    scalar_fields = {
        "current_location_code": "current_location_code",
        "current_point_code": "current_point_code",
        "current_goal": "current_goal",
        "current_action": "current_action",
        "action_status": "action_status",
        "action_started_world_minute": "action_started_world_minute",
        "action_ends_world_minute": "action_ends_world_minute",
        "active_command_id": "active_command_id",
        "state_version": "state_version",
        "last_simulated_world_minute": "last_simulated_world_minute",
        "needs_world_minute": "needs_world_minute",
        "time_revision": "time_revision",
    }
    for source, target in scalar_fields.items():
        if source in snapshot:
            setattr(runtime, target, snapshot[source])

    needs = snapshot.get("public_needs")
    if isinstance(needs, dict):
        for name in NEED_FIELDS:
            if name not in needs:
                continue
            setattr(runtime, f"{name}_fp", round(float(needs[name]) * 10))
            setattr(runtime, f"{name}_remainder", 0)
    runtime.save()


@transaction.atomic
def save_snapshots(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Persist Unity runtime snapshots without CAS or lifecycle validation."""
    try:
        time_state = GlobalGameTimeState.get_instance()
        world_minute = int(payload.get("world_minute", time_state.world_minute))
        time_revision = int(payload.get("time_revision", time_state.time_revision))
        checkpoints = payload.get("checkpoints") or []
        _save_time(world_minute, time_revision)
        results = []
        for snapshot in checkpoints:
            npc_id = str(snapshot.get("npc_id", ""))
            npc = NpcDefinition.objects.get(agent_id=npc_id, is_test_only=False)
            runtime, _ = NpcRuntimeState.objects.get_or_create(npc=npc)
            _apply_snapshot(runtime, dict(snapshot))
            results.append({
                "success": True,
                "npc_id": npc_id,
                "result_code": ResultCode.COMMAND_ACCEPTED,
                "state_version": runtime.state_version,
            })
        return _result(True, ResultCode.COMMAND_ACCEPTED, results=results)
    except (TypeError, ValueError, NpcDefinition.DoesNotExist) as exc:
        return _result(False, ResultCode.INVALID_COMMAND_SCHEMA, message=str(exc))


@transaction.atomic
def save_result(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Persist one terminal Unity result; Backend never approves or re-evaluates it."""
    try:
        command_id = _uuid(payload["command_id"])
        request_id = _uuid(payload["request_id"])
        npc_id = str(payload["npc_id"])
        action = str(payload.get("action") or "UNKNOWN").upper()
        success = bool(payload.get("success"))
        result_code = str(payload.get("result_code") or "")
        world_minute = int(payload.get("world_minute", 0))
        time_revision = int(payload.get("time_revision", 0))
        npc = NpcDefinition.objects.get(agent_id=npc_id, is_test_only=False)
    except (KeyError, TypeError, ValueError, NpcDefinition.DoesNotExist) as exc:
        return _result(False, ResultCode.INVALID_COMMAND_SCHEMA, message=str(exc))

    _save_time(world_minute, time_revision)
    runtime, _ = NpcRuntimeState.objects.get_or_create(npc=npc)
    snapshot = payload.get("state") if isinstance(payload.get("state"), dict) else {}
    _apply_snapshot(runtime, snapshot)

    # NpcCommandState is retained as terminal history only.  No RECEIVED,
    # QUEUED, or RUNNING row is created by the Unity-authoritative flow.
    command = NpcCommandState.objects.filter(command_id=command_id).first()
    if command is None:
        command = NpcCommandState.objects.filter(request_id=request_id).first()
    if command is None:
        command = NpcCommandState(command_id=command_id, message_id=command_id, request_id=request_id, npc=npc)
    command.npc = npc
    command.schema_version = "village.command.v1"
    command.sender_id = npc_id
    command.recipient_id = str(payload.get("environment_id") or "")
    command.kind = action
    command.target_id = str(payload.get("target_id") or "")
    command.location_code = str(snapshot.get("current_location_code") or "")
    command.sanitized_parameters = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    command.payload_sha256 = _payload_hash(payload)
    command.queue_mode = "UNITY_LOCAL"
    command.status = "SUCCEEDED" if success else "FAILED"
    command.sent_at_ms = payload.get("sent_at_ms")
    command.received_at_ms = int(timezone.now().timestamp() * 1000)
    command.ttl_ms = None
    command.not_before_world_minute = None
    command.expires_world_minute = None
    command.duration_minutes = payload.get("duration_minutes")
    command.received_world_minute = world_minute
    command.started_world_minute = payload.get("started_world_minute")
    command.completed_world_minute = world_minute
    command.result_code = result_code
    command.state_version = runtime.state_version
    command.validated_at_state_version = None
    command.save()

    effects = payload.get("effects") if isinstance(payload.get("effects"), dict) else {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    event, _ = VillageWorldEvent.objects.update_or_create(
        dedupe_key=f"unity-result:{request_id}",
        defaults={
            "event_type": "NPC_ACTION_COMPLETED" if success else "NPC_ACTION_FAILED",
            "world_minute": world_minute,
            "time_revision": time_revision,
            "actor_npc": npc,
            "participant_npc_ids": list(payload.get("participant_npc_ids") or [npc_id]),
            "location_code": runtime.current_location_code,
            "point_code": runtime.current_point_code,
            "command_id": command.command_id,
            "request_id": request_id,
            "effects": effects,
            "visibility": "PUBLIC",
            "public_data": {
                "public_needs": snapshot.get("public_needs", {}),
                "reason_code": "" if success else result_code,
            },
            "hub_summary": str(payload.get("message") or "")[:256],
        },
    )
    response_data = {
        "command_id": str(command.command_id),
        "request_id": str(request_id),
        "world_event_id": str(event.event_id),
        "world_minute": world_minute,
        "time_revision": time_revision,
        "state_version": runtime.state_version,
        "effects": effects,
        "data": data,
    }
    receipt, _ = VillageActionReceipt.objects.update_or_create(
        request_id=request_id,
        defaults={
            "command": command,
            "npc": npc,
            "action_kind": action,
            "success": success,
            "result_code": result_code,
            "response_data": response_data,
            "world_minute": world_minute,
            "time_revision": time_revision,
            "state_version": runtime.state_version,
            "world_event": event,
        },
    )
    return _result(
        True,
        result_code or ResultCode.COMMAND_ACCEPTED,
        receipt=receipt,
        world_event=event,
        **response_data,
    )
