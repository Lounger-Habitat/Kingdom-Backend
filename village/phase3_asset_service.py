"""Phase 3 unified asset service -- single entry point for all economy mutations.

Every Phase 3 action that changes inventory, money, tool durability, farm
plots, orders, or public fund MUST go through ``commit_economy_action``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict, List, Optional, Tuple

from django.db import IntegrityError, models, transaction

from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot

from .models import (
    EconomicLedgerEntry,
    NpcCommandState,
    NpcNeedsDaySummary,
    NpcRuntimeState,
    VillageActionReceipt,
    VillageFarmPlot,
    VillageSimulationConfig,
    VillageWorldEvent,
)
from .phase3_contract import (
    ACTOR_WHITELIST,
    LOCATION_REQUIREMENTS,
    PARAM_WHITELIST,
    VILLAGE_ECONOMY_ACTION_V1,
    VILLAGE_ECONOMY_RESULT_V1,
    ActionName,
    ResultCode,
)
from .phase4_visibility import enqueue_event_deliveries


# ── Response builders ────────────────────────────────────────────────────────


def _ok(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "schema_version": VILLAGE_ECONOMY_RESULT_V1,
        "status": "SUCCEEDED",
        "result_code": ResultCode.OK,
        "replayed": False,
        "world_minute": 0,
        "asset_versions": {},
        "effects": {},
        "data": {},
        "event": {},
    }
    base.update(overrides)
    return base


def _fail(result_code: str, **overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "schema_version": VILLAGE_ECONOMY_RESULT_V1,
        "status": "FAILED",
        "result_code": result_code,
        "replayed": False,
        "world_minute": 0,
        "asset_versions": {},
        "effects": {},
        "data": {},
        "event": {},
    }
    base.update(overrides)
    return base


def _payload_hash(payload: Dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _feature_enabled() -> bool:
    return VillageSimulationConfig.objects.filter(
        config_key="default", village_economy_enabled=True
    ).exists()


# Phase 3 direct-trade commands are additionally gated by the dedicated
# ``village_direct_trade_enabled`` flag so the legacy order path can be
# retired independently of the rest of the economy loop.
_DIRECT_TRADE_COMMANDS = frozenset({
    "PROPOSE_TRADE",
    "RESPOND_TRADE",
    "CANCEL_TRADE_OFFER",
    "LIST_STORE_ITEM",
    "UPDATE_STORE_PRICE",
    "DELIST_STORE_ITEM",
    "BUY_FROM_STORE",
    "BUY_FROM_MARKETPLACE",
    "SELL_ON_MARKETPLACE",
    "DELIST_FROM_MARKETPLACE",
})
_PARTICIPANT_TRADE_EVENTS = frozenset({
    "PROPOSE_TRADE", "RESPOND_TRADE", "CANCEL_TRADE_OFFER",
})


def _direct_trade_enabled() -> bool:
    return VillageSimulationConfig.objects.filter(
        config_key="default", village_direct_trade_enabled=True
    ).exists()


def _validate_payload(payload: Dict[str, Any]) -> Optional[str]:
    if not isinstance(payload, dict):
        return ResultCode.P3_INVALID_PARAMS
    if payload.get("schema_version") != VILLAGE_ECONOMY_ACTION_V1:
        return ResultCode.P3_INVALID_PARAMS
    for key in ("command_id", "request_id", "message_id"):
        try:
            uuid.UUID(str(payload[key]))
        except (KeyError, TypeError, ValueError):
            return ResultCode.P3_INVALID_PARAMS
    npc_id = payload.get("npc_id")
    if not isinstance(npc_id, int) or isinstance(npc_id, bool):
        return ResultCode.P3_INVALID_PARAMS
    for int_field in ("completed_world_minute", "time_revision"):
        val = payload.get(int_field)
        if not isinstance(val, int) or isinstance(val, bool):
            return ResultCode.P3_INVALID_PARAMS
    if not isinstance(payload.get("params"), dict):
        return ResultCode.P3_INVALID_PARAMS
    return None


# ── Shared asset helpers ─────────────────────────────────────────────────────


def get_available_slot_amount(slot: NpcGlobalInventorySlot) -> int:
    """Return *slot* amount minus ACTIVE delivery reservations."""
    from .models import NpcTradeDelivery

    reserved = (
        NpcTradeDelivery.objects.filter(
            seller_slot_instance_id=slot.instance_id, status="ACTIVE"
        ).aggregate(total=models.Sum("reserved_amount"))["total"]
        or 0
    )
    return max(0, slot.item_amount - reserved)


def get_available_money(inventory: NpcGlobalInventory) -> int:
    """Return *inventory* money minus ACTIVE delivery reserved money."""
    from .models import NpcTradeDelivery

    reserved = (
        NpcTradeDelivery.objects.filter(
            seller_order__npc=inventory.npc, status="ACTIVE"
        ).aggregate(total=models.Sum("reserved_money"))["total"]
        or 0
    )
    return max(0, inventory.money - reserved)


def consume_fefo(
    npc_id: int,
    allowed_item_ids: tuple,
    amount: int,
) -> List[Tuple[NpcGlobalInventorySlot, int]]:
    """Consume *amount* items from NPC inventory using FEFO order.

    Order: expires NULL last, then by expires_world_minute ASC,
    produced_world_minute ASC, instance_id ASC.

    Returns list of ``(slot, consumed_amount)`` tuples.
    Raises ``ValueError("INSUFFICIENT_ITEM")`` if not enough.
    """
    inventory = NpcGlobalInventory.objects.select_for_update().get(
        npc__agent_id=str(npc_id)
    )
    slots = list(
        NpcGlobalInventorySlot.objects.select_for_update()
        .filter(bag=inventory, item_id__in=allowed_item_ids, item_amount__gt=0)
        .order_by(
            models.F("expires_world_minute").asc(nulls_last=True),
            models.F("produced_world_minute").asc(nulls_last=True),
            "instance_id",
        )
    )

    consumed: List[Tuple[NpcGlobalInventorySlot, int]] = []
    remaining = amount
    for slot in slots:
        if remaining <= 0:
            break
        take = min(slot.item_amount, remaining)
        slot.item_amount -= take
        slot.save(update_fields=["item_amount"])
        consumed.append((slot, take))
        remaining -= take

    if remaining > 0:
        raise ValueError("INSUFFICIENT_ITEM")
    return consumed


def add_batch(
    inventory: NpcGlobalInventory,
    item_id: int,
    amount: int,
    produced_world_minute: int,
    expires_world_minute: Optional[int],
    source_request_id: uuid.UUID,
) -> NpcGlobalInventorySlot:
    """Add a new inventory batch. Never merges with existing slots."""
    max_idx = (
        NpcGlobalInventorySlot.objects.filter(bag=inventory)
        .order_by("-slot_index")
        .values_list("slot_index", flat=True)
        .first()
    )
    next_index = (max_idx + 1) if max_idx is not None else 0
    return NpcGlobalInventorySlot.objects.create(
        bag=inventory,
        slot_index=next_index,
        item_id=item_id,
        item_amount=amount,
        produced_world_minute=produced_world_minute,
        expires_world_minute=expires_world_minute,
        source_request_id=source_request_id,
    )


def consume_tool_use(npc_id: int, tool_item_id: int) -> NpcGlobalInventorySlot:
    """Deduct 1 use from the earliest available tool batch.

    Raises ``ValueError("TOOL_BROKEN")`` if no tool with remaining uses.
    """
    inventory = NpcGlobalInventory.objects.select_for_update().get(
        npc__agent_id=str(npc_id)
    )
    slot = (
        NpcGlobalInventorySlot.objects.select_for_update()
        .filter(
            bag=inventory,
            item_id=tool_item_id,
            item_amount__gt=0,
        )
        .exclude(remaining_uses=0)
        .order_by("slot_index")
        .first()
    )
    if slot is None:
        raise ValueError("TOOL_BROKEN")

    if slot.remaining_uses is not None:
        if slot.remaining_uses <= 0:
            raise ValueError("TOOL_BROKEN")
        slot.remaining_uses -= 1
        slot.save(update_fields=["remaining_uses"])
    return slot


def deterministic_rng(request_id: uuid.UUID, action_name: str, day_index: int):
    """Return a deterministic ``random.Random`` seeded from request context.

    Same (request_id, action, day) always produces the same sequence, so
    retries yield identical results.
    """
    import random

    seed_str = f"{request_id}:{action_name}:{day_index}"
    seed_int = int(hashlib.sha256(seed_str.encode()).hexdigest(), 16) % (2**31)
    return random.Random(seed_int)


# ── Dispatch table (lazy-loaded to avoid circular imports) ────────────────────

_ACTION_HANDLERS: Dict[str, Any] = {}


def _ensure_handlers() -> None:
    """Lazy-load all economy action handlers on first use."""
    if _ACTION_HANDLERS:
        return
    from .phase3_production import _do_plant, _do_harvest, _do_hunt, _do_cook
    from .phase3_trade import _do_create_order, _do_cancel_order, _do_start_delivery
    from .phase3_public_economy import (
        handle_import_goods,
        handle_export_goods,
        handle_rate_dish,
        handle_publish_price_index,
        handle_audit_public_fund,
        handle_eat,
        handle_community_meal,
    )
    from .phase3_direct_trade import (
        _do_buy_from_store,
        _do_cancel_trade_offer,
        _do_delist_store_item,
        _do_list_store_item,
        _do_propose_trade,
        _do_respond_trade,
        _do_update_store_price,
    )
    from .phase3_marketplace import (
        _do_buy_from_marketplace,
        _do_delist_from_marketplace,
        _do_sell_on_marketplace,
    )
    _ACTION_HANDLERS.update({
        "PLANT": _do_plant,
        "HARVEST": _do_harvest,
        "HUNT": _do_hunt,
        "COOK": _do_cook,
        "CREATE_ORDER": _do_create_order,
        "CANCEL_ORDER": _do_cancel_order,
        "START_DELIVERY": _do_start_delivery,
        "IMPORT_GOODS": handle_import_goods,
        "EXPORT_GOODS": handle_export_goods,
        "RATE_DISH": handle_rate_dish,
        "PUBLISH_PRICE_INDEX": handle_publish_price_index,
        "AUDIT_PUBLIC_FUND": handle_audit_public_fund,
        "EAT": handle_eat,
        "COMMUNITY_MEAL": handle_community_meal,
        "PROPOSE_TRADE": _do_propose_trade,
        "RESPOND_TRADE": _do_respond_trade,
        "CANCEL_TRADE_OFFER": _do_cancel_trade_offer,
        "LIST_STORE_ITEM": _do_list_store_item,
        "UPDATE_STORE_PRICE": _do_update_store_price,
        "DELIST_STORE_ITEM": _do_delist_store_item,
        "BUY_FROM_STORE": _do_buy_from_store,
        "BUY_FROM_MARKETPLACE": _do_buy_from_marketplace,
        "SELL_ON_MARKETPLACE": _do_sell_on_marketplace,
        "DELIST_FROM_MARKETPLACE": _do_delist_from_marketplace,
    })


# ── Main entry point ─────────────────────────────────────────────────────────


def commit_economy_action(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The single entry point for all Phase 3 economy action mutations.

    Parameters
    ----------
    payload : dict
        Must match ``village.economy-action.v1`` schema.

    Returns
    -------
    dict
        ``village.economy-result.v1`` response.
    """
    # 1. Feature flag
    if not _feature_enabled():
        return _fail(ResultCode.P3_FEATURE_DISABLED)

    # 2. Validate payload
    validation = _validate_payload(payload)
    if validation:
        return _fail(validation)

    # 3. Check idempotency
    request_id = uuid.UUID(str(payload["request_id"]))
    payload_hash = _payload_hash(payload)
    existing = VillageActionReceipt.objects.filter(request_id=request_id).first()
    if existing:
        stored_hash = (existing.response_data or {}).get("request_payload_sha256")
        if stored_hash == payload_hash:
            response_data = dict(existing.response_data or {})
            response_data.pop("request_payload_sha256", None)
            response_data["replayed"] = True
            return response_data
        return _fail(ResultCode.P3_REQUEST_CONFLICT, status="CONFLICT")

    # 4. Validate actor/location/params against contract
    command_name = str(payload.get("command_name", "")).upper()
    npc_id = int(payload["npc_id"])
    location = str(payload.get("current_location_code", ""))
    params = payload["params"]

    # Direct-trade commands are gated by their own feature flag (design 12).
    if command_name in _DIRECT_TRADE_COMMANDS and not _direct_trade_enabled():
        return _fail(ResultCode.P3_FEATURE_DISABLED)

    try:
        action_enum = ActionName(command_name)
    except ValueError:
        return _fail(ResultCode.P3_INVALID_PARAMS)

    allowed_actors = ACTOR_WHITELIST.get(action_enum, set())
    if allowed_actors and str(npc_id) not in allowed_actors:
        return _fail(ResultCode.P3_FORBIDDEN_ACTOR)

    required_location = LOCATION_REQUIREMENTS.get(action_enum)
    if required_location and location != required_location:
        return _fail(ResultCode.P3_WRONG_LOCATION)

    allowed_params = PARAM_WHITELIST.get(action_enum, frozenset())
    # 线缆兼容隧道键：部署的 Hub 只转发 action id/name/params，命令元数据经 params
    # 隧道传输（Unity EnvironmentWS.TryRestoreVillageCommandV1FromParams 恢复后原样
    # 透传给 Backend）。它不是业务参数，白名单校验必须忽略。
    unknown = set(params.keys()) - allowed_params - {"__village_command_v1"}
    if unknown:
        return _fail(ResultCode.P3_INVALID_PARAMS)

    _ensure_handlers()
    handler = _ACTION_HANDLERS.get(command_name)
    if handler is None:
        return _fail(ResultCode.P3_INVALID_PARAMS)

    # 5. Transaction with atomic lock + dispatch + persistence
    try:
        with transaction.atomic():
            # Lock time
            time_state = GlobalGameTimeState.objects.select_for_update().get(pk=1)
            world_minute = time_state.world_minute

            # Re-check idempotency under lock
            existing = VillageActionReceipt.objects.filter(request_id=request_id).first()
            if existing:
                stored_hash = (existing.response_data or {}).get("request_payload_sha256")
                if stored_hash == payload_hash:
                    response_data = dict(existing.response_data or {})
                    response_data.pop("request_payload_sha256", None)
                    response_data["replayed"] = True
                    return response_data
                return _fail(ResultCode.P3_REQUEST_CONFLICT, status="CONFLICT")

            command_id = uuid.UUID(str(payload["command_id"]))
            command = NpcCommandState.objects.select_for_update().filter(command_id=command_id).first()
            runtime = None
            if command is not None:
                if (
                    command.request_id != request_id
                    or str(command.npc.agent_id) != str(npc_id)
                    or command.kind != command_name
                ):
                    return _fail(ResultCode.P3_REQUEST_CONFLICT, status="CONFLICT")
                runtime = NpcRuntimeState.objects.select_for_update().filter(npc=command.npc).first()
                if runtime is None:
                    return _fail(ResultCode.P3_INVALID_PARAMS)
                if command.status != "RUNNING" or (
                    runtime.active_command_id is not None
                    and runtime.active_command_id != command.command_id
                ):
                    return _fail(ResultCode.P3_REQUEST_CONFLICT, status="CONFLICT")
            if runtime is None and command_name in {"EAT", "COMMUNITY_MEAL"}:
                runtime = NpcRuntimeState.objects.select_for_update().filter(
                    npc__agent_id=str(npc_id)
                ).first()
                if runtime is None:
                    return _fail(ResultCode.P3_INVALID_PARAMS)

            # Dispatch to production handler
            result = handler(npc_id, params, world_minute, request_id)

            if result.get("result_code") != ResultCode.OK:
                return result

            if command_name in {"EAT", "COMMUNITY_MEAL"}:
                runtime.hunger_fp = max(0, runtime.hunger_fp - 450)
                runtime.energy_fp = min(1000, runtime.energy_fp + 50)
                summary, _ = NpcNeedsDaySummary.objects.select_for_update().get_or_create(
                    npc=runtime.npc, day_index=max(0, world_minute - 1) // 1440,
                )
                summary.successful_meals += 1
                summary.save(update_fields=["successful_meals", "updated_at"])

            if runtime is not None and command is not None:
                from .phase2_services import settle_action_needs_locked
                started = command.started_world_minute
                ended = (
                    min(world_minute, started + command.duration_minutes)
                    if started is not None and command.duration_minutes else None
                )
                error = settle_action_needs_locked(
                    runtime, command_name, started, ended, base_until=world_minute
                )
                if error:
                    return _fail(error)

            # 6. Write persistence records
            result["request_id"] = str(request_id)
            result["world_minute"] = world_minute

            # Look up NpcDefinition object (needed for FK references)
            npc_obj = NpcDefinition.objects.get(agent_id=str(npc_id))

            # VillageWorldEvent
            event_visibility = result.get(
                "event_visibility",
                "PARTICIPANTS" if command_name in _PARTICIPANT_TRADE_EVENTS else "PUBLIC",
            )
            event = VillageWorldEvent.objects.create(
                dedupe_key=f"economy:{request_id}",
                event_type=f"ECONOMY_{command_name}",
                world_minute=world_minute,
                time_revision=int(payload.get("time_revision", world_minute)),
                actor_npc=npc_obj,
                participant_npc_ids=result.get("participant_npc_ids") or [str(npc_id)],
                witness_npc_ids=result.get("witness_npc_ids") or [],
                player_ids=result.get("player_ids") or [],
                location_code=location,
                command_id=uuid.UUID(str(payload["command_id"])),
                request_id=request_id,
                effects=result.get("effects", {}),
                visibility=event_visibility,
                public_data={"result_code": result.get("event_result_code", result["result_code"])},
            )
            enqueue_event_deliveries(event)
            if command_name == "RESPOND_TRADE" and result.get("data", {}).get("status") == "ACCEPTED":
                commitment_id = result.get("data", {}).get("commitment_id")
                if commitment_id:
                    from .phase4_social import complete_delivery_commitment
                    complete_delivery_commitment(
                        commitment_id,
                        event,
                        world_minute=world_minute,
                        emergency=bool(result.get("data", {}).get("emergency_supply")),
                    )
            result["event"] = {
                "event_id": str(event.event_id),
                "event_sequence": event.event_sequence,
            }

            # Persist the exact successful response after every response field has
            # been populated.  Replays must differ only by ``replayed=true``;
            # copying before the event was attached made the replay lose event_id
            # and event_sequence.
            response_data = dict(result)
            response_data["request_payload_sha256"] = payload_hash

            # NpcCommandState (terminal)
            if command is None:
                command = NpcCommandState.objects.create(
                    command_id=command_id,
                    message_id=uuid.UUID(str(payload["message_id"])),
                    request_id=request_id,
                    npc=npc_obj,
                    schema_version=VILLAGE_ECONOMY_ACTION_V1,
                    sender_id=str(npc_id),
                    recipient_id="economy-service",
                    kind=command_name,
                    location_code=location,
                    sanitized_parameters=params,
                    payload_sha256=payload_hash,
                    queue_mode="ECONOMY",
                    status="SUCCEEDED",
                    completed_world_minute=world_minute,
                    received_world_minute=world_minute,
                    result_code=ResultCode.OK,
                )
            else:
                command.status = "SUCCEEDED"
                command.result_code = ResultCode.OK
                command.location_code = location
                command.completed_world_minute = world_minute
                command.save(update_fields=[
                    "status", "result_code", "location_code",
                    "completed_world_minute", "updated_at",
                ])

            # VillageActionReceipt
            receipt = VillageActionReceipt.objects.create(
                request_id=request_id,
                command=command,
                npc=npc_obj,
                action_kind=f"ECONOMY_{command_name}",
                success=True,
                result_code=ResultCode.OK,
                response_data=response_data,
                world_minute=world_minute,
                time_revision=int(payload.get("time_revision", world_minute)),
                state_version=runtime.state_version + 1 if runtime is not None else None,
                world_event=event,
            )
            if runtime is not None:
                runtime.state_version += 1
                runtime.current_action = ""
                runtime.action_status = "IDLE"
                runtime.action_started_world_minute = None
                runtime.action_ends_world_minute = None
                runtime.active_command_id = None
                runtime.last_simulated_world_minute = world_minute
                runtime.time_revision = time_state.time_revision
                runtime.save()
            from .runtime_services import OutboxService
            OutboxService.ensure_outcome(receipt)

            # EconomicLedgerEntry
            EconomicLedgerEntry.objects.create(
                request_id=request_id,
                entry_type=command_name,
                actor_npc=npc_obj,
                changes=result.get("effects", {}),
                external_delta=result.get("external_delta", 0),
                world_minute=world_minute,
                event=event,
            )

            return result

    except IntegrityError:
        # Receipt PK conflict = concurrent replay
        existing = VillageActionReceipt.objects.filter(request_id=request_id).first()
        if existing:
            response_data = dict(existing.response_data or {})
            response_data.pop("request_payload_sha256", None)
            response_data["replayed"] = True
            return response_data
        return _fail(ResultCode.P3_INVALID_PARAMS)
