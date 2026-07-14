"""Data migration: backfill item_bag_name with _{slot_index} suffix for existing slots."""

from django.db import migrations


def add_suffix(apps, schema_editor):
    InventorySlot = apps.get_model("inventory", "InventorySlot")
    Item = apps.get_model("items", "Item")

    # Build item_id -> item_name lookup
    item_names = {item.item_id: item.item_name for item in Item.objects.all()}

    updated = 0
    for slot in InventorySlot.objects.filter(item_amount__gt=0).exclude(item_id=0):
        base_name = item_names.get(slot.item_id, "")
        if not base_name:
            continue

        expected = f"{base_name}_{slot.slot_index}"
        if slot.item_bag_name != expected:
            slot.item_bag_name = expected
            slot.save(update_fields=["item_bag_name"])
            updated += 1

    print(f"\n  [backfill] Updated {updated} inventory slots with _{{slot_index}} suffix.")


def reverse_noop(apps, schema_editor):
    """Reverse migration is a no-op; cannot reliably strip suffixes."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0007_bagtemplateslot_hide_in_shop_and_more"),
        ("items", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(add_suffix, reverse_noop),
    ]
