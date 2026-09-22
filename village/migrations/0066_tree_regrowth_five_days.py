from django.db import migrations
from django.db.models import F


def extend_existing_stumps(apps, schema_editor):
    Tree = apps.get_model("village", "VillageTreeNode")
    Tree.objects.using(schema_editor.connection.alias).filter(
        status="STUMP", felled_world_minute__isnull=False,
    ).update(regrow_world_minute=F("felled_world_minute") + 7200)


def restore_one_day(apps, schema_editor):
    Tree = apps.get_model("village", "VillageTreeNode")
    Tree.objects.using(schema_editor.connection.alias).filter(
        status="STUMP", felled_world_minute__isnull=False,
    ).update(regrow_world_minute=F("felled_world_minute") + 1440)


class Migration(migrations.Migration):
    dependencies = [("village", "0065_forestry_seed")]
    operations = [migrations.RunPython(extend_existing_stumps, restore_one_day)]
