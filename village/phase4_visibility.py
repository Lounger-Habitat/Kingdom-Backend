"""Phase four event audience projection.

This module deliberately only projects an already-persisted ``VillageWorldEvent``
into the existing delivery outbox.  It does not own event creation, cursors, or
retry state.
"""

from __future__ import annotations

from typing import Any, Iterable

from .models import NpcRuntimeState, VillageDeliveryOutbox, VillageWorldEvent


FORMAL_NPC_IDS = ("10011", "10012", "10013", "10014", "10015")
ENV_CLIENTS = "ENV_CLIENTS"
VISIBILITIES = {"PUBLIC", "PARTICIPANTS", "PRESENT", "PLAYER_PRIVATE", "SYSTEM"}


def _ids(values: Iterable[Any] | None) -> list[str]:
    return [str(value) for value in (values or []) if value is not None]


def _visibility(event: VillageWorldEvent) -> str:
    # PRIVATE is the pre-phase-four spelling.  Persisted rows are migrated by
    # the model migration, but accepting it here keeps replay backward-safe.
    value = str(getattr(event, "visibility", "PUBLIC") or "PUBLIC").upper()
    return "PARTICIPANTS" if value == "PRIVATE" else value if value in VISIBILITIES else "SYSTEM"


def resolve_witnesses(
    location_code: str,
    participant_npc_ids: Iterable[Any] | None = None,
) -> list[str]:
    """Return observable NPCs at a location, excluding participants.

    Location and the authoritative runtime status are sufficient for phase four;
    no client-reported geometry or second audience store is used.
    """
    participants = set(_ids(participant_npc_ids))
    if not location_code:
        return []

    rows = NpcRuntimeState.objects.select_related("npc").filter(
        npc__agent_id__in=FORMAL_NPC_IDS,
        current_location_code=location_code,
    ).exclude(action_status="SLEEPING")
    return sorted(
        {
            str(row.npc.agent_id)
            for row in rows
            if str(row.npc.agent_id) not in participants
        }
    )


def _event_content(event: VillageWorldEvent, recipient: str | None = None) -> dict[str, Any]:
    visibility = _visibility(event)
    participants = _ids(getattr(event, "participant_npc_ids", []))
    witnesses = _ids(getattr(event, "witness_npc_ids", []))
    players = _ids(getattr(event, "player_ids", []))
    is_player_delivery = recipient == ENV_CLIENTS
    data = dict(getattr(event, "public_data", {}) or {})

    # private_data is only ever sent to the immutable NPC participant set.  The
    # player projection is intentionally summary-only.
    if not is_player_delivery and visibility == "PARTICIPANTS" and recipient in participants:
        data["private_data"] = dict(getattr(event, "private_data", {}) or {})

    content = {
        "schema_version": "village.event.v1",
        "event_id": str(event.event_id),
        "event_sequence": event.event_sequence,
        "event_type": event.event_type,
        "world_minute": event.world_minute,
        "time_revision": event.time_revision,
        "actor_npc_id": str(event.actor_npc.agent_id) if event.actor_npc else None,
        "participant_npc_ids": participants,
        "witness_npc_ids": witnesses,
        "location_code": event.location_code,
        "point_code": event.point_code,
        "command_id": str(event.command_id) if event.command_id else None,
        "request_id": str(event.request_id) if event.request_id else None,
        "cause_event_id": str(event.cause_event_id) if event.cause_event_id else None,
        "visibility": visibility,
        "player_ids": players if is_player_delivery else [],
        "public_status_code": (data.get("public_status_code") or ""),
        "public_data": data,
        "summary": event.player_summary if is_player_delivery else event.hub_summary,
    }
    return content


def event_payload(event: VillageWorldEvent, recipient: str | None = None) -> dict[str, Any]:
    """Build the stable event content stored in an outbox row."""
    return _event_content(event, recipient)


def _recipients(event: VillageWorldEvent) -> list[str]:
    visibility = _visibility(event)
    participants = _ids(getattr(event, "participant_npc_ids", []))
    witnesses = _ids(getattr(event, "witness_npc_ids", []))
    players = _ids(getattr(event, "player_ids", []))

    if visibility == "PUBLIC":
        recipients = list(FORMAL_NPC_IDS)
    elif visibility == "PARTICIPANTS":
        recipients = participants
    elif visibility == "PRESENT":
        recipients = list(dict.fromkeys(participants + witnesses))
    else:
        recipients = []

    if visibility != "SYSTEM" and (visibility == "PUBLIC" or players):
        recipients.append(ENV_CLIENTS)
    return list(dict.fromkeys(recipient for recipient in recipients if recipient))


def enqueue_event_deliveries(event: VillageWorldEvent) -> list[VillageDeliveryOutbox]:
    """Project event audiences to the existing outbox, idempotently.

    Callers normally invoke this inside their transaction.  The unique dedupe
    key also makes a post-commit retry safe.
    """
    entries: list[VillageDeliveryOutbox] = []
    for recipient in _recipients(event):
        dedupe_key = f"event:{event.event_id}:{recipient}"
        entry, _ = VillageDeliveryOutbox.objects.get_or_create(
            dedupe_key=dedupe_key,
            defaults={
                "recipient": recipient,
                "message_kind": "EVENT",
                "event": event,
                "payload": event_payload(event, recipient),
                "priority": 10,
                "status": "PENDING",
            },
        )
        entries.append(entry)
    return entries


def migrate_visibility(value: str) -> str:
    """Normalize the old PRIVATE spelling for callers and serializers."""
    return "PARTICIPANTS" if str(value or "").upper() == "PRIVATE" else str(value or "SYSTEM").upper()
