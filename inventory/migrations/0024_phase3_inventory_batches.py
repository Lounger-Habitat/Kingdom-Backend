"""Add Phase 3 batch-tracking fields to NpcGlobalInventorySlot."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0023_phase2_needs_models"),
    ]

    operations = [
        migrations.AddField(
            model_name="npcglobalinventoryslot",
            name="produced_world_minute",
            field=models.BigIntegerField(blank=True, null=True, verbose_name="生产世界分钟"),
        ),
        migrations.AddField(
            model_name="npcglobalinventoryslot",
            name="expires_world_minute",
            field=models.BigIntegerField(blank=True, null=True, verbose_name="过期世界分钟"),
        ),
        migrations.AddField(
            model_name="npcglobalinventoryslot",
            name="source_request_id",
            field=models.UUIDField(blank=True, null=True, verbose_name="来源请求ID"),
        ),
    ]
