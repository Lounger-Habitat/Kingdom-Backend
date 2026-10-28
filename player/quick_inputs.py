"""Player/NPC suggestion cache projected from existing, accepted speech receipts."""
import hashlib
import unicodedata

from django.db.models import Q

from account.models import GameAccount
from village.models import VillageActionReceipt
from .models import PlayerNpcQuickInputState


def sanitize_phrases(values):
    """Use stable text identities so refreshed suggestions retain local pin/hide choices."""
    if not isinstance(values, list):
        return []
    result, seen = [], set()
    for value in values[:50]:
        text = value.get("text") if isinstance(value, dict) else value
        if not isinstance(text, str):
            continue
        text = " ".join(text.split())
        try:
            if not text or len(text.encode("utf-16-le")) // 2 > 100:
                continue
            key = unicodedata.normalize("NFKC", text)
            if key in seen or any(unicodedata.category(char) == "Cc" for char in text):
                continue
            identity = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        except UnicodeError:
            continue
        seen.add(key)
        result.append(dict(id="personalized:" + identity, text=text, source="personalized"))
        if len(result) == 6:
            break
    return result


def _source(parameters):
    metadata = parameters.get("input_metadata")
    if metadata is None:
        return "manual", False
    if not isinstance(metadata, dict):
        return "unknown", False
    return metadata.get("source", "unknown"), metadata.get("edited") is True


def _autonomous(parameters):
    source, edited = _source(parameters)
    return source in {"manual", "voice"} or source in {"quick_preset", "quick_custom", "quick_personalized"} and edited


def _receipts(player_id, npc_id):
    # JSON predicates are applied in the database before any bounded history is loaded.
    return VillageActionReceipt.objects.filter(
        npc_id=npc_id, action_kind="SPEECH", success=True,
        response_data__parameters__speech_scope="player_dialogue",
        response_data__parameters__player_id=str(player_id),
        response_data__parameters__speaker_role__in=("player", "npc"),
        response_data__parameters__text__regex=r"\S",
    )


def _autonomous_count(player_id, npc_id):
    return _receipts(player_id, npc_id).filter(
        response_data__parameters__speaker_role="player",
    ).filter(
        Q(response_data__parameters__input_metadata__source__in=("manual", "voice"))
        | Q(response_data__parameters__input_metadata__source__in=("quick_preset", "quick_custom", "quick_personalized"),
            response_data__parameters__input_metadata__edited=True)
        | Q(response_data__parameters__input_metadata__isnull=True)
        | Q(response_data__parameters__input_metadata=None)
    ).count()


def _synchronize(state, *, activity_at=None):
    count = _autonomous_count(state.player_id, state.npc_id)
    query = PlayerNpcQuickInputState.objects.filter(pk=state.pk)
    # Counts come from distinct durable receipts, so repeated callbacks/replays cannot add evidence.
    query.filter(history_revision__lt=count).update(history_revision=count)
    query.filter(history_initialized=False).update(history_initialized=True)
    if activity_at is not None:
        query.filter(Q(latest_activity_at__isnull=True) | Q(latest_activity_at__lt=activity_at)).update(
            latest_activity_at=activity_at,
        )
    state.refresh_from_db()
    return state


def initialize_state(player, npc):
    state, _ = PlayerNpcQuickInputState.objects.get_or_create(player=player, npc=npc)
    if not state.history_initialized:
        latest = _receipts(player.pk, npc.pk).order_by("-created_at").values_list("created_at", flat=True).first()
        _synchronize(state, activity_at=latest)
    return state


def observe_speech_receipt(receipt):
    """Called only after an accepted receipt commits; never generates text or calls HTTP."""
    if not receipt.success or receipt.action_kind != "SPEECH" or receipt.npc_id is None:
        return
    parameters = (receipt.response_data or {}).get("parameters")
    if not isinstance(parameters, dict) or parameters.get("speech_scope") != "player_dialogue":
        return
    if parameters.get("speaker_role") not in {"player", "npc"}:
        return
    if not isinstance(parameters.get("text"), str) or not parameters["text"].strip():
        return
    identity = parameters.get("player_id")
    if isinstance(identity, bool) or not isinstance(identity, (str, int)):
        return
    try:
        identity = int(identity)
    except (TypeError, ValueError):
        return
    if identity <= 0 or identity > 9223372036854775807 or receipt.npc.is_test_only:
        return
    player = GameAccount.objects.filter(pk=identity, is_active=True).first()
    if player is None:
        return
    state, _ = PlayerNpcQuickInputState.objects.get_or_create(player=player, npc_id=receipt.npc_id)
    if not state.history_initialized or parameters.get("speaker_role") == "player" and _autonomous(parameters):
        _synchronize(state, activity_at=receipt.created_at)
    else:
        PlayerNpcQuickInputState.objects.filter(pk=state.pk).filter(
            Q(latest_activity_at__isnull=True) | Q(latest_activity_at__lt=receipt.created_at),
        ).update(latest_activity_at=receipt.created_at)


def history_for_state(state, limit=50):
    """At most 50 same-player/same-NPC accepted utterances, in chronological order."""
    limit = max(0, min(int(limit), 50))
    rows = list(_receipts(state.player_id, state.npc_id).order_by("-created_at", "-request_id")[:limit])
    result = []
    for receipt in reversed(rows):
        parameters = receipt.response_data["parameters"]
        text = parameters.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        role = parameters["speaker_role"]
        source, edited = _source(parameters)
        result.append(dict(role=role, text=text.strip()[:1000], source=source if role == "player" else "npc",
            edited=edited if role == "player" else False,
            autonomous=role == "player" and _autonomous(parameters),
            conversation_id=str(parameters.get("conversation_id", ""))[:256],
            created_at=receipt.created_at.isoformat(), receipt_id=str(receipt.pk)))
    return result
