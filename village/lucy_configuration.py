"""Idempotent definition/configuration rollout; never grants cash or supplies."""
def configure_lucy(apps=None):
    if apps is None:
        from django.apps import apps
    from .lucy_catalog import install_catalog, RECIPE_DEFINITIONS
    required = {i for row in RECIPE_DEFINITIONS.values() for i in row["ingredients"]}
    Item = apps.get_model("items", "Item")
    missing = sorted(required - set(Item.objects.filter(item_id__in=required, is_deleted=False).values_list("item_id", flat=True)))
    if not missing:
        install_catalog(apps)
    Profile = apps.get_model("village", "NpcProfile")
    for profile in Profile.objects.filter(npc__agent_id="10014", npc__is_test_only=False):
        profile.capabilities = list(dict.fromkeys([*[c for c in profile.capabilities if c != "UPDATE_STORE_PRICE"], "WITHDRAW_STORE_FUNDS", "REQUEST_DISH_REVIEW", "CANCEL_DISH_REVIEW"]))
        profile.save(update_fields=["capabilities"])
    Duration = apps.get_model("village", "VillageActionDurationConfig")
    for action in ("REQUEST_DISH_REVIEW", "CANCEL_DISH_REVIEW"):
        Duration.objects.get_or_create(action_name=action, defaults={"duration_minutes": 1})
    Account = apps.get_model("village", "VillageStoreAccount")
    from django.db.models import F
    Account.objects.filter(store_id="RESTAURANT_STORE").exclude(minimum_operating_balance=0).update(
        minimum_operating_balance=0, account_version=F("account_version") + 1)
    return {"catalog_ready": not missing, "missing_ingredient_ids": missing}


def normalize_existing_menu():
    """Reprice current unsold menu only; incomplete legacy ratings are delisted."""
    from .models import VillageStoreListing, VillageStoreInventorySlot, VillageStore
    from .lucy_cooking import fixed_price
    changes = []
    for listing in VillageStoreListing.objects.select_for_update().filter(store_id="RESTAURANT_STORE", status="ACTIVE", public_meal_code=""):
        reason = None
        try:
            prices = {fixed_price(s.item_id, s.item_attributes) for s in VillageStoreInventorySlot.objects.filter(listing=listing, item_amount__gt=0)}
            if len(prices) != 1:
                reason = "LEGACY_BATCH_PRICES_DIFFER"
        except ValueError as error:
            reason = str(error)
        old_price, old_mode = listing.unit_price, listing.price_mode
        if reason:
            listing.status = "DELISTED"
            VillageStoreInventorySlot.objects.filter(listing=listing).update(listing=None)
        else:
            listing.unit_price = prices.pop()
            listing.price_mode = "ORIGINAL"
        if reason or listing.unit_price != old_price or old_mode != "ORIGINAL":
            changes.append(dict(listing_id=str(listing.pk), old_price=old_price, new_price=listing.unit_price, status=listing.status, reason=reason))
        listing.save(update_fields=["status", "unit_price", "price_mode", "updated_at"])
    if changes:
        from django.db.models import F
        VillageStore.objects.filter(pk="RESTAURANT_STORE").update(inventory_version=F("inventory_version") + 1)
    return changes
