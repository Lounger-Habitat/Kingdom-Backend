"""Jack's server-owned hunt/process lifecycle, reservations and fatigue."""
import uuid

from inventory.models import NpcGlobalInventory, NpcGlobalInventorySlot
from items.models import Item
from .models import NpcCommandState, VillageActionDurationConfig, VillageSimulationConfig
from .phase3_catalog import (
    BYPRODUCT_ITEM_IDS, HUNT_ANIMALS_CODE, HUNT_MEAT_SPOILAGE_MINUTES,
    HUNT_TOOL_ITEM_ID, PREY_ITEM_IDS, PREY_RECIPES,
)

JACK_ACTIONS = frozenset({"HUNT", "PROCESS_GAME"})
RULE_VERSION = 1


def validate_intent(action, params):
    allowed = {"expected_asset_version", "hunt_code" if action == "HUNT" else "prey_instance_id"}
    if set(params) - allowed:
        return "P3_INVALID_PARAMS"
    version = params.get("expected_asset_version")
    if version is not None and (type(version) is not int or version < 0):
        return "P3_INVALID_PARAMS"
    if action == "HUNT":
        if params.get("hunt_code") != HUNT_ANIMALS_CODE:
            return "HUNT_CODE_RETIRED"
    else:
        try:
            uuid.UUID(str(params.get("prey_instance_id")))
        except (ValueError, TypeError):
            return "P3_INVALID_PARAMS"
    return None


def reserved_amount(slot, exclude_command_id=None):
    rows = NpcCommandState.objects.filter(
        npc_id=slot.bag.npc_id, kind__in=JACK_ACTIONS, status__in={"RUNNING", "SETTLING"},
    )
    if exclude_command_id is not None:
        rows = rows.exclude(command_id=exclude_command_id)
    instance = str(slot.instance_id)
    return sum(
        int(state.get("tool_instance_id") == instance) + int(state.get("prey_instance_id") == instance)
        for state in rows.values_list("production_state", flat=True)
        if state.get("rule_version") == RULE_VERSION
    )


def is_jack_work(command):
    return command.kind in JACK_ACTIONS and command.production_state.get("rule_version") == RULE_VERSION


def work_unavailable(runtime, command, minute, *, resources=True):
    from .runtime_services import CommandService
    from .phase3_asset_service import get_available_slot_amount
    if str(runtime.npc.agent_id) != "10012":
        return "P3_FORBIDDEN_ACTOR"
    if runtime.action_status == "AWAY" or (
        runtime.away_until_world_minute is not None and runtime.away_until_world_minute > minute
    ):
        return "NPC_AWAY"
    error = CommandService.unavailable_reason(command.kind, runtime)
    if error or not resources:
        return error
    state = command.production_state
    for key in ("tool_instance_id", "prey_instance_id"):
        value = state.get(key)
        if not value:
            continue
        slot = NpcGlobalInventorySlot.objects.select_for_update().filter(
            instance_id=value, bag__npc=runtime.npc,
        ).first()
        if slot is None or get_available_slot_amount(slot, minute, command.command_id) < 1:
            return "PRODUCTION_RESOURCE_UNAVAILABLE"
        if key == "tool_instance_id" and (slot.remaining_uses is None or slot.remaining_uses <= 0):
            return "P3_TOOL_BROKEN"
        if key == "prey_instance_id" and (
            slot.item_id not in PREY_ITEM_IDS or slot.produced_world_minute is None
            or slot.expires_world_minute is None
        ):
            return "PREY_FRESHNESS_UNKNOWN"
    return None


def start_work(runtime, command, minute):
    """Called after lifecycle validation and before the RUNNING transition."""
    from .phase3_asset_service import get_available_slot_amount, get_usable_tool_slot
    error = work_unavailable(runtime, command, minute, resources=False)
    if error:
        return error
    if runtime.active_command_id is not None:
        return "NPC_BUSY"
    inventory = NpcGlobalInventory.objects.select_for_update().get(npc=runtime.npc)
    expected = command.sanitized_parameters.get("expected_asset_version")
    if expected is not None and inventory.asset_version != expected:
        return "P3_ASSET_VERSION_CONFLICT"
    try:
        tool = get_usable_tool_slot(inventory, HUNT_TOOL_ITEM_ID, minute)
    except ValueError as exc:
        return str(exc)
    state = dict(command.production_state)
    state.update(tool_instance_id=str(tool.instance_id), needs_until=minute)
    if command.kind == "PROCESS_GAME":
        prey = NpcGlobalInventorySlot.objects.select_for_update().filter(
            bag=inventory, instance_id=command.sanitized_parameters["prey_instance_id"],
            item_id__in=PREY_ITEM_IDS,
        ).first()
        if prey is None or get_available_slot_amount(prey, minute) < 1:
            return "PREY_UNAVAILABLE"
        if prey.produced_world_minute is None or prey.expires_world_minute is None:
            return "PREY_FRESHNESS_UNKNOWN"
        state["prey_instance_id"] = str(prey.instance_id)
        state["outputs"] = {str(i): n for i, n in PREY_RECIPES[prey.item_id]["outputs"].items()}
    else:
        day = minute // 1440
        previous = NpcCommandState.objects.filter(
            npc=runtime.npc, kind="HUNT", started_world_minute__gte=day * 1440,
            started_world_minute__lt=(day + 1) * 1440,
        ).exclude(command_id=command.command_id).count()
        config = VillageSimulationConfig.objects.get(config_key="default")
        extra = config.jack_repeat_hunt_energy * 10 if previous else 0
        if runtime.energy_fp - extra <= 100:
            return "ENERGY_TOO_LOW"
        # HUNT duration is a frozen pursuit budget, never a reward timer.
        # Species is supplied by the durable animal life only after an Env kill.
        state.update(hunt_day=day, hunt_ordinal=previous + 1, extra_energy_fp=extra)
        runtime.energy_fp -= extra
    command.production_state = state
    return None


def settle_work_needs(runtime, command, minute):
    """Ticks, cancellation and settlement share a durable minute cursor."""
    from .phase2_services import settle_action_needs_locked
    started = command.started_world_minute
    if started is None:
        return None
    state = dict(command.production_state)
    changed_fields = set(state.get("needs_changed_fields", []))
    cursor = int(state.get("needs_until", started))
    end = min(minute, started + command.duration_minutes)
    if command.kind == "HUNT" and state.get("death_world_minute") is not None:
        end = min(end, state["death_world_minute"])
    error = work_unavailable(runtime, command, cursor)
    if runtime.active_command_id != command.command_id:
        error = error or "ACTION_NOT_ALLOWED"
    while cursor < end and not error:
        error = settle_action_needs_locked(
            runtime, command.kind, cursor, cursor + 1,
            base_until=cursor + 1, changed_fields=changed_fields,
        )
        cursor += 1
        error = error or work_unavailable(runtime, command, cursor)
    state["needs_until"] = cursor
    state["needs_changed_fields"] = sorted(changed_fields)
    command.production_state = state
    command.save(update_fields=["production_state", "updated_at"])
    runtime.save()
    return error


def finish_work(npc_id, params, minute, request_id):
    from .phase3_asset_service import add_batch, consume_tool_use, _fail, _ok
    command = NpcCommandState.objects.select_for_update().filter(request_id=request_id).first()
    if command is None or not is_jack_work(command) or command.status != "RUNNING":
        return _fail("PRODUCTION_COMMAND_REQUIRED")
    if command.started_world_minute is None or (command.kind != "HUNT" and minute < command.started_world_minute + command.duration_minutes):
        return _fail("PRODUCTION_NOT_FINISHED")
    inventory = NpcGlobalInventory.objects.select_for_update().get(npc__agent_id=str(npc_id))
    expected = params.get("expected_asset_version")
    if expected is not None and inventory.asset_version != expected:
        return _fail("P3_ASSET_VERSION_CONFLICT")
    state = command.production_state
    end = command.started_world_minute + command.duration_minutes
    from .models import NpcRuntimeState
    runtime = NpcRuntimeState.objects.select_for_update().get(npc=inventory.npc)
    error = work_unavailable(runtime, command, minute)
    if error:
        return _fail(error)
    if command.kind == "HUNT":
        from .wildlife import confirmed_kill
        kill = confirmed_kill(command)
        if kill is None:
            return _fail("HUNT_KILL_REQUIRED")
        if kill.death_world_minute > end:
            return _fail("HUNT_TIMEOUT")
        from .wildlife import SPECIES
        outputs = {SPECIES[kill.species]: 1}
        produced = kill.death_world_minute
        expires = produced + HUNT_MEAT_SPOILAGE_MINUTES
        consumed = []
    else:
        prey = NpcGlobalInventorySlot.objects.select_for_update().get(instance_id=state["prey_instance_id"])
        outputs = {int(i): n for i, n in state["outputs"].items()}
        produced, expires = prey.produced_world_minute, prey.expires_world_minute
        prey.item_amount -= 1
        prey.save(update_fields=["item_amount"])
        consumed = [{"item_id": prey.item_id, "amount": 1, "instance_id": str(prey.instance_id)}]
    try:
        tool = consume_tool_use(npc_id, HUNT_TOOL_ITEM_ID, minute,
            exclude_command_id=command.command_id, slot_instance_id=state["tool_instance_id"])
    except ValueError as exc:
        return _fail(str(exc))
    added = []
    item_names = dict(Item.objects.filter(item_id__in=[*outputs, HUNT_TOOL_ITEM_ID]).values_list("item_id", "item_name"))
    for item_id, amount in outputs.items():
        expiry = None if item_id in BYPRODUCT_ITEM_IDS else expires
        slot = add_batch(inventory, item_id, amount, produced, expiry, request_id)
        added.append({"item_id": item_id, "item_name": item_names.get(item_id, str(item_id)),
                      "amount": amount, "instance_id": str(slot.instance_id),
                      "produced_world_minute": produced, "expires_world_minute": expiry})
    inventory.asset_version += 1
    inventory.save(update_fields=["asset_version", "updated_at"])
    return _ok(asset_versions={str(npc_id): inventory.asset_version}, effects={
        "items_added": added, "items_consumed": consumed,
        "tool_used": {"item_id": HUNT_TOOL_ITEM_ID, "item_name": item_names.get(HUNT_TOOL_ITEM_ID, "猎具"),
                      "instance_id": str(tool.instance_id), "uses": 1, "remaining_uses": tool.remaining_uses},
    }, data={"production": {"rule_version": RULE_VERSION, "hunt_ordinal": state.get("hunt_ordinal"),
        "extra_energy": state.get("extra_energy_fp", 0) / 10, "outputs": added, "inputs": consumed}})


def production_info(npc, minute):
    from .phase3_asset_service import get_available_slot_amount
    count = NpcCommandState.objects.filter(npc=npc, kind="HUNT",
        started_world_minute__gte=minute // 1440 * 1440, started_world_minute__lt=(minute // 1440 + 1) * 1440).count()
    config = VillageSimulationConfig.objects.filter(config_key="default").first()
    slots = NpcGlobalInventorySlot.objects.filter(bag__npc=npc, item_id__in=PREY_ITEM_IDS, item_amount__gt=0)
    inventory = NpcGlobalInventory.objects.get(npc=npc)
    from .models import NpcRuntimeState
    from .runtime_services import CommandService
    runtime = NpcRuntimeState.objects.get(npc=npc)
    reasons = []
    if runtime.active_command_id is not None:
        reasons.append("NPC_BUSY")
    reason = CommandService.unavailable_reason("PROCESS_GAME", runtime)
    if reason:
        reasons.append(reason)
    if runtime.action_status == "AWAY" or (runtime.away_until_world_minute is not None and runtime.away_until_world_minute > minute):
        reasons.append("NPC_AWAY")
    if not any(get_available_slot_amount(t, minute) > 0 for t in inventory.slots.filter(item_id=HUNT_TOOL_ITEM_ID, usage__remaining__gt=0)):
        reasons.append("TOOL_UNAVAILABLE")
    if VillageActionDurationConfig.configured_duration("PROCESS_GAME") is None:
        reasons.append("INVALID_ACTION_DURATION")
    prey_rows = []
    for slot in slots:
        blocked = list(reasons)
        if slot.freshness_unknown or slot.produced_world_minute is None or slot.expires_world_minute is None:
            blocked.append("PREY_FRESHNESS_UNKNOWN")
        if slot.expires_world_minute is not None and slot.expires_world_minute <= minute:
            blocked.append("PREY_EXPIRED")
        if get_available_slot_amount(slot, minute) < 1:
            blocked.append("PREY_UNAVAILABLE_OR_RESERVED")
        prey_rows.append({"instance_id": str(slot.instance_id), "item_id": slot.item_id,
            "available_quantity": get_available_slot_amount(slot, minute), "expires_world_minute": slot.expires_world_minute,
            "can_process": not blocked, "blocked_reasons": blocked})
    return {
        "hunt_code": HUNT_ANIMALS_CODE, "hunts_started_today": count,
        "next_hunt_extra_energy": config.jack_repeat_hunt_energy if count and config else 0,
        "durations": {a: VillageActionDurationConfig.configured_duration(a) for a in sorted(JACK_ACTIONS)},
        "processing_location": "BUTCHER_YARD",
        "recipes": [{"prey_item_id": i, **r} for i, r in PREY_RECIPES.items()],
        "prey": prey_rows,
    }
