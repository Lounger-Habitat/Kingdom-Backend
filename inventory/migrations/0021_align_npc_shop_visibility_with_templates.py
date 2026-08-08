from django.db import migrations


def align_npc_shop_visibility_with_templates(apps, schema_editor):
    BagTemplateSlot = apps.get_model("inventory", "BagTemplateSlot")
    NpcGlobalInventory = apps.get_model("inventory", "NpcGlobalInventory")

    for bag in NpcGlobalInventory.objects.all().iterator():
        visible_item_ids = list(
            BagTemplateSlot.objects.filter(
                template__npc_id=bag.npc_id,
                hide_in_shop=False,
                item_id__isnull=False,
            ).values_list("item_id", flat=True)
        )

        bag.slots.filter(item_id__in=visible_item_ids).update(hide_in_shop=False)
        bag.slots.exclude(item_id__in=visible_item_ids).update(hide_in_shop=True)


class Migration(migrations.Migration):
    dependencies = [("inventory", "0020_chinese_model_labels")]

    operations = [
        migrations.RunPython(
            align_npc_shop_visibility_with_templates,
            migrations.RunPython.noop,
        ),
    ]
