from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("game_time", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="gametimestate",
            name="game_hour",
            field=models.IntegerField(default=7),
        ),
        migrations.AddField(
            model_name="gametimestate",
            name="game_minute",
            field=models.IntegerField(default=0),
        ),
    ]
