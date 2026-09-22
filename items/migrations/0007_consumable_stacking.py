from django.db import migrations, models
from django.utils import timezone


def correct_catalog(apps, schema_editor):
    items = apps.get_model("items", "Item").objects.using(schema_editor.connection.alias)
    items.filter(item_id=1010, item_type="HoeTool", use_effect_type="").update(
        item_type="Commodity", stackable=True, updated_at=timezone.now())
    items.filter(item_id=1011).update(stackable=False, updated_at=timezone.now())


def restore_catalog(apps, schema_editor):
    items = apps.get_model("items", "Item").objects.using(schema_editor.connection.alias)
    items.filter(item_id=1010, item_type="Commodity", use_effect_type="").update(
        item_type="HoeTool", stackable=True, updated_at=timezone.now())
    items.filter(item_id=1011).update(stackable=True, updated_at=timezone.now())


class Migration(migrations.Migration):
    dependencies = [("items", "0006_inventory_display_rules")]
    operations = [
        migrations.AlterField(
            model_name="item", name="stackable",
            field=models.BooleanField(default=True, help_text="允许跨批次展示堆叠；未使用的一次性消耗品可堆叠，菜品和工具仍独立展示")),
        migrations.RunPython(correct_catalog, restore_catalog),
    ]
