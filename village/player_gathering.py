"""G1 gathering categories and physical tool use, inside the player transaction.

Overview is pure: it never creates a player collection point, item, or stock.
Catalog/stock installation is explicit and finite; the fixed listing is the
installation marker even after its stock has sold out or been removed.
"""
import uuid

from django.db import models, transaction

from game_time.models import GlobalGameTimeState
from inventory.components import copy_batch_attributes
from inventory.models import (CollectionPoint, CollectionPointLevelItem, InventorySlot,
                              NpcDefinition, PlayerCollectionOverride, PlayerCollectionPoint)
from items.models import Item, ItemType
from player.models import ItemGameplayDefinition
from player.progression import growth_snapshot
from .models import (EconomicLedgerEntry, VillageAccount, VillageStore,
                     VillageStoreAccount, VillageStoreInventorySlot, VillageStoreListing,
                     VillageWorldEvent)

TOOL_ITEM_ID = 4101
TOOL_NAME = "采集工具包"
TOOL_PRICE = 50
TOOL_MAX_USES = 20
INITIAL_STOCK = 20
REQUIRED_LEVEL = 2
STOCK_REQUEST_ID = uuid.uuid5(uuid.NAMESPACE_URL, "kingdom:g1:gathering-tool:initial-stock:v1")
TOOL_LISTING_ID = uuid.uuid5(uuid.NAMESPACE_URL, "kingdom:g1:gathering-tool:listing:v1")
CATEGORIES = (
    ("leafy", "叶菜", (1014, 1023, 1024, 1059)),
    ("roots", "根茎", (1025, 1042, 1043)),
    ("fruiting", "果菜", (1001, 1031, 1047)),
    ("aromatics", "辛香", (1033, 1040, 1073)),
    ("mushrooms", "菌菇", (1035, 1036, 1037)),
)
CATEGORY_ITEMS = {code: frozenset(ids) for code, _, ids in CATEGORIES}


class GatheringError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def reject(code, message):
    raise GatheringError(code, message)


@transaction.atomic
def ensure_gathering_catalog(minute):
    """Trusted initialization only. No wallet/stock is created by observation."""
    item, _ = Item.objects.get_or_create(item_id=TOOL_ITEM_ID, defaults=dict(
        item_name=TOOL_NAME, item_type=ItemType.COLLECT_TOOL, item_price=TOOL_PRICE,
        max_use_count=TOOL_MAX_USES, stackable=False, collectable=False,
        item_description="T1采集工具包。采集2级后可装备，植物采摘时间缩短15%，每次成功采摘消耗1耐久。"))
    if item.item_type != ItemType.COLLECT_TOOL:
        reject("GATHERING_CATALOG_CONFLICT", "采集工具目录编号冲突，请联系管理员。")
    ItemGameplayDefinition.objects.get_or_create(item=item, defaults=dict(
        capability_code="targeted_gathering", tier=1, required_skill="gathering", required_level=REQUIRED_LEVEL,
        effects=[dict(code="category_selection", description="按食材类别采集", uses_per_success=1)]))
    if item.is_deleted:
        return None
    store = VillageStore.objects.select_for_update().filter(pk="GENERAL_STORE").first()
    nora = NpcDefinition.objects.filter(agent_id="10015", is_test_only=False).first()
    if (store is None or nora is None or store.operator_npc_id != nora.pk or
            not VillageStoreAccount.objects.filter(store=store).exists()):
        return None
    from .production_tools import _initial_tool_listing
    existing = _initial_tool_listing(store, TOOL_ITEM_ID, TOOL_PRICE, TOOL_MAX_USES)
    listing = VillageStoreListing.objects.filter(pk=TOOL_LISTING_ID).first()
    if listing is not None:
        return listing
    if EconomicLedgerEntry.objects.filter(request_id=STOCK_REQUEST_ID).exists():
        # Administrative removal of the listing is not a new stock entitlement.
        return None
    listing = existing or VillageStoreListing.objects.create(listing_id=TOOL_LISTING_ID, store=store,
        item_id=TOOL_ITEM_ID, unit_price=TOOL_PRICE, price_mode="ORIGINAL", status="ACTIVE",
        listed_by_npc=nora, created_request_id=STOCK_REQUEST_ID, listed_world_minute=minute)
    for index in range(INITIAL_STOCK):
        VillageStoreInventorySlot.objects.create(
            instance_id=uuid.uuid5(TOOL_LISTING_ID, str(index)), store=store, listing=listing,
            item_id=TOOL_ITEM_ID, item_amount=1, remaining_uses=TOOL_MAX_USES,
            produced_world_minute=minute, quality=3,
            common_attributes={"initial_stock_code": "G1_GATHERING_TOOLS", "item_bag_name": TOOL_NAME})
    store.inventory_version += 1
    store.save(update_fields=["inventory_version", "updated_at"])
    revision = GlobalGameTimeState.objects.filter(pk=1).values_list("time_revision", flat=True).first()
    event = VillageWorldEvent.objects.create(dedupe_key="g1:gathering-tool:initial-stock:v1",
        request_id=STOCK_REQUEST_ID, event_type="GATHERING_TOOL_STOCKED", world_minute=minute,
        time_revision=revision or 0, location_code=store.location_code, participant_npc_ids=[nora.agent_id],
        visibility="PUBLIC", player_summary="Nora 杂货铺备好了一批采集工具包。",
        hub_summary="G1一次性采集工具库存已入店；售完不自动补货。",
        public_data=dict(item_id=TOOL_ITEM_ID, quantity=INITIAL_STOCK, listing_id=str(listing.pk)))
    EconomicLedgerEntry.objects.create(request_id=STOCK_REQUEST_ID, entry_type="GATHERING_TOOL_STOCK",
        world_minute=minute, event=event,
        changes={"STORE:GENERAL_STORE": {str(TOOL_ITEM_ID): INITIAL_STOCK},
                 "initial_stock_code": "G1_GATHERING_TOOLS", "listing_id": str(listing.pk)})
    from .phase4_visibility import enqueue_event_deliveries
    enqueue_event_deliveries(event)
    return listing


def _level(player):
    # Shares G0's legacy floor without initializing the six skill rows.
    return next(skill["level"] for skill in growth_snapshot(player, read_only=True)["skills"]
                if skill["code"] == "gathering")


def _pool_item_ids(player, collect_type="采集食材"):
    """Read-only equivalent of the effective legacy pool's eligibility rules."""
    point = CollectionPoint.objects.filter(collect_type=collect_type).first()
    if point is None or point.action_cost <= 0 or point.collect_count <= 0:
        return set()
    owned = PlayerCollectionPoint.objects.filter(player=player, collection_point=point).first()
    level = owned.current_level if owned else point.default_level
    overrides = {o.item_id: o for o in PlayerCollectionOverride.objects.filter(player_collection_point=owned)} if owned else {}
    ids = set()
    for row in CollectionPointLevelItem.objects.filter(
            level__collection_point=point, level__level=level).select_related("item"):
        override = overrides.get(row.item_id)
        if row.item.is_deleted or (override and override.is_excluded):
            continue
        weight, minimum, maximum = row.collect_weight, row.collect_min_amount, row.collect_max_amount
        if override:
            if override.override_weight is not None:
                weight = override.override_weight
            if override.override_min_amount is not None:
                minimum = override.override_min_amount
            if override.override_max_amount is not None:
                maximum = override.override_max_amount
        if weight > 0 and 0 < minimum <= maximum:
            ids.add(row.item_id)
    return ids


def _usable(slot, minute):
    from player.equipment import stats_for
    stats = stats_for(slot)
    maximum = stats["max_durability"] if stats else 0
    return (slot.item_amount > 0 and type(slot.remaining_uses) is int and (0 < slot.remaining_uses <= maximum))


def _tool_shop(minute):
    from .lucy_restaurant import store_available
    from .phase3_direct_trade import _TRADABLE_STORE_STATUSES
    data = dict(available=False, listing_id=None, item_id=TOOL_ITEM_ID, unit_price=TOOL_PRICE,
                stock=0, maximum_uses=TOOL_MAX_USES, reason="STORE_UNAVAILABLE")
    store = VillageStore.objects.filter(pk="GENERAL_STORE").first()
    if (store is None or not VillageStoreAccount.objects.filter(store=store).exists()
            or not VillageAccount.objects.filter(account_code="PUBLIC_FUND").exists()):
        return data
    if store.status not in _TRADABLE_STORE_STATUSES:
        data["reason"] = "TRADE_STORE_CLOSED"
        return data
    item = Item.objects.filter(pk=TOOL_ITEM_ID, is_deleted=False).first()
    if item is None:
        data["reason"] = "GATHERING_TOOL_UNAVAILABLE"
        return data
    for listing in VillageStoreListing.objects.filter(store=store, item_id=TOOL_ITEM_ID,
            status="ACTIVE").order_by("unit_price", "pk"):
        stock = sum(store_available(s, minute) for s in listing.inventory_slots.all()
                    if s.remaining_uses == TOOL_MAX_USES)
        if stock > 0:
            data.update(available=True, listing_id=str(listing.pk), unit_price=listing.unit_price,
                        stock=stock, reason=None)
            return data
    data["reason"] = "TRADE_INSUFFICIENT_ITEM"
    return data


def gathering_overview(player, bag, minute):
    """Private read-only snapshot, also safe before first participation."""
    level = _level(player)
    ids = _pool_item_ids(player)
    categories = [dict(code=code, title=title, item_ids=sorted(ids.intersection(items)),
                       available=bool(ids.intersection(items))) for code, title, items in CATEGORIES]
    from .production_tools import tools_overview
    tools = tools_overview(player, bag, minute, capability="targeted_gathering")["tools"]
    usable = any(tool["usable"] for tool in tools)
    reason = ("SKILL_LEVEL_REQUIRED" if level < REQUIRED_LEVEL else "GATHERING_TOOL_REQUIRED" if not usable
              else "COLLECTION_CATEGORY_EMPTY" if not any(c["available"] for c in categories) else None)
    return dict(level=level, targeted_available=reason is None, reason=reason, categories=categories,
                tools=tools, tool_shop=_tool_shop(minute))


def prepare_targeted_collection(player, bag, params, pool, weights, *, minute=None):
    """Validate only; do not wear a tool or change AP before production succeeds."""
    has_category, has_tool = "category" in params, "tool_instance_id" in params
    if not has_category and not has_tool:
        return pool, weights, None
    if not has_category or not has_tool or not isinstance(params["category"], str):
        reject("INVALID_PARAMS", "类别采集需要同时选择类别和本人持有的工具实例。")
    category = params["category"]
    if category not in CATEGORY_ITEMS:
        reject("INVALID_PARAMS", "食材类别无效。")
    try:
        instance_id = uuid.UUID(str(params["tool_instance_id"]))
    except (ValueError, TypeError, AttributeError):
        reject("INVALID_PARAMS", "采集工具批次标识无效。")
    if _level(player) < REQUIRED_LEVEL:
        reject("SKILL_LEVEL_REQUIRED", "采集达到2级后可以按类别寻找食材。")
    if bag.player_id != player.pk or bag.character_id != player.username:
        reject("GATHERING_TOOL_REQUIRED", "请选择本人背包中的采集工具包。")
    if not Item.objects.filter(pk=TOOL_ITEM_ID, is_deleted=False).exists():
        reject("GATHERING_TOOL_UNAVAILABLE", "采集工具包尚未开放。")
    from player.models import EquipmentDefinition
    from player.equipment import stats_for
    tool_ids = EquipmentDefinition.objects.filter(category="gathering", enabled=True).values_list("item_id", flat=True)
    tool = InventorySlot.objects.select_for_update().filter(
        bag=bag, item_id__in=tool_ids, instance_id=instance_id, item_amount__gt=0).first()
    if tool is None:
        reject("GATHERING_TOOL_REQUIRED", "本人背包中没有这件采集工具包。")
    stats = stats_for(tool)
    if not stats or _level(player) < stats["required_skill_level"]:
        reject("SKILL_LEVEL_REQUIRED", "采集技能未达到该工具等级的使用要求。")
    if minute is None:
        minute = GlobalGameTimeState.objects.filter(pk=1).values_list("world_minute", flat=True).first()
        if minute is None:
            reject("WORLD_NOT_READY", "村庄时间尚未就绪。")
    if not _usable(tool, minute):
        reject("GATHERING_TOOL_UNUSABLE", "采集工具已损坏、过期或耐久信息无效。")
    selected = [(entry, weight) for entry, weight in zip(pool, weights)
                if entry["item"].pk in CATEGORY_ITEMS[category] and not entry["item"].is_deleted
                and weight > 0 and 0 < entry["min_amount"] <= entry["max_amount"]]
    if not selected:
        reject("COLLECTION_CATEGORY_EMPTY", "当前采集点没有这一类别的可采资源，请选择其他类别或基础采集。")
    return [e for e, _ in selected], [w for _, w in selected], tool


@transaction.atomic
def wear_tool(tool):
    """Consume one physical use after a successful collection, in its transaction."""
    if tool is None:
        return None
    if tool.item_amount > 1:
        tool.item_amount -= 1
        tool.save(update_fields=["item_amount"])
        index = tool.bag.slots.aggregate(n=models.Max("slot_index"))["n"]
        used = InventorySlot(bag=tool.bag, slot_index=0 if index is None else index + 1,
                             item_id=tool.item_id, item_amount=1, item_bag_name=tool.item_bag_name)
        copy_batch_attributes(tool, used)
        used.save()
        tool = used
    tool.remaining_uses -= 1
    tool.save(update_fields=["remaining_uses"])
    return dict(item_id=tool.item_id, instance_id=str(tool.instance_id), uses=1, remaining_uses=tool.remaining_uses)
