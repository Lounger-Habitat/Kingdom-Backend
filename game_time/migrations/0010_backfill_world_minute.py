# Data migration: backfill world_minute from existing calendar fields

from django.db import migrations


def backfill_world_minute(apps, schema_editor):
    """从现有日历字段反算 world_minute。"""
    GlobalGameTimeState = apps.get_model("game_time", "GlobalGameTimeState")
    from datetime import date

    for state in GlobalGameTimeState.objects.all():
        try:
            d = date(state.game_year, state.game_month, state.game_day)
            state.world_minute = (d.toordinal() - 1) * 1440 + state.game_hour * 60 + state.game_minute
            state.save(update_fields=["world_minute"])
        except ValueError:
            # 无效日期，使用默认值
            state.world_minute = 0
            state.save(update_fields=["world_minute"])


def reverse_backfill(apps, schema_editor):
    """反向迁移：将 world_minute 重置为 0。"""
    GlobalGameTimeState = apps.get_model("game_time", "GlobalGameTimeState")
    GlobalGameTimeState.objects.all().update(world_minute=0)


class Migration(migrations.Migration):

    dependencies = [
        ("game_time", "0009_globalgametimestate_time_revision_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_world_minute, reverse_backfill),
    ]
