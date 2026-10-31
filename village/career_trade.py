"""Local-production evidence and bounded merchant demand for career goods.

This module only reads existing assets and receipts. A forecast is a small
procurement opportunity, never a reservation of another character's inventory.
"""
import uuid

from django.db.models import F

from inventory.models import InventorySlot, NpcGlobalInventorySlot
from items.quality import valid_quality

from .career_catalog import (CROP_RULES, RECIPES, PREY_OUTPUTS,
    NEW_CONTENT_ITEM_IDS, DISH_ITEM_IDS, MEAT_ITEM_IDS, BYPRODUCT_ITEM_IDS)
from .models import NpcRuntimeState, VillageFarmPlot
from .phase3_asset_service import get_available_slot_amount

THEMES = (("FARM", "农产收购"), ("MEAT", "肉类收购"),
          ("BYPRODUCT", "副产品收购"), ("MEALS", "餐食收购"))


def _source_ids(slot, *, trace=True):
    attributes = getattr(slot, "item_attributes", {}) or {}
    dish = getattr(slot, "dish_metadata", {}) or {}
    values = [dish.get("production_request_id"), attributes.get("source_request_id"),
              getattr(slot, "source_request_id", None)]
    original = getattr(slot, "source_slot_instance_id", None)
    if trace and original:
        source = NpcGlobalInventorySlot.objects.filter(instance_id=original).first()
        source = source or InventorySlot.objects.filter(instance_id=original).first()
        if source is not None:
            values.extend(_source_ids(source, trace=False))
    result = []
    for value in values:
        try:
            parsed = uuid.UUID(str(value))
        except (ValueError, TypeError, AttributeError):
            continue
        if parsed not in result:
            result.append(parsed)
    return result


def source_request_id(slot, fallback=None):
    """Keep a batch's production source across a store/owner transfer."""
    values = _source_ids(slot)
    return values[0] if values else fallback


def transfer_attributes(slot):
    from .phase3_asset_service import BATCH_ITEM_ATTRIBUTES
    attributes = {key: getattr(slot, key) for key in BATCH_ITEM_ATTRIBUTES}
    source = source_request_id(slot)
    if source:
        attributes["source_request_id"] = str(source)
    return attributes


def is_local_production(slot):
    from .career_development import has_production_evidence
    return any(has_production_evidence(source, slot.item_id) for source in _source_ids(slot))


def record_sale(actor_type, actor_id, request_id, batches, world_minute,
                *, income, external=False, source=""):
    """Only paid sales of verified career output advance a milestone."""
    if income <= 0:
        return
    from .career_development import record_business_evidence
    items, sources = {}, set()
    for slot, amount in batches:
        if slot.item_id not in NEW_CONTENT_ITEM_IDS or amount <= 0 or not is_local_production(slot):
            continue
        items[slot.item_id] = items.get(slot.item_id, 0) + amount
        sources.update(str(value) for value in _source_ids(slot))
    if items:
        record_business_evidence("SALE", request_id, actor_type, str(actor_id), items,
            world_minute, metadata=dict(income=income, external=external, source=source,
                provenance_valid=True, source_request_ids=sorted(sources)))


def item_theme(item_id):
    from .phase3_catalog import (FARM_CROP_RULES, HUNT_MEAT_ITEM_IDS,
        BYPRODUCT_ITEM_IDS as LEGACY_BYPRODUCTS)
    if item_id in DISH_ITEM_IDS or item_id in {1053, 1067}:
        return "MEALS"
    if item_id in BYPRODUCT_ITEM_IDS or item_id in LEGACY_BYPRODUCTS:
        return "BYPRODUCT"
    if item_id in MEAT_ITEM_IDS or item_id in HUNT_MEAT_ITEM_IDS or item_id == 29107:
        return "MEAT"
    if item_id in {row["output_item_id"] for row in FARM_CROP_RULES.values()}:
        return "FARM"
    return "BYPRODUCT"


def local_stock(item_id, minute):
    """Count qualified local batches, retaining private holdings on the server."""
    from .models import VillageStoreInventorySlot
    rows = list(NpcGlobalInventorySlot.objects.filter(item_id=item_id, item_amount__gt=0,
        bag__npc__is_test_only=False, bag__npc__agent_id__in=["10011", "10012", "10013", "10014", "10015"]))
    rows += list(InventorySlot.objects.filter(item_id=item_id, item_amount__gt=0,
        bag__player__is_active=True, bag__player__is_ai=False,
        bag__character_id=F("bag__player__username")))
    rows += list(VillageStoreInventorySlot.objects.filter(item_id=item_id, item_amount__gt=0))
    total = 0
    for slot in rows:
        if not valid_quality(slot.quality) or not is_local_production(slot) or (slot.produced_world_minute or 0) > minute:
            continue
        if isinstance(slot, NpcGlobalInventorySlot):
            total += get_available_slot_amount(slot, minute)
        else:
            total += max(0, slot.item_amount - getattr(slot, "reserved_amount", 0))
    return total


def _npc_resources(npc_id, minute):
    runtime = NpcRuntimeState.objects.filter(npc__agent_id=npc_id, npc__is_test_only=False).first()
    if not runtime or runtime.health_fp <= 300 or runtime.action_status == "AWAY":
        return {}
    amounts = {}
    for slot in NpcGlobalInventorySlot.objects.filter(bag__npc__agent_id=npc_id, item_amount__gt=0):
        amount = get_available_slot_amount(slot, minute)
        if slot.remaining_uses is not None and slot.remaining_uses <= 0:
            amount = 0
        amounts[slot.item_id] = amounts.get(slot.item_id, 0) + amount
    return amounts


def forecast_supply(item_id, minute, deadline):
    """A maximum four-unit forecast based on actual inputs, never an Agent plan."""
    from .career_development import content_access
    access = content_access(item_id)
    for code, rule in CROP_RULES.items():
        if rule["output_item_id"] != item_id:
            continue
        growing = VillageFarmPlot.objects.filter(crop_code=code, status="GROWING",
            reclamation_status="READY", mature_world_minute__lte=deadline - 30).count()
        if growing:
            # Already paid and reserved crops remain deliverable when all trial
            # quota is in the ground; only another planting needs spare quota.
            return min(4, growing * rule["yield_min"])
        if not access["can_produce"]:
            return 0
        limit = 4 if access["unlocked"] else min(4, access.get("trial_remaining", 0))
        from .farm_irrigation import effective_growth_minutes
        if minute + effective_growth_minutes(rule["growth_minutes"]) + 60 > deadline:
            return 0
        resources = _npc_resources("10011", minute)
        empty = VillageFarmPlot.objects.filter(status="EMPTY", reclamation_status="READY",
            lease_enabled=False, lessee_player__isnull=True).exists()
        return min(limit, rule["yield_min"]) if empty and resources.get(rule["seed_item_id"], 0) and resources.get(4001, 0) else 0
    if not access["can_produce"]:
        return 0
    limit = 4 if access["unlocked"] else min(4, access.get("trial_remaining", 0))
    if item_id in MEAT_ITEM_IDS or item_id in BYPRODUCT_ITEM_IDS:
        resources = _npc_resources("10012", minute)
        if not resources.get(4002, 0) or deadline - minute < 180:
            return 0
        possible = sum(resources.get(prey, 0) * outputs.get(item_id, 0)
                       for prey, outputs in PREY_OUTPUTS.items())
        return min(limit, possible)
    recipe = next((row for row in RECIPES.values() if row["item_id"] == item_id), None)
    if recipe and deadline - minute >= 60:
        resources = _npc_resources("10014", minute)
        batches = min((resources.get(i, 0) // n for i, n in recipe["ingredients"].items()), default=0)
        return min(limit, batches * recipe["output"]) if batches else 0
    return 0
