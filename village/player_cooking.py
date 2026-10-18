"""Trusted precise-batch cooking; previews are pure, execution shares asset receipts."""
from copy import deepcopy
import hashlib
import hmac
import json
import uuid

from django.db import transaction
from django.conf import settings
from django.utils import timezone
from cooking.dish_rules import calculate_quality
from inventory.models import InventorySlot
from items.models import Item, ItemType
from player.models import (DefaultRecipe, PlayerGrowthState, PlayerMilestoneProgress,
                           PlayerRecipe, PlayerState)
from player.progression import award_skill, growth_snapshot
from player.services import recover_action_points, ACTION_POINT_RECOVERY_SECONDS, NATURAL_ACTION_POINT_CAP
from recipes.models import Recipe, RecipeIngredient
from .models import EconomicLedgerEntry, VillagePlayerActionReceipt
from .phase3_asset_service import deterministic_rng
from .production_tools import resolve_tool, wear_tool, tools_overview, ToolError, _index
from .player_cooking_rules import (VILLAGE_RECIPES, NONPERISHABLE_IDS, RULE_VERSION,
    XP_PER_BATCH, FIRST_COOK_BONUS, AP_PER_BATCH, SHELF_LIFE)

class CookingError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


PITY_POLICY = "普通正确且鲜期合格的单批料理沿用旧保底；村庄配方和批量制作不消耗旧保底计数。"


def reject(code, message):
    raise CookingError(code, message)


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        reject("INVALID_PARAMS", "请选择有效的本人食材批次。")


def _level(player):
    return next(s["level"] for s in growth_snapshot(player, read_only=True)["skills"] if s["code"] == "cooking")


def _knowledge(player):
    state = PlayerState.objects.filter(player_id=str(player.pk)).first()
    rows = list(PlayerRecipe.objects.filter(player=state)) if state else []
    by_id = {row.catalog_recipe_id: row for row in rows if row.catalog_recipe_id}
    by_name = {row.recipe_name: row for row in rows}
    return by_id, by_name, set(DefaultRecipe.objects.values_list("recipe_name", flat=True))


def _visibility(recipe, knowledge):
    by_id, by_name, defaults = knowledge
    known = by_id.get(recipe.pk) or by_name.get(recipe.recipe_name)
    village = recipe.pk in VILLAGE_RECIPES
    return bool(known or recipe.recipe_name in defaults or village), (known.show_ingredient_count if known else village)


def _ingredient_ids():
    return set(RecipeIngredient.objects.filter(count__gt=0).values_list("item_id", flat=True))


def _is_ingredient(slot, item, ids):
    return (item is not None and not item.is_deleted and item.pk in ids
        and item.item_type not in (ItemType.SEED, ItemType.FURNITURE, ItemType.USABLE, ItemType.HOE_TOOL,
            ItemType.CHOP_TOOL, ItemType.BREAK_TOOL, ItemType.REAP_TOOL, ItemType.WATER_TOOL, ItemType.COLLECT_TOOL)
        and not slot.dish_metadata and not slot.ingredient and slot.remaining_uses is None)


def _perishable(item):
    return item.pk not in NONPERISHABLE_IDS and item.item_type != ItemType.SEASONING


def _ingredient_view(slot, item, minute):
    reason = ("FRESHNESS_UNKNOWN" if slot.freshness_unknown else "INGREDIENT_EXPIRED"
              if slot.expires_world_minute is not None and slot.expires_world_minute <= minute else
              "PROVENANCE_INVALID" if slot.produced_world_minute is not None and slot.produced_world_minute > minute else
              "PROVENANCE_REQUIRED" if _perishable(item) and slot.expires_world_minute is not None
                  and (slot.produced_world_minute is None or not slot.source_request_id) else "")
    legacy = _perishable(item) and slot.expires_world_minute is None
    return dict(instance_id=str(slot.instance_id), item_id=slot.item_id, item_name=item.item_name,
        quantity=slot.item_amount, available_quantity=0 if reason else slot.item_amount,
        quality=slot.quality, produced_world_minute=slot.produced_world_minute,
        expires_world_minute=slot.expires_world_minute, freshness_unknown=slot.freshness_unknown,
        source_request_id=str(slot.source_request_id) if slot.source_request_id else None,
        legacy_perishable=legacy, reason=reason)


def _available_ap(player):
    state = PlayerState.objects.filter(player_id=str(player.pk)).first()
    if state is None:
        return NATURAL_ACTION_POINT_CAP
    if state.action_points >= NATURAL_ACTION_POINT_CAP:
        return state.action_points
    elapsed = max(0, int((timezone.now() - state.action_points_recovered_at).total_seconds()))
    return min(NATURAL_ACTION_POINT_CAP, state.action_points + elapsed // ACTION_POINT_RECOVERY_SECONDS)


def cooking_overview(player, bag, minute, read_only=False):
    level = _level(player)
    knowledge = _knowledge(player)
    tools = [row for row in tools_overview(player, bag, minute, read_only=read_only)["tools"] if row["capability"] == "cooking"]
    recipes = []
    for recipe in Recipe.objects.prefetch_related("ingredients").order_by("pk"):
        known, visible = _visibility(recipe, knowledge)
        if recipe.village_only and recipe.pk not in VILLAGE_RECIPES or not known:
            continue
        required, tier = VILLAGE_RECIPES.get(recipe.pk, (1, 1))
        reason = "SKILL_LEVEL_REQUIRED" if level < required else "TOOL_REQUIRED" if not any(t["usable"] and t["tier"] >= tier for t in tools) else ""
        if not recipe.ingredients.all() or not Item.objects.filter(pk=recipe.item_id, is_deleted=False).exists():
            reason = "RECIPE_CATALOG_UNAVAILABLE"
        recipes.append(dict(id=recipe.pk, name=recipe.recipe_name, item_id=recipe.item_id,
            output_quantity=recipe.output_quantity, required_level=required, minimum_tool_tier=tier,
            available=not reason, reason=reason, known=known, quantities_visible=visible,
            ingredients=[dict(item_id=i.item_id, item_name=i.item_name, count=i.count if visible else None)
                         for i in recipe.ingredients.all()]))
    ids = _ingredient_ids()
    items = {i.pk: i for i in Item.objects.filter(pk__in=ids, is_deleted=False)}
    ingredients = [_ingredient_view(s, items[s.item_id], minute) for s in bag.slots.filter(item_amount__gt=0).order_by("slot_index")
                   if _is_ingredient(s, items.get(s.item_id), ids)] if bag.pk else []
    goals = [dict(code=code, title=title, completed=PlayerMilestoneProgress.objects.filter(player=player, code=code).exists(),
                  location_code=location) for code, title, location in (
        ("first_player_cooking", "亲自制作合格料理", "RESTAURANT"),
        ("first_formal_dish_review", "完成本人料理的正式点评", "RECORD_HALL"))]
    return dict(revision=PlayerGrowthState.objects.filter(player=player).values_list("revision", flat=True).first() or 0,
        observed_world_minute=minute, level=level, max_batches=3 if level >= 4 and any(t["usable"] and t["tier"] >= 2 for t in tools) else 1,
        recipes=recipes, ingredients=ingredients, tools=tools, goals=goals, pity_policy=PITY_POLICY)


def _prepare(player, bag, params, minute, *, execute=False):
    allowed = {"recipe_id", "batches", "selections", "tool_instance_id"}
    if not isinstance(params, dict) or set(params) - allowed:
        reject("INVALID_PARAMS", "烹饪参数无效。")
    if not bag.pk or bag.player_id != player.pk or bag.character_id != player.username:
        reject("INGREDIENT_NOT_OWNED", "只能使用本人主背包中的食材。")
    batches = params.get("batches", 1)
    if type(batches) is not int or not 1 <= batches <= 3:
        reject("INVALID_PARAMS", "每次请选择1至3个标准批次。")
    selected = params.get("selections")
    if not isinstance(selected, list) or not 0 < len(selected) <= 64:
        reject("INVALID_PARAMS", "请选择实际食材批次和数量。")
    ids, rows, amounts, views = _ingredient_ids(), [], {}, []
    seen = set()
    for entry in selected:
        if not isinstance(entry, dict) or set(entry) != {"instance_id", "quantity"} or type(entry["quantity"]) is not int or not 0 < entry["quantity"] <= 1000000:
            reject("INVALID_PARAMS", "食材数量必须为正整数。")
        identifier = _uuid(entry["instance_id"])
        if identifier in seen:
            reject("DUPLICATE_INGREDIENT_BATCH", "同一食材批次只能选择一次。")
        seen.add(identifier)
        slot = InventorySlot.objects.filter(bag=bag, instance_id=identifier, item_amount__gt=0).first()
        item = Item.objects.filter(pk=slot.item_id).first() if slot else None
        if slot is None or not _is_ingredient(slot, item, ids):
            reject("INGREDIENT_NOT_OWNED", "食材不属于本人，或该物品不可作为原料。")
        view = _ingredient_view(slot, item, minute)
        if view["reason"]:
            reject(view["reason"], "所选原料已经过期、来源异常或鲜期不明。")
        if view["available_quantity"] < entry["quantity"]:
            reject("INSUFFICIENT_INGREDIENTS", "所选食材批次的实际数量不足。")
        rows.append((slot, entry["quantity"]))
        amounts[slot.item_id] = amounts.get(slot.item_id, 0) + entry["quantity"]
        views.append(dict(view, selected_quantity=entry["quantity"]))
    recipe_id = params.get("recipe_id")
    if recipe_id is not None and (type(recipe_id) is not int or recipe_id <= 0):
        reject("INVALID_PARAMS", "菜谱标识无效。")
    recipe = None
    if recipe_id is not None:
        recipe = Recipe.objects.prefetch_related("ingredients").filter(pk=recipe_id).first()
        if not recipe:
            reject("RECIPE_CATALOG_UNAVAILABLE", "菜谱目录尚未安装。")
    else:
        recipe = next((r for r in Recipe.objects.filter(village_only=False).prefetch_related("ingredients").order_by("item_id", "pk")
                       if set(i.item_id for i in r.ingredients.all()) == set(amounts)), None)
    knowledge = _knowledge(player)
    known, visible = _visibility(recipe, knowledge) if recipe else (False, False)
    if recipe_id is not None and recipe and not known:
        reject("RECIPE_NOT_KNOWN", "该普通菜谱尚未掌握，可通过自由配料探索。")
    if recipe and recipe.village_only and recipe.pk not in VILLAGE_RECIPES:
        reject("RECIPE_NOT_RELEASED", "该村庄配方尚未向玩家开放。")
    required, tier = VILLAGE_RECIPES.get(recipe.pk, (1, 1)) if recipe else (1, 1)
    level = _level(player)
    if level < required:
        reject("SKILL_LEVEL_REQUIRED", "烹饪等级未达到配方要求。")
    if batches > 1 and (level < 4 or not visible):
        reject("BATCH_RECIPE_KNOWLEDGE_REQUIRED", "多批制作需要烹饪4级及已明确数量的标准配方。")
    if not params.get("tool_instance_id"):
        reject("TOOL_MISSING", "请选择本人实际持有的炊具。")
    try:
        tool = resolve_tool(player, bag, "cooking", params["tool_instance_id"], max(tier, 2 if batches > 1 else 1), minute=minute)
    except ToolError as exc:
        reject(exc.code, exc.message)
    if tool.remaining_uses < batches:
        reject("TOOL_BROKEN", "所选炊具耐久不足以完成全部批次。")
    if _available_ap(player) < AP_PER_BATCH * batches:
        reject("INSUFFICIENT_ACTION_POINTS", "行动力不足以完成全部批次。")
    ingredients = list(recipe.ingredients.all()) if recipe else []
    expected = {}
    for ingredient in ingredients:
        if ingredient.count <= 0:
            reject("RECIPE_CATALOG_UNAVAILABLE", "菜谱配比配置无效。")
        expected[ingredient.item_id] = expected.get(ingredient.item_id, 0) + ingredient.count * batches
    exact = recipe is not None and amounts == expected
    village = recipe is not None and recipe.pk in VILLAGE_RECIPES
    legacy = any(v["legacy_perishable"] for v in views)
    if village and legacy:
        reject("LEGACY_FRESHNESS_UNKNOWN", "历史无期限易腐原料不能制作可信村庄鲜餐。")
    if village and not exact and (visible or execute) or batches > 1 and not exact:
        reject("INGREDIENTS_MISMATCH", "所选原料不满足标准配方，请核对已知配比。")
    matched = recipe is not None and set(expected) == set(amounts)
    output = Item.objects.filter(pk=recipe.item_id, is_deleted=False).first() if recipe and matched else None
    if recipe and matched and (output is None or recipe.output_quantity < 1):
        reject("RECIPE_CATALOG_UNAVAILABLE", "菜品物品或标准产量尚未配置。")
    expiries = [v["expires_world_minute"] for v in views if v["expires_world_minute"] is not None
                and _perishable(Item.objects.get(pk=v["item_id"]))]
    expires = None if legacy else min([minute + SHELF_LIFE, *expiries])
    quantity = recipe.output_quantity * batches if exact else 1
    mask = recipe_id is None and not known
    payload = dict(recipe_id=recipe.pk if recipe else None, recipe_version=recipe.version if recipe else None,
        recipe_ingredients=expected, recipe_output=(recipe.item_id, recipe.output_quantity) if recipe else None,
        batches=batches, level=level, tool_instance_id=str(tool.instance_id), tool_item_id=tool.item_id,
        tool_uses=tool.remaining_uses, tool_amount=tool.item_amount,
        inputs=sorted(views, key=lambda v: v["instance_id"]), rule_version=RULE_VERSION)
    preview = dict(available=True, reason="", quote_hash=hmac.new(settings.SECRET_KEY.encode(),
        json.dumps(payload, sort_keys=True).encode(), hashlib.sha256).hexdigest(),
        recipe_id=None if mask else recipe.pk if recipe else None,
        recipe_name="探索料理（制作后揭晓）" if mask else recipe.recipe_name if recipe else "自由配料尝试",
        output_item_id=None if mask else output.pk if output else None,
        output_quantity=None if mask or not visible else quantity, batches=batches,
        action_cost=AP_PER_BATCH * batches, tool_uses=batches, expires_world_minute=expires,
        freshness_unknown=legacy, selected_batches=views, missing_items=[],
        warnings=(["历史无期限易腐原料的成品仍为鲜期不明，不能用于鲜餐交付。"] if legacy else [])
            + (["数量未显示，成品与配比结果在实际制作后揭晓。"] if not visible else []) + [PITY_POLICY])
    return dict(recipe=recipe if matched else None, exact=exact, output=output, quantity=quantity,
        rows=rows, amounts=amounts, tool=tool, batches=batches, level=level, legacy=legacy,
        expires=expires, preview=preview)


def preview_cooking(player, bag, params, minute):
    try:
        return _prepare(player, bag, params, minute)["preview"]
    except CookingError as exc:
        return dict(available=False, reason=exc.code, message=exc.message, quote_hash=None,
                    selected_batches=[], missing_items=_visible_missing(player, bag, params, minute), warnings=[])


def _visible_missing(player, bag, params, minute):
    """Only formal visible quantities can produce numerical shortage hints."""
    if not bag.pk or not isinstance(params, dict) or type(params.get("recipe_id")) is not int:
        return []
    batches = params.get("batches", 1)
    if type(batches) is not int or not 1 <= batches <= 3:
        return []
    recipe = Recipe.objects.prefetch_related("ingredients").filter(pk=params["recipe_id"]).first()
    if not recipe or not _visibility(recipe, _knowledge(player))[1]:
        return []
    result = []
    for ingredient in recipe.ingredients.all():
        item = Item.objects.filter(pk=ingredient.item_id, is_deleted=False).first()
        available = 0
        if item:
            for slot in bag.slots.filter(item_id=item.pk, item_amount__gt=0):
                view = _ingredient_view(slot, item, minute)
                if not view["reason"] and not (recipe.pk in VILLAGE_RECIPES and view["legacy_perishable"]):
                    available += slot.item_amount
        shortage = max(0, ingredient.count * batches - available)
        if shortage:
            result.append(dict(item_id=ingredient.item_id, item_name=ingredient.item_name, quantity=shortage,
                location_code="BILL_FARM" if ingredient.item_id in {1030, 1005, 1025, 1042, 1038, 1043} else "GENERAL_STORE",
                reason="FRESH_INGREDIENT_REQUIRED" if recipe.pk in VILLAGE_RECIPES else "INGREDIENT_REQUIRED"))
    return result


@transaction.atomic
def execute_player_cooking(player, bag, params, rid, clock):
    if not isinstance(params, dict) or "expected_quote" not in params:
        reject("INVALID_PARAMS", "请先预览并确认本次烹饪。")
    supplied_quote = params["expected_quote"]
    base = {k: v for k, v in params.items() if k != "expected_quote"}
    plan = _prepare(player, bag, base, clock.world_minute, execute=True)
    if not isinstance(supplied_quote, str) or supplied_quote != plan["preview"]["quote_hash"]:
        reject("QUOTE_CHANGED", "所选批次、工具或配方已变化，请重新预览。")
    state, _ = PlayerState.objects.get_or_create(player_id=str(player.pk))
    state = PlayerState.objects.select_for_update().get(pk=state.pk)
    recover_action_points(state)
    cost = AP_PER_BATCH * plan["batches"]
    if state.action_points < cost:
        reject("INSUFFICIENT_ACTION_POINTS", "行动力不足。")
    consumed = []
    for source, quantity in plan["rows"]:
        source = InventorySlot.objects.select_for_update().get(pk=source.pk)
        if source.item_amount < quantity:
            reject("INSUFFICIENT_INGREDIENTS", "食材批次数量已经变化。")
        consumed.append(dict(_ingredient_view(source, Item.objects.get(pk=source.item_id), clock.world_minute), quantity=quantity))
        source.item_amount -= quantity
        source.save(update_fields=["item_amount"])
    tool, used = plan["tool"], None
    for _ in range(plan["batches"]):
        used = wear_tool(tool)
        tool = InventorySlot.objects.get(bag=bag, instance_id=used["instance_id"])
    used["uses"] = plan["batches"]
    state.action_points -= cost
    state.action_points_recovered_at = timezone.now()
    state.save(update_fields=["action_points", "action_points_recovered_at"])
    recipe = plan["recipe"]
    rng = deterministic_rng(rid, "PLAYER_COOK_V1", clock.world_minute // 1440)
    from cooking.views import FAILED_DISH_ITEM_ID, _apply_pity
    item_id = recipe.item_id if recipe else FAILED_DISH_ITEM_ID
    seconds = max(0, recipe.default_cook_time + rng.randint(-40 if plan["exact"] else -50, 40 if plan["exact"] else 50)) if recipe else 0
    amounts = {key: value / plan["batches"] for key, value in plan["amounts"].items()}
    quality = calculate_quality(recipe, amounts, plan["exact"], seconds, rng) if recipe else 1
    if recipe and plan["exact"] and plan["level"] >= 2 and not plan["legacy"]:
        quality = max(3, quality)
    pity_counts, pity_changed = None, False
    if recipe and not recipe.village_only and plan["exact"] and not plan["legacy"] and plan["batches"] == 1:
        # Keep the original player's counters/config and the original tier rules.
        # Their mutation remains inside the same asset and response transaction.
        from cooking.models import PityCounter
        PityCounter.objects.select_for_update().get_or_create(player=player)
        normal_quality, normal_seconds = quality, seconds
        quality, seconds, pity_counts = _apply_pity(player, recipe.item_id, quality, seconds, recipe.default_cook_time)
        pity_changed = (quality, seconds) != (normal_quality, normal_seconds)
    batch_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"player-cooking:{player.pk}:{rid}"))
    metadata = dict(rule_version=RULE_VERSION, maker_player_id=str(player.pk), production_request_id=str(rid),
        production_batch_id=batch_id, recipe_id=recipe.pk if recipe else None, recipe_name=recipe.recipe_name if recipe else "失败的菜品",
        recipe={k: getattr(recipe, k) for k in ("version", "difficulty", "cuisine", "dish_category", "serving_style", "taste_profile", "default_cook_time")} if recipe else {},
        ingredients=deepcopy(consumed), ingredients_exact=plan["exact"], standard_batches=plan["batches"],
        output_quantity=plan["quantity"], quality=quality,
        cook_time_source="LEGACY_PITY_SECONDS" if pity_changed else "RECIPE_RANDOM_SECONDS" if recipe else "FAILED_RECIPE",
        freshness_policy="LEGACY_UNKNOWN" if plan["legacy"] else "EARLIEST_PERISHABLE_INPUT")
    if pity_counts is not None:
        metadata["pity_counts"] = pity_counts
    slot = InventorySlot.objects.create(bag=bag, slot_index=_index(bag), item_id=item_id,
        item_amount=plan["quantity"], quality=quality, produced_world_minute=clock.world_minute,
        expires_world_minute=plan["expires"], freshness_unknown=plan["legacy"], source_request_id=rid,
        ingredient="、".join(f"{v['item_name']}×{v['quantity']}" for v in consumed), cook_time=seconds,
        dish_metadata=metadata, rated=False, rating_price=-1)
    learned = False
    if recipe:
        _, learned = PlayerRecipe.objects.get_or_create(player=state, recipe_name=recipe.recipe_name,
            defaults=dict(catalog_recipe=recipe, show_ingredient_count=recipe.pk in VILLAGE_RECIPES))
    dish = dict(instance_id=str(slot.instance_id), item_id=item_id, quantity=slot.item_amount, quality=quality,
        cook_time_seconds=seconds, dish_metadata=deepcopy(metadata), produced_world_minute=clock.world_minute,
        expires_world_minute=slot.expires_world_minute, freshness_unknown=slot.freshness_unknown)
    rewards = []
    if recipe and plan["exact"] and not plan["legacy"]:
        rewards = [award_skill(player, source_type="player_cooking", source_id=rid, skill_code="cooking",
            xp=XP_PER_BATCH * plan["batches"], milestone=dict(code="first_player_cooking", title="亲自制作合格料理", xp=FIRST_COOK_BONUS),
            facts=dict(recipe_id=recipe.pk, recipe_version=recipe.version, production_batch_id=batch_id,
                output_item_id=item_id, output_instance_id=str(slot.instance_id), quantity=slot.item_amount,
                quality=quality, standard_batches=plan["batches"], freshness_unknown=slot.freshness_unknown))]
    EconomicLedgerEntry.objects.create(request_id=uuid.uuid5(uuid.NAMESPACE_URL, f"player-cooking-ledger:{player.pk}:{rid}"),
        entry_type="PLAYER_COOK", world_minute=clock.world_minute,
        changes={"player_id": str(player.pk), "inputs": consumed, "output": dish, "tool_used": used})
    return dict(dish=dish, items_consumed=consumed, items_added=[dish], tool_used=used,
        action_cost=cost, action_points=state.action_points, growth_rewards=rewards, is_newly_learned=learned)


def is_player_cooked(slot, player):
    """Verify the immutable production fact, including after trade/sample splitting."""
    metadata = slot.dish_metadata or {}
    if metadata.get("maker_player_id") != str(player.pk) or metadata.get("rule_version") != RULE_VERSION:
        return False
    try:
        rid = uuid.UUID(metadata.get("production_request_id", ""))
    except (ValueError, TypeError, AttributeError):
        return False
    receipt = VillagePlayerActionReceipt.objects.filter(player=player, request_id=rid, action="cook_dish").first()
    dish = (receipt.response.get("data", {}).get("result", {}).get("dish", {}) if receipt and receipt.response.get("success") else {})
    original = dish.get("dish_metadata") or {}
    return (dish.get("item_id") == slot.item_id and metadata.get("recipe_id") is not None
        and original.get("ingredients_exact") is True and dish.get("freshness_unknown") is False
        and str(slot.source_request_id) == str(rid)
        and dish.get("produced_world_minute") == slot.produced_world_minute
        and dish.get("expires_world_minute") == slot.expires_world_minute
        and dish.get("freshness_unknown") == slot.freshness_unknown and dish.get("quality") == slot.quality
        and all(metadata.get(key) == original.get(key) for key in
                ("production_batch_id", "recipe_id", "recipe", "ingredients", "standard_batches",
                 "maker_player_id", "production_request_id")))
