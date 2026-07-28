import random
import uuid as uuid_mod
from django.db import transaction
from django.db.models import Max
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view
from rest_framework.response import Response

from config.auth import get_player_id
from inventory.models import InventoryBag, InventorySlot
from inventory.serializers import serialize_bag
from recipes.models import Recipe
from player.models import PlayerState, PlayerRecipe
from items.models import Item
from .models import PityConfig, PityCounter


@extend_schema(
    request={
        "application/json": {
            "type": "object",
            "properties": {
                "recipeId": {"type": "integer"},
                "normalQuality": {"type": "integer", "minimum": 1, "maximum": 6},
                "cookTime": {"type": "integer"},
                "defaultCookTime": {"type": "integer"},
                "ingredientsCorrect": {"type": "boolean"},
            },
            "required": ["recipeId", "normalQuality", "cookTime", "defaultCookTime", "ingredientsCorrect"],
        }
    },
    responses={
        200: {
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "data": {
                    "type": "object",
                    "properties": {
                        "finalQuality": {"type": "integer"},
                        "cookTime": {"type": "integer"},
                        "pityCount4": {"type": "integer"},
                        "pityCount5": {"type": "integer"},
                        "pityCount6": {"type": "integer"},
                    },
                },
            },
        }
    },
)
@api_view(["POST"])
def pity_check(request):
    """
    烹饪保底检查。
    客户端计算完正常品质后调用此接口，后端根据保底机制返回最终品质和烹饪时间。
    """
    player_id = get_player_id(request)
    if not player_id:
        return Response({"success": False, "message": "未登录"}, status=401)

    data = request.data
    normal_quality = data.get("normalQuality", 3)
    cook_time = data.get("cookTime", 0)
    default_cook_time = data.get("defaultCookTime", 0)
    ingredients_correct = data.get("ingredientsCorrect", False)

    # 食材不正确，不触发保底，不增加计数器
    if not ingredients_correct:
        return Response({
            "success": True,
            "data": {
                "finalQuality": normal_quality,
                "cookTime": cook_time,
                "pityCount4": 0,
                "pityCount5": 0,
                "pityCount6": 0,
            },
        })

    config = PityConfig.get_config()
    counter = PityCounter.get_or_create_for(request.user)

    with transaction.atomic():

        # 各计数器 +1
        counter.count_4 += 1
        counter.count_5 += 1
        counter.count_6 += 1

        final_quality = normal_quality
        final_cook_time = cook_time

        # 从高到低检查保底
        # 绝品 (quality >= 6)
        pity_6_triggered, final_quality, final_cook_time = _check_tier(
            counter.count_6,
            config.quality_6_normal_phase,
            config.quality_6_guarantee_at,
            config.quality_6_ramp_probabilities,
            config.quality_6_time_range,
            default_cook_time,
            final_quality,
            final_cook_time,
            target_quality=6,
        )
        if pity_6_triggered:
            counter.count_4 = 0
            counter.count_5 = 0
            counter.count_6 = 0
            counter.save()
            return _build_response(final_quality, final_cook_time, counter)

        # 极品 (quality >= 5)
        pity_5_triggered, final_quality, final_cook_time = _check_tier(
            counter.count_5,
            config.quality_5_normal_phase,
            config.quality_5_guarantee_at,
            config.quality_5_ramp_probabilities,
            config.quality_5_time_range,
            default_cook_time,
            final_quality,
            final_cook_time,
            target_quality=5,
        )
        if pity_5_triggered:
            counter.count_4 = 0
            counter.count_5 = 0
            counter.save()
            return _build_response(final_quality, final_cook_time, counter)

        # 优秀 (quality >= 4)
        pity_4_triggered, final_quality, final_cook_time = _check_tier(
            counter.count_4,
            config.quality_4_normal_phase,
            config.quality_4_guarantee_at,
            config.quality_4_ramp_probabilities,
            config.quality_4_time_range,
            default_cook_time,
            final_quality,
            final_cook_time,
            target_quality=4,
        )
        if pity_4_triggered:
            counter.count_4 = 0
            counter.save()
            return _build_response(final_quality, final_cook_time, counter)

        # 未触发任何保底，根据最终品质重置对应计数器
        if final_quality >= 6:
            counter.count_4 = 0
            counter.count_5 = 0
            counter.count_6 = 0
        elif final_quality >= 5:
            counter.count_4 = 0
            counter.count_5 = 0
        elif final_quality >= 4:
            counter.count_4 = 0

        counter.save()
        return _build_response(final_quality, final_cook_time, counter)


QUALITY_NAMES = {1: "略逊", 2: "一般", 3: "正常", 4: "优秀", 5: "极品", 6: "绝品"}
FAILED_DISH_ITEM_ID = 1078


@api_view(["POST"])
def cook(request):
    """
    服务端权威烹饪端点。
    接收食材列表，在一个事务中完成配方匹配、品质计算、保底、扣食材、加菜品。
    """
    player_id = get_player_id(request)
    if not player_id:
        return Response({"success": False, "message": "未登录"}, status=401)

    character_id = request.data.get("characterId", "player")
    ingredients_raw = request.data.get("ingredients", [])

    if not ingredients_raw:
        return Response({"success": False, "message": "食材列表为空"}, status=400)

    # 规范化食材列表
    ingredients = []
    for ing in ingredients_raw:
        item_id = ing.get("itemId")
        count = ing.get("count", 1)
        if item_id is None or count <= 0:
            return Response({"success": False, "message": "食材参数不正确"}, status=400)
        ingredients.append({"itemId": int(item_id), "count": int(count)})

    with transaction.atomic():
        bag = InventoryBag.objects.filter(
            player=request.user, character_id=character_id
        ).first()
        if not bag:
            return Response({"success": False, "message": "背包不存在"}, status=400)

        # 1. 验证食材并收集 slot 引用
        ingredient_slots = []  # [(slot, deduct_count), ...]
        for ing in ingredients:
            remaining = ing["count"]
            slots_for_item = list(
                bag.slots.filter(item_id=ing["itemId"], item_amount__gt=0).order_by("slot_index")
            )
            total_available = sum(s.item_amount for s in slots_for_item)
            if total_available < remaining:
                item_name = _get_item_name(ing["itemId"])
                return Response({
                    "success": False,
                    "message": f"背包中食材【{item_name}】数量不足，需要{ing['count']}，仅有{total_available}",
                }, status=400)
            for slot in slots_for_item:
                if remaining <= 0:
                    break
                deduct = min(slot.item_amount, remaining)
                ingredient_slots.append((slot, deduct))
                remaining -= deduct

        # 2. 构建食材描述字符串
        ingredient_str = ",".join(f"{_get_item_name(ing['itemId'])}*{ing['count']}" for ing in ingredients)

        # 3. 配方匹配
        input_item_ids = set(ing["itemId"] for ing in ingredients)
        all_recipes = list(Recipe.objects.prefetch_related("ingredients").all())

        matched_recipe = None
        for recipe in all_recipes:
            recipe_item_ids = set(ri.item_id for ri in recipe.ingredients.all())
            # 双向 Except：输入中多余的 + 配方中缺少的 = 不匹配集合
            extra_in_input = input_item_ids - recipe_item_ids
            missing_from_input = recipe_item_ids - input_item_ids
            if not extra_in_input and not missing_from_input:
                matched_recipe = recipe
                break

        if matched_recipe is None:
            # 无匹配配方：扣食材，给失败菜品
            for slot, deduct in ingredient_slots:
                slot.item_amount -= deduct
                slot.save()
                _clear_slot_if_empty(slot)

            _add_dish_to_bag(bag, FAILED_DISH_ITEM_ID, ingredient_str, 0, 1)
            bag.refresh_from_db()
            return Response({
                "success": True,
                "message": "配方不存在，食材变成了失败的菜品",
                "cookResult": {
                    "recipeName": "失败的菜品",
                    "itemId": FAILED_DISH_ITEM_ID,
                    "quality": 1,
                    "cookTime": 0,
                    "ingredientsExact": False,
                    "isNewlyLearned": False,
                    "ingredient": ingredient_str,
                    "qualityName": "略逊",
                    "pityCount4": 0,
                    "pityCount5": 0,
                    "pityCount6": 0,
                },
                "data": serialize_bag(bag),
            })

        # 4. 食材完全匹配检查
        recipe_ing_map = {ri.item_id: ri.count for ri in matched_recipe.ingredients.all()}
        player_ing_map = {}
        for ing in ingredients:
            player_ing_map[ing["itemId"]] = player_ing_map.get(ing["itemId"], 0) + ing["count"]

        ingredients_exact = (
            len(player_ing_map) == len(recipe_ing_map)
            and all(player_ing_map.get(k, 0) == v for k, v in recipe_ing_map.items())
        )

        # 5. 烹饪时间生成（先生成，供品质计算使用）
        cook_time_range = 40 if ingredients_exact else 50
        cook_time = max(0, matched_recipe.default_cook_time + random.randint(-cook_time_range, cook_time_range))

        # 6. 品质计算
        dish_quality = _calculate_dish_quality(matched_recipe, player_ing_map, ingredients_exact, cook_time)

        # 7. 保底检查（仅食材完全匹配时）
        pity_counts = {"count4": 0, "count5": 0, "count6": 0}
        if ingredients_exact:
            dish_quality, cook_time, pity_counts = _apply_pity(
                request.user, matched_recipe.item_id, dish_quality, cook_time, matched_recipe.default_cook_time
            )

        # 8. 菜谱解锁
        player_state, _ = PlayerState.objects.get_or_create(player_id=player_id)
        is_newly_learned = False
        if not PlayerRecipe.objects.filter(player=player_state, recipe_name=matched_recipe.recipe_name).exists():
            PlayerRecipe.objects.create(
                player=player_state,
                recipe_name=matched_recipe.recipe_name,
                catalog_recipe=matched_recipe,
                show_ingredient_count=False,
            )
            is_newly_learned = True

        # 9. 扣食材
        for slot, deduct in ingredient_slots:
            slot.item_amount -= deduct
            slot.save()
            _clear_slot_if_empty(slot)

        # 10. 加菜品
        _add_dish_to_bag(bag, matched_recipe.item_id, ingredient_str, cook_time, dish_quality)

        bag.refresh_from_db()

        return Response({
            "success": True,
            "message": "烹饪成功",
            "cookResult": {
                "recipeName": matched_recipe.recipe_name,
                "itemId": matched_recipe.item_id,
                "quality": dish_quality,
                "cookTime": cook_time,
                "ingredientsExact": ingredients_exact,
                "isNewlyLearned": is_newly_learned,
                "ingredient": ingredient_str,
                "qualityName": QUALITY_NAMES.get(dish_quality, "未知"),
                "pityCount4": pity_counts["count4"],
                "pityCount5": pity_counts["count5"],
                "pityCount6": pity_counts["count6"],
            },
            "data": serialize_bag(bag),
        })


# ─── cook 辅助函数 ────────────────────────────────────────────


def _get_item_name(item_id):
    try:
        return Item.objects.get(item_id=item_id).item_name
    except Item.DoesNotExist:
        return str(item_id)


def _clear_slot_if_empty(slot):
    if slot.item_amount <= 0:
        slot.item_id = 0
        slot.item_amount = 0
        slot.instance_id = uuid_mod.uuid4()
        slot.rated = False
        slot.rating_price = -1
        slot.overall_score = 0
        slot.ingredient = ""
        slot.item_bag_name = ""
        slot.cook_time = 0
        slot.dish_quality = 3
        slot.hide_in_shop = False
        slot.remaining_uses = None
        slot.review_comment = ""
        slot.save()


def _add_dish_to_bag(bag, item_id, ingredient, cook_time, dish_quality):
    """在背包中找空槽位放入菜品。"""
    empty = bag.slots.filter(item_amount=0).first()
    if not empty:
        max_index = bag.slots.aggregate(Max("slot_index"))["slot_index__max"]
        next_index = (max_index + 1) if max_index is not None else 0
        empty = InventorySlot.objects.create(bag=bag, slot_index=next_index)

    item_name = ""
    try:
        item_name = Item.objects.get(item_id=item_id).item_name
    except Item.DoesNotExist:
        pass

    empty.item_id = item_id
    empty.item_amount = 1
    empty.ingredient = ingredient
    empty.cook_time = cook_time
    empty.dish_quality = dish_quality
    empty.item_bag_name = f"{item_name}_{empty.slot_index}"
    empty.save()


def _calculate_dish_quality(recipe, player_ing_map, ingredients_exact, cook_time):
    """
    品质计算（移植自 Unity CraftingPanel.CalculateDishQuality）。
    基础品质 + 食材匹配惩罚 + 随机波动 + 烹饪时间加成。
    """
    quality = recipe.default_dish_quality

    # 食材匹配惩罚
    for ri in recipe.ingredients.all():
        player_count = player_ing_map.get(ri.item_id, 0)
        if player_count <= 0:
            quality -= 3  # 完全没放
        elif player_count < ri.count:
            quality -= 1  # 数量不足
        elif player_count > ri.count:
            quality -= 1  # 数量超过

    # 随机波动：70%不变，18% -1，12% +1
    roll = random.randint(0, 99)
    if roll >= 70:
        quality += -1 if roll < 88 else 1

    # 烹饪时间加成（使用实际烹饪时间与默认时间的偏差）
    time_bonus = _get_cook_time_quality_bonus(cook_time, recipe.default_cook_time)
    quality += time_bonus

    # 完全匹配时品质最低为2
    if ingredients_exact:
        quality = max(quality, 2)

    return max(1, min(6, quality))


def _get_cook_time_quality_bonus(actual, default_time):
    """烹饪时间品质加成（移植自 Unity GetCookTimeQualityBonus）。"""
    diff = abs(actual - default_time)
    if diff == 0:
        return 2
    if diff <= 6:
        return 1
    if diff <= 35:
        return 0
    return -1


def _apply_pity(user, recipe_id, quality, cook_time, default_cook_time):
    """
    应用保底机制（复用 pity_check 的逻辑）。
    返回 (final_quality, final_cook_time, pity_counts)。
    """
    config = PityConfig.get_config()
    counter = PityCounter.get_or_create_for(user)

    counter.count_4 += 1
    counter.count_5 += 1
    counter.count_6 += 1

    final_quality = quality
    final_cook_time = cook_time

    pity_counts = {"count4": counter.count_4, "count5": counter.count_5, "count6": counter.count_6}

    # 绝品
    triggered, final_quality, final_cook_time = _check_tier(
        counter.count_6, config.quality_6_normal_phase, config.quality_6_guarantee_at,
        config.quality_6_ramp_probabilities, config.quality_6_time_range,
        default_cook_time, final_quality, final_cook_time, target_quality=6,
    )
    if triggered:
        counter.count_4 = counter.count_5 = counter.count_6 = 0
        counter.save()
        return final_quality, final_cook_time, _pity_counts(counter)

    # 极品
    triggered, final_quality, final_cook_time = _check_tier(
        counter.count_5, config.quality_5_normal_phase, config.quality_5_guarantee_at,
        config.quality_5_ramp_probabilities, config.quality_5_time_range,
        default_cook_time, final_quality, final_cook_time, target_quality=5,
    )
    if triggered:
        counter.count_4 = counter.count_5 = 0
        counter.save()
        return final_quality, final_cook_time, _pity_counts(counter)

    # 优秀
    triggered, final_quality, final_cook_time = _check_tier(
        counter.count_4, config.quality_4_normal_phase, config.quality_4_guarantee_at,
        config.quality_4_ramp_probabilities, config.quality_4_time_range,
        default_cook_time, final_quality, final_cook_time, target_quality=4,
    )
    if triggered:
        counter.count_4 = 0
        counter.save()
        return final_quality, final_cook_time, _pity_counts(counter)

    # 未触发保底，重置对应计数器
    if final_quality >= 6:
        counter.count_4 = counter.count_5 = counter.count_6 = 0
    elif final_quality >= 5:
        counter.count_4 = counter.count_5 = 0
    elif final_quality >= 4:
        counter.count_4 = 0

    counter.save()
    return final_quality, final_cook_time, _pity_counts(counter)


def _pity_counts(counter):
    return {"count4": counter.count_4, "count5": counter.count_5, "count6": counter.count_6}


def _check_tier(
    count,
    normal_phase,
    guarantee_at,
    ramp_probabilities,
    time_range,
    default_cook_time,
    current_quality,
    current_cook_time,
    target_quality,
):
    """
    检查某个品质层级的保底是否触发。
    返回 (是否触发, 最终品质, 最终烹饪时间)。
    """
    if current_quality >= target_quality:
        # 已经达到该品质，不需要保底
        return False, current_quality, current_cook_time

    if count < normal_phase:
        # 还在正常概率阶段
        return False, current_quality, current_cook_time

    # 计算在概率提升阶段的位置
    ramp_index = count - normal_phase  # 0-based
    total_ramp_steps = guarantee_at - normal_phase

    if ramp_index >= len(ramp_probabilities):
        # 超出概率数组范围，强制触发
        return True, target_quality, _reroll_cook_time(default_cook_time, time_range)

    # 按概率触发
    pity_prob = ramp_probabilities[ramp_index]
    roll = random.randint(1, 100)
    if roll <= pity_prob:
        return True, target_quality, _reroll_cook_time(default_cook_time, time_range)

    return False, current_quality, current_cook_time


def _reroll_cook_time(default_cook_time, time_range):
    """保底触发时，重新生成烹饪时间到合理区间。"""
    if time_range == 0:
        return default_cook_time
    offset = random.randint(-time_range, time_range)
    return max(0, default_cook_time + offset)


def _build_response(final_quality, final_cook_time, counter):
    return Response({
        "success": True,
        "data": {
            "finalQuality": final_quality,
            "cookTime": final_cook_time,
            "pityCount4": counter.count_4,
            "pityCount5": counter.count_5,
            "pityCount6": counter.count_6,
        },
    })
