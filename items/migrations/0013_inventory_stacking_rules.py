from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("items", "0012_professional_catalog")]
    operations = [
        migrations.AlterField(model_name="item", name="stackable", field=models.BooleanField(default=True,
            help_text="同库存归属、物品和品质的普通物品合堆；菜品、工具和武器始终每件独立")),
        migrations.AlterField(model_name="item", name="max_stack_size", field=models.PositiveIntegerField(null=True, blank=True,
            help_text="每堆实际库存的数量上限，留空表示不限；独立物品每堆固定一件")),
    ]
