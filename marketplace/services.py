import math
import uuid
from datetime import timedelta

from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from account.models import GameAccount
from inventory.models import (
    InventoryBag,
    InventorySlot,
    NpcDefinition,
    NpcGlobalInventory,
    NpcGlobalInventorySlot,
)
from items.models import Item

from .models import MarketListing, MarketListingSource, MarketListingStatus, MarketTransaction


MARKETPLACE_LISTING_DURATION_HOURS = 168  # 7 days


def _world_minute():
    from game_time.models import GlobalGameTimeState
    return GlobalGameTimeState.objects.select_for_update().get(pk=1).world_minute


def _listing_batch(listing):
    """Old NPC perishables without captured batch metadata remain unverified."""
    snap = dict(listing.item_snapshot or {})
    # Never restore mutable instance attributes from the historical audit JSON.
    snap.update({"itemID": listing.item_id, "itemAmount": listing.total_amount,
                 "rated": listing.rated, "ratingPrice": listing.rating_price,
                 "overallScore": listing.overall_score, "ingredient": listing.ingredient,
                 "cookTime": listing.cook_time, "dishQuality": listing.dish_quality,
                 "remainingUses": listing.remaining_uses, "reviewComment": listing.review_comment,
                 "dishMetadata": listing.dish_metadata, "itemBagName": listing.item_bag_name})
    if listing.source_type == MarketListingSource.NPC and "batchMetadataVersion" not in snap:
        from village.phase3_catalog import CORN_OUTPUT_ITEM_ID, HUNT_MEAT_ITEM_IDS, PREY_ITEM_IDS, RECIPE_WHITELIST
        from recipes.models import Recipe
        perishables = set(HUNT_MEAT_ITEM_IDS) | set(PREY_ITEM_IDS) | {CORN_OUTPUT_ITEM_ID}
        perishables.update(Recipe.objects.filter(pk__in=RECIPE_WHITELIST).values_list("item_id", flat=True))
        if listing.item_id in perishables:
            snap["freshnessUnknown"] = True
    return snap


def _apply_batch(slot, snap):
    slot.dish_metadata = dict(snap.get("dishMetadata") or {})
    slot.produced_world_minute = snap.get("producedWorldMinute")
    slot.expires_world_minute = snap.get("expiresWorldMinute")
    slot.freshness_unknown = bool(snap.get("freshnessUnknown", False))
    slot.source_request_id = snap.get("sourceRequestId")


def get_player_main_bag(player):
    return InventoryBag.objects.get_or_create(player=player, character_id=player.username)


def get_npc_main_bag(npc):
    return NpcGlobalInventory.objects.get_or_create(npc=npc)


def _lock_player_bag(bag):
    return InventoryBag.objects.select_for_update().get(pk=bag.pk)


def _lock_npc_bag(bag):
    return NpcGlobalInventory.objects.select_for_update().get(pk=bag.pk)


def _save_money_change(bag, source):
    if isinstance(bag, InventoryBag):
        bag._money_change_source = source
        bag.save()
    else:
        bag.save(update_fields=["money", "updated_at"])


def _bump_npc_asset_version(bag):
    if isinstance(bag, NpcGlobalInventory):
        bag.asset_version += 1
        bag.save(update_fields=["asset_version", "updated_at"])


def _owner_sort_key(owner):
    owner_type = "npc" if owner["kind"] == "npc" else "player"
    return (owner_type, owner["id"])


def _resolve_owner(actor):
    if isinstance(actor, GameAccount):
        bag, _ = get_player_main_bag(actor)
        return {
            "kind": "player",
            "id": str(actor.id),
            "account": actor,
            "npc": None,
            "bag": bag,
            "slot_model": InventorySlot,
            "source_type": MarketListingSource.PLAYER,
            "money_source_buy": "marketplace_buy",
            "money_source_sell": "marketplace_sell",
            "money_source_admin_buy": "admin_marketplace_buy",
            "money_source_admin_sell": "admin_marketplace_sell",
        }
    if isinstance(actor, NpcDefinition):
        bag, _ = get_npc_main_bag(actor)
        return {
            "kind": "npc",
            "id": actor.agent_id,
            "account": None,
            "npc": actor,
            "bag": bag,
            "slot_model": NpcGlobalInventorySlot,
            "source_type": MarketListingSource.NPC,
            "money_source_buy": "marketplace_buy_npc",
            "money_source_sell": "marketplace_sell_npc",
            "money_source_admin_buy": "admin_marketplace_buy_npc",
            "money_source_admin_sell": "admin_marketplace_sell_npc",
        }
    raise TypeError("Unsupported marketplace actor")


def _lock_owner(owner):
    if owner["kind"] == "player":
        owner["bag"] = _lock_player_bag(owner["bag"])
    else:
        owner["bag"] = _lock_npc_bag(owner["bag"])
    return owner


def _lock_owners(*owners):
    locked = []
    for owner in sorted(owners, key=_owner_sort_key):
        locked.append(_lock_owner(owner))
    return locked


def find_slot_by_instance(bag, instance_id):
    return bag.slots.filter(instance_id=instance_id, item_amount__gt=0).first()


def find_empty_slot(bag):
    empty = bag.slots.filter(item_amount=0).first()
    if empty:
        return empty
    max_index = bag.slots.aggregate(Max("slot_index"))["slot_index__max"]
    next_index = (max_index + 1) if max_index is not None else 0
    slot_model = NpcGlobalInventorySlot if isinstance(bag, NpcGlobalInventory) else InventorySlot
    return slot_model(bag=bag, slot_index=next_index)


def clear_slot_if_empty(slot):
    if slot.item_amount <= 0:
        slot.item_id = 0
        slot.item_amount = 0
        slot.instance_id = uuid.uuid4()
        slot.rated = False
        slot.rating_price = -1
        slot.overall_score = 0
        slot.ingredient = ""
        slot.item_bag_name = ""
        slot.cook_time = 0
        slot.dish_quality = 3
        slot.hide_in_shop = False
        slot.remaining_uses = None
        slot.review_comment = ""
        _apply_batch(slot, {})
        update_fields = [
            "item_id",
            "item_amount",
            "instance_id",
            "rated",
            "rating_price",
            "overall_score",
            "ingredient",
            "item_bag_name",
            "cook_time",
            "dish_quality",
            "hide_in_shop",
            "remaining_uses",
            "review_comment", "produced_world_minute", "expires_world_minute", "freshness_unknown", "dish_metadata",
        ]
        update_fields.append("source_request_id")
        slot.save(update_fields=update_fields)


def snapshot_slot(slot):
    return {
        "dishMetadata": slot.dish_metadata,
        "instanceID": str(slot.instance_id),
        "itemID": slot.item_id,
        "itemAmount": slot.item_amount,
        "rated": slot.rated,
        "ratingPrice": slot.rating_price,
        "overallScore": slot.overall_score,
        "ingredient": slot.ingredient,
        "itemBagName": slot.item_bag_name,
        "cookTime": slot.cook_time,
        "dishQuality": slot.dish_quality,
        "hideInShop": slot.hide_in_shop,
        "remainingUses": slot.remaining_uses,
        "reviewComment": slot.review_comment,
        "batchMetadataVersion": 1,
        "producedWorldMinute": slot.produced_world_minute,
        "expiresWorldMinute": slot.expires_world_minute,
        "freshnessUnknown": slot.freshness_unknown,
        "sourceRequestId": str(slot.source_request_id) if getattr(slot, "source_request_id", None) else None,
    }


def build_default_item_snapshot(item, amount):
    remaining_uses = item.max_use_count if item.item_type == "Usable" else None
    return {
        "instanceID": str(uuid.uuid4()),
        "itemID": item.item_id,
        "itemAmount": amount,
        "rated": False,
        "ratingPrice": -1,
        "overallScore": 0,
        "ingredient": "",
        "itemBagName": "",
        "cookTime": 0,
        "dishQuality": 3,
        "hideInShop": False,
        "remainingUses": remaining_uses,
        "reviewComment": "",
    }


def is_special_instance(snap):
    return any(
        [
            snap.get("rated"),
            snap.get("ratingPrice", -1) >= 0,
            snap.get("overallScore", 0) > 0,
            bool(snap.get("ingredient")),
            snap.get("cookTime", 0) > 0,
            snap.get("dishQuality", 3) != 3,
            snap.get("remainingUses") is not None,
            bool(snap.get("reviewComment")),
            snap.get("producedWorldMinute") is not None,
            snap.get("expiresWorldMinute") is not None,
            bool(snap.get("freshnessUnknown")),
            bool(snap.get("dishMetadata")),
        ]
    )


def calculate_commission(total_price):
    return math.ceil(total_price * 8 / 100)


def calculate_settlement(listing, amount):
    total_price = listing.price * amount
    if listing.source_type == MarketListingSource.SYSTEM:
        return {
            "total_price": total_price,
            "commission": 0,
            "seller_earnings": 0,
            "system_income": total_price,
        }
    commission = calculate_commission(total_price)
    return {
        "total_price": total_price,
        "commission": commission,
        "seller_earnings": total_price - commission,
        "system_income": commission,
    }


def _get_item_snapshot_fields(source_slot, amount):
    snap = snapshot_slot(source_slot)
    snap["itemAmount"] = amount
    return {
        "source_instance_id": source_slot.instance_id,
        "item_id": source_slot.item_id,
        "item_snapshot": snap,
        "rated": source_slot.rated,
        "rating_price": source_slot.rating_price,
        "overall_score": source_slot.overall_score,
        "ingredient": source_slot.ingredient,
        "item_bag_name": source_slot.item_bag_name,
        "cook_time": source_slot.cook_time,
        "dish_quality": source_slot.dish_quality,
        "remaining_uses": source_slot.remaining_uses,
        "review_comment": source_slot.review_comment,
    }


def _listing_identity_fields(owner):
    if owner["kind"] == "player":
        return {"source_type": MarketListingSource.PLAYER, "seller": owner["account"], "seller_npc": None}
    if owner["kind"] == "npc":
        return {"source_type": MarketListingSource.NPC, "seller": None, "seller_npc": owner["npc"]}
    return {"source_type": MarketListingSource.SYSTEM, "seller": None, "seller_npc": None}


def _transaction_identity_fields(listing, buyer_owner):
    return {
        "source_type": listing.source_type,
        "seller": listing.seller,
        "seller_npc": listing.seller_npc,
        "buyer": buyer_owner["account"],
        "buyer_npc": buyer_owner["npc"],
    }


def _listing_seller_owner(listing):
    if listing.source_type == MarketListingSource.PLAYER and listing.seller:
        return _resolve_owner(listing.seller)
    if listing.source_type == MarketListingSource.NPC and listing.seller_npc:
        return _resolve_owner(listing.seller_npc)
    return None


def _return_items_to_bag(bag, listing, amount):
    snap = _listing_batch(listing)
    special = is_special_instance(snap)

    if not special:
        existing = bag.slots.filter(item_id=listing.item_id, item_amount__gt=0,
            produced_world_minute__isnull=True, expires_world_minute__isnull=True, freshness_unknown=False,
            usage__remaining__isnull=True).first()
        if existing:
            existing.item_amount += amount
            existing.save(update_fields=["item_amount"])
            return existing

    slot = find_empty_slot(bag)
    slot.item_id = listing.item_id
    slot.item_amount = amount
    slot.rated = listing.rated
    slot.rating_price = listing.rating_price
    slot.overall_score = listing.overall_score
    slot.ingredient = listing.ingredient
    slot.item_bag_name = listing.item_bag_name
    slot.cook_time = listing.cook_time
    slot.dish_quality = listing.dish_quality
    slot.remaining_uses = listing.remaining_uses
    slot.review_comment = listing.review_comment
    _apply_batch(slot, snap)
    slot.save()
    return slot


def _add_snapshot_item_to_bag(bag, snap, amount):
    special = is_special_instance(snap)

    if not special:
        existing = bag.slots.filter(item_id=snap.get("itemID", 0), item_amount__gt=0,
            produced_world_minute__isnull=True, expires_world_minute__isnull=True, freshness_unknown=False,
            usage__remaining__isnull=True).first()
        if existing:
            existing.item_amount += amount
            existing.save(update_fields=["item_amount"])
            return existing

    slot = find_empty_slot(bag)
    slot.item_id = snap.get("itemID", 0)
    slot.item_amount = amount
    slot.rated = snap.get("rated", False)
    slot.rating_price = snap.get("ratingPrice", -1)
    slot.overall_score = snap.get("overallScore", 0)
    slot.ingredient = snap.get("ingredient", "")
    item = Item.objects.filter(item_id=slot.item_id).first()
    item_name = item.item_name if item else f"物品#{slot.item_id}"
    slot.item_bag_name = f"{item_name}_{slot.slot_index}"
    slot.cook_time = snap.get("cookTime", 0)
    slot.dish_quality = snap.get("dishQuality", 3)
    slot.hide_in_shop = False
    slot.remaining_uses = snap.get("remainingUses")
    slot.review_comment = snap.get("reviewComment", "")
    _apply_batch(slot, snap)
    slot.save()
    return slot


def _expire_single_listing(listing):
    seller_owner = _listing_seller_owner(listing)
    if listing.remaining_amount > 0 and seller_owner is not None:
        _lock_owner(seller_owner)
        _return_items_to_bag(seller_owner["bag"], listing, listing.remaining_amount)
        _bump_npc_asset_version(seller_owner["bag"])
    listing.remaining_amount = 0
    listing.status = MarketListingStatus.EXPIRED
    listing.save(update_fields=["remaining_amount", "status", "updated_at"])


def browse_listings(
    *,
    keyword="",
    item_type="",
    item_id=None,
    min_price=None,
    max_price=None,
    sort_by="newest",
    status=MarketListingStatus.ACTIVE,
    expire=True,
):
    if expire:
        expire_due_listings()
    qs = MarketListing.objects.all()
    if not expire and status == MarketListingStatus.ACTIVE:
        qs = qs.filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()))
    if status:
        qs = qs.filter(status=status)
    if keyword:
        qs = qs.filter(
            Q(item_name_snapshot__icontains=keyword)
            | Q(seller__username__icontains=keyword)
            | Q(seller_npc__name__icontains=keyword)
        )
    if item_type:
        qs = qs.filter(item_type_snapshot=item_type)
    if item_id is not None:
        qs = qs.filter(item_id=int(item_id))
    if min_price is not None:
        qs = qs.filter(price__gte=int(min_price))
    if max_price is not None:
        qs = qs.filter(price__lte=int(max_price))
    sort_map = {
        "price_asc": "price",
        "price_desc": "-price",
        "newest": "-created_at",
        "oldest": "created_at",
        "amount_desc": "-remaining_amount",
    }
    return qs.select_related("seller", "seller_npc").order_by(sort_map.get(sort_by, "-created_at"))


def get_npc_listings(npc, status="", *, expire=True):
    if expire:
        expire_due_listings()
    qs = MarketListing.objects.filter(seller_npc=npc)
    if not expire and status == MarketListingStatus.ACTIVE:
        qs = qs.filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()))
    if status:
        qs = qs.filter(status=status)
    return qs.select_related("seller_npc").order_by("-created_at")


def get_npc_history(npc, role="all"):
    qs = MarketTransaction.objects.select_related("seller", "seller_npc", "buyer", "buyer_npc")
    if role == "buyer":
        return qs.filter(buyer_npc=npc).order_by("-created_at")
    if role == "seller":
        return qs.filter(seller_npc=npc).order_by("-created_at")
    return qs.filter(Q(buyer_npc=npc) | Q(seller_npc=npc)).order_by("-created_at")


@transaction.atomic
def _create_listing_for_owner(owner, instance_id, amount, price, *, expected_asset_version=None):
    world_minute = _world_minute()
    owner = _lock_owner(owner)
    if owner["kind"] == "npc" and expected_asset_version is not None and owner["bag"].asset_version != expected_asset_version:
        return None, "资产版本不匹配"

    slot = find_slot_by_instance(owner["bag"], instance_id)
    if not slot:
        return None, "物品未找到"
    if amount <= 0:
        return None, "数量必须大于0"
    if price <= 0:
        return None, "价格必须大于0"
    if slot.item_amount < amount:
        return None, "物品数量不足"
    if slot.freshness_unknown:
        return None, "物品保质期来源未核实，暂不能出售"
    if slot.expires_world_minute is not None and slot.expires_world_minute <= world_minute:
        return None, "物品已过期"
    if owner["kind"] == "npc":
        from village.phase3_asset_service import get_available_slot_amount
        if get_available_slot_amount(slot, world_minute) < amount:
            return None, "未预留的有效物品数量不足"

    commission = calculate_commission(price * amount)
    if price * amount - commission <= 0 and owner["kind"] != "system":
        return None, "价格过低，手续费后收入为0"

    item = Item.objects.filter(item_id=slot.item_id).first()
    listing = MarketListing.objects.create(
        **_listing_identity_fields(owner),
        **_get_item_snapshot_fields(slot, amount),
        item_name_snapshot=item.item_name if item else "",
        item_type_snapshot=item.item_type if item else "",
        icon_url_snapshot=(item.icon_url or "") if item else "",
        total_amount=amount,
        remaining_amount=amount,
        price=price,
        status=MarketListingStatus.ACTIVE,
        expires_at=timezone.now() + timedelta(hours=MARKETPLACE_LISTING_DURATION_HOURS),
    )

    slot.item_amount -= amount
    slot.save(update_fields=["item_amount"])
    clear_slot_if_empty(slot)
    _bump_npc_asset_version(owner["bag"])
    return listing, None


@transaction.atomic
def create_listing(player, instance_id, amount, price):
    return _create_listing_for_owner(_resolve_owner(player), instance_id, amount, price)


@transaction.atomic
def create_npc_listing(npc, instance_id, amount, price, expected_asset_version=None):
    return _create_listing_for_owner(
        _resolve_owner(npc), instance_id, amount, price, expected_asset_version=expected_asset_version
    )


@transaction.atomic
def _delist_listing_for_owner(owner, listing_id, *, expected_asset_version=None, include_returned_items=False):
    try:
        listing = MarketListing.objects.select_for_update().get(pk=listing_id)
    except MarketListing.DoesNotExist:
        return None, "挂单不存在"

    owner = _lock_owner(owner)
    if owner["kind"] == "npc" and expected_asset_version is not None and owner["bag"].asset_version != expected_asset_version:
        return None, "资产版本不匹配"
    if owner["kind"] == "player" and listing.seller_id != owner["account"].id:
        return None, "无权操作此挂单"
    if owner["kind"] == "npc" and listing.seller_npc_id != owner["npc"].id:
        return None, "无权操作此挂单"
    if listing.status != MarketListingStatus.ACTIVE:
        return None, "挂单不在出售中状态"

    returned_items = []
    if listing.remaining_amount > 0:
        received = _return_items_to_bag(owner["bag"], listing, listing.remaining_amount)
        if include_returned_items:
            from village.agent_feedback import received_batch
            returned_items.append(received_batch(received, listing.remaining_amount))

    listing.remaining_amount = 0
    listing.status = MarketListingStatus.DELISTED
    listing.save(update_fields=["remaining_amount", "status", "updated_at"])
    _bump_npc_asset_version(owner["bag"])
    return ({"bag": owner["bag"], "returned_items": returned_items} if include_returned_items else owner["bag"]), None


@transaction.atomic
def delist_listing(player, listing_id):
    return _delist_listing_for_owner(_resolve_owner(player), listing_id)


@transaction.atomic
def delist_npc_listing(npc, listing_id, expected_asset_version=None, include_returned_items=False):
    return _delist_listing_for_owner(
        _resolve_owner(npc), listing_id, expected_asset_version=expected_asset_version, include_returned_items=include_returned_items
    )


@transaction.atomic
def delist_system_listing(listing_id):
    try:
        listing = MarketListing.objects.select_for_update().get(pk=listing_id)
    except MarketListing.DoesNotExist:
        return None, "挂单不存在"

    if listing.source_type != MarketListingSource.SYSTEM:
        return None, "非系统挂单"

    listing.remaining_amount = 0
    listing.status = MarketListingStatus.DELISTED
    listing.save(update_fields=["remaining_amount", "status", "updated_at"])
    return listing, None


@transaction.atomic
def _buy_listing_for_owner(buyer_owner, listing_id, amount, *, expected_asset_version=None, admin_purchase=False):
    world_minute = _world_minute()
    if amount <= 0:
        return None, "数量必须大于0"

    try:
        listing = MarketListing.objects.select_for_update().get(pk=listing_id)
    except MarketListing.DoesNotExist:
        return None, "挂单不存在"

    if listing.status != MarketListingStatus.ACTIVE:
        return None, "挂单不在出售中状态"
    batch = _listing_batch(listing)
    if batch.get("freshnessUnknown"):
        return None, "挂单物品保质期来源未核实，暂不能成交"
    expiry = batch.get("expiresWorldMinute")
    if expiry is not None and expiry <= world_minute:
        return None, "挂单物品已过期"
    if listing.expires_at and listing.expires_at <= timezone.now():
        _expire_single_listing(listing)
        return None, "挂单已过期"

    seller_owner = _listing_seller_owner(listing)
    if seller_owner is not None and seller_owner["kind"] == buyer_owner["kind"] and seller_owner["id"] == buyer_owner["id"]:
        return None, "不能购买自己的挂单"
    if amount > listing.remaining_amount:
        return None, "购买数量超过剩余数量"

    if seller_owner is None:
        _lock_owner(buyer_owner)
    else:
        _lock_owners(buyer_owner, seller_owner)
    if buyer_owner["kind"] == "npc" and expected_asset_version is not None and buyer_owner["bag"].asset_version != expected_asset_version:
        return None, "资产版本不匹配"

    settlement = calculate_settlement(listing, amount)
    total_price = settlement["total_price"]
    if buyer_owner["bag"].money < total_price:
        return None, "金币不足"

    buyer_owner["bag"].money -= total_price
    _save_money_change(
        buyer_owner["bag"],
        buyer_owner["money_source_admin_buy"] if admin_purchase else buyer_owner["money_source_buy"],
    )

    if seller_owner is not None and listing.source_type != MarketListingSource.SYSTEM:
        seller_owner["bag"].money += settlement["seller_earnings"]
        _save_money_change(
            seller_owner["bag"],
            seller_owner["money_source_admin_sell"] if admin_purchase else seller_owner["money_source_sell"],
        )

    received = _add_snapshot_item_to_bag(buyer_owner["bag"], batch, amount)
    listing.remaining_amount -= amount
    if listing.remaining_amount <= 0:
        listing.status = MarketListingStatus.SOLD_OUT
    listing.save(update_fields=["remaining_amount", "status", "updated_at"])

    _bump_npc_asset_version(buyer_owner["bag"])
    if seller_owner is not None:
        _bump_npc_asset_version(seller_owner["bag"])

    tx = MarketTransaction.objects.create(
        listing=listing,
        **_transaction_identity_fields(listing, buyer_owner),
        item_id=listing.item_id,
        item_name_snapshot=listing.item_name_snapshot,
        amount=amount,
        unit_price=listing.price,
        total_price=total_price,
        commission=settlement["commission"],
        seller_earnings=settlement["seller_earnings"],
        system_income=settlement["system_income"],
    )

    result = {"bag": buyer_owner["bag"], "transaction": tx, "listing": listing}
    if buyer_owner["kind"] == "npc":
        from village.agent_feedback import received_batch
        result["purchased_batches"] = [received_batch(received, amount)]
    return result, None


@transaction.atomic
def buy_listing(buyer, listing_id, amount):
    return _buy_listing_for_owner(_resolve_owner(buyer), listing_id, amount)


@transaction.atomic
def buy_listing_for_npc(npc, listing_id, amount, expected_asset_version=None):
    return _buy_listing_for_owner(
        _resolve_owner(npc), listing_id, amount, expected_asset_version=expected_asset_version
    )


@transaction.atomic
def expire_due_listings(limit=100):
    now = timezone.now()
    qs = list(
        MarketListing.objects.select_for_update()
        .filter(status=MarketListingStatus.ACTIVE, expires_at__lte=now)
        .order_by("pk")[:limit]
    )
    expired_ids = []
    for listing in qs:
        _expire_single_listing(listing)
        expired_ids.append(listing.pk)
    return expired_ids


@transaction.atomic
def admin_buy_listing(buyer, listing_id, amount):
    return _buy_listing_for_owner(_resolve_owner(buyer), listing_id, amount, admin_purchase=True)


def create_system_listing(item_id, amount, price, expires_at=None, snapshot_overrides=None):
    if amount <= 0 or price <= 0:
        return None, "数量和价格必须大于0"

    try:
        item = Item.objects.get(item_id=item_id)
    except Item.DoesNotExist:
        return None, "物品不存在"

    snap = build_default_item_snapshot(item, amount)
    if snapshot_overrides:
        snap.update(snapshot_overrides)

    listing = MarketListing.objects.create(
        source_type=MarketListingSource.SYSTEM,
        seller=None,
        seller_npc=None,
        item_id=item_id,
        item_name_snapshot=item.item_name,
        item_type_snapshot=item.item_type,
        icon_url_snapshot=item.icon_url or "",
        total_amount=amount,
        remaining_amount=amount,
        price=price,
        status=MarketListingStatus.ACTIVE,
        item_snapshot=snap,
        rated=snap.get("rated", False),
        rating_price=snap.get("ratingPrice", -1),
        overall_score=snap.get("overallScore", 0),
        ingredient=snap.get("ingredient", ""),
        item_bag_name=snap.get("itemBagName", ""),
        cook_time=snap.get("cookTime", 0),
        dish_quality=snap.get("dishQuality", 3),
        remaining_uses=snap.get("remainingUses"),
        review_comment=snap.get("reviewComment", ""),
        expires_at=expires_at,
    )
    return listing, None
