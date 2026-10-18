"""G3 cookware definitions and one finite supply for an existing formal store."""
from django.db import migrations
import uuid


def install(apps, schema_editor):
    alias = schema_editor.connection.alias
    Item = apps.get_model("items", "Item")
    Definition = apps.get_model("player", "ItemGameplayDefinition")
    for key, name, tier, uses, level, price in [(4106, "基础炊具", 1, 20, 1, 50),
                                               (4107, "改良炊具", 2, 50, 4, 150)]:
        item, _ = Item.objects.using(alias).get_or_create(item_id=key, defaults=dict(item_name=name,
            item_type="CollectTool", item_price=price, max_use_count=uses, stackable=False, collectable=False))
        if item.item_name != name or item.item_type != "CollectTool":
            raise ValueError(f"G3 cookware item ID collision: {key}")
        Definition.objects.using(alias).get_or_create(item_id=key, defaults=dict(capability_code="cooking",
            tier=tier, required_skill="cooking", required_level=level,
            effects=[dict(code="standard_cooking_batches", maximum=3 if tier == 2 else 1)]))
    # Existing villages receive the initial independent cookware supply during
    # rollout. A new empty village can install it later through the same marker
    # in ensure_production_catalog; neither path creates a store or wallet.
    Store = apps.get_model("village", "VillageStore")
    Account = apps.get_model("village", "VillageStoreAccount")
    Listing = apps.get_model("village", "VillageStoreListing")
    Slot = apps.get_model("village", "VillageStoreInventorySlot")
    Usage = apps.get_model("village", "VillageStoreInventorySlotUsage")
    Ledger = apps.get_model("village", "EconomicLedgerEntry")
    Clock = apps.get_model("game_time", "GlobalGameTimeState")
    marker = uuid.uuid5(uuid.NAMESPACE_URL, "kingdom:g3:cooking-stock:4106:v1")
    store = Store.objects.using(alias).filter(pk="GENERAL_STORE", operator_npc__agent_id="10015",
                                              operator_npc__is_test_only=False).first()
    if not store or not Account.objects.using(alias).filter(store_id=store.pk).exists() or Ledger.objects.using(alias).filter(request_id=marker).exists():
        return
    minute = Clock.objects.using(alias).filter(pk=1).values_list("world_minute", flat=True).first() or 0
    listing = Listing.objects.using(alias).create(listing_id=uuid.uuid5(marker, "listing"), store_id=store.pk,
        item_id=4106, unit_price=50, price_mode="ORIGINAL", status="ACTIVE", listed_by_npc_id=store.operator_npc_id,
        created_request_id=marker, listed_world_minute=minute)
    for index in range(20):
        slot = Slot.objects.using(alias).create(instance_id=uuid.uuid5(marker, str(index)), store_id=store.pk,
            listing_id=listing.pk, item_id=4106, item_amount=1, quality=3, produced_world_minute=minute,
            common_attributes={"initial_stock_code": "G3_COOKING"})
        Usage.objects.using(alias).create(owner_id=slot.pk, remaining=20)
    Ledger.objects.using(alias).create(request_id=marker, entry_type="G3_INITIAL_STOCK", world_minute=minute,
        changes={"STORE:GENERAL_STORE": {"4106": 20}, "source": "G3_FINITE_COOKING_STOCK"})
    store.inventory_version += 1
    store.save(using=alias, update_fields=["inventory_version"])


class Migration(migrations.Migration):
    dependencies = [("items", "0009_production_tools_and_crops")]
    operations = [migrations.RunPython(install, migrations.RunPython.noop)]
