from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("player", "0002_total_game_days"),
    ]

    operations = [
        migrations.AddField(
            model_name="playerstate",
            name="game_hour",
            field=models.IntegerField(default=7),
        ),
        migrations.AddField(
            model_name="playerstate",
            name="game_minute",
            field=models.IntegerField(default=0),
        ),
    ]
