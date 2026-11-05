"""Authoritative inventory writes: material stacks and individual equipment/dishes."""
from copy import deepcopy
import uuid

from django.db import transaction
from django.db.models import Max

from inventory.components import BATCH_COPY_FIELDS
from items.models import Item, ItemType


TOOL_TYPES = frozenset((ItemType.HOE_TOOL, ItemType.CHOP_TOOL, ItemType.BREAK_TOOL,
    ItemType.REAP_TOOL, ItemType.WATER_TOOL, ItemType.COLLECT_TOOL))


def attributes_from_slot(source):
    values = deepcopy(getattr(source, "item_attributes", {}) or {})
    values.update({name: deepcopy(getattr(source, name)) for name in
        (*BATCH_COPY_FIELDS, "item_bag_name", "hide_in_shop") if hasattr(source, name)})
    return values


def can_stack_item(item):
    if item is None or item.item_type == ItemType.FOOD or item.item_type in TOOL_TYPES:
        return False
    if item.item_type == ItemType.USABLE and item.max_use_count != 1:
        return False
    from player.models import EquipmentDefinition
    cache = getattr(getattr(item, "_state", None), "fields_cache", {})
    equipment = cache.get("equipment_definition") if "equipment_definition" in cache else None if item._state.adding else EquipmentDefinition.objects.filter(item_id=item.pk).first()
    return bool(item.stackable) and not equipment


def _individual_attributes(attributes):
    return (bool(attributes.get("dish_metadata")) or bool(attributes.get("ingredient"))
        or bool(attributes.get("rated")) or attributes.get("remaining_uses") not in (None, 1)
        or attributes.get("equipment_level", 1) != 1)


def _attributes(item, attributes):
    values = deepcopy(attributes or {})
    if "dish_quality" in values:
        values["quality"] = values.pop("dish_quality")
    values.setdefault("quality", 3)
    if not values.get("item_bag_name"):
        values["item_bag_name"] = item.item_name
    if can_stack_item(item) and not _individual_attributes(values):
        values.update(produced_world_minute=None, expires_world_minute=None,
            source_request_id=None, freshness_unknown=False)
    return values


def _add(model, scope, item, amount, attributes, instance_id=None, *, indexed=False):
    if type(amount) is not int or amount <= 0:
        raise ValueError("INVALID_ITEM_AMOUNT")
    attributes = _attributes(item, attributes)
    fields = {field.name for field in model._meta.concrete_fields}
    attributes = {name: value for name, value in attributes.items()
        if name in fields or name in BATCH_COPY_FIELDS and hasattr(model, name) or name == "source_npc_id"}
    stackable = can_stack_item(item) and not _individual_attributes(attributes)
    limit = item.max_stack_size if stackable else 1
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("INVALID_MAX_STACK_SIZE")
    remaining, result = amount, []
    if stackable:
        candidates = model.objects.select_for_update().filter(**scope, item_id=item.pk,
            quality=attributes["quality"], item_amount__gt=0).order_by("pk")
        for slot in candidates:
            if _individual_attributes({name: getattr(slot, name, None) for name in BATCH_COPY_FIELDS}):
                continue
            delta = remaining if limit is None else min(remaining, max(0, limit - slot.item_amount))
            if not delta:
                continue
            slot.item_amount += delta
            updates = ["item_amount"]
            for name, value in (("produced_world_minute", None), ("expires_world_minute", None),
                    ("source_request_id", None), ("freshness_unknown", False)):
                if hasattr(slot, name):
                    setattr(slot, name, value)
                    updates.append(name)
            if hasattr(slot, "common_attributes"):
                common = deepcopy(slot.common_attributes or {})
                for name in ("source_request_id", "produced_world_minute", "expires_world_minute", "freshness_unknown"):
                    common.pop(name, None)
                slot.common_attributes = common
                slot.source_slot_instance_id = None
                updates.extend(["common_attributes", "source_slot_instance_id"])
            slot.save(update_fields=updates)
            result.append((slot, delta))
            remaining -= delta
            if not remaining:
                return result
    maximum = model.objects.filter(**scope).aggregate(value=Max("slot_index"))["value"] if indexed else None
    index = 0 if maximum is None else maximum + 1
    while remaining:
        delta = remaining if limit is None else min(remaining, limit)
        values = dict(scope, item_id=item.pk, item_amount=delta, **attributes)
        new_id = instance_id or uuid.uuid4()
        values["instance_id"] = new_id
        if "item_bag_name" in fields:
            values["item_bag_name"] = f"{(values.get('item_bag_name') or item.item_name)[:66]}_{new_id.hex if hasattr(new_id, 'hex') else str(new_id).replace('-', '')}"
        if indexed:
            values["slot_index"] = index
            index += 1
        instance_id = None
        slot = model.objects.create(**values)
        result.append((slot, delta))
        remaining -= delta
    return result


@transaction.atomic
def add_inventory_items(owner, item_id, amount, *, attributes=None, instance_id=None):
    """Return (destination slot, added quantity), retaining existing stack IDs."""
    type(owner).objects.select_for_update().get(pk=owner.pk)
    item = Item.objects.get(pk=item_id)
    allowed = set(BATCH_COPY_FIELDS) | {"dish_quality", "item_bag_name", "hide_in_shop"}
    values = {name: value for name, value in (attributes or {}).items() if name in allowed}
    return _add(owner.slots.model, {"bag": owner}, item, amount, values, instance_id, indexed=True)


@transaction.atomic
def add_store_items(store, item_id, amount, *, listing=None,
                    attributes=None, source_instance_id=None):
    """Store stacks belong to one store and listing; reservations stay on that stack."""
    from village.models import VillageStoreInventorySlot
    type(store).objects.select_for_update().get(pk=store.pk)
    item = Item.objects.get(pk=item_id)
    values = deepcopy(attributes or {})
    legacy = values.pop("item_attributes", None)
    if legacy is not None:
        common = {key: value for key, value in legacy.items() if key not in BATCH_COPY_FIELDS and key != "dish_quality"}
        values.update({key: value for key, value in legacy.items() if key in BATCH_COPY_FIELDS or key == "dish_quality"})
        values.setdefault("common_attributes", common)
    common = values.setdefault("common_attributes", {})
    for name in ("source_request_id", "freshness_unknown", "item_bag_name", "hide_in_shop"):
        if name in values:
            value = values[name]
            common[name] = str(value) if isinstance(value, uuid.UUID) else value
    if can_stack_item(item) and not _individual_attributes(values):
        for name in ("source_request_id", "produced_world_minute", "expires_world_minute", "freshness_unknown"):
            common.pop(name, None)
        values.pop("source_slot_instance_id", None)
        source_instance_id = None
    allowed = set(BATCH_COPY_FIELDS) | {"dish_quality", "common_attributes", "source_npc", "source_npc_id", "reserved_amount", "source_slot_instance_id"}
    concrete = {field.name for field in VillageStoreInventorySlot._meta.concrete_fields}
    values = {name: value for name, value in values.items() if name in allowed and (name in concrete or name in BATCH_COPY_FIELDS or name in {"dish_quality", "source_npc_id"})}
    if source_instance_id is not None:
        values["source_slot_instance_id"] = source_instance_id
    # Store provenance lives in common_attributes; it is retained only for individual items.
    values = _attributes(item, values)
    values.pop("source_request_id", None)
    values.pop("freshness_unknown", None)
    if listing is not None:
        from village.store_quality import assign_listing
        probe = VillageStoreInventorySlot(store=store, listing=listing, item_id=item_id,
            item_amount=amount, **values)
        assign_listing(probe)
        listing = probe.listing
    return _add(VillageStoreInventorySlot, {"store": store, "listing": listing}, item, amount, values)
