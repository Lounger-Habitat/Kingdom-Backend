import django.utils.timezone
from django.db import migrations, models


def set_existing_players_to_200(apps, schema_editor):
    PlayerState = apps.get_model("player", "PlayerState")
    PlayerState.objects.update(action_points=200, action_points_recovered_at=django.utils.timezone.now())


class Migration(migrations.Migration):
    dependencies = [("player", "0011_playerstate_total_wealth_snapshot")]
    operations = [
        migrations.RemoveField(model_name="playerstate", name="day_start_money"),
        migrations.RemoveField(model_name="playerstate", name="day_start_money_game_day"),
        migrations.RemoveField(model_name="playerstate", name="game_day"),
        migrations.RemoveField(model_name="playerstate", name="game_hour"),
        migrations.RemoveField(model_name="playerstate", name="game_minute"),
        migrations.RemoveField(model_name="playerstate", name="game_month"),
        migrations.RemoveField(model_name="playerstate", name="game_year"),
        migrations.RemoveField(model_name="playerstate", name="season"),
        migrations.RemoveField(model_name="playerstate", name="time_initialized"),
        migrations.RemoveField(model_name="playerstate", name="total_game_days"),
        migrations.RemoveField(model_name="playerstate", name="total_wealth_snapshot"),
        migrations.AddField(model_name="playerstate", name="action_points_recovered_at", field=models.DateTimeField(default=django.utils.timezone.now)),
        migrations.AlterField(model_name="playerstate", name="action_points", field=models.IntegerField(default=200)),
        migrations.RunPython(set_existing_players_to_200, migrations.RunPython.noop),
    ]
