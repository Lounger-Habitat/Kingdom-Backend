"""Fixed career content. Importing the catalogue never mutates a saved world."""

TOMATO = 1047  # The existing 西红柿 item; only its new farming source is gated.
SOYBEAN, PIG_LIVER, CHICKEN_GIZZARD, ANIMAL_BONE, DEER_HIDE = range(29402, 29407)
TOMATO_LIVER_SOUP, SOY_PORK_STEW, ONION_GIZZARD = range(29407, 29410)
TOMATO_SEED, SOYBEAN_SEED = 29411, 29412

ITEMS = {
    TOMATO: dict(name="西红柿", type="蔬菜", price=5, icon_source=1047,
                 description="可食用农产品；村庄试种事务开放后可自行种植。"),
    SOYBEAN: dict(name="大豆", type="蔬菜", price=6, icon_source=1030,
                  description="第二期试种作物，可用于黄豆炖肉或出售。"),
    PIG_LIVER: dict(name="猪肝", type="肉类", price=8, icon_source=1009,
                   description="真实猪类猎物分割取得的食材，可制作番茄猪肝汤。"),
    CHICKEN_GIZZARD: dict(name="鸡胗", type="肉类", price=8, icon_source=1056,
                         description="真实鸡猎物分割取得的食材，可制作葱香鸡胗。"),
    ANIMAL_BONE: dict(name="兽骨", type="Commodity", price=4, icon_source=29105,
                     description="真实大型猎物加工副产品，可出售给商队。"),
    DEER_HIDE: dict(name="鹿皮", type="Commodity", price=10, icon_source=29105,
                   description="鹿的专用皮料；该分割方案以鹿皮替代同一头鹿的粗皮革。"),
    TOMATO_LIVER_SOUP: dict(name="番茄猪肝汤", type="食物", price=15, icon_source=29203,
                           description="由真实西红柿、猪肝和盐制作，每标准批三份。"),
    SOY_PORK_STEW: dict(name="黄豆炖肉", type="食物", price=20, icon_source=29203,
                       description="由真实大豆、五花肉和盐制作，每标准批三份。"),
    ONION_GIZZARD: dict(name="葱香鸡胗", type="食物", price=12, icon_source=1053,
                       description="由真实鸡胗、洋葱和盐制作，每标准批三份。"),
    TOMATO_SEED: dict(name="番茄种子", type="Seed", price=5, icon_source=1051,
                     description="用于西红柿试种与正式生产；每块田消耗一份。"),
    SOYBEAN_SEED: dict(name="大豆种子", type="Seed", price=7, icon_source=1051,
                      description="用于第二期大豆试种；玩家需要种植4级和T2农具。"),
}

CROP_RULES = {
    "TOMATO": dict(display_name="番茄", seed_item_id=TOMATO_SEED, output_item_id=TOMATO,
        growth_minutes=1440, yield_min=4, yield_max=6, guaranteed_seed_return=1,
        bonus_seed_chance=.25, required_level=1, minimum_tool_tier=1, harvest_xp=20),
    "SOYBEAN": dict(display_name="大豆", seed_item_id=SOYBEAN_SEED, output_item_id=SOYBEAN,
        growth_minutes=2160, yield_min=6, yield_max=8, guaranteed_seed_return=1,
        bonus_seed_chance=.25, required_level=4, minimum_tool_tier=2, harvest_xp=25),
}

RECIPES = {
    29421: dict(item_id=TOMATO_LIVER_SOUP, name="番茄猪肝汤", output=3, seconds=240,
                ingredients={TOMATO: 2, PIG_LIVER: 1, 1049: 1}, level=2, tier=1),
    29422: dict(item_id=SOY_PORK_STEW, name="黄豆炖肉", output=3, seconds=360,
                ingredients={SOYBEAN: 2, 1057: 2, 1049: 1}, level=4, tier=2),
    29423: dict(item_id=ONION_GIZZARD, name="葱香鸡胗", output=3, seconds=180,
                ingredients={CHICKEN_GIZZARD: 1, 1038: 1, 1049: 1}, level=4, tier=2),
}

NEW_CONTENT_ITEM_IDS = frozenset({TOMATO, SOYBEAN, PIG_LIVER, CHICKEN_GIZZARD,
    ANIMAL_BONE, DEER_HIDE, TOMATO_LIVER_SOUP, SOY_PORK_STEW, ONION_GIZZARD})
EXPORT_ITEM_IDS = NEW_CONTENT_ITEM_IDS
SEED_ITEM_IDS = frozenset({TOMATO_SEED, SOYBEAN_SEED})
IMPORT_ITEM_IDS = SEED_ITEM_IDS
EXPORT_PRICES = {item_id: ITEMS[item_id]["price"] for item_id in EXPORT_ITEM_IDS}
BYPRODUCT_ITEM_IDS = frozenset({ANIMAL_BONE, DEER_HIDE})
MEAT_ITEM_IDS = frozenset({PIG_LIVER, CHICKEN_GIZZARD})
DISH_ITEM_IDS = frozenset(spec["item_id"] for spec in RECIPES.values())
ICON_PATHS = {item_id: f"items/icons/career/{name}.png" for item_id, name in {
    SOYBEAN: "soybean", PIG_LIVER: "pig-liver", CHICKEN_GIZZARD: "chicken-gizzard",
    ANIMAL_BONE: "animal-bone", DEER_HIDE: "deer-hide", TOMATO_LIVER_SOUP: "tomato-liver-soup",
    SOY_PORK_STEW: "soy-pork-stew", ONION_GIZZARD: "onion-gizzard",
    TOMATO_SEED: "tomato-seed", SOYBEAN_SEED: "soybean-seed"}.items()}

# Existing prey IDs deliberately stay unchanged. This is a replacement plan,
# not another recipe that can claim a carcass for a second time.
PREY_OUTPUTS = {
    29101: {CHICKEN_GIZZARD: 1},
    29102: {PIG_LIVER: 1, ANIMAL_BONE: 1},
    29103: {ANIMAL_BONE: 1},
    29106: {DEER_HIDE: 1},
    29108: {PIG_LIVER: 1, ANIMAL_BONE: 1},
}


def content_access(item_id):
    """Only new production sources consult career state; legacy paths stay open."""
    if item_id not in NEW_CONTENT_ITEM_IDS:
        return dict(can_produce=True, unlocked=True, status="COMPLETED",
                    blocked_reason="", unavailable_reason="")
    from .career_development import content_access as development_access
    return development_access(item_id)


def processing_outputs(prey_item_id):
    from .phase3_catalog import PREY_RECIPES
    outputs = dict(PREY_RECIPES.get(prey_item_id, {}).get("outputs", {}))
    added = {item: amount for item, amount in PREY_OUTPUTS.get(prey_item_id, {}).items()
             if content_access(item)["can_produce"]}
    if DEER_HIDE in added:
        outputs.pop(29105, None)  # One deer skin is never also paid as rough leather.
    outputs.update(added)
    return outputs


def processing_duration(duration, outputs):
    import math
    return max(1, math.ceil(duration * 1.25)) if set(map(int, outputs)) & NEW_CONTENT_ITEM_IDS else duration


def reserve_outputs(outputs, request_id, world_minute):
    """Reserve all new outputs together, so one failed line cannot leak quota."""
    fresh = {int(i): n for i, n in outputs.items() if int(i) in NEW_CONTENT_ITEM_IDS and n > 0}
    if not fresh:
        return True
    from django.db import transaction
    from .career_development import reserve_trial_production

    class NoTrialCapacity(Exception):
        pass

    try:
        with transaction.atomic():
            for item_id, amount in fresh.items():
                if not reserve_trial_production(item_id, amount, request_id, world_minute):
                    raise NoTrialCapacity
    except NoTrialCapacity:
        return False
    return True


def record_production(actor_type, actor_id, request_id, items, world_minute, **metadata):
    from .career_development import record_business_evidence
    metadata.setdefault("production_request_id", str(request_id))
    metadata.setdefault("source_request_id", str(request_id))
    metadata.setdefault("produced_world_minute", world_minute)
    return record_business_evidence("PRODUCTION", request_id, actor_type, str(actor_id),
        {int(i): n for i, n in items.items() if n > 0}, world_minute, metadata=metadata)


def trial_request_id(actor_id, request_id):
    return f"PLAYER:{actor_id}:{request_id}"


def recipe_reference_cost(recipe_id, *, batches=1, tool=None):
    """Current catalogue-value estimate; never a price promise or money debit."""
    spec = RECIPES.get(recipe_id)
    if not spec:
        return None
    from items.models import Item
    ingredients = []
    for item_id, quantity in spec["ingredients"].items():
        item = Item.objects.filter(pk=item_id, is_deleted=False).first()
        price = item.item_price if item and item.item_price >= 0 else None
        ingredients.append(dict(item_id=item_id, quantity=quantity * batches,
            reference_unit_price=price,
            reference_value=price * quantity * batches if price is not None else None))
    raw_known = all(row["reference_value"] is not None for row in ingredients)
    raw = sum(row["reference_value"] for row in ingredients) if raw_known else None
    tool_item = Item.objects.filter(pk=tool.item_id, is_deleted=False).first() if tool else None
    tool_known = bool(tool_item and tool_item.max_use_count > 0 and tool_item.item_price > 0)
    wear = round(tool_item.item_price * batches / tool_item.max_use_count, 2) if tool_known else None
    total = raw + wear if raw_known and tool_known else raw
    output = spec["output"] * batches
    return dict(estimate_only=True, reference_quality=3,
        basis="按当前物品基准价估算，不代表实际采购支出，也不扣款。",
        ingredients=ingredients, input_reference_value=raw, standard_batches=batches,
        output_quantity=output, input_per_serving_reference_cost=round(raw / output, 2) if raw_known else None,
        tool_uses=batches if tool else None, tool_reference_cost=wear, tool_cost_known=tool_known,
        per_serving_reference_cost=round(total / output, 2) if total is not None else None,
        reference_cost_complete=raw_known and tool_known, cook_time_seconds=spec["seconds"] * batches,
        cost_scope="原料与所选工具参考损耗" if tool_known else "仅原料参考价值，工具损耗待核实")


def install_catalog(apps=None, alias="default"):
    """Install definitions only, retaining existing identities and custom lists."""
    if apps is None:
        from django.apps import apps
    Item = apps.get_model("items", "Item")
    Recipe = apps.get_model("recipes", "Recipe")
    Ingredient = apps.get_model("recipes", "RecipeIngredient")
    Config = apps.get_model("village", "VillageSimulationConfig")
    # Fresh databases may not yet have the optional imported ingredient catalogue.
    for item_id, name, price, kind in ((1049, "盐", 2, "调料类"),
            (1057, "五花肉", 12, "肉类"), (1038, "洋葱", 4, "蔬菜")):
        Item.objects.using(alias).get_or_create(pk=item_id, defaults=dict(
            item_name=name, item_type=kind, item_price=price, collectable=False))
    for item_id, spec in ITEMS.items():
        existing = Item.objects.using(alias).filter(pk=item_id).first()
        if existing and existing.item_name != spec["name"]:
            raise ValueError(f"Career item ID collision: {item_id}")
        if item_id == TOMATO and existing:
            continue  # Preserve the existing tomato's price, image and attributes.
        icon = Item.objects.using(alias).filter(pk=spec["icon_source"]).first()
        Item.objects.using(alias).update_or_create(pk=item_id, defaults=dict(
            item_name=spec["name"], item_type=spec["type"], item_price=spec["price"],
            item_description=spec["description"], sell_percentage=1, collectable=False,
            quality_enabled=spec["type"] != "Seed", is_deleted=False,
            icon_url=None if item_id in ICON_PATHS else icon.icon_url if icon else None,
            icon_image=ICON_PATHS.get(item_id) or (icon.icon_image if icon else None)))
    for recipe_id, spec in RECIPES.items():
        existing = Recipe.objects.using(alias).filter(pk=recipe_id).first()
        if existing and (existing.item_id != spec["item_id"] or existing.recipe_name != spec["name"]):
            raise ValueError(f"Career recipe ID collision: {recipe_id}")
        recipe, _ = Recipe.objects.using(alias).update_or_create(pk=recipe_id, defaults=dict(
            item_id=spec["item_id"], recipe_name=spec["name"], output_quantity=spec["output"],
            cuisine="村庄家常", dish_category="主菜" if recipe_id == 29423 else "汤品",
            serving_style="位上", taste_profile="咸鲜", difficulty=2,
            default_cook_time=spec["seconds"], default_dish_quality=3, village_only=True))
        Ingredient.objects.using(alias).filter(recipe=recipe).exclude(item_id__in=spec["ingredients"]).delete()
        for item_id, amount in spec["ingredients"].items():
            item = Item.objects.using(alias).get(pk=item_id)
            Ingredient.objects.using(alias).update_or_create(recipe=recipe, item_id=item_id,
                defaults=dict(item_name=item.item_name, count=amount))
    for cfg in Config.objects.using(alias).all():
        imports = list(cfg.caravan_item_ids or [])
        exports = list(cfg.caravan_export_item_ids or [])
        cfg.caravan_item_ids = imports + sorted(IMPORT_ITEM_IDS - set(imports))
        cfg.caravan_export_item_ids = exports + sorted(EXPORT_ITEM_IDS - set(exports))
        cfg.save(update_fields=["caravan_item_ids", "caravan_export_item_ids"])
