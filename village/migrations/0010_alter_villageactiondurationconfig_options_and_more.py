from django.db import migrations, models


TIMED_ACTION_DURATIONS = {
    "WAIT": 1,
    "EAT": 30,
    "REST": 30,
    "SLEEP": 30,
    "SOCIALIZE": 30,
    "WORK_PLACEHOLDER": 30,
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

NAVIGATION_TIMEOUTS = {"MOVE": 180, "GO_HOME": 180}


def seed_all_action_configs(apps, schema_editor):
    config = apps.get_model("village", "VillageActionDurationConfig")
    for action_name, duration_minutes in TIMED_ACTION_DURATIONS.items():
        config.objects.get_or_create(
            action_name=action_name,
            defaults={"duration_minutes": duration_minutes},
        )
    for action_name, timeout_seconds in NAVIGATION_TIMEOUTS.items():
        config.objects.get_or_create(
            action_name=action_name,
            defaults={"navigation_timeout_seconds": timeout_seconds},
        )


class Migration(migrations.Migration):

    dependencies = [("village", "0009_villageactiondurationconfig")]

    operations = [
        migrations.AlterModelOptions(
            name="villageactiondurationconfig",
            options={
                "ordering": ["action_name"],
                "verbose_name": "村庄动作时长配置",
                "verbose_name_plural": "村庄动作时长配置",
            },
        ),
        migrations.RemoveConstraint(
            model_name="villageactiondurationconfig",
            name="village_action_duration_positive",
        ),
        migrations.AddField(
            model_name="villageactiondurationconfig",
            name="navigation_timeout_seconds",
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name="导航超时（秒）"),
        ),
        migrations.AlterField(
            model_name="villageactiondurationconfig",
            name="action_name",
            field=models.CharField(
                choices=[
                    ("MOVE", "移动（MOVE）"), ("WAIT", "等待（WAIT）"), ("GO_HOME", "回家（GO_HOME）"),
                    ("EAT", "进食（EAT）"), ("REST", "休息（REST）"), ("SLEEP", "睡眠（SLEEP）"),
                    ("SOCIALIZE", "社交（SOCIALIZE）"), ("WORK_PLACEHOLDER", "工作（WORK_PLACEHOLDER）"),
                    ("PLANT", "播种（PLANT）"), ("HARVEST", "收获（HARVEST）"), ("HUNT", "狩猎（HUNT）"),
                    ("COOK", "烹饪（COOK）"), ("CREATE_ORDER", "创建订单（CREATE_ORDER）"),
                    ("CANCEL_ORDER", "取消订单（CANCEL_ORDER）"), ("START_DELIVERY", "开始配送（START_DELIVERY）"),
                    ("IMPORT_GOODS", "外部进货（IMPORT_GOODS）"), ("EXPORT_GOODS", "外部出货（EXPORT_GOODS）"),
                    ("RATE_DISH", "菜品评分（RATE_DISH）"), ("PUBLISH_PRICE_INDEX", "发布价格指数（PUBLISH_PRICE_INDEX）"),
                    ("AUDIT_PUBLIC_FUND", "审计公款（AUDIT_PUBLIC_FUND）"),
                ],
                max_length=32,
                unique=True,
                verbose_name="动作",
            ),
        ),
        migrations.AlterField(
            model_name="villageactiondurationconfig",
            name="duration_minutes",
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name="时长（世界分钟）"),
        ),
        migrations.AddConstraint(
            model_name="villageactiondurationconfig",
            constraint=models.CheckConstraint(
                condition=models.Q(duration_minutes__isnull=True) | models.Q(duration_minutes__gt=0),
                name="village_action_duration_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="villageactiondurationconfig",
            constraint=models.CheckConstraint(
                condition=models.Q(navigation_timeout_seconds__isnull=True) | models.Q(navigation_timeout_seconds__gt=0),
                name="village_navigation_timeout_positive",
            ),
        ),
        migrations.RunPython(seed_all_action_configs, migrations.RunPython.noop),
    ]
