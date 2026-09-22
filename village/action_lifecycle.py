"""Agent operation identity and recovery over the existing command/receipt stores."""
import re

from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from account.env_auth import EnvServerJWTAuthentication
from .models import NpcCommandState, NpcRuntimeState, VillageActionReceipt, VillageDeliveryOutbox, VillageSimulationConfig

PENDING = ("RECEIVED", "QUEUED", "RUNNING", "SETTLING")


def current_epoch():
    config, _ = VillageSimulationConfig.objects.get_or_create(config_key="default")
    return str(config.action_epoch)


def valid_execution(value):
    return (isinstance(value, dict)
            and isinstance(value.get("operation_key"), str)
            and re.fullmatch(r"[0-9a-f]{32}", value["operation_key"]) is not None
            and isinstance(value.get("action_id"), str) and 0 < len(value["action_id"]) <= 160
            and isinstance(value.get("epoch"), str)
            and isinstance(value.get("request"), dict)
            and isinstance(value["request"].get("name"), str)
            and isinstance(value["request"].get("params"), dict))


def operation_result(command):
    execution = command.agent_execution
    result = {**execution, "status": "running" if command.status in ("RUNNING", "SETTLING") else "accepted",
              "sequence": 2 if command.status in ("RUNNING", "SETTLING") else 1,
              "command_id": str(command.command_id)}
    if command.status in PENDING:
        return result
    delivery = VillageDeliveryOutbox.objects.filter(command=command, message_kind="OUTCOME").first()
    receipt = VillageActionReceipt.objects.filter(command=command).first()
    if delivery:
        outcome = dict(delivery.payload)
    elif receipt:
        outcome = dict(receipt.response_data or {})
        outcome.update(success=receipt.success, result_code=receipt.result_code)
    else:
        # A terminal command without its durable result is not proof of success.
        return {**result, "status": "unknown"}
    outcome.update(ref_id=str(command.command_id), action=command.kind,
                   schema_version="village.outcome.v1", terminal=True, phase="COMPLETED",
                   agent_execution=execution)
    return {**result, "status": "terminal", "outcome": outcome}


@api_view(["GET"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def runtime_operation(request, npc_id):
    """Only Env's authenticated NPC routing can select the owner of a receipt."""
    key = request.query_params.get("operation_key", "")
    command = NpcCommandState.objects.filter(npc__agent_id=npc_id,
        agent_execution__operation_key=key, agent_execution__epoch=current_epoch()).first() if key else None
    result = operation_result(command) if command else {
        "operation_key": key, "epoch": current_epoch(), "status": "not_found"}
    return Response({"success": True, "resultCode": "OK", "data": result})


@api_view(["GET"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def runtime_resources(request, npc_id):
    claims = [{"id": str(row.command_id), "resources": ["body"], "allowed_actions": [],
               "reason": "已有身体动作尚未结算，请等待原动作完成。"}
              for row in NpcCommandState.objects.filter(npc__agent_id=npc_id, status__in=PENDING)]
    runtime = NpcRuntimeState.objects.filter(npc__agent_id=npc_id).first()
    if not claims and runtime and runtime.action_status not in ("IDLE", ""):
        claims.append({"id": f"npc:{npc_id}:{runtime.action_status}", "resources": ["body"],
                       "allowed_actions": [], "reason": "NPC 的身体仍被当前活动占用。"})
    return Response({"success": True, "resultCode": "OK", "data": {
        "version": 1, "epoch": current_epoch(), "claims": claims}})
