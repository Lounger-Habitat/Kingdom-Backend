from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Mapping

from django.db import transaction

from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition

from .models import NpcRuntimeState, NpcSubjectiveGoalState, VillageActionReceipt


ACTION = "UPDATE_SUBJECTIVE_GOAL"
FORMAL_NPC_IDS = {"10011", "10012", "10013", "10014", "10015"}


def _result(success: bool, code: str, data: dict | None = None) -> dict:
    return {"success": success, "result_code": code, "data": data or {}}


def _state_data(state: NpcSubjectiveGoalState | None) -> dict:
    if state is None:
        return {"subjective_goal": None, "goal_version": 0}
    goal = None
    if state.status == "ACTIVE" and state.goal_id:
        goal = {
            "goal_id": str(state.goal_id),
            "summary": state.summary,
            "status": state.status,
            "visibility": state.visibility,
            "goal_version": state.goal_version,
            "updated_world_minute": state.updated_world_minute,
        }
    return {"subjective_goal": goal, "goal_version": state.goal_version,
            "goal_id": str(state.goal_id) if state.goal_id else None, "goal_status": state.status}


def _payload_hash(envelope: Mapping[str, Any]) -> str:
    raw = json.dumps(envelope, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


@transaction.atomic
def update_subjective_goal(envelope: Mapping[str, Any], *, action_task=None) -> dict:
    if not isinstance(envelope, Mapping):
        return _result(False, "INVALID_GOAL_SCHEMA")
    content = envelope.get("content")
    if not isinstance(content, Mapping):
        return _result(False, "INVALID_GOAL_SCHEMA")
    params = content.get("params")
    if not isinstance(params, Mapping) or set(params) != ({
        "goal_id", "summary", "status", "visibility", "expected_goal_version"
    } if action_task is None else {"goal_id", "summary", "status", "visibility"}):
        return _result(False, "INVALID_GOAL_SCHEMA")
    sender = str(envelope.get("sender", ""))
    if sender not in FORMAL_NPC_IDS:
        return _result(False, "SENDER_BINDING_INVALID")
    if (action_task is None and content.get("schema_version") != "village.command.v1") or str(content.get("name", "")).upper() != ACTION:
        return _result(False, "INVALID_GOAL_SCHEMA")
    try:
        uuid.UUID(str(envelope.get("envelope_id", "")))
        if action_task is None:
            uuid.UUID(str(content.get("id", "")))
        request_id = action_task.pk if action_task is not None else uuid.UUID(str(content.get("request_id", "")))
        goal_id = uuid.UUID(str(params.get("goal_id", "")))
    except (ValueError, TypeError, AttributeError):
        return _result(False, "INVALID_GOAL_SCHEMA")
    summary = params.get("summary")
    status = str(params.get("status", "")).upper()
    visibility = str(params.get("visibility", "")).upper()
    expected = params.get("expected_goal_version")
    if (not isinstance(summary, str) or not summary.strip() or len(summary.strip()) > 120
            or status not in {"ACTIVE", "COMPLETED", "ABANDONED"}
            or visibility != "PUBLIC" or action_task is None and (
                isinstance(expected, bool) or not isinstance(expected, int) or expected < 0)):
        return _result(False, "INVALID_GOAL_SCHEMA")

    digest = _payload_hash(envelope)
    receipt = VillageActionReceipt.objects.select_for_update().filter(request_id=request_id).first()
    if receipt:
        saved = dict(receipt.response_data or {})
        if receipt.action_kind != ACTION or saved.pop("payload_sha256", None) != digest:
            return _result(False, "GOAL_REQUEST_CONFLICT")
        saved["replayed"] = True
        return _result(receipt.success, receipt.result_code, saved)

    runtime = NpcRuntimeState.objects.select_for_update().select_related("npc").filter(
        npc__agent_id=sender, npc__is_test_only=False
    ).first()
    if runtime is None:
        return _result(False, "NPC_NOT_FOUND")
    state, _ = NpcSubjectiveGoalState.objects.select_for_update().get_or_create(npc=runtime.npc)
    if action_task is None and state.goal_version != expected:
        return _result(False, "GOAL_VERSION_CONFLICT", _state_data(state))
    if status != "ACTIVE" and (state.status != "ACTIVE" or state.goal_id != goal_id):
        return _result(False, "GOAL_NOT_FOUND", _state_data(state))

    clock = GlobalGameTimeState.get_instance()
    state.goal_id = goal_id
    state.summary = summary.strip()
    state.status = status
    state.visibility = visibility
    state.goal_version += 1
    state.updated_world_minute = clock.world_minute
    state.save()
    code = {"ACTIVE": "GOAL_UPDATED", "COMPLETED": "GOAL_COMPLETED", "ABANDONED": "GOAL_ABANDONED"}[status]
    data = {**_state_data(state), "replayed": False}
    VillageActionReceipt.objects.create(
        request_id=request_id,
        npc=runtime.npc,
        action_kind=ACTION,
        success=True,
        result_code=code,
        response_data={**data, "payload_sha256": digest},
        world_minute=clock.world_minute,
        time_revision=clock.time_revision,
        state_version=runtime.state_version,
    )
    return _result(True, code, data)
