"""Authenticated cache reads; the conversation UI never starts model generation."""
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from account.authentication import GameJWTAuthentication
from inventory.models import NpcDefinition
from .quick_inputs import initialize_state, sanitize_phrases


@api_view(["GET"])
@authentication_classes([GameJWTAuthentication])
@permission_classes([IsAuthenticated])
def npc_quick_inputs(request):
    if "player_id" in request.query_params:
        return Response({"code": "PLAYER_ID_NOT_ACCEPTED"}, status=400)
    npc_id = request.query_params.get("npc_id", "")
    if not npc_id or len(npc_id) > 100:
        return Response({"code": "NPC_ID_REQUIRED"}, status=400)
    npc = NpcDefinition.objects.filter(agent_id=npc_id, is_test_only=False).first()
    if npc is None:
        return Response({"code": "NPC_NOT_FOUND"}, status=404)
    state = initialize_state(request.user, npc)
    from .quick_input_generation import generation_configured
    items = sanitize_phrases(state.phrases)
    status = "disabled" if not generation_configured() else (
        "pending" if state.generation_token or state.history_revision > state.generated_revision
        else "ready" if items else "empty"
    )
    return Response(dict(npc_id=npc.agent_id, version=state.version,
        updated_at=state.generated_at.isoformat() if state.generated_at else None, status=status, items=items))
