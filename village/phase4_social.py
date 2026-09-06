"""Phase four social mutations.

The service is intentionally small: commitments share one model and every
event is projected through the existing delivery outbox.  Callers invoke it
inside the authoritative Backend transaction.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from typing import Any, Mapping

from django.db import models, transaction

from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition, NpcGlobalInventory

from .models import (
    EconomicLedgerEntry,
    NpcCommitment,
    NpcCommandState,
    NpcRelationship,
    NpcRelationshipChange,
    NpcRuntimeState,
    NpcStoryFactAccess,
    PlayerStoryFactAccess,
    VillageActionReceipt,
    VillageActionDurationConfig,
    VillageLocationDefinition,
    VillageSimulationConfig,
    VillageWorldEvent,
)
from .phase4_contract import (
    FACT_CODES,
    FORMAL_NPC_IDS,
    RELATIONSHIP_FIELDS,
    RELATIONSHIP_LIMITS,
    RELATIONSHIP_RULES,
    RelationshipCategory,
    normalize_delta,
    relationship_label,
    relationship_snapshot,
    validate_commitment_target,
)
from .phase4_visibility import enqueue_event_deliveries, resolve_witnesses


SOCIAL_RESULT_SCHEMA = "village.social-result.v1"
OK = "OK"


def _result(code: str = OK, **values: Any) -> dict[str, Any]:
    result = {
        "schema_version": SOCIAL_RESULT_SCHEMA,
        "status": "SUCCEEDED" if code == OK else "FAILED",
        "result_code": code,
        "replayed": False,
        "world_minute": 0,
        "state_version": 0,
        "duration_minutes": 1,
        "presentation_status": "ACTING",
        "data": {},
    }
    result.update(values)
    return result


def social_enabled() -> bool:
    return VillageSimulationConfig.objects.filter(
        config_key="default", village_social_enabled=True
    ).exists()


def _npc(agent_id: Any) -> NpcDefinition | None:
    value = str(agent_id)
    if value not in FORMAL_NPC_IDS:
        return None
    return NpcDefinition.objects.filter(agent_id=value, is_test_only=False).first()


def _payload_hash(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _event(
    *,
    event_type: str,
    world_minute: int,
    time_revision: int,
    request_id: uuid.UUID | None,
    command_id: uuid.UUID | None,
    actor: NpcDefinition | None,
    participants: list[str] | None = None,
    location_code: str = "",
    visibility: str = "PARTICIPANTS",
    witnesses: list[str] | None = None,
    player_ids: list[str] | None = None,
    public_data: Mapping[str, Any] | None = None,
    private_data: Mapping[str, Any] | None = None,
    suffix: str = "",
) -> VillageWorldEvent:
    request_part = str(request_id or uuid.uuid4())
    dedupe = f"phase4:{event_type}:{request_part}:{suffix or 'event'}"
    event = VillageWorldEvent.objects.create(
        dedupe_key=dedupe[:128],
        event_type=event_type,
        world_minute=world_minute,
        time_revision=time_revision,
        actor_npc=actor,
        participant_npc_ids=[str(value) for value in (participants or [])],
        witness_npc_ids=[str(value) for value in (witnesses or [])],
        player_ids=[str(value) for value in (player_ids or [])],
        location_code=location_code or "",
        command_id=command_id,
        request_id=request_id,
        effects={},
        visibility=visibility,
        public_data=dict(public_data or {}),
        private_data=dict(private_data or {}),
        player_summary=str((public_data or {}).get("player_summary", ""))[:256],
        hub_summary=str((public_data or {}).get("hub_summary", event_type))[:256],
    )
    enqueue_event_deliveries(event)
    return event


def _relation(from_npc: NpcDefinition, to_npc: NpcDefinition) -> NpcRelationship:
    relation, _ = NpcRelationship.objects.select_for_update().get_or_create(
        from_npc=from_npc,
        to_npc=to_npc,
        defaults={"affinity": 0, "trust": 0, "obligation": 0, "conflict": 0},
    )
    return relation


def reduce_relationship(
    event: VillageWorldEvent,
    from_npc_id: Any,
    to_npc_id: Any,
    category: str,
    requested_delta: Mapping[str, Any],
    *,
    major: bool = False,
) -> dict[str, int]:
    """Apply one directed, audited and idempotent relationship change."""
    from_npc = _npc(from_npc_id)
    to_npc = _npc(to_npc_id)
    if from_npc is None or to_npc is None or from_npc.pk == to_npc.pk:
        return {}
    category = str(category).upper()
    requested = normalize_delta(requested_delta)
    relation = _relation(from_npc, to_npc)
    if NpcRelationshipChange.objects.filter(
        relationship=relation, source_event=event
    ).exists():
        return {}

    day = max(0, int(event.world_minute) // 1440)
    previous_count = NpcRelationshipChange.objects.filter(
        relationship=relation, category=category, world_day=day
    ).count()
    multiplier = 1.0 if previous_count == 0 else 0.5 if previous_count == 1 else 0.25
    cap = 15 if major else 10
    before = relationship_snapshot(relation)
    applied: dict[str, int] = {}
    after = dict(before)
    for field, raw_value in requested.items():
        value = int(raw_value)
        scaled = int(math.floor(abs(value) * multiplier))
        if value and scaled == 0:
            scaled = 1
        scaled = scaled if value >= 0 else -scaled
        used = sum(
            abs(int((change.applied_delta or {}).get(field, 0)))
            for change in NpcRelationshipChange.objects.filter(
                relationship=relation, world_day=day
            )
        )
        remaining = max(0, cap - used)
        scaled = max(-remaining, min(remaining, scaled))
        low, high = RELATIONSHIP_LIMITS[field]
        final = max(low, min(high, before[field] + scaled))
        applied[field] = final - before[field]
        after[field] = final
    applied = {key: value for key, value in applied.items() if value}
    NpcRelationshipChange.objects.create(
        relationship=relation,
        source_event=event,
        category=category,
        world_day=day,
        requested_delta=requested,
        applied_delta=applied,
        before_values=before,
        after_values=after,
    )
    if applied:
        for field, value in after.items():
            setattr(relation, field, value)
        relation.version += 1
        relation.last_meaningful_event = event
        relation.save(update_fields=[
            *RELATIONSHIP_FIELDS, "version", "last_meaningful_event", "updated_at",
        ])
    return applied


def _apply_rule(
    event: VillageWorldEvent,
    from_npc_id: str,
    to_npc_id: str,
    category: str,
    *,
    major: bool = False,
) -> dict[str, int]:
    return reduce_relationship(
        event,
        from_npc_id,
        to_npc_id,
        category,
        RELATIONSHIP_RULES.get(category, {}),
        major=major,
    )


def complete_delivery_commitment(
    commitment_id: Any,
    event: VillageWorldEvent,
    *,
    world_minute: int | None = None,
    emergency: bool = False,
) -> NpcCommitment | None:
    """Close DELIVERY after phase-three accepted the linked trade offer."""
    commitment_uuid = _uuid(commitment_id)
    commitment = NpcCommitment.objects.select_for_update().filter(
        commitment_id=commitment_uuid
    ).first()
    if commitment is None or commitment.status != "OPEN" or commitment.commitment_type != "DELIVERY":
        return None
    commitment.status = "COMPLETED"
    commitment.settled_event = event
    commitment.settled_world_minute = int(world_minute if world_minute is not None else event.world_minute)
    commitment.save(update_fields=["status", "settled_event", "settled_world_minute", "updated_at"])
    category = RelationshipCategory.EMERGENCY_SUPPLY.value if emergency else RelationshipCategory.DELIVERY.value
    _apply_rule(
        event,
        str(commitment.beneficiary_npc.agent_id),
        str(commitment.promisor_npc.agent_id),
        category,
    )
    return commitment


def _commitment_data(commitment: NpcCommitment) -> dict[str, Any]:
    return {
        "commitment_id": str(commitment.commitment_id),
        "commitment_type": commitment.commitment_type,
        "promisor_npc_id": str(commitment.promisor_npc.agent_id),
        "beneficiary_npc_id": str(commitment.beneficiary_npc.agent_id),
        "target": dict(commitment.target or {}),
        "due_world_minute": commitment.due_world_minute,
        "status": commitment.status,
    }


def create_commitment(
    npc_id: Any,
    params: Mapping[str, Any],
    world_minute: int,
    request_id: uuid.UUID,
    command_id: uuid.UUID | None = None,
    time_revision: int | None = None,
) -> dict[str, Any]:
    promisor = _npc(npc_id)
    beneficiary = _npc(params.get("beneficiary_npc_id"))
    kind = str(params.get("commitment_type", "")).upper()
    target = validate_commitment_target(kind, params.get("target"))
    due = params.get("due_world_minute")
    if promisor is None or beneficiary is None or promisor.pk == beneficiary.pk:
        return _result("SOCIAL_INVALID_PARAMS")
    if target is None or not isinstance(due, int) or isinstance(due, bool) or due <= world_minute:
        return _result("SOCIAL_INVALID_PARAMS")
    if kind in {"JOINT_WORK", "MEETING"} and not VillageLocationDefinition.objects.filter(
        location_code=target["location_code"], is_active=True
    ).exists():
        return _result("SOCIAL_INVALID_PARAMS")
    if NpcCommitment.objects.filter(
        promisor_npc=promisor,
        beneficiary_npc=beneficiary,
        commitment_type=kind,
        target=target,
        status="OPEN",
    ).exists():
        return _result("COMMITMENT_DUPLICATE_OPEN")
    location = NpcRuntimeState.objects.filter(npc=promisor).values_list(
        "current_location_code", flat=True
    ).first() or ""
    event = _event(
        event_type="COMMITMENT_CREATED",
        world_minute=world_minute,
        time_revision=int(time_revision if time_revision is not None else world_minute),
        request_id=request_id,
        command_id=command_id,
        actor=promisor,
        participants=[str(promisor.agent_id), str(beneficiary.agent_id)],
        location_code=location,
        visibility="PARTICIPANTS",
        public_data={"commitment_type": kind, "hub_summary": "A structured commitment was created."},
        private_data={"target": target, "due_world_minute": due},
    )
    commitment = NpcCommitment.objects.create(
        commitment_type=kind,
        promisor_npc=promisor,
        beneficiary_npc=beneficiary,
        target=target,
        due_world_minute=due,
        source_request_id=request_id,
        created_event=event,
        created_world_minute=world_minute,
    )
    return _result(
        data=_commitment_data(commitment),
        commitment_type=kind,
        world_event_id=str(event.event_id),
        duration_minutes=VillageActionDurationConfig.configured_duration("CREATE_COMMITMENT"),
        presentation_status="ACTING",
    )


def cancel_commitment(
    npc_id: Any,
    params: Mapping[str, Any],
    world_minute: int,
    request_id: uuid.UUID,
    command_id: uuid.UUID | None = None,
    time_revision: int | None = None,
) -> dict[str, Any]:
    promisor = _npc(npc_id)
    commitment = NpcCommitment.objects.select_for_update().filter(
        commitment_id=_uuid(params.get("commitment_id")), promisor_npc=promisor
    ).first() if promisor else None
    if commitment is None:
        return _result("COMMITMENT_NOT_FOUND")
    if commitment.status != "OPEN":
        return _result("COMMITMENT_ALREADY_RESOLVED")
    if commitment.due_world_minute <= world_minute:
        return _result("COMMITMENT_EXPIRED")
    event = _event(
        event_type="COMMITMENT_CANCELLED",
        world_minute=world_minute,
        time_revision=int(time_revision if time_revision is not None else world_minute),
        request_id=request_id,
        command_id=command_id,
        actor=promisor,
        participants=[str(commitment.promisor_npc.agent_id), str(commitment.beneficiary_npc.agent_id)],
        location_code=NpcRuntimeState.objects.filter(npc=promisor).values_list("current_location_code", flat=True).first() or "",
        visibility="PARTICIPANTS",
        public_data={"commitment_id": str(commitment.commitment_id), "hub_summary": "A commitment was cancelled."},
    )
    commitment.status = "CANCELLED"
    commitment.settled_event = event
    commitment.settled_world_minute = world_minute
    commitment.save(update_fields=["status", "settled_event", "settled_world_minute", "updated_at"])
    return _result(data=_commitment_data(commitment), world_event_id=str(event.event_id),
                   duration_minutes=VillageActionDurationConfig.configured_duration("CANCEL_COMMITMENT"))


def _transfer_commitment_assets(
    commitment: NpcCommitment,
    world_minute: int,
    request_id: uuid.UUID,
) -> tuple[bool, str, dict[str, Any]]:
    """Move NPC assets using phase-three's existing FEFO and reservation checks."""
    from .phase3_asset_service import add_batch
    from .phase3_direct_trade import _consume_personal_fefo_available, _get_buyer_available_money

    promisor_id = str(commitment.promisor_npc.agent_id)
    beneficiary_id = str(commitment.beneficiary_npc.agent_id)
    first, second = sorted([promisor_id, beneficiary_id])
    first_inv = NpcGlobalInventory.objects.select_for_update().get(npc__agent_id=first)
    second_inv = NpcGlobalInventory.objects.select_for_update().get(npc__agent_id=second)
    promisor_inv = first_inv if first == promisor_id else second_inv
    beneficiary_inv = second_inv if first == promisor_id else first_inv
    target = commitment.target or {}
    changes: dict[str, Any] = {}
    if commitment.commitment_type == "REPAYMENT" or target.get("asset_kind") == "GOLD":
        gold = int(target.get("gold_amount", 0))
        if _get_buyer_available_money(promisor_inv) < gold:
            return False, "INSUFFICIENT_MONEY", {}
        promisor_inv.money -= gold
        beneficiary_inv.money += gold
        promisor_inv.asset_version += 1
        beneficiary_inv.asset_version += 1
        promisor_inv.save(update_fields=["money", "asset_version", "updated_at"])
        beneficiary_inv.save(update_fields=["money", "asset_version", "updated_at"])
        changes = {promisor_id: {"money": -gold}, beneficiary_id: {"money": gold}}
    else:
        item_id = int(target.get("item_id", 0))
        amount = int(target.get("amount", 0))
        try:
            consumed = _consume_personal_fefo_available(promisor_inv, item_id, amount, world_minute)
        except ValueError:
            return False, "INSUFFICIENT_ITEM", {}
        for slot, taken in consumed:
            add_batch(
                beneficiary_inv,
                item_id,
                taken,
                slot.produced_world_minute or world_minute,
                slot.expires_world_minute,
                request_id,
                remaining_uses=slot.remaining_uses,
            )
        promisor_inv.asset_version += 1
        beneficiary_inv.asset_version += 1
        promisor_inv.save(update_fields=["asset_version", "updated_at"])
        beneficiary_inv.save(update_fields=["asset_version", "updated_at"])
        changes = {
            promisor_id: {str(item_id): -amount},
            beneficiary_id: {str(item_id): amount},
        }
    return True, OK, changes


def perform_commitment(
    npc_id: Any,
    params: Mapping[str, Any],
    world_minute: int,
    request_id: uuid.UUID,
    command_id: uuid.UUID | None = None,
    time_revision: int | None = None,
    duration_minutes: int | None = None,
) -> dict[str, Any]:
    promisor = _npc(npc_id)
    commitment = NpcCommitment.objects.select_for_update().filter(
        commitment_id=_uuid(params.get("commitment_id")), promisor_npc=promisor
    ).select_related("promisor_npc", "beneficiary_npc").first() if promisor else None
    if commitment is None:
        return _result("COMMITMENT_NOT_FOUND")
    if duration_minutes is None:
        duration_minutes = VillageActionDurationConfig.configured_duration(
            commitment.commitment_type if commitment.commitment_type in {"JOINT_WORK", "MEETING"}
            else "PERFORM_COMMITMENT"
        )
    if commitment.status != "OPEN":
        return _result("COMMITMENT_ALREADY_RESOLVED")
    if commitment.commitment_type == "DELIVERY":
        return _result("DELIVERY_REQUIRES_TRADE")
    if commitment.due_world_minute <= world_minute:
        return _result("COMMITMENT_EXPIRED")

    phase = str(params.get("phase", "COMPLETE")).upper()
    if phase not in {"START", "COMPLETE", "RESULT"}:
        return _result("SOCIAL_INVALID_PARAMS")
    runtimes = list(
        NpcRuntimeState.objects.select_for_update()
        .filter(npc__in=[promisor, commitment.beneficiary_npc])
        .select_related("npc")
        .order_by("npc__agent_id")
    )
    runtime = next((row for row in runtimes if row.npc_id == promisor.pk), None)
    beneficiary_runtime = next(
        (row for row in runtimes if row.npc_id == commitment.beneficiary_npc_id), None
    )
    target = commitment.target or {}
    location = str(target.get("location_code", ""))
    if commitment.commitment_type in {"JOINT_WORK", "MEETING"}:
        if not runtime or not beneficiary_runtime or runtime.current_location_code != location or beneficiary_runtime.current_location_code != location:
            return _result("SOCIAL_NOT_COLOCATED")
        actor_is_current_command = (
            command_id is not None
            and runtime.active_command_id == command_id
            and runtime.current_action == "PERFORM_COMMITMENT"
        )
        if (runtime.action_status != "IDLE" and not actor_is_current_command) or beneficiary_runtime.action_status != "IDLE":
            return _result("SOCIAL_PARTICIPANT_UNAVAILABLE")

    participants = [str(promisor.agent_id), str(commitment.beneficiary_npc.agent_id)]
    witnesses = resolve_witnesses(location, participants)
    if phase == "START":
        dedupe_key = f"phase4:COMMITMENT_ACTION_STARTED:{request_id}:start"
        event = VillageWorldEvent.objects.filter(dedupe_key=dedupe_key).first()
        if event is None:
            event = _event(
                event_type="COMMITMENT_ACTION_STARTED",
                world_minute=world_minute,
                time_revision=int(time_revision if time_revision is not None else world_minute),
                request_id=request_id,
                command_id=command_id,
                actor=promisor,
                participants=participants,
                location_code=location,
                visibility="PRESENT",
                witnesses=witnesses,
                public_data={
                    "commitment_id": str(commitment.commitment_id),
                    "commitment_type": commitment.commitment_type,
                    "hub_summary": "A commitment action started.",
                },
                suffix="start",
            )
        return _result(
            data=_commitment_data(commitment),
            commitment_type=commitment.commitment_type,
            duration_minutes=duration_minutes,
            presentation_status="WORKING" if commitment.commitment_type == "JOINT_WORK" else "SOCIALIZING" if commitment.commitment_type == "MEETING" else "TRADING",
            world_event_id=str(event.event_id),
            phase="START",
        )

    changes = {}
    if commitment.commitment_type in {"REPAYMENT", "DONATION"}:
        success, code, changes = _transfer_commitment_assets(commitment, world_minute, request_id)
        if not success:
            return _result(code)
    event = _event(
        event_type="COMMITMENT_COMPLETED",
        world_minute=world_minute,
        time_revision=int(time_revision if time_revision is not None else world_minute),
        request_id=request_id,
        command_id=command_id,
        actor=promisor,
        participants=participants,
        location_code=location,
        visibility="PRESENT",
        witnesses=witnesses,
        public_data={
            "commitment_id": str(commitment.commitment_id),
            "commitment_type": commitment.commitment_type,
            "hub_summary": "A commitment action was completed.",
        },
        suffix="complete",
    )
    commitment.status = "COMPLETED"
    commitment.settled_event = event
    commitment.settled_world_minute = world_minute
    commitment.save(update_fields=["status", "settled_event", "settled_world_minute", "updated_at"])
    if changes:
        EconomicLedgerEntry.objects.create(
            request_id=request_id,
            entry_type=commitment.commitment_type,
            actor_npc=promisor,
            changes=changes,
            external_delta=0,
            world_minute=world_minute,
            event=event,
        )
    category = (
        RelationshipCategory.JOINT_WORK.value
        if commitment.commitment_type == "JOINT_WORK"
        else RelationshipCategory.RESCUE_GIFT.value
        if commitment.commitment_type == "DONATION"
        else None
    )
    if category == RelationshipCategory.JOINT_WORK.value:
        _apply_rule(event, participants[0], participants[1], category)
        _apply_rule(event, participants[1], participants[0], category)
    elif category:
        _apply_rule(event, participants[1], participants[0], category)
    return _result(
        data={**_commitment_data(commitment), "asset_changes": changes},
        effects=changes,
        commitment_type=commitment.commitment_type,
        duration_minutes=duration_minutes,
        presentation_status="WORKING" if commitment.commitment_type == "JOINT_WORK" else "SOCIALIZING" if commitment.commitment_type == "MEETING" else "TRADING",
        world_event_id=str(event.event_id),
        phase=phase,
    )


def breach_due_commitments(world_minute: int, time_revision: int | None = None) -> int:
    count = 0
    for commitment_id in list(NpcCommitment.objects.filter(
        status="OPEN", due_world_minute__lte=world_minute
    ).values_list("commitment_id", flat=True)):
        commitment = NpcCommitment.objects.select_for_update().filter(
            commitment_id=commitment_id, status="OPEN"
        ).select_related("promisor_npc", "beneficiary_npc").first()
        if commitment is None:
            continue
        event = _event(
            event_type="COMMITMENT_BREACHED",
            world_minute=world_minute,
            time_revision=int(time_revision if time_revision is not None else world_minute),
            request_id=uuid.uuid5(uuid.NAMESPACE_URL, f"phase4:breach:{commitment_id}:{world_minute}"),
            command_id=None,
            actor=commitment.promisor_npc,
            participants=[str(commitment.promisor_npc.agent_id), str(commitment.beneficiary_npc.agent_id)],
            location_code="",
            visibility="PARTICIPANTS",
            public_data={"commitment_id": str(commitment.commitment_id), "hub_summary": "A commitment was breached."},
            suffix=str(commitment_id),
        )
        commitment.status = "BREACHED"
        commitment.settled_event = event
        commitment.settled_world_minute = world_minute
        commitment.save(update_fields=["status", "settled_event", "settled_world_minute", "updated_at"])
        _apply_rule(event, str(commitment.beneficiary_npc.agent_id), str(commitment.promisor_npc.agent_id), RelationshipCategory.BREACH.value)
        count += 1
    return count


def inspect_social_state(npc_id: Any) -> dict[str, Any]:
    npc = _npc(npc_id)
    if npc is None:
        return _result("NPC_NOT_FOUND")
    relationships = NpcRelationship.objects.filter(
        from_npc=npc
    ).select_related("to_npc")
    commitments = NpcCommitment.objects.filter(
        models.Q(promisor_npc=npc) | models.Q(beneficiary_npc=npc)
    ).select_related("promisor_npc", "beneficiary_npc").order_by("-updated_at")[:20]
    rel_data = []
    for relation in relationships:
        other = relation.to_npc
        has_open = NpcCommitment.objects.filter(
            status="OPEN",
        ).filter(
            models.Q(promisor_npc=npc, beneficiary_npc=other)
            | models.Q(promisor_npc=other, beneficiary_npc=npc)
        ).exists()
        rel_data.append({
            "other_npc_id": str(other.agent_id),
            "label": relationship_label(relationship_snapshot(relation), has_open),
            "has_open_commitment": has_open,
        })
    return _result(data={
        "npc_id": str(npc.agent_id),
        "relationships": rel_data,
        "commitments": [_commitment_data(row) for row in commitments],
        "fact_codes": list(NpcStoryFactAccess.objects.filter(npc=npc).values_list("fact_code", flat=True)),
    })


def has_npc_story_fact(npc_id: Any, fact_code: str) -> bool:
    """Return whether a formal NPC has unlocked one known story fact."""
    npc = _npc(npc_id)
    return bool(npc and str(fact_code) in FACT_CODES and NpcStoryFactAccess.objects.filter(
        npc=npc, fact_code=str(fact_code)
    ).exists())


def grant_npc_fact(npc_id: Any, fact_code: str, world_minute: int, event: VillageWorldEvent | None = None) -> NpcStoryFactAccess | None:
    npc = _npc(npc_id)
    if npc is None or fact_code not in FACT_CODES:
        return None
    access, created = NpcStoryFactAccess.objects.get_or_create(
        npc=npc,
        fact_code=fact_code,
        defaults={"unlocked_world_minute": world_minute, "unlocked_by_event": event},
    )
    if created and event is None:
        unlock_event = _event(
            event_type="STORY_FACT_UNLOCKED",
            world_minute=world_minute,
            time_revision=world_minute,
            request_id=uuid.uuid4(),
            command_id=None,
            actor=npc,
            participants=[str(npc.agent_id)],
            visibility="PARTICIPANTS",
            public_data={"fact_code": fact_code, "hub_summary": "A story fact was unlocked."},
        )
        access.unlocked_by_event = unlock_event
        access.save(update_fields=["unlocked_by_event"])
    return access


def grant_player_fact(player_id: Any, fact_code: str, world_minute: int, event: VillageWorldEvent | None = None) -> PlayerStoryFactAccess | None:
    player = str(player_id or "")
    if not player or fact_code not in FACT_CODES:
        return None
    access, created = PlayerStoryFactAccess.objects.get_or_create(
        player_id=player,
        fact_code=fact_code,
        defaults={"unlocked_world_minute": world_minute, "unlocked_by_event": event},
    )
    if created and event is None:
        unlock_event = _event(
            event_type="STORY_FACT_UNLOCKED",
            world_minute=world_minute,
            time_revision=world_minute,
            request_id=uuid.uuid4(),
            command_id=None,
            actor=None,
            player_ids=[player],
            visibility="PLAYER_PRIVATE",
            public_data={"fact_code": fact_code, "player_summary": "A private story fact was unlocked."},
        )
        access.unlocked_by_event = unlock_event
        access.save(update_fields=["unlocked_by_event"])
    return access


def complete_social_action(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not social_enabled():
        return _result("VILLAGE_SOCIAL_DISABLED")
    try:
        request_id = uuid.UUID(str(payload["request_id"]))
        command_id = uuid.UUID(str(payload["command_id"]))
    except (KeyError, ValueError, TypeError, AttributeError):
        return _result("SOCIAL_INVALID_PARAMS")
    npc_id = str(payload.get("npc_id", ""))
    action = str(payload.get("action", "")).upper()
    params = payload.get("params")
    if not isinstance(params, Mapping) or action not in {"INSPECT_SOCIAL_STATE", "CREATE_COMMITMENT", "CANCEL_COMMITMENT", "PERFORM_COMMITMENT"}:
        return _result("SOCIAL_INVALID_PARAMS")
    allowed_params = {
        "INSPECT_SOCIAL_STATE": set(),
        "CREATE_COMMITMENT": {"beneficiary_npc_id", "commitment_type", "target", "due_world_minute"},
        "CANCEL_COMMITMENT": {"commitment_id"},
        "PERFORM_COMMITMENT": {"commitment_id", "phase"},
    }[action]
    if set(params) - allowed_params or action == "INSPECT_SOCIAL_STATE" and params:
        return _result("SOCIAL_INVALID_PARAMS")
    digest = _payload_hash(payload)
    with transaction.atomic():
        state = GlobalGameTimeState.objects.select_for_update().get(pk=1)
        world_minute = int(state.world_minute)
        old = VillageActionReceipt.objects.filter(request_id=request_id).first()
        if old:
            saved = dict(old.response_data or {})
            if saved.get("request_payload_sha256") != digest:
                return _result("SOCIAL_REQUEST_CONFLICT", status="CONFLICT")
            saved.pop("request_payload_sha256", None)
            saved["replayed"] = True
            return saved
        command = NpcCommandState.objects.select_for_update().filter(command_id=command_id).first()
        runtime = NpcRuntimeState.objects.select_for_update().filter(npc__agent_id=npc_id).first()
        if command is not None and (
            runtime is None
            or command.request_id != request_id
            or str(command.npc.agent_id) != npc_id
            or command.kind != action
            or command.status != "RUNNING"
            or runtime.active_command_id != command.command_id
        ):
            return _result("SOCIAL_INVALID_PARAMS")
        if command is not None and action != "INSPECT_SOCIAL_STATE":
            if command.started_world_minute is None or not command.duration_minutes:
                return _result("INVALID_ACTION_DURATION")
            if (str(params.get("phase", "COMPLETE")).upper() != "START" and
                    world_minute < command.started_world_minute + command.duration_minutes):
                return _result("ACTION_NOT_FINISHED")
        if action == "INSPECT_SOCIAL_STATE":
            result = inspect_social_state(npc_id)
        elif action == "CREATE_COMMITMENT":
            result = create_commitment(
                npc_id, params, world_minute, request_id, command_id,
                time_revision=int(state.time_revision),
            )
        elif action == "CANCEL_COMMITMENT":
            result = cancel_commitment(
                npc_id, params, world_minute, request_id, command_id,
                time_revision=int(state.time_revision),
            )
        else:
            result = perform_commitment(
                npc_id, params, world_minute, request_id, command_id,
                time_revision=int(state.time_revision),
                duration_minutes=command.duration_minutes if command else None,
            )
        if command is not None:
            result["duration_minutes"] = command.duration_minutes
        result["request_id"] = str(request_id)
        result["world_minute"] = world_minute
        if result["status"] == "SUCCEEDED" and result.get("phase") == "START":
            result["state_version"] = runtime.state_version if runtime else 0
            return result

        if command is not None:
            runtime.state_version += 1
            runtime.current_action = ""
            runtime.action_status = "IDLE"
            runtime.action_started_world_minute = None
            runtime.action_ends_world_minute = None
            runtime.active_command_id = None
            runtime.last_simulated_world_minute = world_minute
            runtime.time_revision = int(state.time_revision)
            runtime.save()
            command.status = "SUCCEEDED" if result["status"] == "SUCCEEDED" else "FAILED"
            command.result_code = result["result_code"]
            command.completed_world_minute = world_minute
            command.state_version = runtime.state_version
            command.save()
        result["state_version"] = runtime.state_version if runtime else 0
        response = dict(result)
        response["request_payload_sha256"] = digest
        event = VillageWorldEvent.objects.filter(event_id=_uuid(result.get("world_event_id"))).first()
        receipt = VillageActionReceipt.objects.create(
            request_id=request_id,
            command=command,
            npc=_npc(npc_id),
            action_kind=f"SOCIAL_{action}",
            success=result["status"] == "SUCCEEDED",
            result_code=result["result_code"],
            response_data=response,
            world_minute=world_minute,
            time_revision=int(state.time_revision),
            state_version=result["state_version"],
            world_event=event,
        )
        from .runtime_services import OutboxService
        OutboxService.ensure_outcome(receipt)
        response.pop("request_payload_sha256", None)
        return response
