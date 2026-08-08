from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("restaurant", "0009_hourly_auto_sales")]
    operations = [
        migrations.AlterModelOptions(
            name="restauranthourlyslot",
            options={"verbose_name": "餐厅每小时售卖额度", "verbose_name_plural": "餐厅每小时售卖额度"},
        ),
        migrations.AlterModelOptions(
            name="restaurantsaleevent",
            options={"verbose_name": "餐厅随机售卖事件", "verbose_name_plural": "餐厅随机售卖事件"},
        ),
    ]
