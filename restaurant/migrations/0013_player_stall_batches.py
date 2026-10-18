from django.db import migrations, models
import django.db.models.deletion


def improve_default_first_level(apps, schema_editor):
    Config = apps.get_model("restaurant", "RestaurantLevelConfig")
    Config.objects.using(schema_editor.connection.alias).filter(level=2,
        required_total_ratings=20, required_dishes_sold=50, cost_money=500,
        required_dish_ids=[], required_dish_scores=[], cost_items=[], reward_recipes=[],
        reward_materials=[]).update(required_total_ratings=0, required_dishes_sold=5,
            cost_money=50, cost_items=[{"itemId": 1002, "amount": 4}])


class Migration(migrations.Migration):
    dependencies = [("restaurant", "0012_restaurantlevelconfig_reward_menu_capacity_bonus_and_more"),
                    ("inventory", "0028_inventoryslotdish_inventoryslotreview_and_more")]
    operations = [
        migrations.AddField("playerrestaurantstate", "stall_opened_world_minute", models.BigIntegerField(blank=True, null=True)),
        migrations.AddField("playerrestaurantstate", "last_stall_tick_world_minute", models.BigIntegerField(blank=True, null=True)),
        migrations.AddField("menuitem", "batch_slot", models.OneToOneField(blank=True, null=True,
            on_delete=django.db.models.deletion.SET_NULL, related_name="stall_menu_item", to="inventory.inventoryslot")),
        migrations.AddField("menuitem", "listed_world_minute", models.BigIntegerField(blank=True, null=True)),
        migrations.AddField("menuitem", "purchase_unit_cost", models.IntegerField(blank=True, null=True)),
        migrations.AddField("menuitem", "cost_provenance", models.JSONField(blank=True, default=dict)),
        migrations.AddField("customertransaction", "world_minute", models.BigIntegerField(blank=True, null=True)),
        migrations.AddField("customertransaction", "feedback_source", models.CharField(default="LEGACY_LLM_OR_UNKNOWN", max_length=32)),
        migrations.AddField("customertransaction", "batch_snapshot", models.JSONField(blank=True, default=dict)),
        migrations.AddField("customertransaction", "unit_cost", models.IntegerField(blank=True, null=True)),
        migrations.RunPython(improve_default_first_level, migrations.RunPython.noop),
    ]
