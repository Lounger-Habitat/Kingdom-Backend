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
    PARAM_WHITELIST,
    VILLAGE_ECONOMY_ACTION_V1,
    VILLAGE_ECONOMY_RESULT_V1,
    ActionName,
    ResultCode,
)
from .phase4_visibility import resolve_witnesses
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


def _economy_event_summary(
    command_name: str,
    npc: NpcDefinition,
    data: Dict[str, Any],
) -> str:
    """Human-readable hub/player summary for a public economy world event.

    Agents receive this text in the event envelope. An informative summary
    lets them react from the message itself instead of re-querying the world
    for every broadcast event.
    """
    from .command_catalog import COMMAND_CATALOG
    from items.models import Item

    label = (COMMAND_CATALOG.get(command_name) or {}).get("label", command_name)
    try:
        name = npc.village_profile.display_name or npc.name
    except Exception:  # pragma: no cover - legacy/test NPCs without a profile
        name = npc.name or str(npc.agent_id)
    if command_name == "REQUEST_DISH_REVIEW":
        return f"Lucy送来具体菜品待Mark品鉴，review_id={data.get('review_id')}；请INSPECT_RATEABLE_DISHES后由LLM给出四项1–10分及评语，通过RATE_DISH回传。"
    if data.get("sale_mode") == "IMMEDIATE_STORE":
        sold = "、".join(f"{row.get('item_name') or row.get('item_id')}×{row.get('amount')}"
                        for row in data.get("items") or [data])
        return (f"{name}向杂货铺售出{sold}，"
                f"商店按物品价值支付{data.get('gold_amount')}金币，交易已完成。")[:256]
    details = []
    imports = data.get("imports")
    if not isinstance(imports, list) and isinstance(data.get("production"), dict):
        imports = data["production"].get("outputs")
    if not isinstance(imports, list):
        imports = [data] if data.get("item_id") is not None else []
    for row in imports:
        if not isinstance(row, dict):
            continue
        item_id = row.get("item_id")
        if not isinstance(item_id, int) or isinstance(item_id, bool):
            continue
        item_name = (
            Item.objects.filter(item_id=item_id, is_deleted=False)
            .values_list("item_name", flat=True)
            .first()
        )
        if item_name:
            chunk = item_name
            amount = row.get("amount")
            if isinstance(amount, int) and not isinstance(amount, bool):
                chunk += f"×{amount}"
            unit_price = row.get("unit_price")
            if isinstance(unit_price, int) and not isinstance(unit_price, bool):
                chunk += f"，单价{unit_price}金"
            details.append(chunk)
    summary = f"{name}{label}"
    if details:
        summary += "：" + "；".join(details)
    return summary[:256]


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


def get_available_slot_amount(slot: NpcGlobalInventorySlot, world_minute=None, exclude_command_id=None) -> int:
    """Return *slot* amount minus ACTIVE delivery reservations."""
    from .models import NpcTradeDelivery

    if getattr(slot, "freshness_unknown", False) or (
        world_minute is not None and slot.expires_world_minute is not None
        and slot.expires_world_minute <= world_minute
    ):
        return 0
    reserved = (
        NpcTradeDelivery.objects.filter(
            seller_slot_instance_id=slot.instance_id, status="ACTIVE"
        ).aggregate(total=models.Sum("reserved_amount"))["total"]
        or 0
    )
    from .jack_production import reserved_amount
    from .lucy_cooking import reservation
    return max(0, slot.item_amount - reserved - reserved_amount(slot, exclude_command_id) - reservation(slot, world_minute))


def get_available_money(inventory: NpcGlobalInventory) -> int:
    """Return *inventory* money minus ACTIVE delivery reserved money."""
    from .models import NpcTradeDelivery

    reserved = (
        NpcTradeDelivery.objects.filter(
            buyer_order__npc=inventory.npc, status="ACTIVE"
        ).aggregate(total=models.Sum("reserved_money"))["total"]
        or 0
    )
    return max(0, inventory.money - reserved)


def consume_fefo(
    npc_id: int,
    allowed_item_ids: tuple,
    amount: int,
    world_minute: int,
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

    if amount <= 0 or sum(get_available_slot_amount(slot, world_minute) for slot in slots) < amount:
        raise ValueError("INSUFFICIENT_ITEM")
    consumed: List[Tuple[NpcGlobalInventorySlot, int]] = []
    remaining = amount
    for slot in slots:
        if remaining <= 0:
            break
        take = min(get_available_slot_amount(slot, world_minute), remaining)
        if not take:
            continue
        slot.item_amount -= take
        slot.save(update_fields=["item_amount"])
        consumed.append((slot, take))
        remaining -= take

    if remaining > 0:
        raise ValueError("INSUFFICIENT_ITEM")
    return consumed


BATCH_ITEM_ATTRIBUTES = ("rated", "rating_price", "overall_score", "ingredient", "item_bag_name",
                         "cook_time", "dish_quality", "hide_in_shop", "review_comment", "freshness_unknown", "dish_metadata")


def add_batch(
    inventory: NpcGlobalInventory,
    item_id: int,
    amount: int,
    produced_world_minute: int,
    expires_world_minute: Optional[int],
    source_request_id: uuid.UUID,
    remaining_uses: Optional[int] = None,
    item_attributes: Optional[Dict[str, Any]] = None,
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
        remaining_uses=remaining_uses,
        produced_world_minute=produced_world_minute,
        expires_world_minute=expires_world_minute,
        source_request_id=source_request_id,
        **{key: value for key, value in (item_attributes or {}).items() if key in BATCH_ITEM_ATTRIBUTES},
    )


def get_usable_tool_slot(inventory, tool_item_id, world_minute, *, exclude_command_id=None, slot_instance_id=None):
    """Select a usable physical tool without changing its quantity or wear."""
    slots = (
        NpcGlobalInventorySlot.objects.select_for_update()
        .filter(
            bag=inventory,
            item_id=tool_item_id,
            item_amount__gt=0,
        )
        .filter(usage__remaining__gt=0)
        .order_by("usage__remaining", "slot_index")
    )
    if slot_instance_id is not None:
        slots = slots.filter(instance_id=slot_instance_id)
    slot = next((row for row in slots if get_available_slot_amount(row, world_minute, exclude_command_id) > 0), None)
    if slot is None:
        if inventory.slots.filter(item_id=tool_item_id, item_amount__gt=0, usage__remaining__isnull=True).exists():
            raise ValueError("TOOL_DURABILITY_UNKNOWN")
        if inventory.slots.filter(item_id=tool_item_id, item_amount__gt=0, usage__remaining__lt=0).exists():
            raise ValueError("TOOL_DURABILITY_INVALID")
        raise ValueError("TOOL_BROKEN")
    return slot


def consume_tool_use(npc_id: int, tool_item_id: int, world_minute=None, *, exclude_command_id=None, slot_instance_id=None) -> NpcGlobalInventorySlot:
    """Wear one physical tool, splitting a stack before the first use."""
    inventory = NpcGlobalInventory.objects.select_for_update().get(npc__agent_id=str(npc_id))
    slot = get_usable_tool_slot(inventory, tool_item_id, world_minute,
        exclude_command_id=exclude_command_id, slot_instance_id=slot_instance_id)

    if slot.item_amount > 1:
        slot.item_amount -= 1
        slot.save(update_fields=["item_amount"])
        slot = add_batch(
            inventory, tool_item_id, 1, slot.produced_world_minute,
            slot.expires_world_minute, slot.source_request_id,
            remaining_uses=slot.remaining_uses,
            item_attributes={key: getattr(slot, key) for key in BATCH_ITEM_ATTRIBUTES},
        )
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
    from .jack_production import finish_work
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
    from .caravan import handle_contact, handle_withdraw, handle_limits, handle_deposit, handle_withdraw_item
    from .lucy_cooking import request_review, cancel_review
    _ACTION_HANDLERS.update({
        "REQUEST_DISH_REVIEW": request_review,
        "CANCEL_DISH_REVIEW": cancel_review,
        "CONTACT_CARAVAN": handle_contact,
        "WITHDRAW_STORE_FUNDS": handle_withdraw,
        "DEPOSIT_STORE_FUNDS": handle_deposit,
        "WITHDRAW_STORE_ITEM": handle_withdraw_item,
        "SET_STORE_LIMITS": handle_limits,
        "PLANT": _do_plant,
        "HARVEST": _do_harvest,
        "HUNT": _do_hunt,
        "PROCESS_GAME": finish_work,
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

    # 4. Validate actor/params against contract. Unity owns NPC location
    # validation; current_location_code is retained below as reported state.
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
            if runtime is None and command_name in {
                "EAT", "COMMUNITY_MEAL", "PLANT", "HARVEST", "HUNT", "PROCESS_GAME", "COOK",
            }:
                runtime = NpcRuntimeState.objects.select_for_update().filter(
                    npc__agent_id=str(npc_id)
                ).first()
                if runtime is None:
                    return _fail(ResultCode.P3_INVALID_PARAMS)

            if command is not None and command_name in {"COOK", "REQUEST_DISH_REVIEW", "CANCEL_DISH_REVIEW", "RATE_DISH"}:
                if any(params.get(key) != command.sanitized_parameters.get(key) for key in PARAM_WHITELIST[action_enum]):
                    return _fail(ResultCode.P3_REQUEST_CONFLICT, status="CONFLICT")
                if command.started_world_minute is None or world_minute < command.started_world_minute + (command.duration_minutes or 0):
                    return _fail("LUCY_ACTION_NOT_FINISHED")

            from .jack_production import JACK_ACTIONS, is_jack_work, settle_work_needs
            jack_work = command is not None and is_jack_work(command)
            if command_name in JACK_ACTIONS:
                if command is None:
                    return _fail("PRODUCTION_COMMAND_REQUIRED")
                accepted = command.sanitized_parameters
                if any(params.get(key) != accepted.get(key) for key in PARAM_WHITELIST[action_enum]):
                    return _fail(ResultCode.P3_REQUEST_CONFLICT, status="CONFLICT")
                if command.started_world_minute is None or world_minute < command.started_world_minute + (command.duration_minutes or 0):
                    return _fail("PRODUCTION_NOT_FINISHED")
            needs_error = settle_work_needs(runtime, command, world_minute) if jack_work else None

            # Dispatch to production handler
            settlement_minute = world_minute
            if command_name in {"IMPORT_GOODS", "EXPORT_GOODS"}:
                from .caravan import sync_visit, trade_window
                sync_visit(world_minute)
                if command is not None:
                    started = command.started_world_minute
                    if started is None or world_minute < started + (command.duration_minutes or 0):
                        return _fail("CARAVAN_ACTION_NOT_FINISHED")
                    settlement_minute = started + (command.duration_minutes or 0)
                    error = trade_window(started, command.duration_minutes or 0)
                else:
                    error = trade_window(world_minute)
                if error:
                    return _fail(error)
            # A handler may return a business error after earlier mutations.
            # Roll its entire write set back before recording the failure.
            with transaction.atomic():
                result = _fail(needs_error) if needs_error else handler(npc_id, params, settlement_minute, request_id)
                if result.get("result_code") != ResultCode.OK:
                    transaction.set_rollback(True)
            from .agent_feedback import add_item_names
            add_item_names(result)

            if result.get("result_code") != ResultCode.OK:
                # Expiration is a terminal offer status, not an asset transfer.
                if command_name == "RESPOND_TRADE" and result["result_code"] == ResultCode.TRADE_OFFER_EXPIRED:
                    from .models import VillageTradeOffer
                    VillageTradeOffer.objects.filter(offer_id=params.get("offer_id"), status="PENDING",
                        expires_world_minute__lte=world_minute).update(status="EXPIRED",
                        resolved_request_id=request_id, resolved_world_minute=world_minute)
                result["world_minute"] = world_minute
                result["request_id"] = str(request_id)
                failure_data = dict(result, request_payload_sha256=payload_hash)
                npc_obj = NpcDefinition.objects.get(agent_id=str(npc_id))
                if command is None:
                    command = NpcCommandState.objects.create(
                        command_id=command_id, message_id=uuid.UUID(str(payload["message_id"])),
                        request_id=request_id, npc=npc_obj, schema_version=VILLAGE_ECONOMY_ACTION_V1,
                        sender_id=str(npc_id), recipient_id="economy-service", kind=command_name,
                        location_code=location, sanitized_parameters=params, payload_sha256=payload_hash,
                        queue_mode="ECONOMY", status="RECEIVED", received_world_minute=world_minute,
                    )
                from .runtime_services import ActionService, _outcome_message
                ActionService.complete_action(
                    command.command_id, request_id, False, result["result_code"],
                    message=(_outcome_message(command_name, result["result_code"], False,
                             world_minute, result.get("data"))
                             if result.get("data", {}).get("sale_mode") == "IMMEDIATE_STORE" else ""),
                    data=failure_data, allow_prestart_failure=True,
                )
                return result

            eat_needs_delta = None
            affected_needs = set(command.production_state.get("needs_changed_fields", [])) if jack_work else set()
            if command_name in {"EAT", "COMMUNITY_MEAL"}:
                hunger_before = runtime.hunger_fp
                energy_before = runtime.energy_fp
                recovery = result.get("needs_delta") or {"hunger_fp": -450, "energy_fp": 50}
                runtime.hunger_fp = max(0, runtime.hunger_fp + recovery["hunger_fp"])
                runtime.energy_fp = min(1000, runtime.energy_fp + recovery["energy_fp"])
                eat_needs_delta = {
                    "hunger": (runtime.hunger_fp - hunger_before) / 10,
                    "energy": (runtime.energy_fp - energy_before) / 10,
                }
                affected_needs.update(name for name, delta in eat_needs_delta.items() if delta)
                summary, _ = NpcNeedsDaySummary.objects.select_for_update().get_or_create(
                    npc=runtime.npc, day_index=max(0, world_minute - 1) // 1440,
                )
                summary.successful_meals += 1
                summary.save(update_fields=["successful_meals", "updated_at"])

            if runtime is not None and command is not None and not jack_work:
                from .phase2_services import settle_action_needs_locked
                started = command.started_world_minute
                ended = (
                    min(world_minute, started + command.duration_minutes)
                    if started is not None and command.duration_minutes else None
                )
                error = settle_action_needs_locked(
                    runtime, command_name, started, ended, base_until=world_minute, changed_fields=affected_needs
                )
                if error:
                    transaction.set_rollback(True)
                    return _fail(error)

            # Production actions also settle an authoritative needs effect via
            # settle_action_needs_locked above.  Include the post-settlement
            # public snapshot so the outbox formatter can put it in message.
            if runtime is not None and command_name in {
                "EAT", "COMMUNITY_MEAL", "PLANT", "HARVEST", "HUNT", "PROCESS_GAME", "COOK",
            }:
                from .phase2_services import public_needs
                result_data = result.setdefault("data", {})
                result_data["public_needs"] = public_needs(runtime)
                result_data["needs_after"] = {
                    name: result_data["public_needs"][name] for name in sorted(affected_needs)
                }
                if eat_needs_delta is not None:
                    result_data["needs_delta"] = eat_needs_delta

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
            event_summary = _economy_event_summary(
                command_name, npc_obj, result.get("data") or {}
            )
            event_participants = result.get("participant_npc_ids") or [str(npc_id)]
            event_private_data = result.get("data", {}) if command_name in {
                "REQUEST_DISH_REVIEW", "CANCEL_DISH_REVIEW", "RATE_DISH"
            } else {}
            if command_name == "BUY_FROM_STORE":
                # The buyer already receives OUTCOME. Notify only the operator,
                # in the same transaction/outbox as the sale, including subsidies.
                from .models import VillageStoreListing, VillageStoreInventorySlot
                from .lucy_restaurant import store_available
                listing = VillageStoreListing.objects.select_related("store__operator_npc").get(
                    pk=result["data"]["listing_id"])
                operator_id = str(listing.store.operator_npc.agent_id)
                event_visibility = "PARTICIPANTS"
                event_participants = [operator_id] if operator_id != str(npc_id) else []
                event_private_data = {key: result["data"][key] for key in (
                    "listing_id", "amount", "gross_amount", "fee_amount", "store_net_amount", "subsidy_amount"
                ) if key in result["data"]}
                event_private_data.update(store_code=listing.store.store_code, item_id=listing.item_id,
                    item_name=result["data"].get("item_name"),
                    remaining_amount=sum(store_available(slot, world_minute) for slot in
                        VillageStoreInventorySlot.objects.filter(listing=listing, item_amount__gt=0)))
                event_summary = (f"{listing.store.store_code}售出物品{listing.item_id}×{event_private_data['amount']}；"
                    f"店款净入账{event_private_data['store_net_amount']}金币，"
                    f"该上架记录剩余{event_private_data['remaining_amount']}份。")
            event = VillageWorldEvent.objects.create(
                dedupe_key=f"economy:{request_id}",
                event_type=f"ECONOMY_{command_name}",
                world_minute=world_minute,
                time_revision=int(payload.get("time_revision", world_minute)),
                actor_npc=npc_obj,
                participant_npc_ids=event_participants,
                witness_npc_ids=(
                    result.get("witness_npc_ids") or resolve_witnesses(
                        location,
                        result.get("participant_npc_ids") or [str(npc_id)],
                    )
                    if command_name in {"EAT", "HUNT"}
                    else result.get("witness_npc_ids") or []
                ),
                player_ids=result.get("player_ids") or [],
                location_code=location,
                command_id=uuid.UUID(str(payload["command_id"])),
                request_id=request_id,
                effects=result.get("effects", {}),
                visibility=event_visibility,
                public_data={"result_code": result.get("event_result_code", result["result_code"]),
                    **({key: value for key, value in result.get("data", {}).items() if key in {
                        "offer_id", "status", "side", "store_code", "payment_source", "proposer_npc_id", "buyer_npc_id", "seller_npc_id",
                        "item_id", "item_name", "amount", "gold_amount", "expires_world_minute", "settlement_completed",
                    }} if command_name in {"PROPOSE_TRADE", "RESPOND_TRADE", "CANCEL_TRADE_OFFER"} else {})},
                private_data=event_private_data,
                player_summary=event_summary,
                hub_summary=event_summary,
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
