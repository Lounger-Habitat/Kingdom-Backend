import uuid
from django.db import migrations, models


def split_shelves(apps, schema_editor):
    alias = schema_editor.connection.alias
    Listing = apps.get_model("village", "VillageStoreListing")
    Slot = apps.get_model("village", "VillageStoreInventorySlot")
    Item = apps.get_model("items", "Item")
    Item.objects.using(alias).filter(item_type__in=["食材", "肉类", "蔬菜", "水果"]).update(quality_enabled=True)
    for listing in list(Listing.objects.using(alias).all()):
        qualities = list(Slot.objects.using(alias).filter(listing=listing, item_amount__gt=0)
                         .order_by("quality").values_list("quality", flat=True).distinct())
        if not qualities:
            continue
        Listing.objects.using(alias).filter(pk=listing.pk).update(quality=qualities[0])
        for quality in qualities[1:]:
            target = Listing.objects.using(alias).create(
                store_id=listing.store_id, item_id=listing.item_id, quality=quality,
                price_mode=listing.price_mode, unit_price=listing.unit_price, status=listing.status,
                public_meal_code=listing.public_meal_code, subsidy_decision_id=listing.subsidy_decision_id,
                listed_by_npc_id=listing.listed_by_npc_id, listed_world_minute=listing.listed_world_minute,
                created_request_id=uuid.uuid5(listing.pk, f"quality-migration:{quality}"))
            Slot.objects.using(alias).filter(listing=listing, quality=quality).update(listing=target)


class Migration(migrations.Migration):
    dependencies = [("village", "0085_villagestoreinventoryslotusage_equipment_level")]
    operations = [
        migrations.AddField(model_name="villagestorelisting", name="quality", field=models.IntegerField(default=3)),
        migrations.RunPython(split_shelves, migrations.RunPython.noop),
    ]
