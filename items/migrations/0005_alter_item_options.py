from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("items", "0004_item_max_use_count_item_use_effect_data_and_more")]
    operations = [
        migrations.AlterModelOptions(
            name="item",
            options={"ordering": ["item_id"], "verbose_name": "道具", "verbose_name_plural": "道具"},
        ),
    ]
