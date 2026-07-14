from django.db import migrations


def reset_last_business_game_day(apps, schema_editor):
    """重置 last_business_game_day 为 0，修复从 game_day 切换回 total_game_days 的兼容问题。"""
    PlayerRestaurantState = apps.get_model('restaurant', 'PlayerRestaurantState')
    PlayerRestaurantState.objects.update(last_business_game_day=0)


class Migration(migrations.Migration):

    dependencies = [
        ('restaurant', '0002_rename_rush_to_daily_business'),
    ]

    operations = [
        migrations.RunPython(reset_last_business_game_day, migrations.RunPython.noop),
    ]
