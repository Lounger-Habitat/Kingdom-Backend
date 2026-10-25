import uuid
from django.db import migrations


def supply_weapons(apps, schema_editor):
    """Existing villages get the same finite supply as a newly initialized world."""
    alias = schema_editor.connection.alias
    Store = apps.get_model("village", "VillageStore")
    store = Store.objects.using(alias).filter(pk="GENERAL_STORE", operator_npc__agent_id="10015").first()
    if not store or not apps.get_model("village", "VillageStoreAccount").objects.using(alias).filter(store=store).exists():
        return
    Ledger = apps.get_model("village", "EconomicLedgerEntry")
    Listing = apps.get_model("village", "VillageStoreListing")
    Stock = apps.get_model("village", "VillageStoreInventorySlot")
    Usage = apps.get_model("village", "VillageStoreInventorySlotUsage")
    Item = apps.get_model("items", "Item")
    minute = apps.get_model("game_time", "GlobalGameTimeState").objects.using(alias).values_list("world_minute", flat=True).first() or 0
    installed = False
    for item in Item.objects.using(alias).filter(pk__in=[4110, 4112], is_deleted=False):
        marker = uuid.uuid5(uuid.NAMESPACE_URL, f"kingdom:g2:production-stock:{item.pk}:v1")
        if Ledger.objects.using(alias).filter(request_id=marker).exists():
            continue
        listing = Listing.objects.using(alias).create(listing_id=uuid.uuid5(marker, "listing"), store=store,
            item_id=item.pk, unit_price=max(1, item.item_price), price_mode="ORIGINAL", status="ACTIVE",
            listed_by_npc_id=store.operator_npc_id, created_request_id=marker, listed_world_minute=minute)
        for index in range(20):
            slot = Stock.objects.using(alias).create(instance_id=uuid.uuid5(marker, str(index)), store=store,
                listing=listing, item_id=item.pk, item_amount=1, quality=3,
                produced_world_minute=minute, common_attributes={"initial_stock_code": "G2_PRODUCTION"})
            Usage.objects.using(alias).create(owner_id=slot.pk, remaining=item.max_use_count)
        Ledger.objects.using(alias).create(request_id=marker, entry_type="G2_INITIAL_STOCK", world_minute=minute,
            changes={"STORE:GENERAL_STORE": {str(item.pk): 20}, "source": "G2_FINITE_PRODUCTION_STOCK"})
        installed = True
    if installed:
        store.inventory_version += 1
        store.save(using=alias, update_fields=["inventory_version", "updated_at"])


class Migration(migrations.Migration):
    dependencies = [("village", "0083_processing_queue")]
    operations = [migrations.RunPython(supply_weapons, migrations.RunPython.noop)]
