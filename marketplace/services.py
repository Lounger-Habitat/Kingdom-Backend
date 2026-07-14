import math
import uuid
from datetime import timedelta

from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from account.models import GameAccount
from inventory.models import InventoryBag, InventorySlot
from inventory.serializers import serialize_bag
from items.models import Item

from .models import MarketListing, MarketListingSource, MarketListingStatus, MarketTransaction


MARKETPLACE_LISTING_DURATION_HOURS = 168  # 7 days


# ──────────────────────────────────────────────────────────────
#  Helpers (reused / adapted from inventory app)
# ──────────────────────────────────────────────────────────────

def get_player_main_bag(player):
    return InventoryBag.objects.get_or_create(player=player, character_id=player.username)


def find_slot_by_instance(bag, instance_id):
    return bag.slots.filter(instance_id=instance_id, item_amount__gt=0).first()


def find_empty_slot(bag):
    empty = bag.slots.filter(item_amount=0).first()
    if empty:
        return empty
    max_index = bag.slots.aggregate(Max("slot_index"))["slot_index__max"]
    next_index = (max_index + 1) if max_index is not None else 0
    return InventorySlot(bag=bag, slot_index=next_index)


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
        slot.save()


def snapshot_slot(slot):
    """Build item_snapshot JSON from an InventorySlot."""
    return {
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
    }


def build_default_item_snapshot(item, amount):
    """Build a default item_snapshot from an Item model (for system listings)."""
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
    """Check if a snapshot represents a special instance (should not be stacked)."""
    if snap.get("rated"):
        return True
    if snap.get("ratingPrice", -1) >= 0:
        return True
    if snap.get("overallScore", 0) > 0:
        return True
    if snap.get("ingredient"):
        return True
    if snap.get("cookTime", 0) > 0:
        return True
    if snap.get("dishQuality", 3) != 3:
        return True
    if snap.get("remainingUses") is not None:
        return True
    if snap.get("reviewComment"):
        return True
    return False


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


# ──────────────────────────────────────────────────────────────
#  Core business logic
# ──────────────────────────────────────────────────────────────

@transaction.atomic
def create_listing(player, instance_id, amount, price):
    """List a player's inventory item on the marketplace."""
    bag, _ = get_player_main_bag(player)
    slot = find_slot_by_instance(bag, instance_id)
    if not slot:
        return None, "物品未找到"
    if slot.item_amount < amount:
        return None, "物品数量不足"
    if amount <= 0:
        return None, "数量必须大于0"
    if price <= 0:
        return None, "价格必须大于0"

    # Ensure seller earns at least 1 after commission
    commission = calculate_commission(price * amount)
    if price * amount - commission <= 0:
        return None, "价格过低，手续费后收入为0"

    # Get item info for snapshots
    item_name = ""
    item_type = ""
    icon_url = ""
    try:
        item = Item.objects.get(item_id=slot.item_id)
        item_name = item.item_name
        item_type = item.item_type
        icon_url = item.icon_url or ""
    except Item.DoesNotExist:
        pass

    snap = snapshot_slot(slot)
    snap["itemAmount"] = amount  # snapshot the listed amount

    now = timezone.now()
    listing = MarketListing.objects.create(
        source_type=MarketListingSource.PLAYER,
        seller=player,
        source_instance_id=slot.instance_id,
        item_id=slot.item_id,
        item_name_snapshot=item_name,
        item_type_snapshot=item_type,
        icon_url_snapshot=icon_url,
        total_amount=amount,
        remaining_amount=amount,
        price=price,
        status=MarketListingStatus.ACTIVE,
        item_snapshot=snap,
        rated=slot.rated,
        rating_price=slot.rating_price,
        overall_score=slot.overall_score,
        ingredient=slot.ingredient,
        item_bag_name=slot.item_bag_name,
        cook_time=slot.cook_time,
        dish_quality=slot.dish_quality,
        remaining_uses=slot.remaining_uses,
        review_comment=slot.review_comment,
        expires_at=now + timedelta(hours=MARKETPLACE_LISTING_DURATION_HOURS),
    )

    # Deduct from inventory
    slot.item_amount -= amount
    slot.save()
    clear_slot_if_empty(slot)

    return listing, None


@transaction.atomic
def delist_listing(player, listing_id):
    """Delist a player's active listing and return items to inventory."""
    try:
        listing = MarketListing.objects.select_for_update().get(pk=listing_id)
    except MarketListing.DoesNotExist:
        return None, "挂单不存在"

    if listing.seller_id != player.id:
        return None, "无权操作此挂单"
    if listing.status != MarketListingStatus.ACTIVE:
        return None, "挂单不在出售中状态"

    bag, _ = get_player_main_bag(player)

    if listing.remaining_amount > 0:
        _return_items_to_bag(bag, listing, listing.remaining_amount)

    listing.remaining_amount = 0
    listing.status = MarketListingStatus.DELISTED
    listing.save()

    return bag, None


@transaction.atomic
def delist_system_listing(listing_id):
    """Admin delist a system listing (no item return)."""
    try:
        listing = MarketListing.objects.select_for_update().get(pk=listing_id)
    except MarketListing.DoesNotExist:
        return None, "挂单不存在"

    if listing.source_type != MarketListingSource.SYSTEM:
        return None, "非系统挂单"

    listing.remaining_amount = 0
    listing.status = MarketListingStatus.DELISTED
    listing.save()
    return listing, None


@transaction.atomic
def buy_listing(buyer, listing_id, amount):
    """Buy items from a marketplace listing."""
    if amount <= 0:
        return None, "数量必须大于0"

    try:
        listing = MarketListing.objects.select_for_update().get(pk=listing_id)
    except MarketListing.DoesNotExist:
        return None, "挂单不存在"

    if listing.status != MarketListingStatus.ACTIVE:
        return None, "挂单不在出售中状态"

    if listing.expires_at and listing.expires_at <= timezone.now():
        # Expire it inline
        _expire_single_listing(listing)
        return None, "挂单已过期"

    if listing.source_type == MarketListingSource.PLAYER and listing.seller_id == buyer.id:
        return None, "不能购买自己的挂单"

    if amount > listing.remaining_amount:
        return None, "购买数量超过剩余数量"

    settlement = calculate_settlement(listing, amount)
    total_price = settlement["total_price"]

    # Get buyer bag
    buyer_bag, _ = get_player_main_bag(buyer)
    if buyer_bag.money < total_price:
        return None, "金币不足"

    # Lock bags in pk order to prevent deadlock
    if listing.source_type == MarketListingSource.PLAYER and listing.seller:
        seller_bag, _ = get_player_main_bag(listing.seller)
        bags = sorted([buyer_bag, seller_bag], key=lambda b: b.pk)
        for b in bags:
            InventoryBag.objects.select_for_update().get(pk=b.pk)
        # Re-fetch after locking
        buyer_bag.refresh_from_db()
        seller_bag.refresh_from_db()
    else:
        InventoryBag.objects.select_for_update().get(pk=buyer_bag.pk)
        buyer_bag.refresh_from_db()
        seller_bag = None

    # Re-check money after lock
    if buyer_bag.money < total_price:
        return None, "金币不足"

    # Deduct buyer money
    buyer_bag.money -= total_price
    buyer_bag._money_change_source = "marketplace_buy"
    buyer_bag.save()

    # Add money to seller (player listings only)
    if seller_bag and listing.source_type == MarketListingSource.PLAYER:
        seller_bag.money += settlement["seller_earnings"]
        seller_bag._money_change_source = "marketplace_sell"
        seller_bag.save()

    # Add items to buyer bag
    _add_snapshot_item_to_bag(buyer_bag, listing.item_snapshot, amount)

    # Update listing
    listing.remaining_amount -= amount
    if listing.remaining_amount <= 0:
        listing.status = MarketListingStatus.SOLD_OUT
    listing.save()

    # Create transaction record
    tx = MarketTransaction.objects.create(
        listing=listing,
        source_type=listing.source_type,
        seller=listing.seller,
        buyer=buyer,
        item_id=listing.item_id,
        item_name_snapshot=listing.item_name_snapshot,
        amount=amount,
        unit_price=listing.price,
        total_price=total_price,
        commission=settlement["commission"],
        seller_earnings=settlement["seller_earnings"],
        system_income=settlement["system_income"],
    )

    return {
        "bag": buyer_bag,
        "transaction": tx,
        "listing": listing,
    }, None


def expire_due_listings(limit=100):
    """Expire active listings that have passed their expires_at. Returns items to sellers."""
    now = timezone.now()
    qs = MarketListing.objects.select_for_update().filter(
        status=MarketListingStatus.ACTIVE,
        expires_at__lte=now,
    )[:limit]

    expired_ids = []
    for listing in qs:
        if listing.remaining_amount > 0 and listing.source_type == MarketListingSource.PLAYER and listing.seller:
            bag, _ = get_player_main_bag(listing.seller)
            _return_items_to_bag(bag, listing, listing.remaining_amount)
        listing.remaining_amount = 0
        listing.status = MarketListingStatus.EXPIRED
        listing.save()
        expired_ids.append(listing.pk)

    return expired_ids


def _expire_single_listing(listing):
    """Expire a single listing (caller must hold lock)."""
    if listing.remaining_amount > 0 and listing.source_type == MarketListingSource.PLAYER and listing.seller:
        bag, _ = get_player_main_bag(listing.seller)
        _return_items_to_bag(bag, listing, listing.remaining_amount)
    listing.remaining_amount = 0
    listing.status = MarketListingStatus.EXPIRED
    listing.save()


def _return_items_to_bag(bag, listing, amount):
    """Return items from a listing back to a player's bag."""
    snap = listing.item_snapshot
    special = is_special_instance(snap)

    if not special:
        # Try to stack on existing slot
        existing = bag.slots.filter(item_id=listing.item_id, item_amount__gt=0).first()
        if existing:
            existing.item_amount += amount
            existing.save()
            return

    # Create new slot
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
    slot.save()


def _add_snapshot_item_to_bag(bag, snap, amount):
    """Add items from a snapshot to a player's bag."""
    special = is_special_instance(snap)

    if not special:
        # Try to stack
        existing = bag.slots.filter(item_id=snap.get("itemID", 0), item_amount__gt=0).first()
        if existing:
            existing.item_amount += amount
            existing.save()
            return

    # Create new slot with new instance_id
    slot = find_empty_slot(bag)
    slot.item_id = snap.get("itemID", 0)
    slot.item_amount = amount
    slot.rated = snap.get("rated", False)
    slot.rating_price = snap.get("ratingPrice", -1)
    slot.overall_score = snap.get("overallScore", 0)
    slot.ingredient = snap.get("ingredient", "")
    # 用实际槽位索引生成，避免与买家背包中已有物品重名
    item = Item.objects.filter(item_id=slot.item_id).first()
    item_name = item.item_name if item else f"物品#{slot.item_id}"
    slot.item_bag_name = f"{item_name}_{slot.slot_index}"
    slot.cook_time = snap.get("cookTime", 0)
    slot.dish_quality = snap.get("dishQuality", 3)
    slot.remaining_uses = snap.get("remainingUses")
    slot.review_comment = snap.get("reviewComment", "")
    slot.save()


@transaction.atomic
def admin_buy_listing(buyer, listing_id, amount):
    """Admin purchase: buy items from a listing on behalf of a player."""
    if amount <= 0:
        return None, "数量必须大于0"

    try:
        listing = MarketListing.objects.select_for_update().get(pk=listing_id)
    except MarketListing.DoesNotExist:
        return None, "挂单不存在"

    if listing.status != MarketListingStatus.ACTIVE:
        return None, "挂单不在出售中状态"

    if listing.expires_at and listing.expires_at <= timezone.now():
        _expire_single_listing(listing)
        return None, "挂单已过期"

    if amount > listing.remaining_amount:
        return None, "购买数量超过剩余数量"

    settlement = calculate_settlement(listing, amount)
    total_price = settlement["total_price"]

    buyer_bag, _ = get_player_main_bag(buyer)
    if buyer_bag.money < total_price:
        return None, "金币不足"

    # Lock bags
    if listing.source_type == MarketListingSource.PLAYER and listing.seller:
        seller_bag, _ = get_player_main_bag(listing.seller)
        bags = sorted([buyer_bag, seller_bag], key=lambda b: b.pk)
        for b in bags:
            InventoryBag.objects.select_for_update().get(pk=b.pk)
        buyer_bag.refresh_from_db()
        seller_bag.refresh_from_db()
    else:
        InventoryBag.objects.select_for_update().get(pk=buyer_bag.pk)
        buyer_bag.refresh_from_db()
        seller_bag = None

    if buyer_bag.money < total_price:
        return None, "金币不足"

    buyer_bag.money -= total_price
    buyer_bag._money_change_source = "admin_marketplace_buy"
    buyer_bag.save()

    if seller_bag and listing.source_type == MarketListingSource.PLAYER:
        seller_bag.money += settlement["seller_earnings"]
        seller_bag._money_change_source = "admin_marketplace_sell"
        seller_bag.save()

    _add_snapshot_item_to_bag(buyer_bag, listing.item_snapshot, amount)

    listing.remaining_amount -= amount
    if listing.remaining_amount <= 0:
        listing.status = MarketListingStatus.SOLD_OUT
    listing.save()

    tx = MarketTransaction.objects.create(
        listing=listing,
        source_type=listing.source_type,
        seller=listing.seller,
        buyer=buyer,
        item_id=listing.item_id,
        item_name_snapshot=listing.item_name_snapshot,
        amount=amount,
        unit_price=listing.price,
        total_price=total_price,
        commission=settlement["commission"],
        seller_earnings=settlement["seller_earnings"],
        system_income=settlement["system_income"],
    )

    return {
        "bag": buyer_bag,
        "transaction": tx,
        "listing": listing,
    }, None


def create_system_listing(item_id, amount, price, expires_at=None, snapshot_overrides=None):
    """Create a system listing from admin."""
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
