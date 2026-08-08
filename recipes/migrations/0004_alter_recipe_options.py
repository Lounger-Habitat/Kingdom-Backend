from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("recipes", "0003_alter_recipe_item_id")]
    operations = [
        migrations.AlterModelOptions(
            name="recipe",
            options={"ordering": ["item_id"], "verbose_name": "菜谱", "verbose_name_plural": "菜谱"},
        ),
    ]
