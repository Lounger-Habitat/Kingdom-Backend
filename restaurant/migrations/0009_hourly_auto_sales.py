import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("account", "0002_gameaccount_is_ai"), ("restaurant", "0008_alter_menuitem_overall_score")]
    operations = [
        migrations.AlterModelOptions(name="menuitem", options={"ordering": ["item_name", "id"], "verbose_name": "菜单菜品", "verbose_name_plural": "菜单菜品"}),
        migrations.RemoveField(model_name="menuitem", name="exhibited_game_day"),
        migrations.RemoveField(model_name="playerrestaurantstate", name="last_business_game_day"),
        migrations.RemoveField(model_name="restaurantconfig", name="base_customer_count"),
        migrations.RemoveField(model_name="restaurantconfig", name="business_hour"),
        migrations.RemoveField(model_name="restaurantconfig", name="business_minute"),
        migrations.RemoveField(model_name="restaurantconfig", name="customers_per_level"),
        migrations.AddField(model_name="restaurantconfig", name="base_hourly_sales", field=models.IntegerField(default=3, verbose_name="每小时基础售卖数")),
        migrations.AddField(model_name="restaurantconfig", name="base_menu_capacity", field=models.IntegerField(default=5, verbose_name="基础菜单总份数上限")),
        migrations.AddField(model_name="restaurantconfig", name="closing_hour", field=models.IntegerField(default=19, help_text="19:00 停止营业", verbose_name="营业结束小时")),
        migrations.AddField(model_name="restaurantconfig", name="hourly_sales_per_level", field=models.IntegerField(default=1, verbose_name="每级每小时增加售卖数")),
        migrations.AddField(model_name="restaurantconfig", name="menu_capacity_per_level", field=models.IntegerField(default=1, verbose_name="每级菜单总份数增加")),
        migrations.AddField(model_name="restaurantconfig", name="opening_hour", field=models.IntegerField(default=10, verbose_name="营业开始小时")),
        migrations.CreateModel(name="RestaurantHourlySlot", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")), ("game_year", models.IntegerField()), ("game_month", models.IntegerField()), ("game_day", models.IntegerField()), ("game_hour", models.IntegerField()), ("quota", models.PositiveIntegerField(default=0)), ("sold_count", models.PositiveIntegerField(default=0)), ("restaurant_level", models.PositiveIntegerField(default=1)),
            ("player", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="restaurant_hourly_slots", to="account.gameaccount")),
        ], options={"unique_together": {("player", "game_year", "game_month", "game_day", "game_hour")}}),
        migrations.CreateModel(name="RestaurantSaleEvent", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")), ("scheduled_minute", models.PositiveSmallIntegerField()), ("processed", models.BooleanField(default=False)), ("skipped", models.BooleanField(default=False)), ("created_at", models.DateTimeField(auto_now_add=True)),
            ("slot", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="sale_events", to="restaurant.restauranthourlyslot")), ("transaction", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="sale_event", to="restaurant.customertransaction")),
        ]),
    ]
