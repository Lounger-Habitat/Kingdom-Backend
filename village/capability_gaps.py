"""Agent-authored capability requests, backed by Action tasks and a JSONL log."""
from contextlib import contextmanager
from copy import deepcopy
import json
import logging
import os
from pathlib import Path
import tempfile

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import NpcProfile, NpcRuntimeState, VillageActionTask
from .phase5_contract import FORMAL_NPC_IDS


ACTION = "REPORT_CAPABILITY_GAP"
RECORD_KEY = "capability_gap_record"
logger = logging.getLogger(__name__)
_SAVED_MESSAGE = "需求已保存，但记录文件暂未写入，需由开发者补导。原请求的信息尚未提供、动作尚未执行，无需等待或重复提交。"
_EXPORTED_MESSAGE = "需求已记录到文件。原请求的信息尚未提供、动作尚未执行；记录不代表能力已开放，无需等待或重复提交。"


def log_path():
    return Path(settings.AGENT_CAPABILITY_GAP_LOG_PATH).expanduser().resolve()


@contextmanager
def _log_lock(path):
    """Serialize writers across Gunicorn workers, including on Windows locally."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with Path(str(path) + ".lock").open("a+b") as guard:
        if os.name == "nt":
            import msvcrt
            if guard.seek(0, os.SEEK_END) == 0:
                guard.write(b"\0")
                guard.flush()
            guard.seek(0)
            msvcrt.locking(guard.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(guard, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                guard.seek(0)
                msvcrt.locking(guard.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(guard, fcntl.LOCK_UN)


def _line(record):
    return (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def append_report(record):
    path = log_path()
    with _log_lock(path), path.open("a+b") as stream:
        stream.seek(0)
        found = False
        while True:
            offset = stream.tell()
            line = stream.readline()
            if not line:
                break
            try:
                previous = json.loads(line)
                if not isinstance(previous, dict):
                    raise ValueError("Capability gap log contains a non-object record.")
            except (ValueError, UnicodeError):
                if line.endswith(b"\n"):
                    raise
                # A process may stop halfway through its final append. The
                # committed database record remains available for re-export.
                stream.truncate(offset)
                break
            found |= previous.get("report_id") == record["report_id"]
            if not line.endswith(b"\n"):
                stream.write(b"\n")
                break
        if not found:
            stream.write(_line(record))
        stream.flush()
        os.fsync(stream.fileno())


def export_task(task):
    """Run only after commit; keep a recoverable record if the log is unwritable."""
    result = deepcopy(task.result)
    record = result.get(RECORD_KEY)
    if record is None:
        return
    exported = False
    try:
        append_report(record)
        exported = True
    except (OSError, ValueError, UnicodeError):
        logger.exception("Capability gap %s saved in database; JSONL export failed", task.pk)
    result["data"].update(file_exported=exported,
                          message=_EXPORTED_MESSAGE if exported else _SAVED_MESSAGE)
    if task.result != result:
        task.result = result
        VillageActionTask.objects.filter(pk=task.pk).update(result=result)


def record_capability_gap(task, world_minute):
    params = task.envelope["payload"]["content"]["params"]
    profile = NpcProfile.objects.select_related("npc").filter(
        npc__agent_id=task.sender, npc__is_test_only=False, is_active=True,
    ).first() if task.sender in FORMAL_NPC_IDS else None
    code = "CAPABILITY_GAP_RECORDED"
    if profile is None:
        code = "SENDER_BINDING_INVALID"
    elif (set(params) - {"type", "request", "purpose", "blocker"}
          or params.get("type") not in ("information", "action")
          or any(not isinstance(params.get(key), str) or not params[key].strip()
                 or len(params[key]) > limit or "\0" in params[key]
                 for key, limit in (("request", 2000), ("purpose", 1000)))
          or "blocker" in params and (not isinstance(params["blocker"], str)
                                     or len(params["blocker"]) > 2000 or "\0" in params["blocker"])):
        code = "INVALID_CAPABILITY_GAP_PARAMS"
    success = code == "CAPABILITY_GAP_RECORDED"
    task.result = {"success": success, "result_code": code, "data": {}}
    if success:
        state = NpcRuntimeState.objects.filter(npc=profile.npc).values(
            "current_location_code", "current_action", "action_status",
        ).first() or {}
        task.result[RECORD_KEY] = {
            "report_id": str(task.pk), "recorded_at_utc": timezone.now().isoformat(),
            "environment_id": task.environment, "epoch": str(task.epoch),
            "agent_id": task.sender, "agent_name": profile.display_name,
            "action_id": task.action_id, "world_minute": world_minute,
            "context_source": "backend_snapshot",
            "location": state.get("current_location_code"),
            "current_action": state.get("current_action"), "action_status": state.get("action_status"),
            "type": params["type"], "request": params["request"], "purpose": params["purpose"],
            "blocker": params.get("blocker", ""),
        }
        task.result["data"] = {"report_id": str(task.pk), "file_exported": False, "message": _SAVED_MESSAGE}
    else:
        task.result["data"]["message"] = "需求未记录，请检查身份和参数；type须为information或action，request和purpose须为非空文字。"
    task.save(update_fields=["result"])
    if success:
        transaction.on_commit(lambda: export_task(task), robust=True)
    return success, code, task, False


def rebuild_log():
    """Recreate a complete log from committed reports, including earlier epochs."""
    path = log_path()
    temporary = None
    count = 0
    with _log_lock(path):
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                tasks = VillageActionTask.objects.filter(result__has_key=RECORD_KEY).order_by(
                    "result__capability_gap_record__recorded_at_utc", "id",
                )
                for task in tasks.iterator():
                    stream.write(_line(task.result[RECORD_KEY]))
                    count += 1
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return count, path
