from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("player", "0016_player_hunting_state")]
    operations = [migrations.CreateModel(name="PlayerMasteryChallenge", fields=[
        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
        ("skill_code", models.CharField(max_length=20, choices=[("gathering", "采集"), ("farming", "种植"), ("forestry", "伐木"), ("hunting", "狩猎"), ("cooking", "烹饪"), ("business", "经营")])),
        ("started_world_minute", models.BigIntegerField()),
        ("rule_version", models.PositiveIntegerField(default=1)),
        ("rules_snapshot", models.JSONField(default=dict)),
        ("contributed_world_minute", models.BigIntegerField(null=True, blank=True)),
        ("contribution_snapshot", models.JSONField(default=list)),
        ("claimed_world_minute", models.BigIntegerField(null=True, blank=True)),
        ("player", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="mastery_challenges", to="account.gameaccount")),
    ], options={"constraints": [models.UniqueConstraint(fields=["player", "skill_code"], name="player_mastery_skill_unique")]})]
