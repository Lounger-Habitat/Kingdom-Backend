"""Persistent player cooking process. The existing cook_dish transaction owns settlement."""
from copy import deepcopy
from datetime import timedelta
import uuid

from django.utils import timezone

from .player_cooking_models import VillagePlayerCookingSession
from .player_cooking_technique import HEATS, advance


def project_session(row, now=None):
    if row is None:
        return None
    now = now or timezone.now()
    status = "EXPIRED" if row.status == "ACTIVE" and row.expires_at <= now else row.status
    elapsed = max(0, (now - row.started_at).total_seconds()) if status == "ACTIVE" else row.state.get("elapsed_seconds", 0)
    state = advance(row.state, row.plan["technique"], elapsed) if status == "ACTIVE" else row.state
    preview, parameters = row.plan["preview"], row.plan["parameters"]
    return dict(session_id=str(row.session_id), status=status, cut_style=row.plan["cut_style"],
        progress=round(state.get("progress", 0), 3), heat=state.get("heat", "HIGH"),
        elapsed_seconds=round(state.get("elapsed_seconds", 0), 3), observed_at=now.isoformat(),
        expires_at=row.expires_at.isoformat(), technique=deepcopy(row.plan["technique"]),
        recipe_id=preview.get("recipe_id"), recipe_name=preview.get("recipe_name"),
        batches=parameters.get("batches", 1), output_quantity=preview.get("output_quantity"),
        tool_instance_id=parameters.get("tool_instance_id"), selections=deepcopy(preview.get("selected_batches", [])))


def active_session(player):
    row = VillagePlayerCookingSession.objects.filter(player=player, status="ACTIVE").first()
    data = project_session(row)
    return data if data and data["status"] == "ACTIVE" else None


def owned_session(player, identifier):
    from .player_cooking import reject, _uuid
    row = VillagePlayerCookingSession.objects.select_for_update().filter(player=player, session_id=_uuid(identifier)).first()
    if row is None:
        reject("COOKING_SESSION_NOT_FOUND", "这锅料理不属于本人或会话已变化，请重新备料。")
    return row


def advance_active(row, now):
    from .player_cooking import reject
    if row.status == "FINISHED":
        reject("COOKING_SESSION_FINISHED", "这锅料理已经出锅并结算，请查看背包，不会再次扣料。")
    if row.status != "ACTIVE":
        reject("COOKING_SESSION_CLOSED", "这锅料理已经结束，请重新备料。")
    if row.expires_at <= now:
        reject("COOKING_SESSION_EXPIRED", "这锅料理已超时，未扣食材，请重新备料。")
    row.state = advance(row.state, row.plan["technique"], (now - row.started_at).total_seconds())


def control_session(player, bag, action, params, minute):
    from .player_cooking import _prepare, reject
    now = timezone.now()
    if action == "start_cooking":
        parameters = {key: value for key, value in params.items() if key != "expected_quote"}
        plan = _prepare(player, bag, parameters, minute, execute=True)
        if params.get("expected_quote") != plan["preview"]["quote_hash"]:
            reject("QUOTE_CHANGED", "食材、炊具或配方已变化，请重新预览备料。")
        row = VillagePlayerCookingSession.objects.select_for_update().filter(player=player).first()
        if row and row.status == "ACTIVE" and row.expires_at > now:
            reject("COOKING_SESSION_ACTIVE", "已有一锅料理正在烹饪，请先出锅或取消。")
        frozen = dict(parameters=deepcopy(parameters), expected_quote=params["expected_quote"],
            cut_style=parameters.get("cut_style", "WHOLE"), technique=deepcopy(plan["preview"]["technique"]),
            preview=deepcopy(plan["preview"]))
        values = dict(session_id=uuid.uuid4(), plan=frozen,
            state=dict(progress=0, elapsed_seconds=0, heat="HIGH", heat_error=0), status="ACTIVE",
            started_at=now, updated_at=now, expires_at=now + timedelta(seconds=120), settlement_request_id=None)
        if row is None:
            row = VillagePlayerCookingSession.objects.create(player=player, **values)
        else:
            for key, value in values.items():
                setattr(row, key, value)
            row.save()
    else:
        row = owned_session(player, params.get("session_id"))
        if action == "cancel_cooking":
            if row.status == "ACTIVE":
                row.state = advance(row.state, row.plan["technique"], (now - row.started_at).total_seconds())
                row.status = "EXPIRED" if row.expires_at <= now else "CANCELLED"
                row.updated_at = now
                row.save(update_fields=["state", "status", "updated_at"])
        else:
            if not isinstance(params.get("heat"), str) or params.get("heat") not in HEATS:
                reject("INVALID_PARAMS", "火力请选择小火、中火或大火。")
            advance_active(row, now)
            # Elapsed integration, never click count, decides fire-control quality.
            row.state["heat"] = params["heat"]
            row.updated_at = now; row.expires_at = now + timedelta(seconds=120)
            row.save(update_fields=["state", "updated_at", "expires_at"])
    return dict(cooking_session=project_session(row, now))
