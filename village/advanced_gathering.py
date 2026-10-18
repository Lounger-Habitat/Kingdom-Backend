"""One shared undergrowth stock, restored by world time, never by UI queries."""
from .models import VillageGatheringResourceState
from .professional_catalog import PROFESSIONAL_COLLECT_TYPES

COLLECT_TYPE = "采集林下食材"
LOCATION = "FOREST_UNDERGROWTH"
CAPACITY, RESTORE_MINUTES = 120, 1440


class AdvancedGatheringError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def _values(row, minute):
    if row is None:
        return CAPACITY, None
    if minute < row.next_restore_world_minute:
        return row.remaining_quantity, row.next_restore_world_minute
    intervals = (minute - row.next_restore_world_minute) // RESTORE_MINUTES + 1
    return CAPACITY, row.next_restore_world_minute + intervals * RESTORE_MINUTES


def resource_overview(minute):
    row = VillageGatheringResourceState.objects.filter(pk=LOCATION).first()
    remaining, restore = _values(row, minute)
    return dict(remaining_quantity=remaining, capacity=CAPACITY,
        next_restore_world_minute=restore, restore_minutes=RESTORE_MINUTES,
        revision=row.revision if row else 0, observed_world_minute=minute)


def prepare_collection(player, bag, params, pool, weights, minute):
    from .production_tools import ToolError, resolve_tool
    from .player_gathering import CATEGORY_ITEMS
    from player.progression import growth_snapshot
    level = next(s["level"] for s in growth_snapshot(player, read_only=True)["skills"] if s["code"] == "gathering")
    if level < 4:
        raise AdvancedGatheringError("SKILL_LEVEL_REQUIRED", "采集达到4级后可寻找林下食材。")
    if not params.get("tool_instance_id"):
        raise AdvancedGatheringError("TOOL_MISSING", "请选择本人可用的T2采集工具包。")
    try:
        tool = resolve_tool(player, bag, "targeted_gathering", params.get("tool_instance_id"),
                            minimum_tier=2, minute=minute)
    except ToolError as exc:
        raise AdvancedGatheringError(exc.code, exc.message) from exc
    category = params.get("category")
    if category is not None:
        if not isinstance(category, str) or category not in CATEGORY_ITEMS:
            raise AdvancedGatheringError("INVALID_PARAMS", "请选择有效的林下食材类别。")
        pairs = [(entry, weight) for entry, weight in zip(pool, weights)
                 if entry["item"].pk in CATEGORY_ITEMS[category]]
        pool, weights = [p[0] for p in pairs], [p[1] for p in pairs]
    if not pool:
        raise AdvancedGatheringError("COLLECTION_CATEGORY_EMPTY", "此点的实际物品池没有所选类别。")
    row, _ = VillageGatheringResourceState.objects.select_for_update().get_or_create(code=LOCATION,
        defaults=dict(next_restore_world_minute=minute + RESTORE_MINUTES))
    remaining, restore = _values(row, minute)
    if remaining <= 0:
        raise AdvancedGatheringError("RESOURCE_DEPLETED", "林下资源已采完，请等待下一次自然恢复。")
    if remaining != row.remaining_quantity or restore != row.next_restore_world_minute:
        row.remaining_quantity, row.next_restore_world_minute = remaining, restore
        row.revision += 1
        row.save(update_fields=["remaining_quantity", "next_restore_world_minute", "revision"])
    return pool, weights, tool, row


def consume_resource(row, quantity):
    if type(quantity) is not int or not 0 < quantity <= row.remaining_quantity:
        raise ValueError("Invalid shared resource quantity")
    row.remaining_quantity -= quantity
    row.revision += 1
    row.save(update_fields=["remaining_quantity", "revision"])


def is_professional_collection(collect_type):
    return collect_type in PROFESSIONAL_COLLECT_TYPES


def professional_resource_overview(collect_type, minute):
    spec = PROFESSIONAL_COLLECT_TYPES[collect_type]
    row = VillageGatheringResourceState.objects.filter(pk=spec["code"]).first()
    remaining = row.remaining_quantity if row else 90
    restore = row.next_restore_world_minute if row else None
    if restore is not None and minute >= restore:
        remaining, restore = 90, restore + ((minute-restore)//1440+1)*1440
    return dict(remaining_quantity=remaining, capacity=90, next_restore_world_minute=restore,
        restore_minutes=1440, revision=row.revision if row else 0, observed_world_minute=minute)


def prepare_professional_collection(player, bag, params, minute):
    from .production_tools import ToolError, resolve_tool
    from player.progression import growth_snapshot
    from items.models import Item
    spec = PROFESSIONAL_COLLECT_TYPES.get(params.get("collect_type"))
    if spec is None:
        raise AdvancedGatheringError("INVALID_PARAMS", "请选择明确的专业采集点。")
    if params.get("category") is not None:
        raise AdvancedGatheringError("INVALID_PARAMS", "专业采集点只产对应的一种原料。")
    level = next(s["level"] for s in growth_snapshot(player, read_only=True)["skills"] if s["code"] == "gathering")
    if level < 7:
        raise AdvancedGatheringError("SKILL_LEVEL_REQUIRED", "专业采集需要采集7级。")
    if not params.get("tool_instance_id"):
        raise AdvancedGatheringError("TOOL_MISSING", "请选择本人T3专业采集包。")
    try:
        tool = resolve_tool(player, bag, "targeted_gathering", params["tool_instance_id"], minimum_tier=3, minute=minute)
    except ToolError as exc:
        raise AdvancedGatheringError(exc.code, exc.message) from exc
    if not Item.objects.filter(pk=spec["item_id"], is_deleted=False).exists():
        raise AdvancedGatheringError("COLLECTION_EMPTY", "该专业原料目录尚未就绪。")
    row, _ = VillageGatheringResourceState.objects.select_for_update().get_or_create(code=spec["code"],
        defaults=dict(remaining_quantity=90, next_restore_world_minute=minute+1440))
    view = professional_resource_overview(params["collect_type"], minute)
    if view["remaining_quantity"] < 2:
        raise AdvancedGatheringError("RESOURCE_DEPLETED", "该专业资源不足两份，请等待自然恢复。")
    if (row.remaining_quantity, row.next_restore_world_minute) != (view["remaining_quantity"], view["next_restore_world_minute"]):
        row.remaining_quantity, row.next_restore_world_minute = view["remaining_quantity"], view["next_restore_world_minute"]
        row.revision += 1
        row.save(update_fields=["remaining_quantity", "next_restore_world_minute", "revision"])
    return dict(item_id=spec["item_id"], quantity=2, quality=4, freshness_minutes=2880, tool=tool, resource=row)


def finish_professional_collection(resource, quantity=2):
    if quantity != 2:
        raise ValueError("Professional collection produces exactly two units")
    consume_resource(resource, quantity)


def collection_points_projection(player, bag, minute):
    from inventory.models import CollectionPoint
    from .player_actions import COLLECTION_LOCATIONS
    from .player_gathering import CATEGORIES, _pool_item_ids
    from .production_tools import tools_overview
    result = []
    for point in CollectionPoint.objects.filter(collect_type__in=COLLECTION_LOCATIONS):
        entry = dict(collect_type=point.collect_type, title=point.name,
            location_code=COLLECTION_LOCATIONS[point.collect_type], action_cost=point.action_cost,
            collect_count=point.collect_count)
        if point.collect_type == COLLECT_TYPE:
            ids = _pool_item_ids(player, COLLECT_TYPE)
            entry.update(tool_required=True, required_level=4, minimum_tool_tier=2,
                categories=[dict(code=code, title=title, item_ids=sorted(ids.intersection(items)),
                                 available=bool(ids.intersection(items))) for code, title, items in CATEGORIES],
                tools=[t for t in tools_overview(player, bag, minute)["tools"]
                       if t["capability"] == "targeted_gathering" and t["tier"] >= 2], **resource_overview(minute))
        elif is_professional_collection(point.collect_type):
            entry.update(tool_required=True, required_level=7, minimum_tool_tier=3, action_cost=20,
                collect_count=1, output_quantity=2, quality=4, freshness_minutes=2880,
                tools=[t for t in tools_overview(player, bag, minute)["tools"]
                       if t["capability"] == "targeted_gathering" and t["tier"] >= 3],
                **professional_resource_overview(point.collect_type, minute))
        result.append(entry)
    return result
