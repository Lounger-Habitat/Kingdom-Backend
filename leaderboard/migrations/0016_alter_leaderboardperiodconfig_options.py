from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("leaderboard", "0015_periodic_leaderboards")]
    operations = [
        migrations.AlterModelOptions(
            name="leaderboardperiodconfig",
            options={"ordering": ["interval_hours", "name"], "verbose_name": "排行榜周期配置", "verbose_name_plural": "排行榜周期配置"},
        ),
    ]
