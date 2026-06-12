from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("leaderboard", "0005_seasonconfig_freshness_top_n"),
    ]

    operations = [
        migrations.CreateModel(
            name="SeasonResetProxy",
            fields=[],
            options={
                "verbose_name": "赛季重置",
                "verbose_name_plural": "赛季重置",
                "proxy": True,
                "indexes": [],
                "constraints": [],
            },
            bases=("leaderboard.season",),
        ),
    ]
