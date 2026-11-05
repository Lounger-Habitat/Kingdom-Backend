"""Explicit, transactional conversion of legacy batches to authoritative stacks.

Completed receipts and ledger snapshots are never rewritten. Empty source rows
remain in place for protected historical foreign keys.
"""
from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import hmac
import json
import uuid

from django.conf import settings
from django.db import transaction
from django.db.models import Max

from .components import BATCH_COPY_FIELDS
from .models import InventoryBag, InventorySlot, NpcGlobalInventory, NpcGlobalInventorySlot
from .storage import attributes_from_slot, can_stack_item, _individual_attributes
from items.models import Item


def _purchase_signature(identity):
    return hmac.new(settings.SECRET_KEY.encode(), json.dumps(identity, sort_keys=True).encode(), hashlib.sha256).hexdigest()


def purchase_source_identity(player, slot):
    """Verify a migration link, then let the original purchase receipt prove cost."""
    token = (slot.dish_metadata or {}).get("legacy_purchase_identity") or {}
    if not isinstance(token, dict):
        return str(slot.instance_id)
    identity = {key: token.get(key) for key in ("player_id", "item_id", "quality", "instance_id")}
    signature = token.get("signature")
    if (identity["player_id"] == str(player.pk) and identity["item_id"] == slot.item_id
            and identity["quality"] == slot.quality and isinstance(identity["instance_id"], str)
            and isinstance(signature, str) and signature.isascii()
            and hmac.compare_digest(signature, _purchase_signature(identity))):
        return identity["instance_id"]
    return str(slot.instance_id)


def _claims(rows, model):
    from village.models import NpcCommandState, NpcTradeDelivery, StarterSupplyDelivery, VillagePlayerProcessingJob
    ids = [row.instance_id for row in rows]
    claims = []
    if model is NpcGlobalInventorySlot:
        for delivery in NpcTradeDelivery.objects.select_for_update().filter(
                status="ACTIVE", seller_slot_instance_id__in=ids):
            claims.append((delivery.reserved_amount, delivery, "seller_slot_instance_id"))
        for command in NpcCommandState.objects.select_for_update().filter(
                npc_id=rows[0].bag.npc_id, status__in=["RUNNING", "SETTLING"]):
            state = command.production_state or {}
            for key in ("prey_instance_id", "tool_instance_id"):
                if state.get(key) in {str(value) for value in ids}:
                    claims.append((1, command, key))
        for job in VillagePlayerProcessingJob.objects.select_for_update().filter(
                status__in=["QUEUED", "RUNNING", "SETTLING"], tool_instance_id__in=ids):
            claims.append((1, job, "tool_instance_id"))
    elif model is InventorySlot:
        for delivery in StarterSupplyDelivery.objects.select_for_update().filter(
                status="HELD", slot_id__in=[row.pk for row in rows]):
            claims.append((delivery.quantity, delivery, "slot"))
    return claims


def _bind(obj, key, row):
    from village.models import NpcCommandState
    if isinstance(obj, NpcCommandState):
        state = deepcopy(obj.production_state)
        state[key] = str(row.instance_id)
        obj.production_state = state
        obj.save(update_fields=["production_state"])
    else:
        setattr(obj, key, row if key == "slot" else row.instance_id)
        obj.save(update_fields=[key])


def _new_row(source):
    model = type(source)
    values = {field.name: deepcopy(getattr(source, field.name)) for field in model._meta.concrete_fields
              if not field.primary_key and field.name not in {"instance_id", "slot_index"}}
    values["instance_id"] = uuid.uuid4()
    if hasattr(source, "slot_index"):
        maximum = model.objects.filter(bag=source.bag).aggregate(n=Max("slot_index"))["n"]
        values["slot_index"] = 0 if maximum is None else maximum + 1
        values["item_bag_name"] = f"{source.item_id}_{values['instance_id']}"
    row = model(**values)
    for name in BATCH_COPY_FIELDS:
        if hasattr(source, name):
            setattr(row, name, deepcopy(getattr(source, name)))
    return row


def _clear_material_origin(row):
    for name, value in (("produced_world_minute", None), ("expires_world_minute", None),
                        ("source_request_id", None), ("freshness_unknown", False)):
        if hasattr(row, name):
            setattr(row, name, value)
    if hasattr(row, "common_attributes"):
        row.common_attributes = {key: value for key, value in (row.common_attributes or {}).items()
            if key not in {"source_request_id", "source_player_id", "produced_world_minute",
                           "expires_world_minute", "freshness_unknown"}}
        row.source_slot_instance_id = None


def _merge(rows, item, counts):
    total = sum(row.item_amount for row in rows)
    limit = total if item.max_stack_size is None else item.max_stack_size
    if type(limit) is not int or limit <= 0:
        raise ValueError(f"Invalid stack limit: {item.pk}")
    claims = sorted(_claims(rows, type(rows[0])), key=lambda claim: claim[0], reverse=True)
    if sum(amount for amount, _, _ in claims) > total or any(amount > limit for amount, _, _ in claims):
        raise ValueError(f"Active reservation cannot fit stack limit: {type(rows[0]).__name__}/{rows[0].pk}")
    amounts, bindings = [], []
    for amount, obj, key in claims:
        index = next((i for i, used in enumerate(amounts) if used + amount <= limit), len(amounts))
        if index == len(amounts):
            amounts.append(0)
        amounts[index] += amount
        bindings.append((obj, key, index))
    remaining = total - sum(amounts)
    for index in range(len(amounts)):
        extra = min(remaining, limit - amounts[index])
        amounts[index] += extra
        remaining -= extra
    while remaining:
        amount = min(remaining, limit)
        amounts.append(amount)
        remaining -= amount
    reserved = sum(getattr(row, "reserved_amount", 0) for row in rows)
    if reserved > total:
        raise ValueError(f"Reserved store amount exceeds stock: {rows[0].pk}")
    targets = []
    for index, amount in enumerate(amounts):
        row = rows[index] if index < len(rows) else _new_row(rows[0])
        row.item_amount = amount
        if hasattr(row, "reserved_amount"):
            row.reserved_amount = min(reserved, amount)
            reserved -= row.reserved_amount
        _clear_material_origin(row)
        row.save()
        targets.append(row)
    for row in rows[len(amounts):]:
        row.item_amount = 0
        if hasattr(row, "reserved_amount"):
            row.reserved_amount = 0
        row.save(update_fields=["item_amount"] + (["reserved_amount"] if hasattr(row, "reserved_amount") else []))
        counts["emptied_material_rows"] += 1
    for obj, key, index in bindings:
        _bind(obj, key, targets[index])
        counts["active_references_bound"] += 1
    counts["material_groups"] += 1
    counts["material_quantity"] += total


def _split(row, counts):
    from restaurant.models import MenuItem
    original_amount = row.item_amount
    if original_amount <= 1:
        return
    menu = MenuItem.objects.select_for_update().filter(batch_slot_id=row.pk).first() if isinstance(row, InventorySlot) else None
    if menu and menu.quantity != original_amount:
        raise ValueError(f"Menu quantity differs from held stock: {menu.pk}")
    if getattr(row, "reserved_amount", 0) > 1:
        raise ValueError(f"Individual store item has multiple reserved units: {row.pk}")
    claims = _claims([row], type(row))
    if sum(amount for amount, _, _ in claims) > original_amount or any(amount != 1 for amount, _, _ in claims):
        raise ValueError(f"Individual reservation needs manual review: {type(row).__name__}/{row.pk}")
    if isinstance(row, InventorySlot) and row.bag.player_id and Item.objects.get(pk=row.item_id).item_type == "食物":
        identity = dict(player_id=str(row.bag.player_id), item_id=row.item_id, quality=row.quality,
                        instance_id=purchase_source_identity(row.bag.player, row))
        row.dish_metadata = dict(row.dish_metadata or {}, legacy_purchase_identity=dict(
            identity, signature=_purchase_signature(identity)))
    row.item_amount = 1
    row.save()
    if menu:
        menu.quantity = 1
        menu.save(update_fields=["quantity"])
    targets = [row]
    for _ in range(original_amount - 1):
        extra = _new_row(row)
        if hasattr(extra, "reserved_amount"):
            extra.reserved_amount = 0
        extra.save()
        targets.append(extra)
        if menu:
            values = {field.name: deepcopy(getattr(menu, field.name)) for field in MenuItem._meta.concrete_fields
                      if not field.primary_key and field.name not in {"batch_slot", "instance_id"}}
            MenuItem.objects.create(**values, batch_slot=extra, instance_id=str(extra.instance_id))
        counts["individual_rows_created"] += 1
    for (_, obj, key), target in zip(claims, targets):
        _bind(obj, key, target)
        counts["active_references_bound"] += 1


@transaction.atomic
def normalize_owner(owner):
    """Normalize one locked owner; callers can include it in a larger edit transaction."""
    from village.models import VillageStoreInventorySlot, VillagePlayerProcessingJob
    owner = type(owner).objects.select_for_update().get(pk=owner.pk)
    slot_model = InventorySlot if isinstance(owner, InventoryBag) else (
        NpcGlobalInventorySlot if isinstance(owner, NpcGlobalInventory) else VillageStoreInventorySlot)
    owner_key = "bag" if slot_model in {InventorySlot, NpcGlobalInventorySlot} else "store"
    counts, groups, before = Counter(), defaultdict(list), Counter()
    state_fields = ["pk", "item_id", "quality", "item_amount", "produced_world_minute", "expires_world_minute"]
    state_fields += ["source_request_id", "freshness_unknown"] if owner_key == "bag" else ["common_attributes", "source_slot_instance_id", "reserved_amount"]
    before_state = list(slot_model.objects.filter(**{owner_key: owner}).order_by("pk").values_list(*state_fields))
    rows = list(slot_model.objects.select_for_update().filter(**{owner_key: owner}, item_amount__gt=0).order_by("pk"))
    items = {item.pk: item for item in Item.objects.filter(pk__in={row.item_id for row in rows})}
    protected_inputs = set(VillagePlayerProcessingJob.objects.filter(
        status__in=["QUEUED", "RUNNING", "SETTLING"], input_slot_id__in=[row.pk for row in rows]
    ).values_list("input_slot_id", flat=True)) if slot_model is InventorySlot else set()
    for row in rows:
        before[(row.item_id, row.quality)] += row.item_amount
        if row.pk in protected_inputs:
            counts["active_processing_inputs_preserved"] += 1
            continue
        item = items.get(row.item_id)
        if item is None:
            raise ValueError(f"Missing item catalog entry: {row.item_id}")
        if can_stack_item(item) and not _individual_attributes(attributes_from_slot(row)):
            groups[(row.item_id, row.quality, getattr(row, "listing_id", None))].append(row)
        else:
            _split(row, counts)
    for (item_id, _, _), group in groups.items():
        _merge(group, items[item_id], counts)
    after = Counter()
    for item_id, quality, amount in slot_model.objects.filter(**{owner_key: owner}, item_amount__gt=0).values_list("item_id", "quality", "item_amount"):
        after[(item_id, quality)] += amount
    if before != after:
        raise ValueError(f"Item conservation check failed: {type(owner).__name__}/{owner.pk}")
    after_state = list(slot_model.objects.filter(**{owner_key: owner}).order_by("pk").values_list(*state_fields))
    if before_state != after_state:
        version_field = "asset_version" if isinstance(owner, NpcGlobalInventory) else "inventory_version" if owner_key == "store" else None
        if version_field:
            setattr(owner, version_field, getattr(owner, version_field) + 1)
            owner.save(update_fields=[version_field])
        counts["owners_changed"] += 1
    counts["owners_checked"] += 1
    return dict(counts)


@transaction.atomic
def normalize_inventory(*, apply=False):
    """Dry-run by default; apply the exact same work only on explicit request."""
    from game_time.models import GlobalGameTimeState
    from village.models import VillageStore
    GlobalGameTimeState.objects.select_for_update().filter(pk=1).first()
    counts = Counter()
    for owner_model in (InventoryBag, NpcGlobalInventory, VillageStore):
        for owner in owner_model.objects.select_for_update().order_by("pk"):
            counts.update(normalize_owner(owner))
    if not apply:
        transaction.set_rollback(True)
    return dict(counts)
