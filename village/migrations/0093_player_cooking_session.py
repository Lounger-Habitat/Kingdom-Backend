import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("village", "0092_system_receipt_lookup_index"),
        ("account", "0004_gameaccount_appearance"),
    ]
    operations = [migrations.CreateModel(
        name="VillagePlayerCookingSession",
        fields=[
            ("player", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, primary_key=True, serialize=False, to="account.gameaccount")),
            ("session_id", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
            ("plan", models.JSONField(default=dict)),
            ("state", models.JSONField(default=dict)),
            ("status", models.CharField(default="ACTIVE", max_length=16)),
            ("started_at", models.DateTimeField()),
            ("updated_at", models.DateTimeField()),
            ("expires_at", models.DateTimeField()),
            ("settlement_request_id", models.UUIDField(blank=True, null=True)),
        ],
    )]
