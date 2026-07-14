from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('restaurant', '0005_add_calendar_fields_to_daily_log'),
    ]

    operations = [
        migrations.AddField(
            model_name='restaurantlevelconfig',
            name='required_dish_scores',
            field=models.JSONField(blank=True, default=list, help_text='[{"itemId": 1048, "minScore": 75}]', verbose_name='菜品评分要求'),
        ),
    ]
