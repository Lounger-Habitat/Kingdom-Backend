from django.db import migrations, models
import django.db.models.deletion


def backfill_trade_proposer(apps, schema_editor):
    offers = apps.get_model("village", "VillageTradeOffer").objects.using(
        schema_editor.connection.alias
    )
    offers.filter(proposer_npc__isnull=True).update(proposer_npc_id=models.F("buyer_npc_id"))


def mark_historical_nights_evaluated(apps, schema_editor):
    summaries = apps.get_model("village", "NpcNeedsDaySummary").objects.using(
        schema_editor.connection.alias
    )
    # Do not retroactively punish every pre-migration night on the first Tick.
    summaries.update(core_night_evaluated=True)


class Migration(migrations.Migration):
    dependencies = [("village", "0031_add_get_rule_capability")]

    operations = [
        migrations.AddField(
            model_name="npcruntimestate",
            name="continuous_sleep_minutes",
            field=models.PositiveIntegerField(default=0, verbose_name="当前连续睡眠分钟"),
        ),
        migrations.AddField(
            model_name="npcruntimestate",
            name="last_sleep_end_world_minute",
            field=models.BigIntegerField(blank=True, null=True, verbose_name="最近睡眠结束世界分钟"),
        ),
        migrations.AddField(
            model_name="npcneedsdaysummary",
            name="longest_continuous_sleep_minutes",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="npcneedsdaysummary",
            name="core_night_sleep_minutes",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="npcneedsdaysummary",
            name="core_night_evaluated",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="npcneedsdaysummary",
            name="core_night_penalized",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="villagetradeoffer",
            name="proposer_npc",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="trade_offers_proposed",
                to="inventory.npcdefinition",
            ),
        ),
        migrations.RunPython(mark_historical_nights_evaluated, migrations.RunPython.noop),
        migrations.RunPython(backfill_trade_proposer, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="villagetradeoffer",
            name="proposer_npc",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="trade_offers_proposed",
                to="inventory.npcdefinition",
            ),
        ),
    ]
