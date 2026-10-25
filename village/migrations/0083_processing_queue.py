from django.db import migrations, models


def add_stall(apps, schema_editor):
    Location = apps.get_model("village", "VillageLocationDefinition")
    Location.objects.using(schema_editor.connection.alias).get_or_create(location_code="PLAYER_STALL", defaults=dict(
        display_name="集市摊位", center_anchor_code="PLAYER_STALL", location_type="market",
        bounds={"center": {"x": 29, "y": 0, "z": -3.8}, "radius": 3}, location_tags=["market", "player_stall"]))


class Migration(migrations.Migration):
    dependencies = [("village", "0082_villageplantnode_villageplayergathering")]
    operations = [
        migrations.RunPython(add_stall, migrations.RunPython.noop),
        migrations.AlterField(model_name="villageplayerprocessingjob", name="tool_instance_id",
                              field=models.UUIDField(blank=True, null=True)),
        migrations.RemoveConstraint(model_name="villageplayerprocessingjob", name="processing_one_unfinished_player"),
        migrations.AddConstraint(model_name="villageplayerprocessingjob", constraint=models.UniqueConstraint(
            fields=("player",), condition=models.Q(status__in=["QUEUED", "RUNNING", "READY", "FAILED"]),
            name="processing_one_unfinished_player")),
    ]
