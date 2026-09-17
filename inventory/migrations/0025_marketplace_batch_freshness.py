from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("inventory", "0024_phase3_inventory_batches")]
    operations = [
        migrations.AddField(model_name="inventoryslot", name="produced_world_minute", field=models.BigIntegerField(blank=True, null=True)),
        migrations.AddField(model_name="inventoryslot", name="expires_world_minute", field=models.BigIntegerField(blank=True, null=True)),
        migrations.AddField(model_name="inventoryslot", name="source_request_id", field=models.UUIDField(blank=True, null=True)),
        migrations.AddField(model_name="inventoryslot", name="freshness_unknown", field=models.BooleanField(default=False)),
        migrations.AddField(model_name="npcglobalinventoryslot", name="freshness_unknown", field=models.BooleanField(default=False)),
    ]
