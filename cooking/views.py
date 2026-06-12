import random
from django.db import transaction
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view
from rest_framework.response import Response

from config.auth import get_player_id
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
        # 锁定计数器行，防止并发问题
        counter = PityCounter.objects.select_for_update().get(pk=counter.pk)

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
