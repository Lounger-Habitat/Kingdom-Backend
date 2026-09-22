"""Authenticated NPC command adapter for personal commissions."""

from .models import VillageActionReceipt, VillageWorldEvent


MUTATIONS = frozenset({
    "PUBLISH_PERSONAL_COMMISSION", "WITHDRAW_PERSONAL_COMMISSION",
})
QUERY = "INSPECT_PERSONAL_COMMISSIONS"


def validate_intent(action, params):
    """NPCs may publish their own commissions or withdraw unaccepted ones."""
    if action == "PUBLISH_PERSONAL_COMMISSION":
        allowed = {"title", "description", "reward_gold", "valid_for_minutes", "default_location_code", "stages"}
        required = {"title", "reward_gold", "valid_for_minutes", "stages"}
    elif action == "WITHDRAW_PERSONAL_COMMISSION":
        allowed = required = {"commission_id"}
    else:
        return "UNKNOWN_ACTION"
    if not isinstance(params, dict) or set(params) - allowed or not required.issubset(params):
        return "INVALID_COMMAND_SCHEMA"
    if action == "WITHDRAW_PERSONAL_COMMISSION" and not isinstance(params.get("commission_id"), str):
        return "INVALID_COMMAND_SCHEMA"
    return None


def dispatch(command, runtime, world_minute):
    from . import personal_commissions as core
    params = command.sanitized_parameters
    if command.kind == "PUBLISH_PERSONAL_COMMISSION":
        return core.publish_commission(command.npc, params, command.request_id, world_minute)
    if command.kind == "WITHDRAW_PERSONAL_COMMISSION":
        return core.withdraw_commission(params.get("commission_id"), "NPC", str(command.npc_id),
            command.request_id, world_minute)
    raise core.CommissionError("COMMISSION_PLAYER_ONLY", "个人委托只能由玩家认领和完成。")


def finish_immediate(command, runtime, clock):
    from .personal_commissions import CommissionError
    from .runtime_services import OutboxService, _outcome_message
    try:
        data = dispatch(command, runtime, clock.world_minute)
        success, code = True, "OK"
    except CommissionError as error:
        success, code, data = False, error.code, {"message": str(error)}
    runtime.state_version += 1
    runtime.save(update_fields=["state_version", "updated_at"])
    event = VillageWorldEvent.objects.create(dedupe_key=f"commission-command:{command.command_id}",
        event_type="NPC_ACTION_COMPLETED" if success else "NPC_ACTION_FAILED", actor_npc=command.npc,
        participant_npc_ids=[str(command.npc.agent_id)], command_id=command.command_id,
        request_id=command.request_id, world_minute=clock.world_minute, time_revision=clock.time_revision,
        location_code=runtime.current_location_code, visibility="PUBLIC",
        player_summary=_outcome_message(command.kind, code, success, clock.world_minute, data),
        hub_summary=_outcome_message(command.kind, code, success, clock.world_minute, data))
    receipt = VillageActionReceipt.objects.create(request_id=command.request_id, command=command,
        npc=command.npc, action_kind=command.kind, success=success, result_code=code, response_data=data,
        world_minute=clock.world_minute, time_revision=clock.time_revision,
        state_version=runtime.state_version, world_event=event)
    command.status = "SUCCEEDED" if success else "FAILED"
    command.result_code = code
    command.completed_world_minute = clock.world_minute
    command.state_version = runtime.state_version
    command.save(update_fields=["status", "result_code", "completed_world_minute", "state_version", "updated_at"])
    OutboxService.ensure_outcome(receipt)
    return success, code, command
