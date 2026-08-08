from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("leaderboard", "0017_chinese_period_field_labels")]
    operations = [
        migrations.AlterModelOptions(name="leaderboardentry", options={"ordering": ["board_type", "game_day", "rank"], "verbose_name": "排行榜条目", "verbose_name_plural": "排行榜条目"}),
        migrations.AlterModelOptions(name="pendingreward", options={"ordering": ["-expire_time"], "verbose_name": "待领取排行榜奖励", "verbose_name_plural": "待领取排行榜奖励"}),
        migrations.AlterModelOptions(name="reportevent", options={"ordering": ["-timestamp"], "verbose_name": "排行榜上报事件", "verbose_name_plural": "排行榜上报事件"}),
        migrations.AlterModelOptions(name="rewardconfig", options={"ordering": ["board_type", "settlement_type", "rank_min"], "verbose_name": "排行榜奖励配置", "verbose_name_plural": "排行榜奖励配置"}),
    ]
