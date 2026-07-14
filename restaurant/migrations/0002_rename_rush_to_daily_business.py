from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('restaurant', '0001_initial'),
    ]

    operations = [
        # 重命名字段
        migrations.RenameField(
            model_name='restaurantconfig',
            old_name='customer_rush_hour',
            new_name='business_hour',
        ),
        migrations.RenameField(
            model_name='restaurantconfig',
            old_name='customer_rush_minute',
            new_name='business_minute',
        ),
        migrations.RenameField(
            model_name='playerrestaurantstate',
            old_name='last_rush_game_day',
            new_name='last_business_game_day',
        ),
        # 重命名模型
        migrations.RenameModel(
            old_name='CustomerRushLog',
            new_name='DailyBusinessLog',
        ),
    ]
