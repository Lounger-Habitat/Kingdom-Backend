from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("player", "0015_starter_supply")]
    operations = [
        migrations.CreateModel(name="PlayerHuntingState", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("current_health", models.PositiveSmallIntegerField(default=100)),
            ("revision", models.PositiveBigIntegerField(default=0)),
            ("recovery_ready_world_minute", models.BigIntegerField(blank=True, null=True)),
            ("authority_session_id", models.CharField(blank=True, default="", max_length=128)),
            ("player", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="hunting_state", to="account.gameaccount")),
        ], options={"constraints": [models.CheckConstraint(condition=models.Q(current_health__lte=100), name="player_health_maximum")]}),
        migrations.CreateModel(name="PlayerCombatReceipt", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("request_id", models.UUIDField()),
            ("authority_session_id", models.CharField(max_length=128)),
            ("payload_hash", models.CharField(max_length=64)),
            ("action", models.CharField(max_length=32)),
            ("response", models.JSONField(default=dict)),
            ("world_minute", models.BigIntegerField()),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("player", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="combat_receipts", to="account.gameaccount")),
        ], options={"constraints": [models.UniqueConstraint(fields=("player", "request_id"), name="player_combat_request_unique")]}),
    ]
