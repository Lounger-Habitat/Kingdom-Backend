"""Versioned, read-only batch contract over normalized instance storage.

Legacy Python properties read the optional typed detail tables. Consumers use
usage/dish/freshness objects, not dish fields to infer generic item behavior.
This adapter also accepts store/market attributes via a slot-shaped object.
"""
from items.models import ItemType
from types import SimpleNamespace


TOOL_TYPES = frozenset((ItemType.HOE_TOOL, ItemType.CHOP_TOOL, ItemType.BREAK_TOOL,
                       ItemType.REAP_TOOL, ItemType.WATER_TOOL, ItemType.COLLECT_TOOL))


def item_rules(item):
    if item is None:
        return {"stackable": False, "maxPerSlot": None, "hasQuality": False,
                "hasUsage": False, "isDish": False}
    dish = item.item_type == ItemType.FOOD
    usage = item.item_type in TOOL_TYPES or item.item_type == ItemType.USABLE
    single_use = item.item_type == ItemType.USABLE and item.max_use_count == 1
    return {
        "stackable": item.stackable and not dish and (not usage or single_use),
        "maxPerSlot": item.max_stack_size,
        "hasQuality": dish or item.quality_enabled,
        "hasUsage": usage,
        "isDish": dish,
    }


def project_batch(slot, item, world_minute):
    """Do not write, merge, rename, or replace any source batch IDs."""
    rules = item_rules(item)
    metadata = getattr(slot, "dish_metadata", None) or {}
    # Preserve special historical instances even if their catalog type changed.
    is_dish = rules["isDish"] or bool(metadata) or slot.rated or bool(slot.ingredient)
    remaining = slot.remaining_uses
    has_usage = rules["hasUsage"] or remaining is not None
    single_use = (item is not None and item.item_type == ItemType.USABLE
                  and item.max_use_count == 1 and remaining == 1)
    usage = None
    if has_usage:
        mode = "UNKNOWN" if remaining is None else "UNLIMITED" if remaining == -1 else "FINITE" if remaining >= 0 else "INVALID"
        usage = {"mode": mode, "remaining": remaining,
                 "maximum": item.max_use_count if item else None}
    produced = getattr(slot, "produced_world_minute", None)
    expires = getattr(slot, "expires_world_minute", None)
    unknown = getattr(slot, "freshness_unknown", False)
    freshness = None
    if expires is not None or unknown:
        state = "UNKNOWN" if unknown or world_minute is None else "EXPIRED" if expires is not None and expires <= world_minute else "FRESH"
        freshness = {"expiresWorldMinute": expires, "state": state}
    dish = None
    if is_dish:
        dish = {"cookTime": slot.cook_time, "ingredients": slot.ingredient,
                "metadata": metadata,
                "review": {"rated": slot.rated, "price": slot.rating_price,
                           "score": slot.overall_score, "comment": slot.review_comment}}
    return {
        "schemaVersion": 1,
        "id": str(slot.instance_id), "itemId": slot.item_id, "quantity": slot.item_amount,
        "quality": slot.dish_quality if rules["hasQuality"] or is_dish else None,
        "stacking": {"stackable": rules["stackable"] and not is_dish and (not has_usage or single_use),
                     "maxPerSlot": rules["maxPerSlot"], **({"singleUse": True} if single_use else {})},
        "usage": usage, "dish": dish, "freshness": freshness,
        "origin": {"producedWorldMinute": produced,
                   "requestId": str(slot.source_request_id) if getattr(slot, "source_request_id", None) else None},
        "snapshotWorldMinute": world_minute,
    }


def project_public_batch(slot, item, world_minute, quantity, *, expires_world_minute=None,
                         freshness_unknown=False):
    """Public tooltip snapshot: no private identity, provenance or arbitrary metadata."""
    view = SimpleNamespace(
        instance_id="", item_id=slot.item_id, item_amount=quantity,
        **{name: getattr(slot, name) for name in (
            "dish_quality", "remaining_uses", "ingredient", "cook_time", "dish_metadata",
            "rated", "rating_price", "overall_score", "review_comment")},
        expires_world_minute=expires_world_minute, freshness_unknown=freshness_unknown,
    )
    result = project_batch(view, item, world_minute)
    result["origin"] = None
    if result["dish"] is not None:
        result["dish"]["metadata"] = {}
    return result
