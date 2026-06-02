from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("player", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="playerstate",
            name="total_game_days",
            field=models.IntegerField(default=1),
        ),
        migrations.AddField(
            model_name="playerstate",
            name="time_initialized",
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name="playerstate",
            name="game_day",
            field=models.IntegerField(default=8),
        ),
        migrations.AlterField(
            model_name="playerstate",
            name="game_month",
            field=models.IntegerField(default=3),
        ),
    ]
