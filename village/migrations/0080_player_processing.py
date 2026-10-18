import django.db.models.deletion
import uuid
from django.db import migrations, models



def install_deer_soups(apps, schema_editor):
    Recipe = apps.get_model("recipes", "Recipe")
    Ingredient = apps.get_model("recipes", "RecipeIngredient")
    for recipe_id, output, name, seconds, quantity, ingredients in (
        (29216, 29202, "玉米肉汤（鹿肉）", 180, 2, {1030: 1, 29107: 1, 1049: 1}),
        (29226, 29203, "清炖肉汤（鹿肉）", 240, 3, {29107: 2, 1049: 1}),
    ):
        existing = Recipe.objects.filter(pk=recipe_id).first()
        if existing and (existing.item_id != output or existing.recipe_name != name):
            raise ValueError(f"G5 recipe ID collision: {recipe_id}")
        recipe, _ = Recipe.objects.update_or_create(pk=recipe_id, defaults=dict(
            item_id=output, recipe_name=name, cuisine="村庄家常", dish_category="汤品",
            serving_style="位上", taste_profile="咸鲜", difficulty=1,
            default_cook_time=seconds, default_dish_quality=3, output_quantity=quantity, village_only=True))
        for item_id, count in ingredients.items():
            Ingredient.objects.update_or_create(recipe=recipe, item_id=item_id, defaults=dict(
                item_name={1030: "玉米", 1049: "盐", 29107: "鹿肉"}[item_id], count=count))


class Migration(migrations.Migration):

    dependencies = [
        ('account', '0004_gameaccount_appearance'),
        ('inventory', '0028_inventoryslotdish_inventoryslotreview_and_more'),
        ('village', '0079_business_order_control'),
        ('items', '0011_hunting_weapons_and_deer'),
    ]

    operations = [
        migrations.RunPython(install_deer_soups, migrations.RunPython.noop),
        migrations.CreateModel(
            name='VillagePlayerProcessingJob',
            fields=[
                ('job_id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('request_id', models.UUIDField(unique=True)),
                ('source_instance_id', models.UUIDField()),
                ('input_snapshot', models.JSONField(default=dict)),
                ('output_spec', models.JSONField(default=dict)),
                ('output_snapshot', models.JSONField(default=list)),
                ('tool_instance_id', models.UUIDField()),
                ('fee_gold', models.PositiveIntegerField()),
                ('duration_minutes', models.PositiveIntegerField()),
                ('status', models.CharField(default='RUNNING', max_length=16)),
                ('started_world_minute', models.BigIntegerField()),
                ('ready_world_minute', models.BigIntegerField()),
                ('needs_until', models.BigIntegerField()),
                ('completed_world_minute', models.BigIntegerField(blank=True, null=True)),
                ('claimed_world_minute', models.BigIntegerField(blank=True, null=True)),
                ('failure_reason', models.CharField(blank=True, default='', max_length=64)),
                ('escrow_bag', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, to='inventory.inventorybag')),
                ('input_slot', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='inventory.inventoryslot')),
                ('player', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='account.gameaccount')),
            ],
            options={
                'constraints': [models.UniqueConstraint(condition=models.Q(('status__in', ['RUNNING', 'READY', 'FAILED'])), fields=('player',), name='processing_one_unfinished_player'), models.UniqueConstraint(condition=models.Q(('status', 'RUNNING')), fields=('status',), name='processing_one_jack_job')],
            },
        ),
    ]
