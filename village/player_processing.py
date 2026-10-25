"""Player-owned carcass escrow using Jack's real work, tool and wallet."""
import copy
import hashlib
import json
import uuid
import math

from django.db import transaction

from game_time.models import GlobalGameTimeState
from inventory.components import BATCH_COPY_FIELDS, copy_batch_attributes
from inventory.models import InventoryBag, InventorySlot, NpcGlobalInventory
from items.models import Item
from player.models import PlayerGrowthState, EquipmentDefinition
from player.equipment import stats_for
from player.progression import ensure_growth
from .models import (EconomicLedgerEntry, NpcRuntimeState, VillageActionDurationConfig,
                     VillagePlayerProcessingJob)
from .phase3_catalog import (BYPRODUCT_ITEM_IDS, HUNT_TOOL_ITEM_ID, PREY_RECIPES,
                            PREY_CHICKEN, PREY_PIG, PREY_CATTLE, PREY_DEER)

from .professional_catalog import PREY_BOAR

FEES = {PREY_CHICKEN: 2, PREY_PIG: 4, PREY_CATTLE: 6, PREY_DEER: 5, PREY_BOAR: 8}
MAX_SUBMISSION_QUANTITY = 100


class ProcessingError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def _fail(code, message="加工条件尚未满足，请刷新后查看。"):
    raise ProcessingError(code, message)


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        _fail("INVALID_PARAMS", "猎物或工单编号无效。")


def jack_processing_busy(npc_id):
    """Shared NPC command gate. npc_id is the public agent id, not a DB PK."""
    return str(npc_id) == "10012" and VillagePlayerProcessingJob.objects.filter(status="RUNNING").exists()


def _runtime_reason(rt, minute):
    from .runtime_services import CommandService
    if rt is None:
        return "JACK_UNAVAILABLE"
    if rt.action_status == "AWAY" or (rt.away_until_world_minute is not None and rt.away_until_world_minute > minute):
        return "NPC_AWAY"
    if rt.action_status == "SLEEPING":
        return "JACK_UNAVAILABLE"
    if rt.active_command_id is not None or rt.action_status not in ("IDLE", "BLOCKED"):
        return "NPC_BUSY"
    if rt.current_location_code != "BUTCHER_YARD":
        return "JACK_NOT_AT_WORK"
    return CommandService.unavailable_reason("PROCESS_GAME", rt) or ""


def _jack_reason(minute, *, exclude_job=None):
    rt = NpcRuntimeState.objects.select_related("npc").filter(npc__agent_id="10012").first()
    reason = _runtime_reason(rt, minute)
    if reason:
        return reason
    jobs = VillagePlayerProcessingJob.objects.filter(status="RUNNING")
    if exclude_job:
        jobs = jobs.exclude(pk=exclude_job)
    else:
        # An accepted NPC command owns the next work slot even before start.
        from .models import NpcCommandState
        if NpcCommandState.objects.filter(npc__agent_id="10012",
                status__in=("RECEIVED", "QUEUED", "RUNNING", "SETTLING")).exists():
            return "NPC_BUSY"
    return "NPC_BUSY" if jobs.exists() else ""


def _next_index(bag):
    return max(bag.slots.values_list("slot_index", flat=True), default=-1) + 1


def _batch(slot):
    return dict(instance_id=str(slot.instance_id), item_id=slot.item_id,
        item_name=slot.item_bag_name or Item.objects.filter(pk=slot.item_id).values_list("item_name", flat=True).first() or str(slot.item_id),
        quantity=slot.item_amount, quality=slot.quality,
        produced_world_minute=slot.produced_world_minute, expires_world_minute=None,
        freshness_unknown=False)


def _snapshot(slot):
    attrs = {key: copy.deepcopy(getattr(slot, key)) for key in (*BATCH_COPY_FIELDS, "hide_in_shop")}
    if attrs["source_request_id"] is not None:
        attrs["source_request_id"] = str(attrs["source_request_id"])
    return dict(**_batch(slot), attributes=attrs)


def _prey_reason(slot, minute, duration):
    if slot is None or slot.item_amount < 1 or slot.item_id not in FEES:
        return "PREY_UNAVAILABLE"
    if not 1 <= slot.quality <= 6:
        return "PREY_QUALITY_INVALID"
    if slot.produced_world_minute is not None and slot.produced_world_minute > minute:
        return "PREY_INVALID_BATCH"
    if slot.dish_metadata or slot.ingredient or slot.remaining_uses is not None:
        return "PREY_INVALID_BATCH"
    item = Item.objects.filter(pk=slot.item_id, is_deleted=False).first()
    return "" if item else "PROCESSING_CATALOG_UNAVAILABLE"


def _outputs(slot):
    spec = PREY_RECIPES.get(slot.item_id, {}).get("outputs", {})
    names = dict(Item.objects.filter(pk__in=spec, is_deleted=False).values_list("pk", "item_name"))
    if len(names) != len(spec):
        return []
    return [dict(item_id=item_id, item_name=names[item_id], quantity=amount, quality=slot.quality,
                 produced_world_minute=slot.produced_world_minute,
                 expires_world_minute=None,
                 freshness_unknown=False) for item_id, amount in spec.items()]


def _tool(minute, *, include_reserved=False):
    from .phase3_asset_service import get_available_slot_amount
    inv = NpcGlobalInventory.objects.filter(npc__agent_id="10012").first()
    if inv is None:
        return None, "JACK_UNAVAILABLE"
    reason = "P3_TOOL_MISSING"
    reserved = None
    tool_ids = EquipmentDefinition.objects.filter(category="processing", enabled=True).values_list("item_id", flat=True)
    for slot in inv.slots.filter(item_id__in=tool_ids, item_amount__gt=0).order_by("slot_index"):
        if slot.remaining_uses is None:
            reason = "P3_TOOL_DURABILITY_UNKNOWN"
        elif slot.remaining_uses <= 0:
            reason = "P3_TOOL_BROKEN"
        elif stats_for(slot) is None:
            reason = "TOOL_CONFIGURATION_UNAVAILABLE"
        elif get_available_slot_amount(slot, minute) > 0:
            return slot, ""
        else:
            reserved = reserved or slot
            reason = "PRODUCTION_RESOURCE_UNAVAILABLE"
    return (reserved if include_reserved else None), reason


def _preview(player, bag, slot, minute, *, quantity=1, read_only=False):
    duration = VillageActionDurationConfig.configured_duration("PROCESS_GAME")
    tool, tool_reason = _tool(minute, include_reserved=True)
    tool_stats = stats_for(tool) if tool else None
    if duration and tool_stats:
        duration = max(1, math.ceil(duration * (1-tool_stats["processing_time_reduction"])))
    reason = "PROCESSING_DURATION_UNAVAILABLE" if not duration else _prey_reason(slot, minute, duration)
    outputs = _outputs(slot) if slot is not None and slot.item_id in FEES else []
    if not outputs:
        reason = reason or "PROCESSING_CATALOG_UNAVAILABLE"
    if slot is not None and slot.item_amount < quantity:
        reason = reason or "PREY_QUANTITY_UNAVAILABLE"
    waiting_reason = _jack_reason(minute) or tool_reason
    unit_fee = FEES.get(slot.item_id, 0) if slot is not None else 0
    fee = unit_fee * quantity
    for output in outputs:
        output["quantity"] *= quantity
    if bag.money < fee:
        reason = reason or "INSUFFICIENT_MONEY"
    payload = dict(player_id=str(player.pk), prey_instance_id=str(slot.instance_id) if slot else None,
        input=_snapshot(slot) if slot else None, quantity=quantity, fee_gold=fee,
        duration_minutes=duration, outputs=outputs, tool_configuration=tool_stats)
    quote = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    if read_only:
        reason = reason or "MODE_READ_ONLY"
    return dict(available=not reason, reason=reason, quote_hash=quote,
        prey_instance_id=payload["prey_instance_id"], quantity=quantity, fee_gold=fee, unit_fee_gold=unit_fee,
        max_quantity=max(0, min(MAX_SUBMISSION_QUANTITY, slot.item_amount if slot else 0,
                                bag.money // unit_fee if unit_fee else 0)),
        duration_minutes=duration, total_duration_minutes=(duration or 0) * quantity,
        outputs=outputs, waiting_reason=waiting_reason), tool


def _entry(job, minute):
    outputs = copy.deepcopy(job.output_snapshot or [dict(item_id=int(i), quantity=n)
                                                   for i, n in job.output_spec.items()])
    names = dict(Item.objects.filter(pk__in={row["item_id"] for row in outputs}).values_list("pk", "item_name"))
    for row in outputs:
        row["item_name"] = row.get("item_name") or names.get(row["item_id"], "产物名称待同步")
        if not job.output_snapshot:
            row["quality"] = job.input_snapshot.get("quality")
    return dict(job_id=str(job.pk), status=job.status, input=copy.deepcopy(job.input_snapshot),
        outputs=outputs,
        fee_gold=job.fee_gold, duration_minutes=job.duration_minutes,
        started_world_minute=None if job.status == "QUEUED" else job.started_world_minute,
        ready_world_minute=None if job.status == "QUEUED" else job.ready_world_minute,
        completed_world_minute=job.completed_world_minute, claimed_world_minute=job.claimed_world_minute,
        failure_reason=job.failure_reason, can_cancel=job.status in ("QUEUED", "RUNNING", "FAILED") and
        (job.status in ("QUEUED", "FAILED") or minute < job.ready_world_minute), can_claim=job.status == "READY")


def processing_overview(player, bag, minute, read_only=False):
    """Never initializes inventory, completes work or mutates any world state."""
    inputs = []
    if bag.pk and bag.player_id == player.pk and bag.character_id == player.username:
        for slot in bag.slots.filter(item_id__in=FEES, item_amount__gt=0).order_by("slot_index"):
            preview, _ = _preview(player, bag, slot, minute, read_only=read_only)
            inputs.append(dict(**_batch(slot), can_start=preview["available"], reason=preview["reason"],
                               max_quantity=preview["max_quantity"],
                               fee_gold=preview["fee_gold"], duration_minutes=preview["duration_minutes"],
                               outputs=preview["outputs"]))
    reason = _jack_reason(minute)
    if not VillageActionDurationConfig.configured_duration("PROCESS_GAME"):
        reason = reason or "PROCESSING_DURATION_UNAVAILABLE"
    if not reason:
        _, reason = _tool(minute)
    return dict(revision=PlayerGrowthState.objects.filter(player=player).values_list("revision", flat=True).first() or 0,
        observed_world_minute=minute, available=bool(VillageActionDurationConfig.configured_duration("PROCESS_GAME")),
        reason="", waiting_reason=reason, inputs=inputs,
        jobs=[_entry(job, minute) for job in VillagePlayerProcessingJob.objects.filter(player=player).order_by("-submitted_at", "job_id")],
        max_submission_quantity=MAX_SUBMISSION_QUANTITY,
        fee_rules=[dict(item_id=i, fee_gold=n) for i, n in FEES.items()])


def _ledger(job, phase, minute, changes, *, rid=None):
    EconomicLedgerEntry.objects.create(request_id=rid or uuid.uuid5(job.pk, phase),
        entry_type="PLAYER_PROCESSING", actor_npc_id=NpcGlobalInventory.objects.filter(npc__agent_id="10012").values_list("npc_id", flat=True).first(),
        changes=dict(processing_job_id=str(job.pk), phase=phase, **changes), external_delta=0, world_minute=minute)


def _bump(player):
    state = ensure_growth(player)
    state.revision += 1
    state.save(update_fields=["revision"])


def _settle_job(job, minute):
    """Caller holds clock/job; failures preserve input and the full fee."""
    if job.status != "RUNNING":
        return False
    from .phase2_services import settle_action_needs_locked
    from .phase3_asset_service import consume_tool_use
    rt = NpcRuntimeState.objects.select_for_update().filter(npc__agent_id="10012").first()
    reason = _jack_reason(job.needs_until, exclude_job=job.pk)
    end = min(minute, job.ready_world_minute)
    while job.needs_until < end and not reason:
        reason = settle_action_needs_locked(rt, "PROCESS_GAME", job.needs_until, job.needs_until + 1,
                                           base_until=job.needs_until + 1)
        job.needs_until += 1
        reason = reason or _runtime_reason(rt, job.needs_until)
    if rt is not None:
        rt.save()
    job.save(update_fields=["needs_until"])
    if not reason and minute < job.ready_world_minute:
        return False
    if not reason:
        try:
            with transaction.atomic():
                held = InventorySlot.objects.select_for_update().get(pk=job.input_slot_id)
                escrow = InventoryBag.objects.select_for_update().get(pk=job.escrow_bag_id)
                if (held.bag_id != escrow.pk or held.item_amount != 1 or escrow.money != job.fee_gold
                        or _snapshot(held)["attributes"] != {"equipment_level": 1, **job.input_snapshot["attributes"]}):
                    _fail("PROCESSING_ESCROW_INVALID")
                # Release only this reservation inside the atomic completion savepoint.
                job.status = "SETTLING"
                job.save(update_fields=["status"])
                tool = consume_tool_use("10012", HUNT_TOOL_ITEM_ID, job.ready_world_minute,
                                        slot_instance_id=job.tool_instance_id)
                inv = NpcGlobalInventory.objects.select_for_update().get(npc__agent_id="10012")
                outputs = []
                for item_id, amount in job.output_spec.items():
                    item_id = int(item_id)
                    item = Item.objects.filter(pk=item_id, is_deleted=False).first()
                    if item is None:
                        _fail("PROCESSING_CATALOG_UNAVAILABLE")
                    slot = InventorySlot.objects.create(bag=escrow, slot_index=_next_index(escrow),
                        item_id=item_id, item_bag_name=item.item_name, item_amount=amount, quality=held.quality,
                        produced_world_minute=held.produced_world_minute,
                        expires_world_minute=None,
                        source_request_id=job.request_id, freshness_unknown=False)
                    outputs.append(_batch(slot))
                held.item_amount = 0
                held.save(update_fields=["item_amount"])
                escrow.money = 0
                escrow.save(update_fields=["money"])
                inv.money += job.fee_gold
                inv.asset_version += 1
                inv.save(update_fields=["money", "asset_version", "updated_at"])
                job.status = "READY"
                job.output_snapshot = outputs
                job.completed_world_minute = job.ready_world_minute
                job.save(update_fields=["status", "output_snapshot", "completed_world_minute"])
                _ledger(job, "complete", job.ready_world_minute, dict(fee_paid=job.fee_gold,
                    input_consumed=1, outputs=outputs, tool_instance_id=str(tool.instance_id),
                    remaining_uses=tool.remaining_uses))
        except (ProcessingError, ValueError) as exc:
            reason = exc.code if isinstance(exc, ProcessingError) else str(exc)
    if reason:
        job.status = "FAILED"
        job.failure_reason = reason
        job.save(update_fields=["status", "failure_reason"])
    _bump(job.player)
    return True


@transaction.atomic
def settle_processing_jobs(minute):
    # The same global mutation lane as player actions, including standalone calls.
    clock = GlobalGameTimeState.objects.select_for_update().first()
    if clock is None:
        return []
    minute = min(int(minute), clock.world_minute)
    affected = []
    for job in VillagePlayerProcessingJob.objects.select_for_update().filter(status="RUNNING").select_related("player"):
        if _settle_job(job, minute):
            affected.append(job.player_id)
    affected.extend(_start_queued_jobs(minute))
    return list(dict.fromkeys(affected))


def _start_queued_jobs(minute):
    """Only actual arrival and a free work slot start the clock and reserve a tool."""
    affected = []
    for job in VillagePlayerProcessingJob.objects.select_for_update().filter(status="QUEUED").select_related("player").order_by("submitted_at", "started_world_minute", "job_id"):
        held = InventorySlot.objects.select_for_update().get(pk=job.input_slot_id)
        reason = _prey_reason(held, minute, job.duration_minutes)
        if reason:
            job.status, job.failure_reason = "FAILED", reason
            job.save(update_fields=["status", "failure_reason"])
            _bump(job.player)
            affected.append(job.player_id)
            continue
        if _jack_reason(minute):
            continue
        tool, reason = _tool(minute)
        if reason:
            continue
        job.status = "RUNNING"
        job.tool_instance_id = tool.instance_id
        job.started_world_minute = job.needs_until = minute
        job.ready_world_minute = minute + job.duration_minutes
        job.save(update_fields=["status", "tool_instance_id", "started_world_minute", "needs_until", "ready_world_minute"])
        _ledger(job, "begin_work", minute, dict(tool_instance_id=str(tool.instance_id)))
        _bump(job.player)
        affected.append(job.player_id)
        break
    return affected


@transaction.atomic
def execute_processing_action(player, bag, action, params, rid, clock):
    """Mutations run in player_actions' existing clock/player/bag transaction."""
    minute = clock.world_minute
    allowed = {"prey_instance_id"} if action == "preview_game_processing" else (
        {"prey_instance_id", "expected_quote"} if action == "start_game_processing" else {"job_id"})
    if set(params) - (allowed | {"quantity"}) or not allowed.issubset(params) or (
            action not in ("preview_game_processing", "start_game_processing") and "quantity" in params):
        _fail("INVALID_PARAMS")
    quantity = params.get("quantity", 1)
    if type(quantity) is not int or not 1 <= quantity <= MAX_SUBMISSION_QUANTITY:
        _fail("INVALID_PARAMS", f"每次可提交 1–{MAX_SUBMISSION_QUANTITY} 份委托。")
    if not bag.pk or bag.player_id != player.pk or bag.character_id != player.username:
        _fail("INVALID_BAG")
    if action in ("preview_game_processing", "start_game_processing"):
        query = bag.slots if action == "preview_game_processing" else bag.slots.select_for_update()
        slot = query.filter(instance_id=_uuid(params["prey_instance_id"]), item_id__in=FEES).first()
        preview, _ = _preview(player, bag, slot, minute, quantity=quantity)
        if action == "preview_game_processing":
            return dict(preview=preview)
        if not preview["available"]:
            _fail(preview["reason"])
        if params["expected_quote"] != preview["quote_hash"]:
            _fail("QUOTE_CHANGED", "报价或批次已变化，请重新预览。")
        jobs = []
        for index in range(quantity):
            job_request = rid if index == 0 else uuid.uuid5(rid, f"processing:{index}")
            job_id = uuid.uuid5(job_request, str(player.pk))
            escrow = InventoryBag.objects.create(player=player, character_id=f"jack-processing:{job_id}", money=preview["unit_fee_gold"])
            held = InventorySlot(bag=escrow, slot_index=0, item_id=slot.item_id,
                                 item_bag_name=slot.item_bag_name, item_amount=1, hide_in_shop=slot.hide_in_shop)
            copy_batch_attributes(slot, held)
            held.save()
            snapshot = _snapshot(held)
            job = VillagePlayerProcessingJob.objects.create(job_id=job_id, player=player, request_id=job_request,
                escrow_bag=escrow, input_slot=held, source_instance_id=slot.instance_id, input_snapshot=snapshot,
                output_spec={str(row["item_id"]): row["quantity"] // quantity for row in preview["outputs"]},
                status="QUEUED", tool_instance_id=None, fee_gold=preview["unit_fee_gold"], duration_minutes=preview["duration_minutes"],
                started_world_minute=minute, ready_world_minute=minute + preview["duration_minutes"], needs_until=minute)
            _ledger(job, "start", minute, dict(player_id=str(player.pk), escrow_gold=job.fee_gold,
                source_instance_id=str(slot.instance_id), input=snapshot), rid=job_request)
            jobs.append(job)
        slot.item_amount -= quantity
        slot.save(update_fields=["item_amount"])
        bag.money -= preview["fee_gold"]
        bag.save(update_fields=["money"])
        _bump(player)
        _start_queued_jobs(minute)
        for job in jobs:
            job.refresh_from_db()
        entries = [_entry(job, minute) for job in jobs]
        return dict(job=entries[0], jobs=entries, quantity=quantity, fee_gold=preview["fee_gold"])
    job = VillagePlayerProcessingJob.objects.select_for_update().filter(pk=_uuid(params["job_id"]), player=player).first()
    if job is None:
        _fail("PROCESSING_JOB_NOT_OWNED")
    _settle_job(job, minute)
    if action == "cancel_game_processing":
        if job.status not in ("QUEUED", "RUNNING", "FAILED"):
            _fail("PROCESSING_CANNOT_CANCEL", "加工已完成，不能撤销或退费。")
        held = InventorySlot.objects.select_for_update().get(pk=job.input_slot_id)
        escrow = InventoryBag.objects.select_for_update().get(pk=job.escrow_bag_id)
        if held.bag_id != escrow.pk or held.item_amount != 1 or escrow.money != job.fee_gold:
            _fail("PROCESSING_ESCROW_INVALID")
        held.bag, held.slot_index = bag, _next_index(bag)
        held.save(update_fields=["bag", "slot_index"])
        bag.money += escrow.money
        bag.save(update_fields=["money"])
        escrow.money = 0
        escrow.save(update_fields=["money"])
        job.status, job.claimed_world_minute = "CANCELLED", minute
        job.save(update_fields=["status", "claimed_world_minute"])
        _ledger(job, "cancel", minute, dict(refunded_gold=job.fee_gold, returned_instance_id=str(held.instance_id)), rid=rid)
        return dict(job=_entry(job, minute), returned_instance_ids=[str(held.instance_id)])
    if action != "claim_game_processing":
        _fail("INVALID_ACTION")
    if job.status != "READY":
        _fail("PROCESSING_NOT_READY", "工单尚未完成，或已领取。")
    escrow = InventoryBag.objects.select_for_update().get(pk=job.escrow_bag_id)
    returned = []
    for snapshot in job.output_snapshot:
        slot = escrow.slots.select_for_update().filter(instance_id=snapshot["instance_id"],
            item_id=snapshot["item_id"], item_amount=snapshot["quantity"]).first()
        if slot is None:
            _fail("PROCESSING_ESCROW_INVALID")
        slot.bag, slot.slot_index = bag, _next_index(bag)
        slot.save(update_fields=["bag", "slot_index"])
        returned.append(str(slot.instance_id))
    job.status, job.claimed_world_minute = "CLAIMED", minute
    job.save(update_fields=["status", "claimed_world_minute"])
    _ledger(job, "claim", minute, dict(returned_instance_ids=returned), rid=rid)
    return dict(job=_entry(job, minute), returned_instance_ids=returned)
