from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("leaderboard", "0006_seasonresetproxy"),
    ]

    operations = [
        migrations.AddField(
            model_name="reportevent",
            name="season_id",
            field=models.CharField(max_length=50, default="", blank=True, db_index=True),
        ),
    ]
