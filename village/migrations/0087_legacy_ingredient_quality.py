"""Legacy catalog ingredients sometimes use the generic Commodity type."""
from django.db import migrations


def enable_legacy_quality(apps, schema_editor):
    alias = schema_editor.connection.alias
    Item = apps.get_model("items", "Item")
    Ingredient = apps.get_model("recipes", "RecipeIngredient")
    neutral = {1049, 1050, 1058, 1076, 1099}
    ids = set(Ingredient.objects.using(alias).filter(count__gt=0)
              .exclude(item_id__in=neutral).values_list("item_id", flat=True))
    # Crops, hunted prey and inherited byproducts retain their existing IDs/types.
    ids.update({1001, 1005, 1025, 1030, 1038, 1042, 1043,
                29101, 29102, 29103, 29104, 29105, 29106, 29107, 29108,
                29308, 29309, 29310, 29311})
    Item.objects.using(alias).filter(pk__in=ids).update(quality_enabled=True)


class Migration(migrations.Migration):
    dependencies = [("village", "0086_store_quality_only"), ("recipes", "0005_lucy_meals_reviews")]
    operations = [migrations.RunPython(enable_legacy_quality, migrations.RunPython.noop)]
