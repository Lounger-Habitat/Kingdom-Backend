import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("village", "0046_village_affairs"), ("account", "0003_alter_gameaccount_options")]

    operations = [
        migrations.CreateModel(
            name="VillagePlayerAffairLabor",
            fields=[
                ("labor_id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("authority_session_id", models.UUIDField()),
                ("status", models.CharField(default="ACTIVE", max_length=16)),
                ("location_code", models.CharField(max_length=64)),
                ("accumulated_minutes", models.PositiveIntegerField(default=0)),
                ("completed_units", models.PositiveIntegerField(default=0)),
                ("last_world_minute", models.BigIntegerField()),
                ("last_time_revision", models.BigIntegerField()),
                ("last_seen_at", models.DateTimeField()),
                ("stop_reason", models.CharField(blank=True, default="", max_length=64)),
                ("affair", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="village.villageaffair")),
                ("player", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="account.gameaccount")),
            ],
            options={"constraints": [models.UniqueConstraint(condition=models.Q(status="ACTIVE"), fields=("player",),
                                                               name="player_one_active_affair_labor")]},
        ),
    ]
