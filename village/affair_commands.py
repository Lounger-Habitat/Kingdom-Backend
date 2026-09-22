"""NPC commands for system-owned affairs; no story or meeting authority."""
import uuid

from django.db import transaction
from inventory.models import NpcGlobalInventory
from .affairs import AffairError, resolve_condition, validate_contribution, contribute
from .models import VillageAffair, EconomicLedgerEntry, VillageRouteAttempt

MUTATIONS = frozenset({"CONTRIBUTE_TO_AFFAIR", "CONTACT_EMERGENCY_CARAVAN"})


def validate_intent(action, params):
    if not isinstance(params, dict):
        return "INVALID_COMMAND_SCHEMA"
    if action == "CONTRIBUTE_TO_AFFAIR":
        required = {"affair_id", "condition_id", "kind"}
        kind = params.get("kind")
        extra = {"item_id", "amount", "food_instance_id"} if kind == "item" else {"amount"} if kind == "gold" else set()
        if kind not in {"item", "gold", "labor"} or set(params) - required - extra or not required <= set(params):
            return "INVALID_COMMAND_SCHEMA"
        if any(not isinstance(params[k], str) or not params[k].strip() for k in ("affair_id", "condition_id")):
            return "INVALID_COMMAND_SCHEMA"
        if kind != "labor" and (type(params.get("amount")) is not int or not 0 < params["amount"] <= 2**31-1):
            return "INVALID_COMMAND_SCHEMA"
        if kind == "item" and (type(params.get("item_id")) is not int or params["item_id"] <= 0):
            return "INVALID_COMMAND_SCHEMA"
        if "food_instance_id" in params:
            try:
                uuid.UUID(str(params["food_instance_id"]))
            except (ValueError, TypeError):
                return "INVALID_COMMAND_SCHEMA"
    elif action == "CONTACT_EMERGENCY_CARAVAN":
        if set(params) - {"route_attempt_id", "item_ids"}:
            return "INVALID_COMMAND_SCHEMA"
        try:
            uuid.UUID(str(params.get("route_attempt_id")))
        except (ValueError, TypeError):
            return "INVALID_COMMAND_SCHEMA"
    else:
        return "INVALID_COMMAND_SCHEMA"
    return None


def get_target(params):
    row = VillageAffair.objects.select_for_update().filter(pk=params.get("affair_id"), kind="RESOURCE_PROJECT").first()
    if row is None:
        raise AffairError("AFFAIR_NOT_FOUND", "事务不存在，请先查询村庄事务。")
    return row


def labor_unavailable(runtime, minute, params):
    from .runtime_services import CommandService
    if runtime.action_status == "AWAY" or (runtime.away_until_world_minute is not None and runtime.away_until_world_minute > minute):
        return "NPC_AWAY"
    error = CommandService.unavailable_reason("CONTRIBUTE_TO_AFFAIR", runtime, params)
    if error:
        return error
    try:
        condition = resolve_condition(get_target(params), params.get("condition_id"), "labor")
    except AffairError as error:
        return error.code
    if condition["locations"] and runtime.current_location_code not in condition["locations"]:
        return "AFFAIR_LOCATION_INVALID"
    return None


def _contribute(command, runtime, minute):
    from .phase5_story import _consume, _money
    params = command.sanitized_parameters
    kind = params["kind"]
    row = get_target(params)
    quantity = 1 if kind == "labor" else params["amount"]
    item_id = params.get("item_id")
    condition = validate_contribution(row, kind, quantity, item_id, params["condition_id"])
    if condition["locations"] and runtime.current_location_code not in condition["locations"]:
        raise AffairError("AFFAIR_LOCATION_INVALID", "请到条件指定地点贡献。")
    actor_id = str(command.npc.agent_id)
    changes = {"affair_id": row.code, "condition_id": condition["id"], "kind": kind}
    if kind == "labor":
        if (command.status != "RUNNING" or runtime.active_command_id != command.command_id
                or command.started_world_minute is None or command.duration_minutes != condition["unit_minutes"]
                or minute < command.started_world_minute + command.duration_minutes):
            raise AffairError("AFFAIR_LABOR_NOT_FINISHED", "劳动尚未完成。")
        error = labor_unavailable(runtime, minute, params)
        if error:
            raise AffairError(error, "当前不能完成劳动。")
    elif kind == "item":
        if not _consume(actor_id, item_id, quantity, condition=condition, instance_id=params.get("food_instance_id")):
            raise AffairError("AFFAIR_RESOURCE_INSUFFICIENT", "合格且未占用的物品不足。")
        changes.update({actor_id: {str(item_id): -quantity}, "AFFAIR:" + row.code: {str(item_id): quantity}})
    elif not _money(actor_id, quantity):
        raise AffairError("AFFAIR_RESOURCE_INSUFFICIENT", "可用金币不足。")
    else:
        changes.update({actor_id: {"money": -quantity}, "AFFAIR:" + row.code: {"money": quantity}})
    projection = contribute(row, kind, quantity, item_id=item_id, condition_id=condition["id"],
        world_minute=minute, request_id=command.request_id, actor_npc=command.npc)
    return {"affair_id": row.code, "condition_id": condition["id"], "kind": kind,
        "amount": quantity, "item_id": item_id, "affair": projection}, changes, 0




def _contact(command, runtime, minute):
    from . import caravan
    from .phase5_story import _money
    if str(command.npc.agent_id) != "10015":
        raise AffairError("ACTION_NOT_ALLOWED", "仅商人可联系应急商队。")
    if runtime.current_location_code not in {"GENERAL_STORE", "MARKET_STREET"}:
        raise AffairError("AFFAIR_LOCATION_INVALID", "请到杂货铺或市场街联系商队。")
    cfg = caravan.config()
    if not cfg or not cfg.village_economy_enabled:
        raise AffairError("VILLAGE_ECONOMY_DISABLED", "经济功能未启用。")
    params = command.sanitized_parameters
    attempt_id = uuid.UUID(str(params["route_attempt_id"]))
    if VillageRouteAttempt.objects.filter(pk=attempt_id).exists():
        raise AffairError("ROUTE_ATTEMPT_REPLAY", "该联系尝试已处理，请查询商队状态。")
    demand = params.get("item_ids")
    if demand is not None:
        error = caravan.validate_demand(demand, cfg)
        if error:
            raise AffairError(error, "商队需求无效。")
    already_open = caravan.route_readiness()["route_open"]
    gold = 0 if already_open else cfg.caravan_contact_cost
    if gold and not _money(str(command.npc.agent_id), gold):
        raise AffairError("AFFAIR_RESOURCE_INSUFFICIENT", "联系商队的金币不足。")
    if demand is not None:
        caravan.save_demand(demand, minute)
    VillageRouteAttempt.objects.create(route_attempt_id=attempt_id, request_id=command.request_id,
        frozen_input={"rule": "NORA_FEE_ONLY_V1"}, seed=f"guaranteed-contact:{attempt_id}", result_code="SUCCESS",
        cost={"gold": gold}, progress={"succeeded": True, "already_open": already_open}, created_world_minute=minute)
    visit = caravan.projection(minute)
    data = dict(route_attempt_id=str(attempt_id), route_result="SUCCESS", route_open=True, already_open=already_open,
        contact_cost=gold, route_frozen=visit["route_frozen"], arrival_world_minute=visit["next_arrival_world_minute"],
        arrival_conditional=False)
    if demand is not None:
        data["requested_items"] = [{"item_id": item} for item in sorted(set(demand))]
    return data, {"10015": {"money": -gold}}, -gold


def execute(command, runtime, minute):
    """Caller holds the clock/runtime locks. Roll back all effects on business failure."""
    from .runtime_services import CommandService
    try:
        with transaction.atomic():
            error = CommandService.unavailable_reason(command.kind, runtime, command.sanitized_parameters)
            if error:
                raise AffairError(error, "当前身体状态不能执行此操作。")
            if runtime.action_status in {"AWAY", "SLEEPING"} or (runtime.away_until_world_minute is not None and runtime.away_until_world_minute > minute):
                raise AffairError("NPC_AWAY", "角色当前不能参与。")
            handler = {"CONTRIBUTE_TO_AFFAIR": _contribute,
                       "CONTACT_EMERGENCY_CARAVAN": _contact}[command.kind]
            data, changes, external = handler(command, runtime, minute)
            EconomicLedgerEntry.objects.create(request_id=command.request_id, entry_type=command.kind,
                actor_npc=command.npc, changes=changes, external_delta=external, world_minute=minute)
        return True, "OK", data
    except AffairError as error:
        return False, error.code, {"message": str(error)}


def finish_immediate(command, runtime, clock):
    from .runtime_services import ActionService
    success, code, data = execute(command, runtime, clock.world_minute)
    # The normal lifecycle owns the receipt and outbox for both instant and timed work.
    command.status = "RUNNING"
    command.started_world_minute = clock.world_minute
    command.save(update_fields=["status", "started_world_minute"])
    ActionService.complete_action(command.command_id, command.request_id, success, code, data=data)
    command.refresh_from_db()
    return success, code, command
