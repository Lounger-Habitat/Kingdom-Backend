"""Daily text records, independent of the NPC's physical action lifecycle."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date

from django.db import transaction

from game_time.models import GlobalGameTimeState
from .models import NpcDailyRecord, NpcRuntimeState, VillageActionReceipt
from .phase5_contract import FORMAL_NPC_IDS


WRITE_ACTIONS = {
    "WRITE_SCHEDULE": "SCHEDULE", "UPDATE_SCHEDULE": "SCHEDULE",
    "WRITE_DIARY": "DIARY", "UPDATE_DIARY": "DIARY",
}
READ_ACTIONS = {"INSPECT_SCHEDULE": "SCHEDULE", "INSPECT_DIARIES": "DIARY"}
MAX_CONTENT_LENGTH = 4000


def _day(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("game_date")
    return date.fromisoformat(value).toordinal() - 1


def _date(day):
    return date.fromordinal(day + 1).isoformat()


def _time(minute):
    day, minute_of_day = divmod(minute, 1440)
    hour, minute = divmod(minute_of_day, 60)
    return f"{_date(day)} {hour:02d}:{minute:02d}"


def record_data(record, *, summary=False):
    data = {
        "record_id": str(record.pk), "kind": record.kind,
        "game_date": _date(record.game_day),
        "created_world_time": _time(record.created_world_minute),
        "updated_world_time": _time(record.updated_world_minute),
    }
    if summary:
        data["summary"] = " ".join(record.content.split())[:120]
    else:
        data["content"] = record.content
    return data


def query_daily_records(npc, kind, params=None):
    params = params if params is not None else {}
    if kind not in {"SCHEDULE", "DIARY"} or set(params) - {"view", "game_date", "before_date", "limit"}:
        raise ValueError("query")
    if hasattr(params, "getlist") and any(len(params.getlist(key)) != 1 for key in params):
        raise ValueError("duplicate parameter")
    today = GlobalGameTimeState.get_instance().world_minute // 1440
    result = {"kind": kind, "current_game_date": _date(today)}
    records = NpcDailyRecord.objects.filter(npc=npc, kind=kind)
    view = params.get("view", "detail")
    if view == "detail":
        if "before_date" in params or "limit" in params:
            raise ValueError("detail parameters")
        day = _day(params["game_date"]) if "game_date" in params else today
        record = records.filter(game_day=day).first()
        return {**result, "game_date": _date(day), "record": record_data(record) if record else None}
    if view != "list" or "game_date" in params:
        raise ValueError("view")
    raw_limit = params.get("limit", 10)
    if not re.fullmatch(r"[0-9]{1,2}", str(raw_limit)) or not 1 <= int(raw_limit) <= 50:
        raise ValueError("limit")
    limit = int(raw_limit)
    if "before_date" in params:
        records = records.filter(game_day__lt=_day(params["before_date"]))
    rows = list(records.order_by("-game_day")[:limit + 1])
    return {
        **result, "records": [record_data(row, summary=True) for row in rows[:limit]],
        "next_before_date": _date(rows[limit - 1].game_day) if len(rows) > limit else None,
    }


def _result(success, code, data=None):
    return {"success": success, "result_code": code, "data": data or {}}


@transaction.atomic
def write_daily_record(envelope):
    if not isinstance(envelope, dict) or not isinstance(envelope.get("content"), dict):
        return _result(False, "INVALID_DAILY_RECORD_SCHEMA")
    content = envelope["content"]
    action = content.get("name")
    params = content.get("params")
    sender = str(envelope.get("sender", ""))
    if sender not in FORMAL_NPC_IDS:
        return _result(False, "SENDER_BINDING_INVALID")
    if not isinstance(action, str) or action not in WRITE_ACTIONS or content.get("schema_version") != "village.command.v1" or not isinstance(params, dict):
        return _result(False, "INVALID_DAILY_RECORD_SCHEMA")
    try:
        uuid.UUID(str(envelope.get("envelope_id", "")))
        uuid.UUID(str(content.get("id", "")))
        request_id = uuid.UUID(str(content.get("request_id", "")))
    except (ValueError, TypeError, AttributeError):
        return _result(False, "INVALID_DAILY_RECORD_SCHEMA")

    runtime = NpcRuntimeState.objects.select_for_update().select_related("npc").filter(
        npc__agent_id=sender, npc__is_test_only=False, npc__village_profile__is_active=True,
    ).first()
    if runtime is None:
        return _result(False, "NPC_NOT_FOUND")
    # Hash only the caller's intent. Unity's envelope timestamps/state versions
    # can change on redelivery; omitted dates must replay their original day.
    digest = hashlib.sha256(json.dumps(
        [sender, action, params], sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()
    receipt = VillageActionReceipt.objects.filter(request_id=request_id).first()
    if receipt:
        saved = dict(receipt.response_data or {})
        if receipt.npc_id != runtime.npc_id or receipt.action_kind != action or saved.pop("payload_sha256", None) != digest:
            return _result(False, "DAILY_RECORD_REQUEST_CONFLICT")
        return _result(receipt.success, receipt.result_code, {**saved, "replayed": True})

    from .runtime_services import CommandService
    if action not in CommandService.allowed_actions_for(runtime.npc):
        return _result(False, "ACTION_NOT_ALLOWED")
    clock = GlobalGameTimeState.get_instance()
    kind = WRITE_ACTIONS[action]
    updating = action.startswith("UPDATE_")

    def finish(success, code, data=None):
        data = {**(data or {}), "replayed": False}
        VillageActionReceipt.objects.create(
            request_id=request_id, npc=runtime.npc, action_kind=action,
            success=success, result_code=code,
            response_data={**data, "payload_sha256": digest},
            world_minute=clock.world_minute, time_revision=clock.time_revision,
            state_version=runtime.state_version,
        )
        return _result(success, code, data)

    allowed = {"content", "game_date"}
    required = {"content"} | ({"game_date"} if updating else set())
    if set(params) - allowed or required - set(params):
        return finish(False, "INVALID_DAILY_RECORD_SCHEMA")
    text = params.get("content")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_CONTENT_LENGTH or "\x00" in text:
        return finish(False, "INVALID_DAILY_RECORD_CONTENT")
    today = clock.world_minute // 1440
    try:
        day = _day(params["game_date"]) if "game_date" in params else today
    except ValueError:
        return finish(False, "INVALID_GAME_DATE")
    if kind == "DIARY" and day > today:
        return finish(False, "DIARY_FUTURE_DATE", {"current_game_date": _date(today)})
    record = NpcDailyRecord.objects.filter(npc=runtime.npc, kind=kind, game_day=day).first()
    data = {"kind": kind, "game_date": _date(day)}
    if updating and record is None:
        return finish(False, "DAILY_RECORD_NOT_FOUND", data)
    if record is not None and not updating:
        return finish(False, f"{kind}_ALREADY_EXISTS", {
            **data, "record": record_data(record),
        })
    if record is None:
        record = NpcDailyRecord(npc=runtime.npc, kind=kind, game_day=day, created_world_minute=clock.world_minute)
    else:
        record.version += 1
    record.content = text
    record.updated_world_minute = clock.world_minute
    record.save()
    return finish(True, f"{kind}_{'UPDATED' if updating else 'CREATED'}", {**data, "record": record_data(record)})
