"""Village-only basic meals. Ingredients are existing item IDs, never prey/seeds."""
MEAT_IDS = (1056, 1009, 1034, 1057, 1077)
STEAMED_CORN, CORN_SOUP, MEAT_SOUP, VEGETABLE_SOUP, RELIEF_PACK = range(29201, 29206)
MEALS = {
    STEAMED_CORN: ("清蒸玉米", 11, 2, 60, {1030: 2}),
    CORN_SOUP: ("玉米肉汤", 14, 2, 180, {1030: 1, 1056: 1, 1049: 1}),
    MEAT_SOUP: ("清炖肉汤", 12, 3, 240, {1056: 2, 1049: 1}),
    VEGETABLE_SOUP: ("三蔬清汤", 10, 3, 120, {1025: 1, 1042: 1, 1023: 1, 1049: 1}),
    RELIEF_PACK: ("玉米冷食包", 0, 2, 0, {1030: 2}),
}
RECIPE_DEFINITIONS = {}
for item_id, (name, price, output, seconds, ingredients) in MEALS.items():
    variants = MEAT_IDS if item_id in (CORN_SOUP, MEAT_SOUP) else (None,)
    for variant_index, meat in enumerate(variants):
        recipe_id = 29200 + (item_id - 29201) * 10 + variant_index + 1
        actual = dict(ingredients)
        if meat is not None:
            amount = actual.pop(1056)
            actual[meat] = amount
        RECIPE_DEFINITIONS[recipe_id] = dict(item_id=item_id, name=name, output=output,
            seconds=seconds, ingredients=actual, meat_id=meat)

PUBLIC_MEALS = {
    "C1": {"item_id": STEAMED_CORN, "subsidy": 9},
    "C2": {"item_id": CORN_SOUP, "subsidy": 12},
    "R1": {"item_id": RELIEF_PACK, "subsidy": 9},
}


def food_recovery(item_id):
    return {"hunger_fp": -250, "energy_fp": 0} if item_id == RELIEF_PACK else {"hunger_fp": -450, "energy_fp": 50}


def install_catalog(apps=None):
    """Add definitions only: no inventory, money, clock, or public decisions."""
    if apps is None:
        from django.apps import apps
    Item, Recipe = apps.get_model("items", "Item"), apps.get_model("recipes", "Recipe")
    Ingredient = apps.get_model("recipes", "RecipeIngredient")
    for item_id, (name, price, output, seconds, ingredients) in MEALS.items():
        existing = Item.objects.filter(item_id=item_id).first()
        if existing and existing.item_name != name:
            raise ValueError(f"Lucy item ID collision: {item_id}")
        icon_source = Item.objects.filter(item_id=1030 if item_id in (STEAMED_CORN, RELIEF_PACK) else 1053).first()
        Item.objects.update_or_create(item_id=item_id, defaults=dict(item_name=name,
            item_type="食物", item_price=price, sell_percentage=1, collectable=False,
            icon_url=icon_source.icon_url if icon_source else None,
            icon_image=icon_source.icon_image if icon_source else None,
            item_description="Lucy制作的村庄餐食；保质期两游戏日。", is_deleted=False))
    for recipe_id, spec in RECIPE_DEFINITIONS.items():
        existing = Recipe.objects.filter(pk=recipe_id).first()
        if existing and existing.item_id != spec["item_id"]:
            raise ValueError(f"Lucy recipe ID collision: {recipe_id}")
        recipe, _ = Recipe.objects.update_or_create(pk=recipe_id, defaults=dict(
            item_id=spec["item_id"], recipe_name=spec["name"] + (f"（{Item.objects.get(item_id=spec['meat_id']).item_name}）" if spec["meat_id"] else ""),
            cuisine="村庄家常", dish_category="主食" if spec["item_id"] in (STEAMED_CORN, RELIEF_PACK) else "汤品",
            serving_style="位上", taste_profile="原味" if spec["item_id"] in (STEAMED_CORN, RELIEF_PACK) else "咸鲜",
            difficulty=1, default_cook_time=spec["seconds"], default_dish_quality=3,
            output_quantity=spec["output"], village_only=True))
        for item_id, count in spec["ingredients"].items():
            Ingredient.objects.update_or_create(recipe=recipe, item_id=item_id,
                defaults=dict(item_name=Item.objects.get(item_id=item_id).item_name, count=count))
