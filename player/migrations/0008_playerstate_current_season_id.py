from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("player", "0007_add_show_ingredient_count"),
    ]

    operations = [
        migrations.AddField(
            model_name="playerstate",
            name="current_season_id",
            field=models.CharField(max_length=50, default="", blank=True),
        ),
    ]
