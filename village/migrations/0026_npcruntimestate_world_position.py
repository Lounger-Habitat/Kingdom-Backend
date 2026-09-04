from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("village", "0025_remove_inspect_orders_capability")]
    operations = [
        migrations.AddField(
            model_name="npcruntimestate",
            name="world_position",
            field=models.JSONField(blank=True, default=dict, verbose_name="世界坐标"),
        ),
    ]
