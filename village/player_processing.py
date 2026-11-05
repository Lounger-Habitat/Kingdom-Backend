"""Automatic workshop processing with player-owned carcass and fee escrow."""
import copy
import hashlib
import json
import uuid

from django.db import transaction

from game_time.models import GlobalGameTimeState
from inventory.components import BATCH_COPY_FIELDS
from inventory.storage import add_inventory_items, attributes_from_slot
from inventory.models import InventoryBag, InventorySlot, NpcGlobalInventory
from items.models import Item
from player.models import PlayerGrowthState
from player.progression import ensure_growth
from .models import (EconomicLedgerEntry, VillageActionDurationConfig,
                     VillagePlayerProcessingJob)
from .phase3_catalog import PREY_CHICKEN, PREY_PIG, PREY_CATTLE, PREY_DEER

from .professional_catalog import PREY_BOAR
from .career_catalog import (NEW_CONTENT_ITEM_IDS, content_access, processing_outputs,
    processing_duration, reserve_outputs, record_production, trial_request_id)

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


def _workshop_waiting_reason():
    # The base workshop has one slot, also enforced by processing_one_jack_job.
    # A future capacity upgrade must widen this gate and its DB constraint together.
    return "PROCESSING_QUEUE_BUSY" if VillagePlayerProcessingJob.objects.filter(status="RUNNING").exists() else ""


def _batch(slot):
    return dict(instance_id=str(slot.instance_id), item_id=slot.item_id,
        item_name=Item.objects.filter(pk=slot.item_id).values_list("item_name", flat=True).first() or str(slot.item_id),
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
    spec = processing_outputs(slot.item_id)
    names = dict(Item.objects.filter(pk__in=spec, is_deleted=False).values_list("pk", "item_name"))
    if len(names) != len(spec):
        return []
    return [dict(item_id=item_id, item_name=names[item_id], quantity=amount, quality=slot.quality,
                 produced_world_minute=slot.produced_world_minute,
                 expires_world_minute=None,
                 freshness_unknown=False) for item_id, amount in spec.items()]


def _preview(player, bag, slot, minute, *, quantity=1, read_only=False, context=None):
    if context is None:
        duration = VillageActionDurationConfig.configured_duration("PROCESS_GAME")
        waiting_reason = _workshop_waiting_reason()
    else:
        duration, waiting_reason = context
    reason = "PROCESSING_DURATION_UNAVAILABLE" if not duration else _prey_reason(slot, minute, duration)
    outputs = _outputs(slot) if slot is not None and slot.item_id in FEES else []
    if duration:
        # Workshop speed belongs here; each accepted job freezes its duration/output spec.
        duration = processing_duration(duration, {row["item_id"]: row["quantity"] for row in outputs})
    if not outputs:
        reason = reason or "PROCESSING_CATALOG_UNAVAILABLE"
    if slot is not None and slot.item_amount < quantity:
        reason = reason or "PREY_QUANTITY_UNAVAILABLE"
    unit_fee = FEES.get(slot.item_id, 0) if slot is not None else 0
    fee = unit_fee * quantity
    maximum = max(0, min(MAX_SUBMISSION_QUANTITY, slot.item_amount if slot else 0,
                        bag.money // unit_fee if unit_fee else 0))
    for output in outputs:
        access = content_access(output["item_id"])
        if access["status"] == "TRIAL":
            maximum = min(maximum, access.get("trial_remaining", 0) // output["quantity"])
    if quantity > maximum and maximum < min(MAX_SUBMISSION_QUANTITY, slot.item_amount if slot else 0):
        reason = reason or ("CAREER_TRIAL_LIMIT" if bag.money >= fee else "INSUFFICIENT_MONEY")
    for output in outputs:
        output["quantity"] *= quantity
    if bag.money < fee:
        reason = reason or "INSUFFICIENT_MONEY"
    payload = dict(player_id=str(player.pk), prey_instance_id=str(slot.instance_id) if slot else None,
        input=_snapshot(slot) if slot else None, quantity=quantity, fee_gold=fee,
        duration_minutes=duration, outputs=outputs, processing_mode="AUTOMATIC_WORKSHOP_V1")
    quote = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    if read_only:
        reason = reason or "MODE_READ_ONLY"
    return dict(available=not reason, reason=reason, quote_hash=quote,
        prey_instance_id=payload["prey_instance_id"], quantity=quantity, fee_gold=fee, unit_fee_gold=unit_fee,
        max_quantity=maximum,
        duration_minutes=duration, total_duration_minutes=(duration or 0) * quantity,
        outputs=outputs, waiting_reason=waiting_reason)


def _entry(job, minute, *, names=None):
    prey = copy.deepcopy(job.input_snapshot)
    outputs = copy.deepcopy(job.output_snapshot or [dict(item_id=int(i), quantity=n)
                                                   for i, n in job.output_spec.items()])
    if names is None:
        item_ids = {row["item_id"] for row in outputs} | {prey.get("item_id")}
        names = dict(Item.objects.filter(pk__in=item_ids).values_list("pk", "item_name"))
    # Legacy snapshots may contain inventory storage names with appended UUIDs.
    # Repair only the projection; escrow snapshots remain frozen for settlement.
    prey["item_name"] = names.get(prey.get("item_id"), prey.get("item_name") or "猎物名称待同步")
    for row in outputs:
        row["item_name"] = names.get(row["item_id"], row.get("item_name") or "产物名称待同步")
        if not job.output_snapshot:
            row["quality"] = job.input_snapshot.get("quality")
    return dict(job_id=str(job.pk), status=job.status, input=prey,
        outputs=outputs,
        fee_gold=job.fee_gold, duration_minutes=job.duration_minutes,
        started_world_minute=None if job.status == "QUEUED" else job.started_world_minute,
        ready_world_minute=None if job.status == "QUEUED" else job.ready_world_minute,
        completed_world_minute=job.completed_world_minute, claimed_world_minute=job.claimed_world_minute,
        failure_reason=job.failure_reason, can_cancel=job.status in ("QUEUED", "RUNNING", "FAILED") and
        (job.status in ("QUEUED", "FAILED") or minute < job.ready_world_minute), can_claim=job.status == "READY")


def processing_overview(player, bag, minute, read_only=False, *, slots=None):
    """Never initializes inventory, completes work or mutates any world state."""
    slots = (bag.slots.filter(item_id__in=FEES, item_amount__gt=0).select_related("usage", "dish").order_by("slot_index") if bag.pk else []) if slots is None else sorted(slots, key=lambda s: s.slot_index)
    slots = [slot for slot in slots if slot.item_id in FEES and slot.item_amount > 0] if bag.pk and bag.player_id == player.pk and bag.character_id == player.username else []
    duration = VillageActionDurationConfig.configured_duration("PROCESS_GAME")
    reason = _workshop_waiting_reason()
    context = (duration, reason)
    inputs = []
    for slot in slots:
        preview = _preview(player, bag, slot, minute, read_only=read_only, context=context)
        inputs.append(dict(**_batch(slot), can_start=preview["available"], reason=preview["reason"],
                           max_quantity=preview["max_quantity"],
                           fee_gold=preview["fee_gold"], duration_minutes=preview["duration_minutes"],
                           outputs=preview["outputs"]))
    reason = reason or ("PROCESSING_DURATION_UNAVAILABLE" if not duration else "")
    jobs = list(VillagePlayerProcessingJob.objects.filter(player=player).order_by("-submitted_at", "job_id"))
    item_ids = {job.input_snapshot.get("item_id") for job in jobs}
    item_ids.update(int(item_id) for job in jobs for item_id in job.output_spec)
    names = dict(Item.objects.filter(pk__in=item_ids).values_list("pk", "item_name")) if jobs else {}
    return dict(revision=PlayerGrowthState.objects.filter(player=player).values_list("revision", flat=True).first() or 0,
        observed_world_minute=minute, available=bool(duration),
        reason="", waiting_reason=reason, inputs=inputs,
        jobs=[_entry(job, minute, names=names) for job in jobs],
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


def _release_trial(job):
    if set(map(int, job.output_spec)) & NEW_CONTENT_ITEM_IDS:
        from .career_development import release_trial_production
        release_trial_production(trial_request_id(job.player_id, job.request_id))


def _settle_job(job, minute):
    """Caller holds clock/job; failures preserve input and the full fee."""
    if job.status != "RUNNING" or minute < job.ready_world_minute:
        return False
    reason = ""
    try:
        with transaction.atomic():
            held = InventorySlot.objects.select_for_update().get(pk=job.input_slot_id)
            escrow = InventoryBag.objects.select_for_update().get(pk=job.escrow_bag_id)
            if (held.bag_id != escrow.pk or held.item_amount != 1 or escrow.money != job.fee_gold
                    or _snapshot(held)["attributes"] != {"equipment_level": 1, **job.input_snapshot["attributes"]}):
                _fail("PROCESSING_ESCROW_INVALID")
            # Jack remains the service owner/payee; his body and tools do no work here.
            inv = NpcGlobalInventory.objects.select_for_update().get(npc__agent_id="10012")
            outputs = []
            for item_id, amount in job.output_spec.items():
                item_id = int(item_id)
                item = Item.objects.filter(pk=item_id, is_deleted=False).first()
                if item is None:
                    _fail("PROCESSING_CATALOG_UNAVAILABLE")
                for slot, delta in add_inventory_items(escrow, item_id, amount,
                        attributes=dict(item_bag_name=item.item_name, quality=held.quality)):
                    outputs.append(dict(_batch(slot), amount=delta, quantity=delta))
            held.item_amount = 0
            held.save(update_fields=["item_amount"])
            escrow.money = 0
            escrow.save(update_fields=["money"])
            inv.money += job.fee_gold
            inv.asset_version += 1
            inv.save(update_fields=["money", "asset_version", "updated_at"])
            job.status = "READY"
            job.output_snapshot = outputs
            job.completed_world_minute = job.needs_until = job.ready_world_minute
            job.save(update_fields=["status", "output_snapshot", "completed_world_minute", "needs_until"])
            _ledger(job, "complete", job.ready_world_minute, dict(fee_paid=job.fee_gold,
                input_consumed=1, outputs=outputs))
            record_production("PLAYER", job.player_id, job.request_id,
                {int(item): amount for item, amount in job.output_spec.items()}, job.ready_world_minute,
                source_kind="PROCESS_GAME", prey_item_id=held.item_id,
                reservation_request_id=trial_request_id(job.player_id, job.request_id),
                output_instance_ids=[row["instance_id"] for row in outputs], outputs=outputs)
    except (ProcessingError, ValueError) as exc:
        reason = exc.code if isinstance(exc, ProcessingError) else str(exc)
    if reason:
        _release_trial(job)
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
    # A coalesced Tick still processes the queue on its original work timeline.
    while True:
        job = VillagePlayerProcessingJob.objects.select_for_update().filter(
            status="RUNNING", ready_world_minute__lte=minute).select_related("player").order_by("ready_world_minute", "job_id").first()
        if job is None:
            break
        if _settle_job(job, job.ready_world_minute):
            affected.append(job.player_id)
        affected.extend(_start_queued_jobs(job.ready_world_minute))
    affected.extend(_start_queued_jobs(minute))
    return list(dict.fromkeys(affected))


def _start_queued_jobs(minute):
    """A free workshop slot starts the next job, independently of any NPC."""
    affected = []
    if _workshop_waiting_reason():
        return affected
    for job in VillagePlayerProcessingJob.objects.select_for_update().filter(status="QUEUED").select_related("player").order_by("submitted_at", "started_world_minute", "job_id"):
        held = InventorySlot.objects.select_for_update().get(pk=job.input_slot_id)
        started = max(minute, job.started_world_minute)
        reason = _prey_reason(held, started, job.duration_minutes)
        if reason:
            _release_trial(job)
            job.status, job.failure_reason = "FAILED", reason
            job.save(update_fields=["status", "failure_reason"])
            _bump(job.player)
            affected.append(job.player_id)
            continue
        job.status = "RUNNING"
        job.tool_instance_id = None
        job.started_world_minute = job.needs_until = started
        job.ready_world_minute = started + job.duration_minutes
        job.save(update_fields=["status", "tool_instance_id", "started_world_minute", "needs_until", "ready_world_minute"])
        _ledger(job, "begin_work", started, {})
        _bump(job.player)
        affected.append(job.player_id)
        break
    return affected


def _claim_job(job, bag, minute, rid):
    if job.status != "READY":
        _fail("PROCESSING_NOT_READY", "工单尚未完成，或已领取。")
    escrow = InventoryBag.objects.select_for_update().get(pk=job.escrow_bag_id)
    returned = []
    for snapshot in job.output_snapshot:
        slot = escrow.slots.select_for_update().filter(instance_id=snapshot["instance_id"],
            item_id=snapshot["item_id"], item_amount=snapshot["quantity"]).first()
        if slot is None:
            _fail("PROCESSING_ESCROW_INVALID")
        restored = add_inventory_items(bag, slot.item_id, snapshot["quantity"], attributes=attributes_from_slot(slot))
        slot.item_amount = 0
        slot.save(update_fields=["item_amount"])
        returned.extend(str(s.instance_id) for s, _ in restored)
    job.status, job.claimed_world_minute = "CLAIMED", minute
    job.save(update_fields=["status", "claimed_world_minute"])
    _ledger(job, "claim", minute, dict(returned_instance_ids=returned), rid=rid)
    return returned


@transaction.atomic
def execute_processing_action(player, bag, action, params, rid, clock):
    """Mutations run in player_actions' existing clock/player/bag transaction."""
    minute = clock.world_minute
    claim_all = action == "claim_game_processing" and params.get("all_ready") is True
    allowed = {"prey_instance_id"} if action == "preview_game_processing" else (
        {"prey_instance_id", "expected_quote"} if action == "start_game_processing" else {"all_ready"} if claim_all else {"job_id"})
    if set(params) - (allowed | {"quantity"}) or not allowed.issubset(params) or (
            action not in ("preview_game_processing", "start_game_processing") and "quantity" in params):
        _fail("INVALID_PARAMS")
    quantity = params.get("quantity", 1)
    if type(quantity) is not int or not 1 <= quantity <= MAX_SUBMISSION_QUANTITY:
        _fail("INVALID_PARAMS", f"每次可提交 1–{MAX_SUBMISSION_QUANTITY} 份委托。")
    if not bag.pk or bag.player_id != player.pk or bag.character_id != player.username:
        _fail("INVALID_BAG")
    if action != "preview_game_processing":
        settle_processing_jobs(minute)
    if claim_all:
        jobs = list(VillagePlayerProcessingJob.objects.select_for_update().filter(player=player, status="READY")
            .order_by("submitted_at", "job_id"))
        if not jobs:
            _fail("PROCESSING_NOT_READY", "暂无已完成且未领取的工单。")
        entries, returned = [], []
        for job in jobs:
            returned.extend(_claim_job(job, bag, minute, uuid.uuid5(rid, f"claim:{job.pk}")))
            entries.append(_entry(job, minute))
        return dict(job=entries[0], jobs=entries, claimed_count=len(entries),
            claimed_items=[item for entry in entries for item in entry["outputs"]], returned_instance_ids=returned)
    if action in ("preview_game_processing", "start_game_processing"):
        query = bag.slots if action == "preview_game_processing" else bag.slots.select_for_update()
        slot = query.filter(instance_id=_uuid(params["prey_instance_id"]), item_id__in=FEES).first()
        preview = _preview(player, bag, slot, minute, quantity=quantity)
        if action == "preview_game_processing":
            return dict(preview=preview)
        if not preview["available"]:
            _fail(preview["reason"])
        if params["expected_quote"] != preview["quote_hash"]:
            _fail("QUOTE_CHANGED", "报价或批次已变化，请重新预览。")
        jobs = []
        for index in range(quantity):
            job_request = rid if index == 0 else uuid.uuid5(rid, f"processing:{index}")
            if not reserve_outputs({row["item_id"]: row["quantity"] // quantity for row in preview["outputs"]}, trial_request_id(player.pk, job_request), minute):
                _fail("CAREER_TRIAL_LIMIT", "本期试加工额度不足，请先完成已有试制品销售。")
            job_id = uuid.uuid5(job_request, str(player.pk))
            escrow = InventoryBag.objects.create(player=player, character_id=f"jack-processing:{job_id}", money=preview["unit_fee_gold"])
            held = add_inventory_items(escrow, slot.item_id, 1, attributes=attributes_from_slot(slot))[0][0]
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
        restored = add_inventory_items(bag, held.item_id, 1, attributes=attributes_from_slot(held))
        held.item_amount = 0
        held.save(update_fields=["item_amount"])
        bag.money += escrow.money
        bag.save(update_fields=["money"])
        escrow.money = 0
        escrow.save(update_fields=["money"])
        job.status, job.claimed_world_minute = "CANCELLED", minute
        job.save(update_fields=["status", "claimed_world_minute"])
        _release_trial(job)
        _ledger(job, "cancel", minute, dict(refunded_gold=job.fee_gold, returned_instance_ids=[str(s.instance_id) for s, _ in restored]), rid=rid)
        _start_queued_jobs(minute)
        return dict(job=_entry(job, minute), returned_instance_ids=[str(s.instance_id) for s, _ in restored])
    if action != "claim_game_processing":
        _fail("INVALID_ACTION")
    returned = _claim_job(job, bag, minute, rid)
    entry = _entry(job, minute)
    return dict(job=entry, claimed_count=1, claimed_items=entry["outputs"], returned_instance_ids=returned)
