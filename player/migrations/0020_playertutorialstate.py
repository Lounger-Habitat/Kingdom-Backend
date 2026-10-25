from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("player", "0019_seed_equipment_levels")]
    operations = [migrations.CreateModel(name="PlayerTutorialState", fields=[
        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
        ("version", models.PositiveIntegerField(default=1)),
        ("revision", models.PositiveBigIntegerField(default=0)),
        ("started", models.BooleanField(default=False)),
        ("paused", models.BooleanField(default=False)),
        ("route_code", models.CharField(default="basics", max_length=20)),
        ("evidence", models.JSONField(blank=True, default=dict)),
        ("completed", models.JSONField(blank=True, default=dict)),
        ("player", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE,
            related_name="tutorial_state", to="account.gameaccount")),
    ], options={"verbose_name": "玩家教学进度", "verbose_name_plural": "玩家教学进度"})]
