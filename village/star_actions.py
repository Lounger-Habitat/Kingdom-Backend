"""Original star-protocol Actions and their private execution records."""
from copy import deepcopy
import time
import uuid

from django.conf import settings
from django.db import transaction

from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition
from .models import NpcProfile, VillageActionTask, VillageSimulationConfig


def valid_envelope(envelope):
    if not isinstance(envelope, dict) or set(envelope) != {"id", "timestamp", "type", "sender", "recipient", "payload"}:
        return False
    payload = envelope.get("payload")
    if not isinstance(payload, dict) or set(payload) != {"type", "content"} or payload.get("type") != "action":
        return False
    content = payload.get("content")
    if not isinstance(content, dict) or set(content) != {"id", "name", "params"} or not isinstance(content["params"], dict):
        return False
    if envelope["type"] != "message" or isinstance(envelope["timestamp"], bool) or not isinstance(envelope["timestamp"], int) or envelope["timestamp"] < 0:
        return False
    for value, maximum in ((content["id"], 256), (content["name"], 128), (envelope["sender"], 32), (envelope["recipient"], 128)):
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            return False
    try:
        uuid.UUID(envelope["id"])
    except (ValueError, TypeError, AttributeError):
        return False
    return True


def task_context(task):
    """HTTP execution data, kept outside the original Action."""
    command = task.command
    return {
        "task_id": str(task.pk), "action": deepcopy(task.envelope),
        "status": command.status if command else ("SUCCEEDED" if task.result.get("success") else "FAILED"),
        "parameters": deepcopy(command.sanitized_parameters if command else task.envelope["payload"]["content"]["params"]),
        "duration_minutes": command.duration_minutes if command else None,
        "result": deepcopy(task.result),
    }


def command_task_context(command):
    task = VillageActionTask.objects.filter(command=command).select_related("command").first()
    return task_context(task) if task else None


def resolve_parameters(parameters, task, world_minute, action):
    params = deepcopy(parameters)
    for field in ("target_name", "target_npc_name"):
        if field not in params:
            continue
        name = params[field]
        kind = params.get("target_type") if field == "target_name" else "NPC"
        destination = "target_id" if field == "target_name" else "target_npc_id"
        if destination in params or not isinstance(name, str) or not name.strip():
            return None, "ACTOR_NAME_INVALID"
        if kind == "NPC":
            matches = list(NpcProfile.objects.filter(display_name=name, is_active=True, npc__is_test_only=False).values_list("npc__agent_id", flat=True)[:2])
        elif kind == "PLAYER":
            from account.models import GameAccount
            matches = list(GameAccount.objects.filter(username=name, is_active=True).values_list("id", flat=True)[:2])
        else:
            return None, "ACTOR_NAME_INVALID"
        if len(matches) != 1:
            return None, "ACTOR_NAME_NOT_FOUND" if not matches else "ACTOR_NAME_AMBIGUOUS"
        params[destination] = str(matches[0])
        del params[field]
    if action == "SOCIALIZE":
        params.setdefault("reservation_id", str(task.pk))
    if action in {"CREATE_ORDER", "PROPOSE_TRADE"}:
        params.setdefault("expires_world_minute", world_minute + 1440)
    if action in {"PUBLISH_PRICE_INDEX", "AUDIT_PUBLIC_FUND"}:
        params.setdefault("day_index", world_minute // 1440)
    if action in {"STORY_VOTE", "STORY_ACT"}:
        from .phase5_story import get_story_snapshot
        params["expected_story_version"] = get_story_snapshot()["data"]["story_version"]
    return params, None


@transaction.atomic
def accept_action(envelope):
    from .runtime_services import CommandService, ResultCode
    if not valid_envelope(envelope):
        return False, ResultCode.INVALID_COMMAND_SCHEMA, None, False
    original = deepcopy(envelope)
    content = original["payload"]["content"]
    sender, environment = original["sender"], original["recipient"]
    if not NpcDefinition.objects.filter(agent_id=sender, is_test_only=False).exists():
        return False, ResultCode.SENDER_BINDING_INVALID, None, False
    GlobalGameTimeState.get_instance()
    clock = GlobalGameTimeState.objects.select_for_update().get(pk=1)
    config, _ = VillageSimulationConfig.objects.get_or_create(config_key="default")
    task = VillageActionTask.objects.filter(environment=environment, sender=sender,
        action_id=content["id"], epoch=config.action_epoch).select_related("command").first()
    if task:
        if CommandService.compute_payload_sha256(task.envelope["payload"]["content"]) != CommandService.compute_payload_sha256(content):
            return False, ResultCode.REQUEST_ID_CONFLICT, None, False
        return (task.result.get("success", True),
                ResultCode.COMMAND_REPLAYED if task.command_id else task.result.get("result_code", ResultCode.COMMAND_ACCEPTED),
                task, True)
    task = VillageActionTask.objects.create(environment=environment, sender=sender,
        action_id=content["id"], epoch=config.action_epoch, envelope=original)

    def finish(ok, code, data=None):
        task.result = {"success": bool(ok), "result_code": code, "data": data or {}}
        task.save(update_fields=["result"])
        return bool(ok), code, task, False

    action = content["name"].upper()
    now_ms = int(time.time() * 1000)
    if original["timestamp"] > now_ms + CommandService.MAX_CLOCK_SKEW_MS:
        return finish(False, ResultCode.CLOCK_SKEW)
    if original["timestamp"] + getattr(settings, "VILLAGE_ACTION_MAX_AGE_MS", 86_400_000) < now_ms:
        return finish(False, ResultCode.MESSAGE_EXPIRED)
    parameters, error = resolve_parameters(content["params"], task, clock.world_minute, action)
    if error:
        return finish(False, error)
    from .agent_queries import QUERY_NAMES, execute_agent_query
    if action in QUERY_NAMES:
        ok, code, data = execute_agent_query(sender, action, parameters)
        return finish(ok, code, data)
    from .daily_records import WRITE_ACTIONS, write_daily_record
    business = {"envelope_id": original["id"], "sender": sender, "recipient": environment,
                "timestamp": original["timestamp"], "content": {"id": content["id"], "name": action, "params": parameters}}
    if action in WRITE_ACTIONS:
        result = write_daily_record(business, action_task=task)
        return finish(result["success"], result["result_code"], result["data"])
    if action == "UPDATE_SUBJECTIVE_GOAL":
        from .goal_services import update_subjective_goal
        result = update_subjective_goal(business, action_task=task)
        return finish(result["success"], result["result_code"], result["data"])
    if action == "WAKE_UP":
        if set(parameters) != {"sleep_action_id"} or not isinstance(parameters["sleep_action_id"], str):
            return finish(False, ResultCode.INVALID_COMMAND_SCHEMA)
        sleeping = VillageActionTask.objects.filter(environment=environment, sender=sender,
            action_id=parameters["sleep_action_id"], epoch=config.action_epoch, command__kind="SLEEP").first()
        if sleeping is None:
            return finish(False, "SLEEP_TASK_NOT_FOUND")
        from .phase2_services import LifeActionService
        result = LifeActionService.interrupt_sleep(sender, str(sleeping.command_id))
        return finish(result["success"], result["result_code"],
            {key: value for key, value in result.items() if key not in {"success", "result_code", "receipt"}})
    ok, code, command = CommandService.accept_command(original["id"], sender, environment,
        business["content"], original["timestamp"], action_task=task)
    if command:
        task.command = command
        task.save(update_fields=["command"])
    if not ok:
        return finish(False, code)
    return True, code, task, False
