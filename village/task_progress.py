"""Read-only participant/history decoration for the player's task progress UI."""
from collections import defaultdict
from datetime import timedelta

from django.db.models import F, Q
from django.utils import timezone

from account.models import GameAccount
from inventory.models import NpcDefinition
from .models import (NpcCommandState, VillageAffairContribution,
                     VillagePersonalCommissionReceipt, VillagePlayerAffairLabor)
from .player_affair_labor import LEASE_SECONDS


def _conditions(task):
    return [condition for stage in task.get("stages", []) for condition in stage.get("conditions", [])]


def _name_records(records, *, npc_primary_key=False):
    player_ids = {r["actor_id"] for r in records if r["actor_type"] == "PLAYER" and r["actor_id"].isdigit()}
    npc_ids = {r["actor_id"] for r in records if r["actor_type"] == "NPC"}
    players = {str(pk): name for pk, name in GameAccount.objects.filter(pk__in=player_ids).values_list("pk", "username")}
    key = "pk" if npc_primary_key else "agent_id"
    if npc_primary_key:
        npc_ids = {value for value in npc_ids if value.isdigit()}
    npcs = {str(pk): name for pk, name in NpcDefinition.objects.filter(**{key + "__in": npc_ids}).values_list(key, "name")}
    for record in records:
        record["actor_name"] = (players if record["actor_type"] == "PLAYER" else npcs).get(record["actor_id"], "参与者未记录")


def _commission_records(commissions):
    """Successful receipts contain monotonic condition counters, including the ACCEPT baseline.

    Diff consecutive snapshots, not the final commission time: each condition can
    finish on a different date. No guess is made if the initial snapshot is missing.
    """
    previous, records = {}, []
    for receipt in VillagePersonalCommissionReceipt.objects.filter(
            commission_id__in=commissions).order_by("pk").iterator():
        task_id = str(receipt.commission_id)
        counters = receipt.response.get("progress", {}).get("conditions", {})
        baseline = previous.get(task_id)
        if baseline is not None and receipt.action in {"CONTRIBUTE", "LABOR"}:
            for condition_id, quantity in counters.items():
                delta = quantity - baseline.get(condition_id, 0)
                if delta > 0:
                    records.append(dict(task_id=task_id, condition_id=condition_id,
                        actor_type=receipt.actor_type, actor_id=receipt.actor_id,
                        quantity=delta, world_minute=receipt.world_minute))
        previous[task_id] = counters
    _name_records(records, npc_primary_key=True)
    return records


def decorate_task_progress(affairs, commissions):
    """Decorate already-authorized overview tasks; never write or advance game state."""
    tasks = {("affair", task["affair_id"]): task for task in affairs}
    tasks.update({("commission", task["commission_id"]): task for task in commissions})
    conditions = {(kind, task_id, condition["condition_id"]): condition
                  for (kind, task_id), task in tasks.items() for condition in _conditions(task)}
    affair_ids = [key for kind, key in tasks if kind == "affair"]
    commission_ids = [key for kind, key in tasks if kind == "commission"]
    entries = defaultdict(list)
    public_records = list(VillageAffairContribution.objects.filter(affair_id__in=affair_ids)
        .order_by("pk").values("affair_id", "condition_id", "actor_type", "actor_id", "quantity", "world_minute"))
    _name_records(public_records)
    for record in public_records:
        entries[("affair", record.pop("affair_id"), record.pop("condition_id"))].append(record)
    for record in _commission_records(commission_ids):
        entries[("commission", record.pop("task_id"), record.pop("condition_id"))].append(record)

    for key, condition in conditions.items():
        history = entries[key]
        contributors = {}
        total, completed_at = 0, None
        for entry in history:
            actor = (entry["actor_type"], entry["actor_id"])
            if actor not in contributors:
                contributors[actor] = {k: entry[k] for k in ("actor_type", "actor_id", "actor_name")}
                contributors[actor]["quantity"] = 0
            contributors[actor]["quantity"] += entry["quantity"]
            total += entry["quantity"]
            if total >= condition["target"] and completed_at is None:
                completed_at = entry["world_minute"]
        history_complete = total == condition["submitted"]
        complete = condition["target"] > 0 and condition["submitted"] >= condition["target"]
        condition.update(contributors=list(contributors.values()), contributions=history,
            history_complete=history_complete,
            completed_world_minute=completed_at if complete and history_complete else None,
            active_workers=[])

    def add_worker(key, actor_type, actor_id, name):
        condition = conditions.get(key)
        task = tasks.get(key[:2])
        if (not condition or not task or task["status"] != "ACTIVE"
                or condition["type"] != "labor" or not condition.get("available")
                or condition["submitted"] >= condition["target"]):
            return
        worker = dict(actor_type=actor_type, actor_id=str(actor_id), actor_name=name)
        if worker not in condition["active_workers"]:
            condition["active_workers"].append(worker)

    # Expired presence must not appear active just because its next tick has not run yet.
    sessions = VillagePlayerAffairLabor.objects.filter(status="ACTIVE",
        last_seen_at__gte=timezone.now() - timedelta(seconds=LEASE_SECONDS)).filter(
            Q(affair_id__in=affair_ids) | Q(commission_id__in=commission_ids)).select_related("player").order_by("player_id")
    for session in sessions:
        kind, task_id = ("commission", str(session.commission_id)) if session.commission_id else ("affair", session.affair_id)
        add_worker((kind, task_id, session.condition_id), "PLAYER", session.player_id, session.player.username)
    commands = NpcCommandState.objects.filter(kind="CONTRIBUTE_TO_AFFAIR", status="RUNNING",
        started_world_minute__isnull=False, npc__village_runtime__active_command_id=F("command_id"),
        npc__village_runtime__action_status="ACTING").select_related("npc").order_by("npc_id")
    for command in commands:
        params = command.sanitized_parameters
        if params.get("kind") == "labor":
            add_worker(("affair", params.get("affair_id"), params.get("condition_id")),
                       "NPC", command.npc.agent_id, command.npc.name)
