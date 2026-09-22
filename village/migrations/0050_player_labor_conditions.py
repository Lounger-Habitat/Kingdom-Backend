from django.db import migrations, models


def interrupt_legacy_sessions(apps, schema_editor):
    # A pre-upgrade Env cannot attest continuity for a condition-bound session.
    # Preserve already credited units; discard only the unfinished interval.
    apps.get_model("village", "VillagePlayerAffairLabor").objects.filter(status="ACTIVE").update(
        status="CANCELLED", accumulated_minutes=0, stop_reason="CONDITIONS_MIGRATED")


class Migration(migrations.Migration):
    dependencies = [("village", "0049_flexible_affair_conditions")]
    operations = [
        migrations.AddField(model_name="villageplayeraffairlabor", name="condition_id",
                            field=models.CharField(default="legacy_labor", max_length=64)),
        migrations.AddField(model_name="villageplayeraffairlabor", name="unit_minutes",
                            field=models.PositiveIntegerField(default=60)),
        migrations.RunPython(interrupt_legacy_sessions, migrations.RunPython.noop),
    ]
