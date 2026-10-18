"""Frozen professional content, collision checked, and finite pumpkin seed stock."""
import uuid
from django.db import migrations


def install(apps, schema_editor):
    alias = schema_editor.connection.alias
    Item = apps.get_model("items", "Item")
    Definition = apps.get_model("player", "ItemGameplayDefinition")
    tools = [(4120, "专业采集包", "targeted_gathering", "gathering", 100, None),
             (4121, "专业农具", "farming", "farming", 100, None),
             (4122, "专业斧", "forestry", "forestry", 100, None),
             (4123, "专业炊具", "cooking", "cooking", 100, None),
             (4124, "专业长矛", "hunting_spear", "hunting", 180, 0),
             (4125, "专业弓", "hunting_bow", "hunting", 180, 2)]
    for key, name, capability, skill, uses, weapon in tools:
        item, _ = Item.objects.using(alias).get_or_create(pk=key, defaults=dict(item_name=name,
            item_type="CollectTool", max_use_count=uses, collectable=False, stackable=False, item_price=350))
        if (item.item_name, item.item_type, item.max_use_count) != (name, "CollectTool", uses):
            raise ValueError(f"G6 tool item collision: {key}")
        effects = ([dict(code="weapon", weapon=weapon, damage_multiplier=1.4)] if weapon is not None
                   else [dict(code="standard_cooking_batches", maximum=5)] if capability == "cooking" else [])
        definition, _ = Definition.objects.using(alias).get_or_create(item_id=key, defaults=dict(
            capability_code=capability, required_skill=skill, required_level=7, tier=3, effects=effects))
        if (definition.capability_code, definition.required_skill, definition.required_level, definition.tier) != (capability, skill, 7, 3):
            raise ValueError(f"G6 capability collision: {key}")
    entries = [(29307, "南瓜种子", "Seed", 8), (29308, "深林菌", "食材", 10),
               (29309, "山香草", "食材", 10), (29310, "林莓", "水果", 10),
               (29311, "专业木材", "Commodity", 12), (29108, "野猪（待处理猎物）", "Commodity", 70),
               (29206, "南瓜菌菇汤", "食物", 28), (29207, "山香草鹿肉汤", "食物", 35),
               (29208, "林莓玉米羹", "食物", 25)]
    for key, name, kind, price in entries:
        item, _ = Item.objects.using(alias).get_or_create(pk=key, defaults=dict(item_name=name, item_type=kind,
            item_price=price, collectable=False, quality_enabled=key != 29307))
        if (item.item_name, item.item_type) != (name, kind):
            raise ValueError(f"G6 catalog item collision: {key}")
    pumpkin = Item.objects.using(alias).filter(pk=1001).first()
    if pumpkin and (pumpkin.item_name, pumpkin.item_type) != ("南瓜", "蔬菜"):
        raise ValueError("G6 pumpkin Item1001 differs from verified live catalog")
    Recipe = apps.get_model("recipes", "Recipe")
    Ingredient = apps.get_model("recipes", "RecipeIngredient")
    for key, output, name, seconds, ingredients in [
        (29251, 29206, "南瓜菌菇汤", 300, [(1001, "南瓜", 1), (29308, "深林菌", 2), (1049, "盐", 1)]),
        (29252, 29207, "山香草鹿肉汤", 300, [(29107, "鹿肉", 2), (29309, "山香草", 1), (1049, "盐", 1)]),
        (29253, 29208, "林莓玉米羹", 240, [(1030, "玉米", 2), (29310, "林莓", 1), (1049, "盐", 1)])]:
        recipe, created = Recipe.objects.using(alias).get_or_create(pk=key, defaults=dict(item_id=output,
            recipe_name=name, output_quantity=3, village_only=True, default_cook_time=seconds,
            default_dish_quality=4, dish_category="汤品", version=1))
        if (recipe.item_id, recipe.recipe_name, recipe.output_quantity, recipe.village_only, recipe.default_cook_time) != (output, name, 3, True, seconds):
            raise ValueError(f"G6 recipe collision: {key}")
        if created:
            for item_id, item_name, count in ingredients:
                Ingredient.objects.using(alias).create(recipe_id=key, item_id=item_id, item_name=item_name, count=count)
        elif sorted(Ingredient.objects.using(alias).filter(recipe_id=key).values_list("item_id", "count")) != sorted((i, n) for i, _, n in ingredients):
            raise ValueError(f"G6 recipe ingredients collision: {key}")
    Point = apps.get_model("inventory", "CollectionPoint")
    Level = apps.get_model("inventory", "CollectionPointLevel")
    Pool = apps.get_model("inventory", "CollectionPointLevelItem")
    for collect_type, name, item_id in [("采集深林菌", "深林菌", 29308), ("采集山香草", "山香草", 29309), ("采集林莓", "林莓", 29310)]:
        point, _ = Point.objects.using(alias).get_or_create(collect_type=collect_type,
            defaults=dict(name=name, item_type="食材", action_cost=20, collect_count=1, default_level=1))
        if (point.name, point.action_cost, point.collect_count) != (name, 20, 1):
            raise ValueError(f"G6 collection point collision: {collect_type}")
        level, _ = Level.objects.using(alias).get_or_create(collection_point=point, level=1)
        pool, _ = Pool.objects.using(alias).get_or_create(level=level, item_id=item_id,
            defaults=dict(collect_weight=100, collect_min_amount=2, collect_max_amount=2))
        if Pool.objects.using(alias).filter(level=level).count() != 1 or (pool.collect_min_amount, pool.collect_max_amount) != (2, 2):
            raise ValueError(f"G6 collection pool collision: {collect_type}")
    # Only future visits read these configuration lists. Explicitly empty lists
    # stay disabled; never mutate a previously frozen visit or demand record.
    Config = apps.get_model("village", "VillageSimulationConfig")
    for config in Config.objects.using(alias).all():
        fields = []
        if config.caravan_item_ids:
            config.caravan_item_ids = sorted(set(config.caravan_item_ids) | {29307})
            fields.append("caravan_item_ids")
        if config.caravan_export_item_ids:
            config.caravan_export_item_ids = sorted(set(config.caravan_export_item_ids) | {1001, 29308, 29309, 29310, 29311})
            fields.append("caravan_export_item_ids")
        if fields:
            config.save(using=alias, update_fields=fields)
    Store = apps.get_model("village", "VillageStore")
    Account = apps.get_model("village", "VillageStoreAccount")
    Listing = apps.get_model("village", "VillageStoreListing")
    Slot = apps.get_model("village", "VillageStoreInventorySlot")
    Ledger = apps.get_model("village", "EconomicLedgerEntry")
    Clock = apps.get_model("game_time", "GlobalGameTimeState")
    marker = uuid.uuid5(uuid.NAMESPACE_URL, "kingdom:g6:pumpkin-stock:29307:v1")
    store = Store.objects.using(alias).filter(pk="GENERAL_STORE", operator_npc__agent_id="10015", operator_npc__is_test_only=False).first()
    if not store or not Account.objects.using(alias).filter(store=store).exists() or Ledger.objects.using(alias).filter(request_id=marker).exists():
        return
    minute = Clock.objects.using(alias).filter(pk=1).values_list("world_minute", flat=True).first() or 0
    listing = Listing.objects.using(alias).create(listing_id=uuid.uuid5(marker, "listing"), store=store,
        item_id=29307, unit_price=8, price_mode="ORIGINAL", status="ACTIVE", listed_by_npc_id=store.operator_npc_id,
        created_request_id=marker, listed_world_minute=minute)
    Slot.objects.using(alias).create(instance_id=uuid.uuid5(marker, "0"), store=store, listing=listing,
        item_id=29307, item_amount=30, quality=3, produced_world_minute=minute,
        common_attributes={"initial_stock_code": "G6_PRODUCTION"})
    Ledger.objects.using(alias).create(request_id=marker, entry_type="G6_INITIAL_STOCK", world_minute=minute,
        changes={"STORE:GENERAL_STORE": {"29307": 30}, "source": "G6_FINITE_PRODUCTION_STOCK"})
    store.inventory_version += 1
    store.save(using=alias, update_fields=["inventory_version"])


class Migration(migrations.Migration):
    dependencies = [("items", "0011_hunting_weapons_and_deer"), ("player", "0017_player_mastery_challenge"),
                    ("recipes", "0005_lucy_meals_reviews"), ("inventory", "0028_inventoryslotdish_inventoryslotreview_and_more")]
    operations = [migrations.RunPython(install, migrations.RunPython.noop)]
