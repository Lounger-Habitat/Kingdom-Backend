import django.db.models.deletion
from django.db import migrations, models


def remove_daily_and_milestone_rewards(apps, schema_editor):
    RewardConfig = apps.get_model("leaderboard", "RewardConfig")
    PendingReward = apps.get_model("leaderboard", "PendingReward")
    PeriodConfig = apps.get_model("leaderboard", "LeaderboardPeriodConfig")
    RewardConfig.objects.filter(settlement_type__in=["daily", "milestone"]).delete()
    PendingReward.objects.filter(settlement_type__in=["daily", "milestone"]).delete()
    PeriodConfig.objects.get_or_create(name="每24小时", defaults={"interval_hours": 24, "enabled": True, "reward_expire_hours": 168})


class Migration(migrations.Migration):
    dependencies = [("leaderboard", "0014_alter_leaderboardentry_board_type_and_more")]
    operations = [
        migrations.CreateModel(name="LeaderboardPeriodConfig", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("name", models.CharField(max_length=100, unique=True)),
            ("interval_hours", models.PositiveSmallIntegerField(default=24)),
            ("enabled", models.BooleanField(default=True)),
            ("reward_expire_hours", models.PositiveIntegerField(default=168)),
        ], options={"ordering": ["interval_hours", "name"]}),
        migrations.AddField(model_name="leaderboardentry", name="period_end", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="leaderboardentry", name="period_key", field=models.CharField(blank=True, db_index=True, default="", max_length=160)),
        migrations.AddField(model_name="leaderboardentry", name="period_start", field=models.DateTimeField(blank=True, null=True)),
        migrations.AlterField(model_name="rewardconfig", name="settlement_type", field=models.CharField(choices=[("periodic", "周期结算"), ("season", "赛季结算"), ("milestone", "里程碑结算")], default="periodic", max_length=20)),
        migrations.AddField(model_name="rewardconfig", name="period_config", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="reward_configs", to="leaderboard.leaderboardperiodconfig")),
        migrations.RunPython(remove_daily_and_milestone_rewards, migrations.RunPython.noop),
    ]
