"""Trusted precise-batch cooking; previews are pure, execution shares asset receipts."""
from copy import deepcopy
import hashlib
import hmac
import json
import uuid

from django.db import transaction
from django.conf import settings
from django.utils import timezone
from items.quality import cooking_quality, ingredient_average, valid_quality, quality_rng
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
from player.equipment import stats_for
from .production_tools import resolve_tool, wear_tool, tools_overview, ToolError, _index
from .player_cooking_rules import (VILLAGE_RECIPES, RULE_VERSION,
    XP_PER_BATCH, FIRST_COOK_BONUS, AP_PER_BATCH)

from .professional_catalog import PROFESSIONAL_RECIPES, batch_evidence
from .career_catalog import (content_access, reserve_outputs, record_production,
    trial_request_id, recipe_reference_cost)
from .player_cooking_technique import CUTS, technique_for, ingredient_feedback

class CookingError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


PITY_POLICY = "正确配比的普通单批料理沿用旧保底；食材品质、技能和炊具共同影响成品，从略逊到绝品均可正常制作和售卖。"


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


def _ingredient_view(slot, item, minute):
    reason = "INGREDIENT_QUALITY_INVALID" if not valid_quality(slot.quality) else "PROVENANCE_INVALID" if slot.produced_world_minute is not None and slot.produced_world_minute > minute else ""
    return dict(instance_id=str(slot.instance_id), item_id=slot.item_id, item_name=item.item_name, item_type=item.item_type,
        quantity=slot.item_amount, available_quantity=0 if reason else slot.item_amount,
        quality=slot.quality, produced_world_minute=slot.produced_world_minute,
        expires_world_minute=None, freshness_unknown=False,
        source_request_id=str(slot.source_request_id) if slot.source_request_id else None,
        legacy_perishable=False, reason=reason)


def _available_ap(player):
    state = PlayerState.objects.filter(player_id=str(player.pk)).first()
    if state is None:
        return NATURAL_ACTION_POINT_CAP
    if state.action_points >= NATURAL_ACTION_POINT_CAP:
        return state.action_points
    elapsed = max(0, int((timezone.now() - state.action_points_recovered_at).total_seconds()))
    return min(NATURAL_ACTION_POINT_CAP, state.action_points + elapsed // ACTION_POINT_RECOVERY_SECONDS)


def cooking_overview(player, bag, minute, read_only=False, *, tool_state=None, levels=None, slots=None):
    if levels is None:
        levels = {row["code"]: row["level"] for row in growth_snapshot(player, read_only=True, compact=True)["skills"]}
    level = levels.get("cooking", 1)
    knowledge = _knowledge(player)
    tool_state = tool_state if tool_state is not None else tools_overview(player, bag, minute, read_only=read_only, levels=levels, slots=slots)
    tools = [row for row in tool_state["tools"] if row["capability"] == "cooking"]
    available_items = set(Item.objects.filter(is_deleted=False).values_list("pk", flat=True))
    recipes = []
    for recipe in Recipe.objects.prefetch_related("ingredients").order_by("pk"):
        known, visible = _visibility(recipe, knowledge)
        if recipe.village_only and recipe.pk not in VILLAGE_RECIPES or not known:
            continue
        required, tier = VILLAGE_RECIPES.get(recipe.pk, (1, 1))
        access = content_access(recipe.item_id)
        reason = access["blocked_reason"] if not access["can_produce"] else "SKILL_LEVEL_REQUIRED" if level < required else "TOOL_REQUIRED" if not any(t["usable"] and t["tier"] >= tier for t in tools) else ""
        if not recipe.ingredients.all() or recipe.item_id not in available_items:
            reason = "RECIPE_CATALOG_UNAVAILABLE"
        recipes.append(dict(id=recipe.pk, name=recipe.recipe_name, item_id=recipe.item_id,
            output_quantity=recipe.output_quantity, required_level=required, minimum_tool_tier=tier,
            available=not reason, reason=reason, known=known, quantities_visible=visible,
            development=access, technique=technique_for(recipe, level=level),
            reference_cost=recipe_reference_cost(recipe.pk),
            ingredients=[dict(item_id=i.item_id, item_name=i.item_name, count=i.count if visible else None)
                         for i in recipe.ingredients.all()]))
    ids = _ingredient_ids()
    items = {i.pk: i for i in Item.objects.filter(pk__in=ids, is_deleted=False)}
    slots = (bag.slots.filter(item_amount__gt=0).select_related("usage", "dish").order_by("slot_index") if bag.pk else []) if slots is None else sorted(slots, key=lambda s: s.slot_index)
    ingredients = [_ingredient_view(s, items[s.item_id], minute) for s in slots
                   if _is_ingredient(s, items.get(s.item_id), ids)] if bag.pk else []
    goals = [dict(code=code, title=title, completed=PlayerMilestoneProgress.objects.filter(player=player, code=code).exists(),
                  location_code=location) for code, title, location in (
        ("first_player_cooking", "亲自制作合格料理", "RESTAURANT"),
        ("first_formal_dish_review", "完成本人料理的正式点评", "RECORD_HALL"))]
    from .player_cooking_sessions import active_session
    return dict(revision=PlayerGrowthState.objects.filter(player=player).values_list("revision", flat=True).first() or 0,
        observed_world_minute=minute, level=level, max_batches=max((t["cooking_batches"] for t in tools if t["usable"]), default=1),
        recipes=recipes, ingredients=ingredients, tools=tools, goals=goals, pity_policy=PITY_POLICY,
        active_session=active_session(player), action_cost_per_batch=AP_PER_BATCH, tool_uses_per_batch=1)


def _prepare(player, bag, params, minute, *, execute=False):
    allowed = {"recipe_id", "batches", "selections", "tool_instance_id", "cut_style"}
    if not isinstance(params, dict) or set(params) - allowed:
        reject("INVALID_PARAMS", "烹饪参数无效。")
    if not isinstance(params.get("cut_style", "WHOLE"), str) or params.get("cut_style", "WHOLE") not in CUTS:
        reject("INVALID_PARAMS", "切法请选择整份、切块或切片，本锅主料统一使用。")
    if not bag.pk or bag.player_id != player.pk or bag.character_id != player.username:
        reject("INGREDIENT_NOT_OWNED", "只能使用本人主背包中的食材。")
    batches = params.get("batches", 1)
    if type(batches) is not int or not 1 <= batches <= 5:
        reject("INVALID_PARAMS", "每次请选择1至5个标准批次。")
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
            reject(view["reason"], "所选原料品质无效或来源记录异常。")
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
    if recipe:
        access = content_access(recipe.item_id)
        if not access["can_produce"]:
            reject(access["blocked_reason"], access["unavailable_reason"])
    required, tier = VILLAGE_RECIPES.get(recipe.pk, (1, 1)) if recipe else (1, 1)
    level = _level(player)
    if level < required:
        reject("SKILL_LEVEL_REQUIRED", "烹饪等级未达到配方要求。")
    if batches > 1 and not visible:
        reject("BATCH_RECIPE_KNOWLEDGE_REQUIRED", "多批制作需要已明确数量的标准配方。")
    if not params.get("tool_instance_id"):
        reject("TOOL_MISSING", "请选择本人实际持有的炊具。")
    try:
        tool = resolve_tool(player, bag, "cooking", params["tool_instance_id"], tier, minute=minute)
    except ToolError as exc:
        reject(exc.code, exc.message)
    tool_stats = stats_for(tool)
    if not tool_stats or batches > tool_stats["cooking_batches"]:
        reject("TOOL_LEVEL_REQUIRED", "所选炊具的单次批数上限不足。")
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
    legacy = False
    if village and not exact and (visible or execute) or batches > 1 and not exact:
        reject("INGREDIENTS_MISMATCH", "所选原料不满足标准配方，请核对已知配比。")
    matched = recipe is not None and set(expected) == set(amounts)
    output = Item.objects.filter(pk=recipe.item_id, is_deleted=False).first() if recipe and matched else None
    if recipe and matched and (output is None or recipe.output_quantity < 1):
        reject("RECIPE_CATALOG_UNAVAILABLE", "菜品物品或标准产量尚未配置。")
    expires = None
    quantity = recipe.output_quantity * batches if exact else 1
    mask = recipe_id is None and not known
    payload = dict(recipe_id=recipe.pk if recipe else None, recipe_version=recipe.version if recipe else None,
        recipe_ingredients=expected, recipe_output=(recipe.item_id, recipe.output_quantity) if recipe else None,
        batches=batches, level=level, tool_instance_id=str(tool.instance_id), tool_item_id=tool.item_id,
        tool_uses=tool.remaining_uses, tool_amount=tool.item_amount, tool_configuration=tool_stats,
        inputs=sorted(views, key=lambda v: v["instance_id"]), rule_version=RULE_VERSION)
    if "cut_style" in params:
        payload["cut_style"] = params["cut_style"]
    preview = dict(available=True, reason="", quote_hash=hmac.new(settings.SECRET_KEY.encode(),
        json.dumps(payload, sort_keys=True).encode(), hashlib.sha256).hexdigest(),
        recipe_id=None if mask else recipe.pk if recipe else None,
        recipe_name="探索料理（制作后揭晓）" if mask else recipe.recipe_name if recipe else "自由配料尝试",
        output_item_id=None if mask else output.pk if output else None,
        output_quantity=None if mask or not visible else quantity, batches=batches,
        action_cost=AP_PER_BATCH * batches, tool_uses=batches, expires_world_minute=expires,
        freshness_unknown=legacy, ingredient_average_quality=ingredient_average(views), selected_batches=views, missing_items=[],
        reference_cost=recipe_reference_cost(recipe.pk, batches=batches, tool=tool) if recipe else None,
        warnings=(["数量未显示，成品与配比结果在实际制作后揭晓。"] if not visible else []) + [PITY_POLICY],
        technique=technique_for(None if mask else recipe, ingredients=views if mask or not recipe else None,
            level=level, tier=tool_stats["tier"]))
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
    if type(batches) is not int or not 1 <= batches <= 5:
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
                if not view["reason"]:
                    available += slot.item_amount
        shortage = max(0, ingredient.count * batches - available)
        if shortage:
            result.append(dict(item_id=ingredient.item_id, item_name=ingredient.item_name, quantity=shortage,
                location_code="BILL_FARM" if ingredient.item_id in {1030, 1005, 1025, 1042, 1038, 1043} else "GENERAL_STORE",
                reason="INGREDIENT_REQUIRED"))
    return result


@transaction.atomic
def execute_player_cooking(player, bag, params, rid, clock):
    session = None
    if isinstance(params, dict) and "session_id" in params:
        if set(params) != {"session_id"}:
            reject("INVALID_PARAMS", "手动出锅只需提供本人的烹饪会话。")
        from .player_cooking_sessions import owned_session, advance_active
        session = owned_session(player, params["session_id"])
        advance_active(session, timezone.now())
        params = dict(deepcopy(session.plan["parameters"]), expected_quote=session.plan["expected_quote"])
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
    if plan["recipe"] and not reserve_outputs({plan["recipe"].item_id: plan["quantity"]}, trial_request_id(player.pk, rid), clock.world_minute):
        reject("CAREER_TRIAL_LIMIT", "本期试制额度不足，请先完成已有菜品试卖。")
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
    rng = quality_rng(rid, "PLAYER_COOK")
    from cooking.views import FAILED_DISH_ITEM_ID, _apply_pity
    item_id = recipe.item_id if recipe else FAILED_DISH_ITEM_ID
    amounts = {key: value / plan["batches"] for key, value in plan["amounts"].items()}
    average = ingredient_average(consumed)
    if session:
        from .player_cooking_technique import evaluate
        quality, seconds, feedback = evaluate(session, average, plan["exact"], recipe)
    else:
        seconds = max(0, recipe.default_cook_time + rng.randint(-40 if plan["exact"] else -50, 40 if plan["exact"] else 50)) if recipe else 0
        quality = calculate_quality(recipe, amounts, plan["exact"], seconds, rng) if recipe else 1
        quality = cooking_quality(quality, average, plan["level"], stats_for(tool)["quality_bonus_chance"], rng) if recipe and plan["batches"] == 1 else quality
        feedback = dict(ingredients=ingredient_feedback(average), cut="本次沿用自动切配。",
            heat="本次沿用原有自动烹饪规则。", timing="本次由自动流程决定出锅。",
            suggestion="可尝试手动切配与控火，在最佳区间出锅。")
    pity_counts, pity_changed = None, False
    if recipe and not recipe.village_only and plan["exact"] and not plan["legacy"] and plan["batches"] == 1:
        # Keep the original player's counters/config and the original tier rules.
        # Their mutation remains inside the same asset and response transaction.
        from cooking.models import PityCounter
        PityCounter.objects.select_for_update().get_or_create(player=player)
        normal_quality, normal_seconds = quality, seconds
        quality, pity_seconds, pity_counts = _apply_pity(player, recipe.item_id, quality, seconds, recipe.default_cook_time)
        if not session:
            seconds = pity_seconds
        pity_changed = (quality, seconds) != (normal_quality, normal_seconds)
    if pity_changed:
        feedback["pity_applied"] = True
        feedback["suggestion"] += " 本次沿用原有保底提升了最终结果。"
    batch_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"player-cooking:{player.pk}:{rid}"))
    metadata = dict(rule_version=RULE_VERSION, maker_player_id=str(player.pk), production_request_id=str(rid),
        production_batch_id=batch_id, recipe_id=recipe.pk if recipe else None, recipe_name=recipe.recipe_name if recipe else "失败的菜品",
        recipe={k: getattr(recipe, k) for k in ("version", "difficulty", "cuisine", "dish_category", "serving_style", "taste_profile", "default_cook_time")} if recipe else {},
        ingredients=deepcopy(consumed), ingredients_exact=plan["exact"], standard_batches=plan["batches"],
        output_quantity=plan["quantity"], quality=quality,
        ingredient_average_quality=average, quality_rule="quality-v2",
        cook_time_source="MANUAL_COOKING" if session else "LEGACY_PITY_SECONDS" if pity_changed else "RECIPE_RANDOM_SECONDS" if recipe else "FAILED_RECIPE",
        cooking_feedback=feedback,
        freshness_policy="QUALITY_ONLY")
    if session:
        metadata.update(cooking_session_id=str(session.session_id), cut_style=session.plan["cut_style"],
            cooking_progress=round(session.state["progress"], 3), cooking_technique=deepcopy(session.plan["technique"]))
    if pity_counts is not None:
        metadata["pity_counts"] = pity_counts
    quality_groups = {}
    for _ in range(plan["batches"] if plan["exact"] else 1):
        improved = quality
        if not session and recipe and plan["exact"] and plan["batches"] > 1:
            improved = cooking_quality(calculate_quality(recipe, amounts, True, seconds, rng), average, plan["level"], stats_for(tool)["quality_bonus_chance"], rng)
        quality_groups[improved] = quality_groups.get(improved, 0) + 1
    output_slots = []
    for group_quality, group_count in quality_groups.items():
        group_metadata = deepcopy(metadata)
        group_metadata.update(quality=group_quality, standard_batches=group_count,
            output_quantity=recipe.output_quantity * group_count if plan["exact"] else plan["quantity"],
            production_batch_id=str(uuid.uuid5(uuid.UUID(batch_id), str(group_quality))))
        from inventory.storage import add_inventory_items
        output_slots.extend(slot for slot, _ in add_inventory_items(bag, item_id, group_metadata["output_quantity"], attributes=dict(
            quality=group_quality, produced_world_minute=clock.world_minute,
            expires_world_minute=plan["expires"], freshness_unknown=plan["legacy"], source_request_id=rid,
            ingredient="、".join(f"{v['item_name']}×{v['quantity']}" for v in consumed), cook_time=seconds,
            dish_metadata=group_metadata, rated=False, rating_price=-1)))
    slot = output_slots[0]
    metadata, quality = slot.dish_metadata, slot.quality
    learned = False
    mastered = False
    if recipe:
        knowledge, learned = PlayerRecipe.objects.get_or_create(player=state, recipe_name=recipe.recipe_name,
            defaults=dict(catalog_recipe=recipe, show_ingredient_count=plan["exact"]))
        # A correct real cook proves the quantities even for ordinary recipes and
        # existing name-only knowledge. All valid qualities can trade and request review.
        mastered = plan["exact"] and (learned or not knowledge.show_ingredient_count)
        if plan["exact"] and (not knowledge.show_ingredient_count or knowledge.catalog_recipe_id != recipe.pk):
            knowledge.catalog_recipe = recipe
            knowledge.show_ingredient_count = True
            knowledge.save(update_fields=["catalog_recipe", "show_ingredient_count"])
    dishes = [dict(instance_id=str(output.instance_id), item_id=item_id, quantity=output.item_amount, quality=output.quality,
        cook_time_seconds=seconds, dish_metadata=deepcopy(output.dish_metadata), produced_world_minute=clock.world_minute,
        expires_world_minute=output.expires_world_minute, freshness_unknown=output.freshness_unknown) for output in output_slots]
    dish = dishes[0]
    rewards = []
    if recipe and plan["exact"] and not plan["legacy"]:
        rewards = [award_skill(player, source_type="player_cooking", source_id=rid, skill_code="cooking",
            xp=XP_PER_BATCH * plan["batches"], milestone=dict(code="first_player_cooking", title="亲自制作合格料理", xp=FIRST_COOK_BONUS),
            facts=dict(recipe_id=recipe.pk, recipe_version=recipe.version, production_batch_id=batch_id,
                output_item_id=item_id, output_instance_id=str(slot.instance_id), quantity=sum(output.item_amount for output in output_slots),
                quality=quality, standard_batches=plan["batches"], freshness_unknown=slot.freshness_unknown,
                source_request_id=str(rid), produced_world_minute=clock.world_minute,
                tool_tier=stats_for(tool)["tier"], tool_equipment_level=tool.equipment_level, outputs=[batch_evidence(output) for output in output_slots]))]
    EconomicLedgerEntry.objects.create(request_id=uuid.uuid5(uuid.NAMESPACE_URL, f"player-cooking-ledger:{player.pk}:{rid}"),
        entry_type="PLAYER_COOK", world_minute=clock.world_minute,
        changes={"player_id": str(player.pk), "inputs": consumed, "output": dish, "outputs": dishes, "tool_used": used})
    record_production("PLAYER", player.pk, rid, {item_id: sum(output.item_amount for output in output_slots)}, clock.world_minute,
        source_kind="COOK", recipe_id=recipe.pk if recipe else None,
        reservation_request_id=trial_request_id(player.pk, rid),
        output_instance_ids=[str(output.instance_id) for output in output_slots])
    result = dict(dish=dish, dishes=dishes, items_consumed=consumed, items_added=dishes, tool_used=used,
        action_cost=cost, action_points=state.action_points, growth_rewards=rewards, is_newly_learned=learned,
        recipe_quantities_unlocked=mastered, cooking_feedback=feedback)
    if session:
        from .player_cooking_sessions import project_session
        session.status = "FINISHED"; session.settlement_request_id = rid; session.updated_at = timezone.now()
        session.save(update_fields=["status", "state", "updated_at", "settlement_request_id"])
        result["cooking_session"] = project_session(session)
    return result


def is_player_cooked(slot, player):
    """Verify the immutable production fact, including after trade/sample splitting."""
    metadata = slot.dish_metadata or {}
    if metadata.get("maker_player_id") != str(player.pk) or metadata.get("rule_version") not in ("player-cooking-v1", RULE_VERSION):
        return False
    try:
        rid = uuid.UUID(metadata.get("production_request_id", ""))
    except (ValueError, TypeError, AttributeError):
        return False
    receipt = VillagePlayerActionReceipt.objects.filter(player=player, request_id=rid, action="cook_dish").first()
    result = receipt.response.get("data", {}).get("result", {}) if receipt and receipt.response.get("success") else {}
    candidates = result.get("dishes") or [result.get("dish", {})]
    dish = next((candidate for candidate in candidates if candidate.get("quality") == slot.quality
        and (candidate.get("dish_metadata") or {}).get("production_batch_id") == metadata.get("production_batch_id")), {})
    original = dish.get("dish_metadata") or {}
    if original.get("cook_time_source") == "MANUAL_COOKING" and (
            slot.cook_time != dish.get("cook_time_seconds") or any(metadata.get(key) != original.get(key) for key in
                ("cook_time_source", "cooking_session_id", "cut_style", "cooking_progress", "cooking_technique", "cooking_feedback"))):
        return False
    return (dish.get("item_id") == slot.item_id and metadata.get("recipe_id") is not None
        and original.get("ingredients_exact") is True
        and str(slot.source_request_id) == str(rid)
        and dish.get("produced_world_minute") == slot.produced_world_minute
        and dish.get("expires_world_minute") == slot.expires_world_minute
        and dish.get("freshness_unknown") == slot.freshness_unknown and dish.get("quality") == slot.quality
        and all(metadata.get(key) == original.get(key) for key in
                ("production_batch_id", "recipe_id", "recipe", "ingredients", "standard_batches",
                 "maker_player_id", "production_request_id")))
