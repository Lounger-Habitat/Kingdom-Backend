from django.db import migrations, models


DEFAULT_DURATIONS = {
    "PLANT": 60,
    "HARVEST": 90,
    "HUNT": 120,
    "COOK": 90,
    "CREATE_ORDER": 1,
    "CANCEL_ORDER": 1,
    "START_DELIVERY": 1,
    "IMPORT_GOODS": 120,
    "EXPORT_GOODS": 120,
    "RATE_DISH": 30,
    "PUBLISH_PRICE_INDEX": 30,
    "AUDIT_PUBLIC_FUND": 30,
}


def seed_action_durations(apps, schema_editor):
    config = apps.get_model("village", "VillageActionDurationConfig")
    for action_name, duration_minutes in DEFAULT_DURATIONS.items():
        config.objects.get_or_create(
            action_name=action_name,
            defaults={"duration_minutes": duration_minutes},
        )


class Migration(migrations.Migration):

    dependencies = [
        ("village", "0008_phase3_economy_models"),
    ]

    operations = [
        migrations.CreateModel(
            name="VillageActionDurationConfig",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("action_name", models.CharField(choices=[
                    ("PLANT", "PLANT"),
                    ("HARVEST", "HARVEST"),
                    ("HUNT", "HUNT"),
                    ("COOK", "COOK"),
                    ("CREATE_ORDER", "CREATE_ORDER"),
                    ("CANCEL_ORDER", "CANCEL_ORDER"),
                    ("START_DELIVERY", "START_DELIVERY"),
                    ("IMPORT_GOODS", "IMPORT_GOODS"),
                    ("EXPORT_GOODS", "EXPORT_GOODS"),
                    ("RATE_DISH", "RATE_DISH"),
                    ("PUBLISH_PRICE_INDEX", "PUBLISH_PRICE_INDEX"),
                    ("AUDIT_PUBLIC_FUND", "AUDIT_PUBLIC_FUND"),
                ], max_length=32, unique=True, verbose_name="动作")),
                ("duration_minutes", models.PositiveIntegerField(verbose_name="时长（世界分钟）")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
            ],
            options={
                "verbose_name": "经济动作时长",
                "verbose_name_plural": "经济动作时长",
                "ordering": ["action_name"],
            },
        ),
        migrations.AddConstraint(
            model_name="villageactiondurationconfig",
            constraint=models.CheckConstraint(
                condition=models.Q(duration_minutes__gt=0),
                name="village_action_duration_positive",
            ),
        ),
        migrations.RunPython(seed_action_durations, migrations.RunPython.noop),
    ]
