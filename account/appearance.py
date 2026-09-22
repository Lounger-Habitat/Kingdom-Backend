"""First-time account appearance selection with atomic, retry-safe persistence."""
import json
from pathlib import Path

from django.db.models import F
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import GameAccount

CATALOG = json.loads(Path(__file__).with_name("appearance_catalog.json").read_text(encoding="utf-8"))
SELECTABLE = {item["id"] for item in CATALOG["models"] if not item.get("npcOnly", False)}


def appearance_profile(account):
    return {"appearance": account.appearance,
            "appearanceRevision": account.appearance_revision,
            "appearanceCatalogVersion": CATALOG["version"]}


@api_view(["GET", "PUT"])
@permission_classes([IsAuthenticated])
def player_appearance(request):
    account = GameAccount.objects.get(pk=request.user.pk)
    if request.method == "GET":
        return Response({"success": True, "data": appearance_profile(account)})
    body = request.data
    if not isinstance(body, dict):
        return Response({"success": False, "code": "INVALID_APPEARANCE"}, status=400)
    appearance = body.get("appearance")
    revision = body.get("expectedRevision")
    valid = (set(body) == {"appearance", "expectedRevision"}
             and type(revision) is int and 0 <= revision <= 2147483646
             and isinstance(appearance, dict)
             and set(appearance) == {"schemaVersion", "modelId"}
             and type(appearance.get("schemaVersion")) is int
             and appearance["schemaVersion"] == 1
             and isinstance(appearance.get("modelId"), str)
             and appearance["modelId"] in SELECTABLE)
    if not valid:
        return Response({"success": False, "code": "INVALID_APPEARANCE",
                         "message": "外观参数无效，NPC 专用模型不可选择"}, status=400)
    # Equal payload retries are idempotent, including a lost successful response.
    if account.appearance != appearance:
        changed = GameAccount.objects.filter(pk=account.pk, appearance__isnull=True, appearance_revision=revision).update(
            appearance=appearance, appearance_revision=F("appearance_revision") + 1)
        account.refresh_from_db()
        if not changed and account.appearance != appearance:
            return Response({"success": False, "code": "APPEARANCE_CONFLICT",
                             "message": "外观已保存，不可更改，请刷新确认", "data": appearance_profile(account)}, status=409)
    return Response({"success": True, "data": appearance_profile(account)})
