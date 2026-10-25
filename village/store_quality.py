"""Keep one quality and one price on each real store shelf."""
import uuid
from django.db import transaction
from items.models import Item
from items.quality import has_quality, item_price


@transaction.atomic
def assign_listing(slot):
    from .models import VillageStore, VillageStoreListing
    VillageStore.objects.select_for_update().get(pk=slot.store_id)
    listing = VillageStoreListing.objects.select_for_update().get(pk=slot.listing_id)
    item = Item.objects.filter(pk=slot.item_id, is_deleted=False).first()
    if not has_quality(item) or listing.public_meal_code:
        return
    price = listing.unit_price
    if listing.price_mode == "ORIGINAL":
        if slot.store_id == "RESTAURANT_STORE":
            from .lucy_cooking import fixed_price
            price = fixed_price(slot.item_id, slot.item_attributes)
        else:
            from .phase3_catalog import BASE_PRICES
            price = item_price(item, slot.quality, BASE_PRICES.get(slot.item_id, item.item_price))
    if not listing.inventory_slots.filter(item_amount__gt=0).exists():
        listing.quality, listing.unit_price = slot.quality, price
        listing.save(update_fields=["quality", "unit_price", "updated_at"])
        slot.listing = listing
        return
    if listing.quality == slot.quality and listing.unit_price == price:
        slot.listing = listing
        return
    target = VillageStoreListing.objects.filter(store_id=slot.store_id, item_id=slot.item_id,
        quality=slot.quality, unit_price=price, price_mode=listing.price_mode,
        public_meal_code="", status="ACTIVE").first()
    if target is None:
        target, _ = VillageStoreListing.objects.get_or_create(
            created_request_id=uuid.uuid5(listing.pk, f"quality:{slot.quality}:price:{price}"),
            defaults=dict(store_id=slot.store_id, item_id=slot.item_id, quality=slot.quality,
                unit_price=price, price_mode=listing.price_mode, status="ACTIVE",
                listed_by_npc_id=listing.listed_by_npc_id, listed_world_minute=listing.listed_world_minute))
        if target.status != "ACTIVE":
            target.status = "ACTIVE"
            target.save(update_fields=["status", "updated_at"])
    slot.listing = target
