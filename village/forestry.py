"""Durable tree ownership and exactly-once wood rewards. Hits are Env-owned."""
import copy
import hashlib
import json
import math
import uuid

from django.db import models, transaction
from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.models import InventoryBag, InventorySlot, NpcGlobalInventory
from items.models import Item
from marketplace.services import get_player_main_bag
from .models import (NpcCommandState, NpcRuntimeState, VillageSimulationConfig,
                     VillageTreeNode, VillageChopAttempt, VillageForestryReceipt,
                     VillageHuntAttempt, VillageWorldEvent)

WOOD = 1002
REGION = "FOREST_LOGGING"
# Returned by bootstrap; clients and Env never choose yields or regrowth deadlines.
RULES = {"species": {"BIRCH": {"hits": 4, "wood": 4}, "PINE": {"hits": 6, "wood": 6},
                     "BROADLEAF": {"hits": 5, "wood": 5}, "DEADWOOD": {"hits": 3, "wood": 3}},
         "range": 2.5, "cooldown_seconds": 1.0, "windup_seconds": .25,
         "claim_idle_seconds": 25, "regrow_minutes": 5 * 1440}
FIELDS = {
    "register": {"species", "position"},
    "register_batch": {"trees"},
    "begin": {"generation", "attempt_id", "actor_type", "actor_id", "command_id"},
    "complete": {"generation", "attempt_id", "claim_version", "felled_world_minute"},
    "cancel": {"generation", "attempt_id", "claim_version"},
    "respawn": {"generation"},
}


class ForestryError(Exception):
    pass


def reject(code):
    raise ForestryError(code)


def identifier(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        reject("INVALID_PARAMS")


def tree_data(node):
    return dict(node_id=str(node.pk), species=node.species, position=node.position,
                generation=node.generation, status=node.status, claim_version=node.claim_version,
                attempt_id=str(node.active_attempt_id) if node.active_attempt_id else None,
                felled_world_minute=node.felled_world_minute, regrow_world_minute=node.regrow_world_minute)


def attempt_data(attempt):
    return dict(attempt_id=str(attempt.pk), node_id=str(attempt.node_id), generation=attempt.generation,
                claim_version=attempt.claim_version, actor_type=attempt.actor_type, actor_id=attempt.actor_id,
                command_id=str(attempt.command_id) if attempt.command_id else None,
                status=attempt.status, started_world_minute=attempt.started_world_minute)


def bootstrap():
    with transaction.atomic():
        clock = GlobalGameTimeState.objects.select_for_update().filter(pk=1).first()
        if clock is None:
            return dict(success=False, result_code="WORLD_NOT_READY", data={})
        # Only the current generation and active attempts, not unbounded historical receipts.
        return dict(success=True, result_code="OK", data=dict(world_minute=clock.world_minute,
            rules=copy.deepcopy(RULES), trees=[tree_data(n) for n in VillageTreeNode.objects.all()],
            attempts=[attempt_data(a) for a in VillageChopAttempt.objects.filter(status="ACTIVE")]))


def _cancel(node, attempt):
    attempt.status = "CANCELLED"
    attempt.save(update_fields=["status"])
    node.status, node.active_attempt_id = "STANDING", None
    node.save(update_fields=["status", "active_attempt_id"])


def release_command_attempts(command):
    for attempt in VillageChopAttempt.objects.select_for_update().filter(command=command, status="ACTIVE"):
        node = VillageTreeNode.objects.select_for_update().get(pk=attempt.node_id)
        if node.active_attempt_id == attempt.pk:
            _cancel(node, attempt)


def confirmed_felling(command):
    attempt = VillageChopAttempt.objects.filter(command=command, status="ACTIVE").select_related("node").first()
    return attempt if attempt and attempt.node.status == "FELLED" and attempt.node.active_attempt_id == attempt.pk else None


def settle_npc_wood(npc_id, params, world_minute, request_id):
    from .phase3_asset_service import _ok, _fail, add_batch
    command = NpcCommandState.objects.filter(request_id=request_id, kind="CHOP_TREE", npc__agent_id=str(npc_id), status="RUNNING").first()
    attempt = confirmed_felling(command) if command else None
    if attempt is None:
        return _fail("TREE_FELL_REQUIRED")
    inventory = NpcGlobalInventory.objects.select_for_update().get(npc=command.npc)
    batch = add_batch(inventory, WOOD, attempt.wood_amount, expires_world_minute=None,
                      produced_world_minute=attempt.node.felled_world_minute, source_request_id=request_id)
    inventory.asset_version += 1
    inventory.save(update_fields=["asset_version"])
    return _ok(asset_versions={str(npc_id): inventory.asset_version},
               effects={"items_added": [dict(item_id=WOOD, amount=attempt.wood_amount, instance_id=str(batch.instance_id))]},
               data={"node_id": str(attempt.node_id), "generation": attempt.generation})


def _execute(action, p, clock):
    if action == "register_batch":
        rows = p.get("trees")
        if "node_id" in p or not isinstance(rows, list) or not 1 <= len(rows) <= 64:
            reject("INVALID_PARAMS")
        if any(not isinstance(row, dict) or set(row) != {"node_id", "species", "position"} for row in rows):
            reject("INVALID_PARAMS")
        if len({identifier(row["node_id"]) for row in rows}) != len(rows):
            reject("INVALID_PARAMS")
        return dict(trees=[_execute("register", row, clock)["tree"] for row in rows])
    node_id = identifier(p.get("node_id"))
    node = VillageTreeNode.objects.select_for_update().filter(pk=node_id).first()
    if action == "register":
        species, position = p.get("species"), p.get("position")
        if not isinstance(species, str) or species not in RULES["species"] or not isinstance(position, dict) or set(position) != {"x", "y", "z"}:
            reject("INVALID_PARAMS")
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in position.values()):
            reject("INVALID_PARAMS")
        if node:
            if node.species != species or node.position != position:
                reject("TREE_ID_CONFLICT")
        else:
            node = VillageTreeNode.objects.create(node_id=node_id, species=species, position=position)
        return dict(tree=tree_data(node))
    if node is None:
        reject("TREE_NOT_FOUND")
    generation = p.get("generation")
    if type(generation) is not int or generation < 1:
        reject("INVALID_PARAMS")
    if action == "respawn":
        if generation != node.generation:
            reject("STALE_GENERATION")
        if node.status != "STUMP" or node.regrow_world_minute is None or clock.world_minute < node.regrow_world_minute:
            reject("TREE_NOT_READY")
        node.generation += 1
        node.status, node.active_attempt_id = "STANDING", None
        node.felled_world_minute = node.regrow_world_minute = None
        node.save()
        return dict(tree=tree_data(node))
    attempt_id = identifier(p.get("attempt_id"))
    attempt = VillageChopAttempt.objects.select_for_update().filter(pk=attempt_id).first()
    if action == "begin":
        actor_type, actor_id = p.get("actor_type"), p.get("actor_id")
        if not isinstance(actor_type, str) or actor_type not in {"NPC", "PLAYER"} or type(actor_id) not in (str, int):
            reject("INVALID_PARAMS")
        actor_id = str(actor_id)
        if not actor_id.isascii() or not actor_id.isdigit() or len(actor_id) > 18:
            reject("INVALID_PARAMS")
        actor_id = str(int(actor_id))
        command_id = identifier(p.get("command_id")) if actor_type == "NPC" else None
        if actor_type == "PLAYER" and p.get("command_id") is not None:
            reject("INVALID_PARAMS")
        if generation != node.generation:
            reject("STALE_GENERATION")
        if attempt:
            if (attempt.node_id, attempt.generation, attempt.actor_type, attempt.actor_id, attempt.command_id) != (node_id, generation, actor_type, actor_id, command_id):
                reject("ATTEMPT_ID_CONFLICT")
            if attempt.status != "ACTIVE":
                reject("ATTEMPT_NOT_ACTIVE")
            return dict(tree=tree_data(node), attempt=attempt_data(attempt))
        if node.status != "STANDING":
            reject("TREE_BUSY" if node.status == "CLAIMED" else "TREE_NOT_READY")
        if VillageChopAttempt.objects.filter(actor_type=actor_type, actor_id=actor_id, status="ACTIVE").exists() or VillageHuntAttempt.objects.filter(actor_type=actor_type, actor_id=actor_id, status="ACTIVE").exists():
            reject("ACTOR_BUSY")
        command = None
        if actor_type == "PLAYER":
            if not GameAccount.objects.filter(pk=int(actor_id), is_active=True, is_ai=False).exists():
                reject("PLAYER_NOT_FOUND")
        else:
            from .runtime_services import CommandService
            command = NpcCommandState.objects.select_for_update().filter(pk=command_id, npc__agent_id=actor_id).first()
            if actor_id != "10012" or command is None or command.kind != "CHOP_TREE" or command.status != "RUNNING":
                reject("PRODUCTION_COMMAND_REQUIRED")
            if command.sanitized_parameters.get("node_id") not in (None, str(node_id)):
                reject("TREE_COMMAND_MISMATCH")
            if command.started_world_minute is None or not command.duration_minutes or clock.world_minute > command.started_world_minute + command.duration_minutes:
                reject("CHOP_TIMEOUT")
            runtime = NpcRuntimeState.objects.select_for_update().get(npc=command.npc)
            if runtime.active_command_id != command.pk:
                reject("PRODUCTION_COMMAND_REQUIRED")
            error = CommandService.unavailable_reason("CHOP_TREE", runtime)
            if error:
                reject(error)
        node.claim_version += 1
        node.status, node.active_attempt_id = "CLAIMED", attempt_id
        node.save(update_fields=["claim_version", "status", "active_attempt_id"])
        attempt = VillageChopAttempt.objects.create(attempt_id=attempt_id, node=node, generation=generation,
            actor_type=actor_type, actor_id=actor_id, command=command, claim_version=node.claim_version,
            started_world_minute=clock.world_minute, wood_amount=RULES["species"][node.species]["wood"])
        return dict(tree=tree_data(node), attempt=attempt_data(attempt))
    if attempt is None or attempt.node_id != node.pk or attempt.generation != generation:
        reject("ATTEMPT_NOT_FOUND")
    if type(p.get("claim_version")) is not int or p["claim_version"] != attempt.claim_version:
        reject("STALE_CLAIM")
    if action == "complete" and attempt.status == "SUCCEEDED":
        return copy.deepcopy(attempt.result)
    if action == "cancel" and attempt.status == "CANCELLED":
        return dict(tree=tree_data(node), attempt=attempt_data(attempt))
    if generation != node.generation or node.active_attempt_id != attempt.pk or attempt.status != "ACTIVE" or node.claim_version != attempt.claim_version:
        reject("ATTEMPT_NOT_ACTIVE")
    if action == "cancel":
        _cancel(node, attempt)
        return dict(tree=tree_data(node), attempt=attempt_data(attempt))
    minute = p.get("felled_world_minute")
    if type(minute) is not int or not attempt.started_world_minute <= minute <= clock.world_minute:
        reject("INVALID_FELL_TIME")
    item = Item.objects.filter(pk=WOOD, is_deleted=False).first()
    if item is None:
        reject("WOOD_ITEM_UNAVAILABLE")
    node.status, node.felled_world_minute = "FELLED", minute
    node.save(update_fields=["status", "felled_world_minute"])
    result = None
    if attempt.actor_type == "NPC":
        from .phase3_asset_service import commit_economy_action
        from .phase3_contract import VILLAGE_ECONOMY_ACTION_V1
        command = NpcCommandState.objects.select_for_update().get(pk=attempt.command_id)
        if command.status != "RUNNING":
            reject("PRODUCTION_COMMAND_REQUIRED")
        if command.started_world_minute is None or not command.duration_minutes or minute > command.started_world_minute + command.duration_minutes:
            reject("CHOP_TIMEOUT")
        result = commit_economy_action(dict(schema_version=VILLAGE_ECONOMY_ACTION_V1,
            command_id=str(command.pk), request_id=str(command.request_id), message_id=str(command.message_id),
            command_name="CHOP_TREE", npc_id=int(attempt.actor_id), current_location_code=REGION,
            completed_world_minute=minute, time_revision=clock.time_revision, params=command.sanitized_parameters))
        if result.get("result_code") != "OK":
            reject(result.get("result_code", "SETTLEMENT_FAILED"))
        added = result.get("effects", {}).get("items_added", [])
    else:
        player = GameAccount.objects.filter(pk=int(attempt.actor_id), is_active=True, is_ai=False).first()
        if player is None:
            reject("PLAYER_NOT_FOUND")
        bag, _ = get_player_main_bag(player)
        bag = InventoryBag.objects.select_for_update().get(pk=bag.pk)
        index = bag.slots.aggregate(n=models.Max("slot_index"))["n"]
        slot = InventorySlot.objects.create(bag=bag, slot_index=0 if index is None else index+1,
            item_id=WOOD, item_amount=attempt.wood_amount, quality=0, produced_world_minute=minute,
            source_request_id=identifier(p["request_id"]), item_bag_name=item.item_name)
        added = [dict(item_id=WOOD, item_name=item.item_name, amount=attempt.wood_amount, instance_id=str(slot.instance_id))]
        VillageWorldEvent.objects.create(dedupe_key=f"forestry:{node.pk}:{generation}", event_type="PLAYER_CHOP_TREE",
            world_minute=minute, time_revision=clock.time_revision, player_ids=[attempt.actor_id],
            request_id=identifier(p["request_id"]), location_code=REGION, effects={"items_added": added},
            visibility="PUBLIC", player_summary=f"砍树获得{item.item_name}×{attempt.wood_amount}", hub_summary="完成树木砍伐")
    node.status, node.regrow_world_minute = "STUMP", minute + RULES["regrow_minutes"]
    node.save(update_fields=["status", "regrow_world_minute"])
    attempt.status, attempt.completed_world_minute = "SUCCEEDED", minute
    data = dict(tree=tree_data(node), attempt=attempt_data(attempt), items_added=added)
    if result is not None:
        data["economy_result"] = result
    attempt.result = data
    attempt.save(update_fields=["status", "completed_world_minute", "result"])
    return data


def execute(payload):
    rid = payload.get("request_id") if isinstance(payload, dict) else None
    try:
        rid = identifier(rid)
        action = payload.get("action")
        if not isinstance(action, str) or action not in FIELDS or set(payload) - ({"action", "request_id", "node_id"} | FIELDS[action]):
            reject("INVALID_PARAMS")
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with transaction.atomic():
            clock = GlobalGameTimeState.objects.select_for_update().filter(pk=1).first()
            if clock is None:
                reject("WORLD_NOT_READY")
            existing = VillageForestryReceipt.objects.filter(pk=rid).first()
            if existing:
                if existing.payload_hash != digest:
                    reject("REQUEST_CONFLICT")
                return copy.deepcopy(existing.response)
            if not VillageSimulationConfig.objects.filter(config_key="default", village_economy_enabled=True).exists():
                reject("FEATURE_DISABLED")
            try:
                with transaction.atomic():
                    data = _execute(action, payload, clock)
                response = dict(success=True, result_code="OK", request_id=str(rid), data=data)
            except ForestryError as exc:
                response = dict(success=False, result_code=str(exc), request_id=str(rid), data={})
            VillageForestryReceipt.objects.create(request_id=rid, payload_hash=digest, response=response, world_minute=clock.world_minute)
            return response
    except ForestryError as exc:
        return dict(success=False, result_code=str(exc), request_id=str(rid or ""), data={})
