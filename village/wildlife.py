"""Env-owned combat facts and server-owned, exactly-once wildlife rewards.

No player action points are read or written here. Env authenticates players and
validates realtime hits; this module validates durable ownership and economics.
"""
import copy
import hashlib
import json
import math
import uuid

from django.db import models, transaction
from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.models import InventoryBag, InventorySlot
from items.models import Item
from marketplace.services import get_player_main_bag
from .models import (NpcCommandState, NpcRuntimeState, VillageHuntAttempt,
                     VillageSimulationConfig, VillageWildlife, VillageWildlifeReceipt,
                     VillageWorldEvent)
from .phase3_catalog import PREY_CHICKEN, PREY_PIG, PREY_CATTLE, HUNT_MEAT_SPOILAGE_MINUTES

SPECIES = {"CHICKEN": PREY_CHICKEN, "PIG": PREY_PIG, "CATTLE": PREY_CATTLE}
FIELDS = {
    "register": {"species", "region_id", "position"},
    "begin": {"attempt_id", "actor_type", "actor_id", "command_id"},
    "complete": {"attempt_id", "claim_version", "death_world_minute"},
    "finish": {"attempt_id", "actor_type", "actor_id", "command_id", "death_world_minute"},
    "cancel": {"attempt_id", "claim_version"},
}


class WildlifeError(Exception):
    pass


def reject(code):
    raise WildlifeError(code)


def identifier(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        reject("INVALID_PARAMS")


def prey_data(prey):
    return dict(prey_id=str(prey.pk), species=prey.species, region_id=prey.region_id,
                position=prey.position, status=prey.status, claim_version=prey.claim_version,
                attempt_id=str(prey.active_attempt_id) if prey.active_attempt_id else None,
                born_world_minute=prey.born_world_minute, death_world_minute=prey.death_world_minute)


def attempt_data(attempt):
    return dict(attempt_id=str(attempt.pk), prey_id=str(attempt.prey_id), actor_type=attempt.actor_type,
                actor_id=attempt.actor_id, command_id=str(attempt.command_id) if attempt.command_id else None,
                claim_version=attempt.claim_version, status=attempt.status,
                started_world_minute=attempt.started_world_minute)


def bootstrap():
    # Completed life tombstones are included: an Env must not recreate an animal
    # from an older local checkpoint. Actual respawns always have fresh UUIDs.
    with transaction.atomic():
        clock = GlobalGameTimeState.objects.select_for_update().filter(pk=1).first()
        return dict(success=True, result_code="OK", data=dict(
            world_minute=clock.world_minute if clock else 0, time_revision=clock.time_revision if clock else 0,
            prey=[prey_data(p) for p in VillageWildlife.objects.all().order_by("born_world_minute", "prey_id")],
            attempts=[attempt_data(a) for a in VillageHuntAttempt.objects.filter(status="ACTIVE")],
            settled_attempts=[a.result for a in VillageHuntAttempt.objects.filter(status="SUCCEEDED", actor_type="NPC")]))


def confirmed_kill(command):
    attempt_id = command.production_state.get("wildlife_attempt_id")
    if not attempt_id:
        return None
    attempt = VillageHuntAttempt.objects.select_related("prey").filter(
        attempt_id=attempt_id, command=command, status="ACTIVE", actor_type="NPC",
        actor_id=str(command.npc.agent_id), prey__status="DEAD").first()
    if not attempt or attempt.prey.active_attempt_id != attempt.pk or attempt.prey.claim_version != attempt.claim_version:
        return None
    return attempt.prey


def release_command_attempts(command):
    """Called inside the existing lifecycle transaction on terminal failure."""
    for attempt in VillageHuntAttempt.objects.select_for_update().filter(command=command, status="ACTIVE"):
        prey = VillageWildlife.objects.select_for_update().get(pk=attempt.prey_id)
        _cancel(prey, attempt)


def _cancel(prey, attempt):
    if attempt.status != "ACTIVE":
        return
    attempt.status = "CANCELLED"
    attempt.save(update_fields=["status"])
    if prey.active_attempt_id == attempt.pk and prey.status == "CLAIMED":
        prey.status, prey.active_attempt_id = "ALIVE", None
        prey.save(update_fields=["status", "active_attempt_id", "updated_at"])


def _execute(action, p, clock):
    if action == "finish":
        # Env owns damage; the final hitter claims and settles this life atomically.
        minute = p.get("death_world_minute")
        if type(minute) is not int or not 0 <= minute <= clock.world_minute:
            reject("INVALID_DEATH_TIME")
        prey = VillageWildlife.objects.select_for_update().filter(pk=identifier(p.get("prey_id"))).first()
        if prey and prey.status == "CLAIMED" and prey.active_attempt_id:
            _cancel(prey, VillageHuntAttempt.objects.select_for_update().get(pk=prey.active_attempt_id))
        hit_clock = copy.copy(clock)
        hit_clock.world_minute = minute
        begun = _execute("begin", p, hit_clock)
        return _execute("complete", dict(p, claim_version=begun["attempt"]["claim_version"]), clock)
    prey_id = identifier(p.get("prey_id"))
    prey = VillageWildlife.objects.select_for_update().filter(pk=prey_id).first()
    if action == "register":
        species, region, position = p.get("species"), p.get("region_id"), p.get("position")
        if not isinstance(species, str) or species not in SPECIES or not isinstance(region, str) or not 0 < len(region) <= 64:
            reject("INVALID_PARAMS")
        if not isinstance(position, dict) or set(position) != {"x", "y", "z"} or any(
            type(v) not in (float, int) or not math.isfinite(v) for v in position.values()
        ):
            reject("INVALID_PARAMS")
        if prey:
            if prey.species != species or prey.region_id != region:
                reject("PREY_ID_CONFLICT")
        else:
            prey = VillageWildlife.objects.create(prey_id=prey_id, species=species, region_id=region,
                position=position, born_world_minute=clock.world_minute)
        return dict(prey=prey_data(prey))
    if prey is None:
        reject("PREY_NOT_FOUND")
    attempt_id = identifier(p.get("attempt_id"))
    attempt = VillageHuntAttempt.objects.select_for_update().filter(pk=attempt_id).first()
    if action == "begin":
        actor_type, actor_id = p.get("actor_type"), p.get("actor_id")
        if not isinstance(actor_type, str) or actor_type not in {"PLAYER", "NPC"} or type(actor_id) not in (str, int):
            reject("INVALID_PARAMS")
        actor_id = str(actor_id)
        if not actor_id.isascii() or not actor_id.isdigit() or len(actor_id) > 18:
            reject("INVALID_PARAMS")
        actor_id = str(int(actor_id))
        command_id = identifier(p.get("command_id")) if actor_type == "NPC" else None
        if actor_type == "PLAYER" and p.get("command_id") is not None:
            reject("INVALID_PARAMS")
        if attempt:
            if (attempt.prey_id, attempt.actor_type, attempt.actor_id, attempt.command_id) != (prey_id, actor_type, actor_id, command_id):
                reject("ATTEMPT_ID_CONFLICT")
            if attempt.status != "ACTIVE":
                reject("ATTEMPT_NOT_ACTIVE")
            return dict(prey=prey_data(prey), attempt=attempt_data(attempt))
        if prey.status != "ALIVE":
            reject("PREY_BUSY" if prey.status == "CLAIMED" else "PREY_NOT_ALIVE")
        command = None
        if actor_type == "PLAYER":
            if not GameAccount.objects.filter(pk=int(actor_id), is_active=True, is_ai=False).exists():
                reject("PLAYER_NOT_FOUND")
        else:
            from .jack_production import is_jack_work, work_unavailable
            command = NpcCommandState.objects.select_for_update().filter(pk=command_id, npc__agent_id=actor_id).first()
            if actor_id != "10012" or not command or command.kind != "HUNT" or command.status != "RUNNING" or not is_jack_work(command):
                reject("PRODUCTION_COMMAND_REQUIRED")
            if command.started_world_minute is None or clock.world_minute > command.started_world_minute + command.duration_minutes:
                reject("HUNT_TIMEOUT")
            runtime = NpcRuntimeState.objects.select_for_update().get(npc=command.npc)
            if runtime.active_command_id != command.pk:
                reject("PRODUCTION_COMMAND_REQUIRED")
            error = work_unavailable(runtime, command, clock.world_minute)
            if error:
                reject(error)
        prey.claim_version += 1
        prey.status, prey.active_attempt_id = "CLAIMED", attempt_id
        prey.save(update_fields=["claim_version", "status", "active_attempt_id", "updated_at"])
        attempt = VillageHuntAttempt.objects.create(attempt_id=attempt_id, prey=prey,
            actor_type=actor_type, actor_id=actor_id, command=command, claim_version=prey.claim_version,
            started_world_minute=clock.world_minute)
        return dict(prey=prey_data(prey), attempt=attempt_data(attempt))
    if not attempt or attempt.prey_id != prey.pk:
        reject("ATTEMPT_NOT_FOUND")
    if "claim_version" in p and (type(p["claim_version"]) is not int or p["claim_version"] != attempt.claim_version):
        reject("STALE_CLAIM")
    if action == "complete" and attempt.status == "SUCCEEDED":
        return copy.deepcopy(attempt.result)
    if action == "cancel" and attempt.status == "CANCELLED":
        return dict(prey=prey_data(prey), attempt=attempt_data(attempt))
    if attempt.status != "ACTIVE" or prey.active_attempt_id != attempt.pk or prey.claim_version != attempt.claim_version:
        reject("ATTEMPT_NOT_ACTIVE")
    if action == "cancel":
        _cancel(prey, attempt)
        return dict(prey=prey_data(prey), attempt=attempt_data(attempt))
    death = p.get("death_world_minute", clock.world_minute)
    if type(death) is not int or not attempt.started_world_minute <= death <= clock.world_minute:
        reject("INVALID_DEATH_TIME")
    prey.death_world_minute, prey.status = death, "DEAD"
    prey.save(update_fields=["death_world_minute", "status", "updated_at"])
    if attempt.actor_type == "NPC":
        from .phase3_asset_service import commit_economy_action
        from .phase3_contract import VILLAGE_ECONOMY_ACTION_V1
        command = NpcCommandState.objects.select_for_update().get(pk=attempt.command_id)
        if command.status != "RUNNING":
            reject("PRODUCTION_COMMAND_REQUIRED")
        if death > command.started_world_minute + command.duration_minutes:
            reject("HUNT_TIMEOUT")
        command.production_state.update(wildlife_attempt_id=str(attempt.pk), prey_item_id=SPECIES[prey.species],
                                        death_world_minute=death)
        command.save(update_fields=["production_state", "updated_at"])
        result = commit_economy_action(dict(schema_version=VILLAGE_ECONOMY_ACTION_V1,
            command_id=str(command.pk), request_id=str(command.request_id), message_id=str(command.message_id),
            command_name="HUNT", npc_id=int(attempt.actor_id), current_location_code=prey.region_id,
            completed_world_minute=death, time_revision=clock.time_revision, params=command.sanitized_parameters))
        if result.get("result_code") != "OK":
            reject(result.get("result_code", "SETTLEMENT_FAILED"))
        added = result.get("effects", {}).get("items_added", [])
    else:
        player = GameAccount.objects.filter(pk=int(attempt.actor_id), is_active=True, is_ai=False).first()
        if not player:
            reject("PLAYER_NOT_FOUND")
        bag, _ = get_player_main_bag(player)
        bag = InventoryBag.objects.select_for_update().get(pk=bag.pk)
        item_id = SPECIES[prey.species]
        item = Item.objects.filter(pk=item_id, is_deleted=False).first()
        if item is None:
            reject("PREY_ITEM_UNAVAILABLE")
        index = bag.slots.aggregate(n=models.Max("slot_index"))["n"]
        slot = InventorySlot.objects.create(bag=bag, slot_index=0 if index is None else index+1,
            item_id=item_id, item_amount=1, quality=3, produced_world_minute=death,
            expires_world_minute=death+HUNT_MEAT_SPOILAGE_MINUTES, source_request_id=identifier(p['request_id']),
            item_bag_name=item.item_name)
        added = [dict(item_id=item_id, item_name=item.item_name, amount=1, instance_id=str(slot.instance_id),
                      produced_world_minute=death, expires_world_minute=slot.expires_world_minute)]
        result = None
        VillageWorldEvent.objects.create(dedupe_key=f"wildlife:{prey.pk}", event_type="PLAYER_WILDLIFE_HUNT",
            world_minute=death, time_revision=clock.time_revision, player_ids=[attempt.actor_id],
            request_id=identifier(p['request_id']), location_code=prey.region_id, effects={"items_added": added},
            visibility="PUBLIC", player_summary=f"猎获{item.item_name}×1", hub_summary=f"猎获{item.item_name}×1")
    prey.status, attempt.status = "CLAIMED_REWARD", "SUCCEEDED"
    prey.save(update_fields=["status", "updated_at"])
    attempt.completed_world_minute = death
    data = dict(prey=prey_data(prey), attempt=attempt_data(attempt), items_added=added)
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
        if not isinstance(action, str) or action not in FIELDS or set(payload) - ({"action", "request_id", "prey_id"} | FIELDS[action]):
            reject("INVALID_PARAMS")
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with transaction.atomic():
            clock = GlobalGameTimeState.objects.select_for_update().filter(pk=1).first()
            if clock is None:
                reject("WORLD_NOT_READY")
            existing = VillageWildlifeReceipt.objects.filter(pk=rid).first()
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
            except WildlifeError as exc:
                response = dict(success=False, result_code=str(exc), request_id=str(rid), data={})
            VillageWildlifeReceipt.objects.create(request_id=rid, payload_hash=digest, response=response,
                                                  world_minute=clock.world_minute)
            return response
    except WildlifeError as exc:
        return dict(success=False, result_code=str(exc), request_id=str(rid or ""), data={})
