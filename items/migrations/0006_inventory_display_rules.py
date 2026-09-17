from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("items", "0005_alter_item_options")]
    operations = [
        migrations.AddField(model_name="item", name="quality_enabled", field=models.BooleanField(default=False, help_text="普通物品是否区分品质；菜品始终保留品质")),
        migrations.AddField(model_name="item", name="stackable", field=models.BooleanField(default=True, help_text="允许跨批次展示堆叠；菜品和有次数的道具仍独立展示")),
        migrations.AddField(model_name="item", name="max_stack_size", field=models.PositiveIntegerField(null=True, blank=True, help_text="每个展示格的数量上限，留空表示不限；不改变实际库存批次")),
        migrations.AddConstraint(model_name="item", constraint=models.CheckConstraint(
            condition=models.Q(max_stack_size__isnull=True) | models.Q(max_stack_size__gt=0), name="item_positive_stack_size")),
    ]
