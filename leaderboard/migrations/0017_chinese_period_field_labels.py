from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("leaderboard", "0016_alter_leaderboardperiodconfig_options")]
    operations = [
        migrations.AlterField(model_name="leaderboardperiodconfig", name="name", field=models.CharField(max_length=100, unique=True, verbose_name="配置名称")),
        migrations.AlterField(model_name="leaderboardperiodconfig", name="interval_hours", field=models.PositiveSmallIntegerField(default=24, verbose_name="结算周期（小时）")),
        migrations.AlterField(model_name="leaderboardperiodconfig", name="enabled", field=models.BooleanField(default=True, verbose_name="启用")),
        migrations.AlterField(model_name="leaderboardperiodconfig", name="reward_expire_hours", field=models.PositiveIntegerField(default=168, verbose_name="奖励过期时间（小时）")),
    ]
