from django.db import migrations, models


def populate_calendar_fields(apps, schema_editor):
    """从 PlayerState 读取年月，用 game_day 作为 day_of_month 回填。"""
    DailyBusinessLog = apps.get_model("restaurant", "DailyBusinessLog")
    PlayerState = apps.get_model("player", "PlayerState")

    for log in DailyBusinessLog.objects.all():
        try:
            ps = PlayerState.objects.get(player_id=log.player.username)
            log.game_year = ps.game_year
            log.game_month = ps.game_month
        except PlayerState.DoesNotExist:
            log.game_year = 2026
            log.game_month = 3
        # 用 game_day（total_game_days）作为 day_of_month 保证旧数据唯一性
        log.game_day_of_month = log.game_day
        log.save(update_fields=["game_year", "game_month", "game_day_of_month"])


class Migration(migrations.Migration):

    dependencies = [
        ("restaurant", "0004_alter_dailybusinesslog_options_and_more"),
    ]

    operations = [
        # 1. 添加新字段
        migrations.AddField(
            model_name="dailybusinesslog",
            name="game_year",
            field=models.IntegerField(default=2026, verbose_name="游戏年"),
        ),
        migrations.AddField(
            model_name="dailybusinesslog",
            name="game_month",
            field=models.IntegerField(default=3, verbose_name="游戏月"),
        ),
        migrations.AddField(
            model_name="dailybusinesslog",
            name="game_day_of_month",
            field=models.IntegerField(default=8, verbose_name="游戏日"),
        ),
        # 2. 回填数据
        migrations.RunPython(populate_calendar_fields, migrations.RunPython.noop),
        # 3. 替换 unique_together
        migrations.AlterUniqueTogether(
            name="dailybusinesslog",
            unique_together={("player", "season_id", "game_year", "game_month", "game_day_of_month")},
        ),
    ]
