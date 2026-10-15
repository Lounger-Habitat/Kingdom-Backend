import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("village", "0073_daily_event_npc_scope")]
    operations = [migrations.CreateModel(
        name="VillageActionTask",
        fields=[
            ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
            ("environment", models.CharField(max_length=128)),
            ("sender", models.CharField(max_length=32)),
            ("action_id", models.CharField(max_length=256)),
            ("epoch", models.UUIDField()),
            ("envelope", models.JSONField()),
            ("result", models.JSONField(default=dict, blank=True)),
            ("command", models.OneToOneField(null=True, blank=True, related_name="star_action_task",
                on_delete=django.db.models.deletion.CASCADE, to="village.npccommandstate")),
        ],
        options={"constraints": [models.UniqueConstraint(
            fields=("environment", "sender", "action_id", "epoch"), name="star_action_owner_identity")]},
    )]
