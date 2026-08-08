import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("restaurant", "0010_chinese_hourly_sales_labels")]
    operations = [
        migrations.AlterField(model_name="restauranthourlyslot", name="player", field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="restaurant_hourly_slots", to="account.gameaccount", verbose_name="玩家")),
        migrations.AlterField(model_name="restauranthourlyslot", name="game_year", field=models.IntegerField(verbose_name="游戏年")),
        migrations.AlterField(model_name="restauranthourlyslot", name="game_month", field=models.IntegerField(verbose_name="游戏月")),
        migrations.AlterField(model_name="restauranthourlyslot", name="game_day", field=models.IntegerField(verbose_name="游戏日")),
        migrations.AlterField(model_name="restauranthourlyslot", name="game_hour", field=models.IntegerField(verbose_name="游戏小时")),
        migrations.AlterField(model_name="restauranthourlyslot", name="quota", field=models.PositiveIntegerField(default=0, verbose_name="本小时售卖额度")),
        migrations.AlterField(model_name="restauranthourlyslot", name="sold_count", field=models.PositiveIntegerField(default=0, verbose_name="已售数量")),
        migrations.AlterField(model_name="restauranthourlyslot", name="restaurant_level", field=models.PositiveIntegerField(default=1, verbose_name="额度冻结时餐厅等级")),
        migrations.AlterField(model_name="restaurantsaleevent", name="slot", field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="sale_events", to="restaurant.restauranthourlyslot", verbose_name="小时售卖额度")),
        migrations.AlterField(model_name="restaurantsaleevent", name="scheduled_minute", field=models.PositiveSmallIntegerField(verbose_name="计划售卖分钟")),
        migrations.AlterField(model_name="restaurantsaleevent", name="processed", field=models.BooleanField(default=False, verbose_name="已处理")),
        migrations.AlterField(model_name="restaurantsaleevent", name="skipped", field=models.BooleanField(default=False, verbose_name="已跳过")),
        migrations.AlterField(model_name="restaurantsaleevent", name="transaction", field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="sale_event", to="restaurant.customertransaction", verbose_name="成交记录")),
        migrations.AlterField(model_name="restaurantsaleevent", name="created_at", field=models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
    ]
