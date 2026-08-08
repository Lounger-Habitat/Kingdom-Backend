from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("game_time", "0007_remove_globalgametimestate_total_game_days")]
    operations = [
        migrations.AlterField(model_name="globalgametimestate", name="current_season_id", field=models.CharField(blank=True, default="", max_length=50, verbose_name="当前赛季标识")),
        migrations.AlterField(model_name="globalgametimestate", name="game_day", field=models.IntegerField(default=8, help_text="日历日（每月中的第几日）", verbose_name="游戏日")),
        migrations.AlterField(model_name="globalgametimestate", name="game_hour", field=models.IntegerField(default=7, verbose_name="游戏小时")),
        migrations.AlterField(model_name="globalgametimestate", name="game_minute", field=models.IntegerField(default=0, verbose_name="游戏分钟")),
        migrations.AlterField(model_name="globalgametimestate", name="game_month", field=models.IntegerField(default=3, verbose_name="游戏月")),
        migrations.AlterField(model_name="globalgametimestate", name="game_year", field=models.IntegerField(default=2026, verbose_name="游戏年")),
        migrations.AlterField(model_name="globalgametimestate", name="season", field=models.IntegerField(default=0, verbose_name="赛季序号")),
        migrations.AlterField(model_name="globalgametimestate", name="updated_at", field=models.DateTimeField(auto_now=True, verbose_name="更新时间")),
    ]
