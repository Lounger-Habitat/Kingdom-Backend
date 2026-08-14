"""Small phase-four read endpoints; mutations stay in phase4_social."""

from __future__ import annotations

from django.db.models import Q
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from account.authentication import GameJWTAuthentication
from account.env_auth import EnvServerJWTAuthentication
from config.auth import get_player_id

from .phase4_visibility import FORMAL_NPC_IDS


def _social_models():
    from .models import (
        NpcCommitment,
        NpcRelationship,
        NpcStoryFactAccess,
        PlayerStoryFactAccess,
    )

    return NpcRelationship, NpcCommitment, NpcStoryFactAccess, PlayerStoryFactAccess


def _label(relationship) -> str:
    if relationship.conflict >= 40:
        return "TENSE"
    if relationship.trust >= 50:
        return "TRUSTED"
    if relationship.affinity >= 30:
        return "FAMILIAR"
    if relationship.affinity <= -20 or relationship.trust <= -20:
        return "DISTANT"
    return "NEUTRAL"


@api_view(["POST"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def complete_social_action(request):
    """Authoritative mutation endpoint for structured social actions."""
    from .phase4_social import complete_social_action as settle

    result = settle(request.data)
    success = result.get("status") == "SUCCEEDED"
    return Response({"success": success, **result}, status=200 if success else 409)


@api_view(["GET"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def social_npc(request, npc_id: str):
    if npc_id not in FORMAL_NPC_IDS:
        return Response({"success": False, "resultCode": "NPC_NOT_FOUND"}, status=404)

    try:
        Relationship, Commitment, FactAccess, _ = _social_models()
        relationships = Relationship.objects.filter(
            from_npc__agent_id=npc_id,
        ).select_related("to_npc")
        commitments = Commitment.objects.filter(
            promisor_npc__agent_id=npc_id,
        ) | Commitment.objects.filter(beneficiary_npc__agent_id=npc_id)
        facts = FactAccess.objects.filter(npc__agent_id=npc_id).values_list("fact_code", flat=True)
    except ImportError:
        return Response({"success": False, "resultCode": "PHASE4_NOT_READY"}, status=503)

    relationship_data = []
    for row in relationships:
        other = row.to_npc
        has_open = commitments.filter(
            status="OPEN",
        ).filter(
            Q(promisor_npc__agent_id=npc_id, beneficiary_npc=other)
            | Q(promisor_npc=other, beneficiary_npc__agent_id=npc_id)
        ).exists()
        relationship_data.append({
            "other_npc_id": str(other.agent_id),
            "label": _label(row),
            "has_open_commitment": has_open,
        })
    commitment_data = [
        {
            "commitment_id": str(row.commitment_id),
            "commitment_type": row.commitment_type,
            "promisor_npc_id": str(row.promisor_npc.agent_id),
            "beneficiary_npc_id": str(row.beneficiary_npc.agent_id),
            "target": dict(row.target or {}),
            "status": row.status,
            "due_world_minute": row.due_world_minute,
        }
        for row in commitments.select_related("promisor_npc", "beneficiary_npc").order_by("-updated_at")[:20]
    ]
    return Response(
        {
            "success": True,
            "data": {
                "npc_id": npc_id,
                "relationships": relationship_data,
                "commitments": commitment_data,
                "fact_codes": list(facts),
            },
        }
    )


@api_view(["GET"])
@authentication_classes([GameJWTAuthentication])
@permission_classes([IsAuthenticated])
def player_facts(request):
    player_id = get_player_id(request)
    if not player_id:
        return Response({"success": False, "resultCode": "UNAUTHENTICATED"}, status=401)
    try:
        _, _, _, FactAccess = _social_models()
        fact_codes = FactAccess.objects.filter(player_id=player_id).values_list("fact_code", flat=True)
    except ImportError:
        return Response({"success": False, "resultCode": "PHASE4_NOT_READY"}, status=503)
    return Response({"success": True, "data": {"fact_codes": list(fact_codes)}})
