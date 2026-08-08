from django.db import migrations


class Migration(migrations.Migration):
    initial = True
    dependencies = [("game_time", "0008_chinese_global_time_labels")]
    operations = [
        migrations.CreateModel(
            name="ScheduledTaskDashboard",
            fields=[],
            options={
                "verbose_name": "当前定时任务",
                "verbose_name_plural": "当前定时任务",
                "proxy": True,
                "indexes": [],
                "constraints": [],
            },
            bases=("game_time.globalgametimestate",),
        ),
    ]
