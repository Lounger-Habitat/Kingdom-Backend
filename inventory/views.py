import uuid
import random
from django.db import transaction
from django.db.models import Max
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view
from rest_framework.response import Response
from .models import (
    InventoryBag, InventorySlot, BagTemplate, BagTemplateSlot,
    CollectionPoint, CollectionPointLevel, CollectionPointLevelItem,
    CollectionPointUpgradeItemCost, PlayerCollectionPoint, PlayerCollectionOverride,
    AiConfig,
)
from .serializers import (
    success_response, dual_bag_response, error_response, serialize_bag,
    InventoryResponseSerializer, DualBagResponseSerializer, ErrorResponseSerializer,
)
from items.models import Item, ItemType


def get_or_create_bag(player, character_id):
    bag, _ = InventoryBag.objects.get_or_create(player=player, character_id=character_id)
    return bag


def find_slot_by_instance(bag, instance_id):
    return bag.slots.filter(instance_id=instance_id, item_amount__gt=0).first()


def find_slot_by_item(bag, item_id):
    return bag.slots.filter(item_id=item_id, item_amount__gt=0).first()


def find_empty_slot(bag):
    """Find first empty slot (item_amount=0) or create a new one."""
    empty = bag.slots.filter(item_amount=0).first()
    if empty:
        return empty
    max_index = bag.slots.aggregate(Max("slot_index"))["slot_index__max"]
    next_index = (max_index + 1) if max_index is not None else 0
    return InventorySlot.objects.create(bag=bag, slot_index=next_index)


def clear_slot_if_empty(slot):
    if slot.item_amount <= 0:
        slot.item_id = 0
        slot.item_amount = 0
        slot.instance_id = uuid.uuid4()
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


@extend_schema(responses={200: InventoryResponseSerializer})
@api_view(["GET"])
def inventory_detail(request, character_id):
    bag = get_or_create_bag(request.user, character_id)
    return Response(success_response(bag))


@extend_schema(responses={200: InventoryResponseSerializer, 400: ErrorResponseSerializer})
@api_view(["POST"])
def inventory_add(request, character_id):
    bag = get_or_create_bag(request.user, character_id)
    item_id = request.data.get("itemId")
    amount = request.data.get("amount", 1)
    ingredient = request.data.get("ingredient", "")
    cook_time = request.data.get("cookTime", 0)
    dish_quality = request.data.get("dishQuality", 3)
    instance_id = request.data.get("instanceId", "")

    if item_id is None:
        return Response(error_response("缺少 itemId"), status=400)

    # Check if item is food or usable
    is_food = False
    is_usable = False
    item_name = ""
    max_use = 1
    try:
        item = Item.objects.get(item_id=item_id)
        is_food = (item.item_type == "食物")
        is_usable = (item.item_type == ItemType.USABLE)
        item_name = item.item_name
        max_use = item.max_use_count
    except Item.DoesNotExist:
        pass

    # NPC背包中的物品默认不在商店显示（NPC商店只卖预设物品）
    is_npc_bag = (character_id != "player")

    if is_food and instance_id:
        # Food: always create a new slot (each dish is unique)
        slot = find_empty_slot(bag)
        slot.item_id = item_id
        slot.item_amount = amount
        slot.ingredient = ingredient
        slot.cook_time = cook_time
        slot.dish_quality = dish_quality
        slot.item_bag_name = f"{item_name}_{slot.slot_index}"
        if is_npc_bag:
            slot.hide_in_shop = True
        slot.save()
    else:
        # Non-food: stack on existing slot
        existing = bag.slots.filter(item_id=item_id, item_amount__gt=0).first()
        if existing:
            existing.item_amount += amount
            if ingredient:
                existing.ingredient = ingredient
            if is_npc_bag:
                existing.hide_in_shop = True
            existing.save()
        else:
            slot = find_empty_slot(bag)
            slot.item_id = item_id
            slot.item_amount = amount
            slot.ingredient = ingredient
            slot.cook_time = cook_time
            slot.dish_quality = dish_quality
            slot.item_bag_name = f"{item_name}_{slot.slot_index}"
            if is_npc_bag:
                slot.hide_in_shop = True
            if is_usable:
                slot.remaining_uses = max_use
            slot.save()

    return Response(success_response(bag))


@extend_schema(responses={200: InventoryResponseSerializer, 400: ErrorResponseSerializer})
@api_view(["POST"])
def inventory_rate(request, character_id):
    bag = get_or_create_bag(request.user, character_id)
    instance_id = request.data.get("instanceId")
    rating_price = request.data.get("ratingPrice", 0)
    overall_score = request.data.get("overallScore", 0)
    review_comment = request.data.get("reviewComment", "")

    if not instance_id:
        return Response(error_response("缺少 instanceId"), status=400)

    slot = find_slot_by_instance(bag, instance_id)
    if not slot:
        return Response(error_response("物品未找到"), status=400)

    if slot.item_amount > 1:
        # Split: decrement original, create new rated slot
        slot.item_amount -= 1
        slot.save()
        new_slot = find_empty_slot(bag)
        new_slot.item_id = slot.item_id
        new_slot.item_amount = 1
        new_slot.rated = True
        new_slot.rating_price = rating_price
        new_slot.overall_score = overall_score
        new_slot.ingredient = slot.ingredient
        new_slot.item_bag_name = slot.item_bag_name
        new_slot.cook_time = slot.cook_time
        new_slot.dish_quality = slot.dish_quality
        new_slot.hide_in_shop = slot.hide_in_shop
        new_slot.review_comment = review_comment
        new_slot.save()
    else:
        # Update in place
        slot.rated = True
        slot.rating_price = rating_price
        slot.overall_score = overall_score
        slot.review_comment = review_comment
        slot.save()

    return Response(success_response(bag))


@extend_schema(responses={200: InventoryResponseSerializer, 400: ErrorResponseSerializer})
@api_view(["POST"])
def inventory_remove(request, character_id):
    bag = get_or_create_bag(request.user, character_id)
    instance_id = request.data.get("instanceId")
    item_id = request.data.get("itemId")
    amount = request.data.get("amount", 1)

    if instance_id:
        slot = find_slot_by_instance(bag, instance_id)
    elif item_id is not None:
        slot = find_slot_by_item(bag, item_id)
    else:
        return Response(error_response("需要 instanceId 或 itemId"), status=400)

    if not slot:
        return Response(error_response("背包中未找到该物品"), status=400)

    slot.item_amount = max(0, slot.item_amount - amount)
    slot.save()
    clear_slot_if_empty(slot)

    return Response(success_response(bag))


@extend_schema(responses={200: InventoryResponseSerializer, 400: ErrorResponseSerializer})
@api_view(["POST"])
def inventory_swap(request, character_id):
    bag = get_or_create_bag(request.user, character_id)
    from_index = request.data.get("fromIndex")
    to_index = request.data.get("toIndex")

    if from_index is None or to_index is None:
        return Response(error_response("需要 fromIndex 和 toIndex"), status=400)

    from_slot, _ = bag.slots.get_or_create(slot_index=from_index, defaults={"item_id": 0, "item_amount": 0})
    to_slot, _ = bag.slots.get_or_create(slot_index=to_index, defaults={"item_id": 0, "item_amount": 0})

    # Swap all fields
    fields = ["item_id", "item_amount", "rated", "rating_price", "ingredient", "item_bag_name", "cook_time", "dish_quality", "hide_in_shop", "remaining_uses", "review_comment"]
    from_vals = {f: getattr(from_slot, f) for f in fields}
    to_vals = {f: getattr(to_slot, f) for f in fields}

    for f in fields:
        setattr(from_slot, f, to_vals[f])
        setattr(to_slot, f, from_vals[f])

    # Swap instance_ids too
    from_id = from_slot.instance_id
    from_slot.instance_id = to_slot.instance_id
    to_slot.instance_id = from_id

    from_slot.save()
    to_slot.save()

    return Response(success_response(bag))


@extend_schema(responses={200: InventoryResponseSerializer, 400: ErrorResponseSerializer})
@api_view(["POST"])
def inventory_trade(request, character_id):
    bag = get_or_create_bag(request.user, character_id)
    instance_id = request.data.get("instanceId")
    amount = request.data.get("amount", 1)
    is_sell = request.data.get("isSell", False)

    if not instance_id:
        return Response(error_response("缺少 instanceId"), status=400)

    slot = find_slot_by_instance(bag, instance_id)
    if not slot:
        return Response(error_response("物品未找到"), status=400)

    # Get item price config
    item_price = 0
    sell_pct = 0.5
    try:
        item = Item.objects.get(item_id=slot.item_id)
        item_price = item.item_price
        sell_pct = item.sell_percentage
    except Item.DoesNotExist:
        pass

    if is_sell:
        # Sell: remove item, add money
        if slot.item_amount < amount:
            return Response(error_response("物品数量不足"), status=400)
        earnings = int(item_price * sell_pct * amount)
        slot.item_amount -= amount
        slot.save()
        clear_slot_if_empty(slot)
        bag.money += earnings
        bag._money_change_source = "npc_sell"
        bag.save()
    else:
        # Buy: deduct money, add item
        cost = item_price * amount
        if bag.money < cost:
            return Response(error_response("金币不足"), status=400)
        bag.money -= cost
        bag._money_change_source = "npc_buy"
        bag.save()

        existing = bag.slots.filter(item_id=slot.item_id, item_amount__gt=0).exclude(pk=slot.pk).first()
        if existing:
            existing.item_amount += amount
            existing.save()
        else:
            slot.item_amount += amount
            slot.save()

    return Response(success_response(bag))


@extend_schema(responses={200: DualBagResponseSerializer, 400: ErrorResponseSerializer})
@api_view(["POST"])
def inventory_transfer(request, character_id):
    bag = get_or_create_bag(request.user, character_id)
    instance_id = request.data.get("instanceId")
    amount = request.data.get("amount", 1)
    target_id = request.data.get("targetCharacterId")

    if not instance_id or not target_id:
        return Response(error_response("需要 instanceId 和 targetCharacterId"), status=400)

    source_slot = find_slot_by_instance(bag, instance_id)
    if not source_slot or source_slot.item_amount < amount:
        return Response(error_response("物品数量不足"), status=400)

    target_bag = get_or_create_bag(request.user, target_id)
    is_npc_target = (target_id != "player")

    # Find or create target slot
    target_slot = target_bag.slots.filter(item_id=source_slot.item_id, item_amount__gt=0).first()
    if target_slot:
        target_slot.item_amount += amount
        # 不改变已有槽位的 hide_in_shop，避免覆盖NPC预设物品的可见性
        target_slot.save()
    else:
        empty = find_empty_slot(target_bag)
        empty.item_id = source_slot.item_id
        empty.item_amount = amount
        empty.rated = source_slot.rated
        empty.rating_price = source_slot.rating_price
        empty.ingredient = source_slot.ingredient
        empty.item_bag_name = source_slot.item_bag_name
        empty.cook_time = source_slot.cook_time
        empty.dish_quality = source_slot.dish_quality
        empty.hide_in_shop = source_slot.hide_in_shop or is_npc_target
        empty.remaining_uses = source_slot.remaining_uses
        empty.review_comment = source_slot.review_comment
        empty.save()

    source_slot.item_amount -= amount
    clear_slot_if_empty(source_slot)

    return Response(dual_bag_response(bag, target_bag))


@extend_schema(responses={200: DualBagResponseSerializer, 400: ErrorResponseSerializer})
@api_view(["POST"])
def inventory_trade_with(request, character_id):
    bag = get_or_create_bag(request.user, character_id)
    other_id = request.data.get("otherCharacterId")
    give_items = request.data.get("giveItem") or []
    give_money = request.data.get("giveMoney") or 0
    get_items = request.data.get("getItem") or []
    get_money = request.data.get("getMoney") or 0

    if not other_id:
        return Response(error_response("缺少 otherCharacterId"), status=400)

    other_bag = get_or_create_bag(request.user, other_id)

    # Validate money
    if bag.money < give_money:
        return Response(error_response("金币不足"), status=400)
    if other_bag.money < get_money:
        return Response(error_response("对方金币不足"), status=400)

    # Validate give items
    for gi in give_items:
        item_id = gi.get("itemID")
        inst_id = gi.get("instanceID")
        amt = gi.get("amount", 1)
        if inst_id:
            slot = find_slot_by_instance(bag, inst_id)
        else:
            slot = bag.slots.filter(item_id=item_id, item_amount__gt=0).first()
        if not slot or slot.item_amount < amt:
            try:
                item_name = Item.objects.get(item_id=item_id).item_name
            except Item.DoesNotExist:
                item_name = str(item_id)
            return Response(error_response(f"物品 {item_name} 数量不足"), status=400)

    # Validate get items
    for gi in get_items:
        item_id = gi.get("itemID")
        amt = gi.get("amount", 1)
        slot = other_bag.slots.filter(item_id=item_id, item_amount__gt=0).first()
        if not slot or slot.item_amount < amt:
            try:
                item_name = Item.objects.get(item_id=item_id).item_name
            except Item.DoesNotExist:
                item_name = str(item_id)
            return Response(error_response(f"对方物品 {item_name} 数量不足"), status=400)

    # Execute transfers
    bag.money -= give_money
    other_bag.money += give_money
    other_bag.money -= get_money
    bag.money += get_money
    bag._money_change_source = "trade_with"
    other_bag._money_change_source = "trade_with"

    for gi in give_items:
        item_id = gi.get("itemID")
        inst_id = gi.get("instanceID")
        amt = gi.get("amount", 1)
        if inst_id:
            source_slot = find_slot_by_instance(bag, inst_id)
        else:
            source_slot = bag.slots.filter(item_id=item_id, item_amount__gt=0).first()

        # 所有物品写入NPC背包，但标记为不在商店显示（NPC商店只卖预设物品）
        target_slot = other_bag.slots.filter(item_id=item_id, item_amount__gt=0).first()
        if target_slot:
            target_slot.item_amount += amt
            # 不改变已有槽位的 hide_in_shop，避免覆盖NPC预设物品的可见性
            target_slot.save()
        else:
            empty = find_empty_slot(other_bag)
            empty.item_id = source_slot.item_id
            empty.item_amount = amt
            empty.rated = source_slot.rated
            empty.rating_price = source_slot.rating_price
            empty.ingredient = source_slot.ingredient
            empty.item_bag_name = source_slot.item_bag_name
            empty.cook_time = source_slot.cook_time
            empty.dish_quality = source_slot.dish_quality
            empty.hide_in_shop = True
            empty.remaining_uses = source_slot.remaining_uses
            empty.review_comment = source_slot.review_comment
            empty.save()

        source_slot.item_amount -= amt
        source_slot.save()
        clear_slot_if_empty(source_slot)

    for gi in get_items:
        item_id = gi.get("itemID")
        amt = gi.get("amount", 1)
        source_slot = other_bag.slots.filter(item_id=item_id, item_amount__gt=0).first()
        target_slot = bag.slots.filter(item_id=item_id, item_amount__gt=0).first()
        if target_slot:
            target_slot.item_amount += amt
            target_slot.save()
        else:
            empty = find_empty_slot(bag)
            empty.item_id = source_slot.item_id
            empty.item_amount = amt
            empty.rated = source_slot.rated
            empty.rating_price = source_slot.rating_price
            empty.ingredient = source_slot.ingredient
            empty.item_bag_name = source_slot.item_bag_name
            empty.cook_time = source_slot.cook_time
            empty.dish_quality = source_slot.dish_quality
            empty.hide_in_shop = source_slot.hide_in_shop
            empty.remaining_uses = source_slot.remaining_uses
            empty.review_comment = source_slot.review_comment
            empty.save()
        source_slot.item_amount -= amt
        source_slot.save()
        clear_slot_if_empty(source_slot)

    bag.save()
    other_bag.save()

    return Response(dual_bag_response(bag, other_bag))


@extend_schema(responses={200: InventoryResponseSerializer})
@api_view(["POST"])
def inventory_compact(request, character_id):
    """整理背包：删除空槽位，有物品的槽位重新从 0 开始连续编号。"""
    bag = get_or_create_bag(request.user, character_id)

    with transaction.atomic():
        # 收集所有有物品的槽位，按原 slot_index 排序保持相对顺序
        filled_slots = list(bag.slots.filter(item_amount__gt=0).order_by("slot_index"))
        # 删除所有槽位
        bag.slots.all().delete()
        # 用新编号重建
        for new_index, old_slot in enumerate(filled_slots):
            InventorySlot.objects.create(
                bag=bag,
                slot_index=new_index,
                instance_id=old_slot.instance_id,
                item_id=old_slot.item_id,
                item_amount=old_slot.item_amount,
                rated=old_slot.rated,
                rating_price=old_slot.rating_price,
                overall_score=old_slot.overall_score,
                ingredient=old_slot.ingredient,
                item_bag_name=old_slot.item_bag_name,
                cook_time=old_slot.cook_time,
                dish_quality=old_slot.dish_quality,
                hide_in_shop=old_slot.hide_in_shop,
                remaining_uses=old_slot.remaining_uses,
                review_comment=old_slot.review_comment,
            )

    return Response(success_response(bag))


@extend_schema(responses={200: InventoryResponseSerializer, 400: ErrorResponseSerializer})
@api_view(["POST"])
def inventory_money(request, character_id):
    bag = get_or_create_bag(request.user, character_id)
    amount = request.data.get("amount", 0)
    bag.money += amount
    if bag.money < 0:
        bag.money = 0
    bag._money_change_source = "direct_adjust"
    bag.save()
    return Response(success_response(bag))


@extend_schema(responses={200: InventoryResponseSerializer, 400: ErrorResponseSerializer})
@api_view(["POST"])
def inventory_use(request, character_id):
    """使用背包中的可使用物品。"""
    bag = get_or_create_bag(request.user, character_id)
    instance_id = request.data.get("instanceId")

    if not instance_id:
        return Response(error_response("缺少 instanceId"), status=400)

    slot = find_slot_by_instance(bag, instance_id)
    if not slot:
        return Response(error_response("物品不存在或不属于该背包"), status=400)

    try:
        item = Item.objects.get(item_id=slot.item_id)
    except Item.DoesNotExist:
        return Response(error_response("物品定义不存在"), status=400)

    if item.item_type != ItemType.USABLE:
        return Response(error_response("该物品不可使用"), status=400)

    if slot.remaining_uses is not None and slot.remaining_uses == 0:
        return Response(error_response("使用次数已用尽"), status=400)

    effect_type = item.use_effect_type
    effect_data = item.use_effect_data or {}

    if effect_type == "unlock_recipe":
        recipe_name = effect_data.get("recipeName", "")
        if not recipe_name:
            return Response(error_response("物品配置错误：缺少 recipeName"), status=400)

        from player.models import PlayerState, PlayerRecipe
        from recipes.models import Recipe as CatalogRecipe

        player_state, _ = PlayerState.objects.get_or_create(player_id=request.user.pk)

        existing_recipe = PlayerRecipe.objects.filter(player=player_state, recipe_name=recipe_name).first()
        if existing_recipe:
            if existing_recipe.show_ingredient_count:
                return Response({
                    "success": True,
                    "message": f"菜谱「{recipe_name}」已完全解锁，无需重复使用",
                    "data": serialize_bag(bag),
                })
            else:
                # 已解锁但未显示数量，使用菜谱解锁数量显示
                existing_recipe.show_ingredient_count = True
                existing_recipe.save(update_fields=["show_ingredient_count"])
                # 消耗物品
                if slot.remaining_uses is not None and slot.remaining_uses != -1:
                    slot.remaining_uses -= 1
                    if slot.remaining_uses <= 0:
                        slot.delete()
                        return Response({
                            "success": True,
                            "message": f"菜谱「{recipe_name}」数量显示已解锁",
                            "data": serialize_bag(bag),
                        })
                    else:
                        slot.save()
                return Response({
                    "success": True,
                    "message": f"菜谱「{recipe_name}」数量显示已解锁",
                    "data": serialize_bag(bag),
                })

        catalog = CatalogRecipe.objects.filter(recipe_name=recipe_name).first()
        if not catalog:
            return Response(error_response(f"菜谱「{recipe_name}」不存在"), status=400)

        with transaction.atomic():
            PlayerRecipe.objects.create(
                player=player_state,
                recipe_name=recipe_name,
                catalog_recipe=catalog,
                show_ingredient_count=True,
            )

            # 消耗（None / -1 均视为无限次，不消耗）
            if slot.remaining_uses is not None and slot.remaining_uses != -1:
                slot.remaining_uses -= 1
                if slot.remaining_uses <= 0:
                    slot.delete()
                    return Response({
                        "success": True,
                        "message": f"菜谱「{recipe_name}」已解锁",
                        "data": serialize_bag(bag),
                    })
                else:
                    slot.save()

            return Response({
                "success": True,
                "message": f"菜谱「{recipe_name}」已解锁",
                "data": serialize_bag(bag),
            })

    if effect_type == "random_unmastered_recipe":
        from player.models import PlayerState, PlayerRecipe
        import random as _random

        player_state, _ = PlayerState.objects.get_or_create(player_id=request.user.pk)
        all_recipe_ids = list(range(3001, 3031))

        # 获取已掌握的菜谱物品ID
        mastered_names = set(
            PlayerRecipe.objects.filter(player=player_state)
            .values_list("recipe_name", flat=True)
        )
        # 将菜谱名映射到物品ID（通过 use_effect_data 中的 recipeName 匹配）
        mastered_item_ids = set(
            Item.objects.filter(use_effect_data__recipeName__in=mastered_names, item_id__in=all_recipe_ids)
            .values_list("item_id", flat=True)
        )

        unmastered_ids = [rid for rid in all_recipe_ids if rid not in mastered_item_ids]

        if not unmastered_ids:
            return Response(error_response("已掌握所有菜谱，无法使用"), status=400)

        item_id = _random.choice(unmastered_ids)

        recipe_item = Item.objects.filter(item_id=item_id).first()
        recipe_max_use = recipe_item.max_use_count if recipe_item else 1

        existing = bag.slots.filter(item_id=item_id, item_amount__gt=0).first()
        if existing:
            existing.item_amount += 1
            if existing.remaining_uses is None:
                existing.remaining_uses = recipe_max_use
            existing.save()
        else:
            target_slot = find_empty_slot(bag)
            target_slot.item_id = item_id
            target_slot.item_amount = 1
            target_slot.remaining_uses = recipe_max_use
            recipe_name_for_bag = recipe_item.item_name if recipe_item else f"菜谱#{item_id}"
            target_slot.item_bag_name = f"{recipe_name_for_bag}_{target_slot.slot_index}"
            target_slot.save()
        recipe_name = recipe_item.item_name if recipe_item else f"菜谱#{item_id}"

        # Consume the box
        if slot.remaining_uses is not None and slot.remaining_uses != -1:
            slot.remaining_uses -= 1
            if slot.remaining_uses <= 0:
                if slot.item_amount > 1:
                    slot.item_amount -= 1
                    slot.remaining_uses = item.max_use_count
                    slot.save()
                else:
                    slot.delete()
                return Response({
                    "success": True,
                    "message": f"获得了「{recipe_name}」",
                    "data": serialize_bag(bag),
                })
            else:
                slot.save()

        return Response({
            "success": True,
            "message": f"获得了「{recipe_name}」",
            "data": serialize_bag(bag),
        })

    if effect_type == "random_recipe":
        import random as _random
        recipe_ids = list(range(3001, 3031))
        item_id = _random.choice(recipe_ids)

        recipe_item = Item.objects.filter(item_id=item_id).first()
        recipe_max_use = recipe_item.max_use_count if recipe_item else 1

        existing = bag.slots.filter(item_id=item_id, item_amount__gt=0).first()
        if existing:
            existing.item_amount += 1
            if existing.remaining_uses is None:
                existing.remaining_uses = recipe_max_use
            existing.save()
        else:
            target_slot = find_empty_slot(bag)
            target_slot.item_id = item_id
            target_slot.item_amount = 1
            target_slot.remaining_uses = recipe_max_use
            recipe_name_for_bag = recipe_item.item_name if recipe_item else f"菜谱#{item_id}"
            target_slot.item_bag_name = f"{recipe_name_for_bag}_{target_slot.slot_index}"
            target_slot.save()

        recipe_name = recipe_item.item_name if recipe_item else f"菜谱#{item_id}"

        # Consume the box (slot is the box slot)
        if slot.remaining_uses is not None and slot.remaining_uses != -1:
            slot.remaining_uses -= 1
            if slot.remaining_uses <= 0:
                if slot.item_amount > 1:
                    slot.item_amount -= 1
                    slot.remaining_uses = item.max_use_count
                    slot.save()
                else:
                    slot.delete()
                return Response({
                    "success": True,
                    "message": f"获得了「{recipe_name}」",
                    "data": serialize_bag(bag),
                })
            else:
                slot.save()

        return Response({
            "success": True,
            "message": f"获得了「{recipe_name}」",
            "data": serialize_bag(bag),
        })

    return Response(error_response(f"未知的使用效果类型: {effect_type}"), status=400)


@api_view(["POST"])
def inventory_bulk_push(request):
    """批量上传多个背包数据（玩家背包 + NPC 背包）。

    请求格式：
    {
        "bags": [
            {
                "characterId": "player",
                "money": 100,
                "items": [
                    {"instanceID": "uuid", "itemID": 1, "itemAmount": 5,
                     "rated": false, "ratingPrice": -1, "ingredient": "", "itemBagName": ""}
                ]
            },
            ...
        ]
    }
    """
    bags_data = request.data.get("bags")
    if not isinstance(bags_data, list):
        return Response(error_response("bags 字段必须是数组"), status=400)

    player = request.user
    results = []

    with transaction.atomic():
        for bag_data in bags_data:
            character_id = bag_data.get("characterId")
            if not character_id:
                results.append({"characterId": None, "success": False, "message": "缺少 characterId"})
                continue

            if character_id == "player":
                # ---- 玩家自己的背包：写入 BagTemplate（全局模板） ----
                tmpl, _ = BagTemplate.objects.get_or_create(character_id=character_id)
                tmpl.money = bag_data.get("money", tmpl.money)
                tmpl.save()

                tmpl.slots.all().delete()
                for idx, item in enumerate(bag_data.get("items", [])):
                    item_id = item.get("itemID", 0)
                    item_obj = Item.objects.filter(item_id=item_id).first()
                    BagTemplateSlot.objects.create(
                        template=tmpl,
                        slot_index=idx,
                        item=item_obj,
                        item_amount=item.get("itemAmount", 0),
                        rated=item.get("rated", False),
                        rating_price=item.get("ratingPrice", -1),
                        ingredient=item.get("ingredient", ""),
                        item_bag_name=item.get("itemBagName", ""),
                        cook_time=item.get("cookTime", 0),
                        dish_quality=item.get("dishQuality", 3),
                        hide_in_shop=item.get("hideInShop", False),
                    )
                results.append({"characterId": character_id, "success": True, "message": "模板同步成功"})
            else:
                # ---- NPC 背包：写入当前玩家的 InventoryBag ----
                bag = get_or_create_bag(player, character_id)
                bag.money = bag_data.get("money", bag.money)
                bag._money_change_source = "npc_sync"
                bag.save()

                bag.slots.all().delete()
                for idx, item in enumerate(bag_data.get("items", [])):
                    instance_id = item.get("instanceID")
                    if instance_id:
                        try:
                            instance_id = uuid.UUID(instance_id)
                        except (ValueError, AttributeError):
                            instance_id = uuid.uuid4()
                    else:
                        instance_id = uuid.uuid4()

                    # NPC背包中的物品默认不在商店显示（NPC商店只卖预设物品）
                    hide = item.get("hideInShop", False) or True

                    InventorySlot.objects.create(
                        bag=bag,
                        slot_index=idx,
                        instance_id=instance_id,
                        item_id=item.get("itemID", 0),
                        item_amount=item.get("itemAmount", 0),
                        rated=item.get("rated", False),
                        rating_price=item.get("ratingPrice", -1),
                        ingredient=item.get("ingredient", ""),
                        item_bag_name=item.get("itemBagName", ""),
                        cook_time=item.get("cookTime", 0),
                        dish_quality=item.get("dishQuality", 3),
                        hide_in_shop=hide,
                        remaining_uses=item.get("remainingUses"),
                    )
                results.append({"characterId": character_id, "success": True, "message": "同步成功"})

    return Response({"success": True, "message": "批量同步完成", "data": results})


# ──────────────────────────────────────────────────────────────
#  采集系统（基于采集点等级配置）
# ──────────────────────────────────────────────────────────────

def _build_collect_pool(player, collection_point):
    """根据玩家等级 + 覆盖配置，构建最终采集物品池。返回 (pool_items, pool_weights, error_response)。"""
    # 获取或创建玩家采集点等级
    player_cp, _ = PlayerCollectionPoint.objects.get_or_create(
        player=player, collection_point=collection_point,
        defaults={"current_level": collection_point.default_level},
    )

    # 查询当前等级的物品池
    level_items = list(
        CollectionPointLevelItem.objects.filter(
            level__collection_point=collection_point,
            level__level=player_cp.current_level,
        ).select_related("item")
    )
    if not level_items:
        return [], [], Response({
            "code": "NO_ITEMS",
            "message": f"等级 {player_cp.current_level} 没有可采集的物品",
        }, status=200)

    # 查询玩家覆盖
    overrides = {
        o.item_id: o
        for o in PlayerCollectionOverride.objects.filter(
            player_collection_point=player_cp,
        )
    }

    # 应用覆盖，构建最终池
    pool_items = []
    pool_weights = []
    for li in level_items:
        override = overrides.get(li.item_id)
        if override and override.is_excluded:
            continue
        weight = li.collect_weight
        min_amt = li.collect_min_amount
        max_amt = li.collect_max_amount
        if override:
            if override.override_weight is not None:
                weight = override.override_weight
            if override.override_min_amount is not None:
                min_amt = override.override_min_amount
            if override.override_max_amount is not None:
                max_amt = override.override_max_amount
        pool_items.append({"item": li.item, "min_amount": min_amt, "max_amount": max_amt})
        pool_weights.append(weight)

    if not pool_items:
        return [], [], Response({
            "code": "NO_ITEMS",
            "message": "采集池为空（所有物品可能被排除）",
        }, status=200)

    return pool_items, pool_weights, None


@api_view(["POST"])
def inventory_collect(request, character_id):
    """采集接口：扣行动力 + 随机生成物品 + 写入背包，一步完成。"""
    from player.models import PlayerState

    collect_type = request.data.get("collectType", "")
    try:
        collection_point = CollectionPoint.objects.get(collect_type=collect_type)
    except CollectionPoint.DoesNotExist:
        return Response({
            "code": "INVALID_COLLECT_TYPE",
            "message": f"未知的采集类型: {collect_type}",
        }, status=400)

    # 校验行动力
    player_id = request.user.pk
    player, _ = PlayerState.objects.get_or_create(player_id=player_id)
    if player.action_points < collection_point.action_cost:
        return Response({
            "code": "INSUFFICIENT_ACTION_POINTS",
            "message": "行动力不足",
        }, status=200)

    # 构建采集池
    pool_items, pool_weights, err = _build_collect_pool(request.user, collection_point)
    if err:
        return err

    # 加权随机选取（不放回）
    count = min(collection_point.collect_count, len(pool_items))
    selected = []
    remaining_pool = list(pool_items)
    remaining_weights = list(pool_weights)
    for _ in range(count):
        pick = random.choices(remaining_pool, weights=remaining_weights, k=1)[0]
        selected.append(pick)
        idx = remaining_pool.index(pick)
        remaining_pool.pop(idx)
        remaining_weights.pop(idx)

    # 生成采集结果
    collected_items = []
    for entry in selected:
        item = entry["item"]
        amount = random.randint(entry["min_amount"], entry["max_amount"])
        collected_items.append({"itemId": item.item_id, "itemName": item.item_name, "amount": amount})

    # 事务：扣行动力 + 写入背包
    bag = get_or_create_bag(request.user, character_id)
    with transaction.atomic():
        player.action_points -= collection_point.action_cost
        player.save()

        for ci in collected_items:
            existing = bag.slots.filter(item_id=ci["itemId"], item_amount__gt=0).first()
            if existing:
                existing.item_amount += ci["amount"]
                existing.save()
            else:
                slot = find_empty_slot(bag)
                slot.item_id = ci["itemId"]
                slot.item_amount = ci["amount"]
                slot.item_bag_name = f"{ci['itemName']}_{slot.slot_index}"
                slot.save()

    return Response({
        "code": 0,
        "data": {
            "collectedItems": collected_items,
            "bagData": serialize_bag(bag),
            "action_points": player.action_points,
            "action_points_max": player.action_points_max,
        },
    })


@api_view(["POST"])
def inventory_collect_upgrade(request, character_id):
    """采集点升级接口：消耗资源，提升采集点等级。"""
    from player.models import PlayerState

    collect_type = request.data.get("collectType", "")
    try:
        collection_point = CollectionPoint.objects.get(collect_type=collect_type)
    except CollectionPoint.DoesNotExist:
        return Response({
            "code": "INVALID_COLLECT_TYPE",
            "message": f"未知的采集类型: {collect_type}",
        }, status=400)

    # 获取玩家采集点
    player_cp, _ = PlayerCollectionPoint.objects.get_or_create(
        player=request.user, collection_point=collection_point,
        defaults={"current_level": collection_point.default_level},
    )

    # 检查下一级是否存在
    next_level = player_cp.current_level + 1
    try:
        current_level_config = CollectionPointLevel.objects.get(
            collection_point=collection_point, level=player_cp.current_level,
        )
    except CollectionPointLevel.DoesNotExist:
        return Response({
            "code": "MAX_LEVEL_REACHED",
            "message": "当前等级配置不存在",
        }, status=200)

    next_level_exists = CollectionPointLevel.objects.filter(
        collection_point=collection_point, level=next_level,
    ).exists()
    if not next_level_exists:
        return Response({
            "code": "MAX_LEVEL_REACHED",
            "message": "已达到最高等级",
        }, status=200)

    # 获取升级消耗
    cost_money = current_level_config.upgrade_cost_money
    item_costs = list(
        CollectionPointUpgradeItemCost.objects.filter(level=current_level_config)
        .select_related("item")
    )

    bag = get_or_create_bag(request.user, character_id)

    # 检查金币
    if bag.money < cost_money:
        return Response({
            "code": "INSUFFICIENT_RESOURCES",
            "message": f"金币不足，需要 {cost_money}，当前 {bag.money}",
        }, status=200)

    # 检查物品
    missing = []
    for ic in item_costs:
        total = sum(
            s.item_amount
            for s in bag.slots.filter(item_id=ic.item.item_id, item_amount__gt=0)
        )
        if total < ic.amount:
            missing.append(f"{ic.item.item_name}(需要{ic.amount}，拥有{total})")
    if missing:
        return Response({
            "code": "INSUFFICIENT_RESOURCES",
            "message": f"物品不足: {', '.join(missing)}",
        }, status=200)

    # 事务：扣除资源 + 升级
    with transaction.atomic():
        # 扣金币
        bag.money -= cost_money
        bag._money_change_source = "upgrade"
        bag.save()

        # 扣物品
        for ic in item_costs:
            remaining = ic.amount
            for slot in bag.slots.filter(item_id=ic.item.item_id, item_amount__gt=0):
                if remaining <= 0:
                    break
                deduct = min(slot.item_amount, remaining)
                slot.item_amount -= deduct
                slot.save()
                remaining -= deduct
                if slot.item_amount <= 0:
                    clear_slot_if_empty(slot)

        # 升级
        player_cp.current_level = next_level
        player_cp.save()

    return Response({
        "code": 0,
        "data": {
            "newLevel": next_level,
            "bagData": serialize_bag(bag),
        },
    })


def _get_level_benefits(collection_point, level):
    """获取采集点某等级的增益数据（采集数量范围）。"""
    from .models import CollectionPointLevelItem, CollectionPointLevel
    try:
        lv = CollectionPointLevel.objects.get(collection_point=collection_point, level=level)
    except CollectionPointLevel.DoesNotExist:
        return None
    first_item = CollectionPointLevelItem.objects.filter(level=lv).first()
    if not first_item:
        return None
    return {
        "collectMin": first_item.collect_min_amount,
        "collectMax": first_item.collect_max_amount,
    }


@api_view(["GET"])
def inventory_collect_upgrade_info(request, character_id):
    """查询采集点升级消耗，供客户端展示确认框。"""
    collect_type = request.query_params.get("collectType", "")
    try:
        collection_point = CollectionPoint.objects.get(collect_type=collect_type)
    except CollectionPoint.DoesNotExist:
        return Response({
            "code": "INVALID_COLLECT_TYPE",
            "message": f"未知的采集类型: {collect_type}",
        }, status=400)

    # 获取玩家采集点
    player_cp, _ = PlayerCollectionPoint.objects.get_or_create(
        player=request.user, collection_point=collection_point,
        defaults={"current_level": collection_point.default_level},
    )

    # 检查下一级是否存在
    next_level = player_cp.current_level + 1
    has_next = CollectionPointLevel.objects.filter(
        collection_point=collection_point, level=next_level,
    ).exists()

    if not has_next:
        # 满级：仍返回当前等级增益
        current_benefits = _get_level_benefits(collection_point, player_cp.current_level)
        return Response({
            "code": 0,
            "data": {
                "collectType": collect_type,
                "currentLevel": player_cp.current_level,
                "nextLevel": None,
                "upgradeCost": None,
                "canUpgrade": False,
                "currentBenefits": current_benefits,
                "nextBenefits": None,
            },
        })

    # 当前等级配置
    try:
        current_level_config = CollectionPointLevel.objects.get(
            collection_point=collection_point, level=player_cp.current_level,
        )
    except CollectionPointLevel.DoesNotExist:
        return Response({
            "code": 0,
            "data": {
                "collectType": collect_type,
                "currentLevel": player_cp.current_level,
                "nextLevel": next_level,
                "upgradeCost": None,
                "canUpgrade": False,
                "currentBenefits": None,
                "nextBenefits": None,
            },
        })

    # 升级消耗
    cost_money = current_level_config.upgrade_cost_money
    item_costs = list(
        CollectionPointUpgradeItemCost.objects.filter(level=current_level_config)
        .select_related("item")
    )

    # 玩家背包
    bag = get_or_create_bag(request.user, character_id)

    # 计算每种物品的拥有量
    cost_items = []
    can_upgrade = bag.money >= cost_money
    for ic in item_costs:
        owned = sum(
            s.item_amount
            for s in bag.slots.filter(item_id=ic.item.item_id, item_amount__gt=0)
        )
        if owned < ic.amount:
            can_upgrade = False
        cost_items.append({
            "itemId": ic.item.item_id,
            "itemName": ic.item.item_name,
            "amount": ic.amount,
            "owned": owned,
        })

    current_benefits = _get_level_benefits(collection_point, player_cp.current_level)
    next_benefits = _get_level_benefits(collection_point, next_level)

    return Response({
        "code": 0,
        "data": {
            "collectType": collect_type,
            "currentLevel": player_cp.current_level,
            "nextLevel": next_level,
            "upgradeCost": {
                "money": cost_money,
                "moneyOwned": bag.money,
                "items": cost_items,
            },
            "canUpgrade": can_upgrade,
            "currentBenefits": current_benefits,
            "nextBenefits": next_benefits,
        },
    })


# ──────────────────────────────────────────────────────────────
#  AI 点评系统
# ──────────────────────────────────────────────────────────────

import json
import random
import httpx
from django.conf import settings


def _calc_quality_price(base_price, dish_quality):
    """根据品质计算售价（与Unity端QualityPriceHelper逻辑一致）。"""
    coeffs = {1: 0.3, 2: 0.7, 3: 1.0, 4: 1.5, 5: 2.0, 6: 3.0}
    coeff = coeffs.get(dish_quality, 3.0)
    return round(base_price * coeff)


def _calc_rating_price(quality_price, dish_quality):
    """根据品质和基础价格，按概率分布计算点评价值。

    品质越高，波动范围越大；低品质也有几率提升价值。
    返回整数（金币）。
    """
    # 每个品质对应的：(低价范围下限%, 低价范围上限%, 高价范围下限%, 高价范围上限%, 低价概率)
    ranges = {
        6: (0.30, 1.00, 1.00, 3.00, 0.45),
        5: (0.40, 1.00, 1.00, 2.00, 0.45),
        4: (0.50, 1.00, 1.00, 1.80, 0.45),
        3: (0.60, 1.00, 1.00, 1.60, 0.50),
        2: (0.70, 1.00, 1.00, 1.40, 0.50),
        1: (0.80, 1.00, 1.00, 1.20, 0.50),
    }
    lo_min, lo_max, hi_min, hi_max, lo_prob = ranges.get(dish_quality, (0.60, 1.00, 1.00, 1.60, 0.50))

    if random.random() < lo_prob:
        pct = random.uniform(lo_min, lo_max)
    else:
        pct = random.uniform(hi_min, hi_max)

    return max(1, round(quality_price * pct))


def _calc_overall_score(scores, difficulty):
    """计算总分（与Unity端逻辑一致）。"""
    color = scores.get("color", 0)
    aroma = scores.get("aroma", 0)
    taste = scores.get("taste", 0)
    presentation = scores.get("presentation", 0)

    base_bonus = (difficulty - 1) * 5
    weighted = color * 0.2 + aroma * 0.2 + taste * 0.4 + presentation * 0.2
    jitter = random.uniform(-3, 3)
    raw = base_bonus + weighted / 10 * (100 - base_bonus) + jitter
    return round(max(0, min(100, raw)) * 10) / 10


def _build_judge_prompt(item_detail, slot, recipe):
    """构建AI点评提示词。"""
    base_price = round(item_detail.item_price * item_detail.sell_percentage)
    quality_price = _calc_quality_price(base_price, slot.dish_quality)

    cuisine = recipe.cuisine if recipe else "未分类"
    dish_category = recipe.dish_category if recipe else "主菜"
    serving_style = recipe.serving_style if recipe else "大盘"
    taste_profile = recipe.taste_profile if recipe else ""
    difficulty = recipe.difficulty if recipe else 2
    default_cook_time = recipe.default_cook_time if recipe else 0
    cook_time = slot.cook_time if slot.cook_time > 0 else default_cook_time
    dish_quality = slot.dish_quality if slot.dish_quality > 0 else 3

    back_json = {
        "data": {
            "comment": "色泽红亮入口即化，火候到位，值这个价。",
            "scores": {"color": 8.3, "aroma": 7.6, "taste": 9.1, "presentation": 7.4},
            "customerFeedback": "炖得够烂，再来一碗！"
        }
    }

    system_prompt = (
        "你是「老陈」，一位来自江浙沪的资深美食点评家，偏好甜口，对甜度较高、用料扎实的菜品会给出更高评价，"
        "对过于油腻或调味粗糙的菜品会略显苛刻。评价风格风趣幽默，偶尔带点个人小故事。\n\n"
        "任务：根据提供的菜品信息，撰写点评和评分。\n\n"
        "品质等级参考（重要）：1略逊（做工粗糙，明显失误，分项1-3）2一般（中规中矩，无功无过，分项3-5）"
        "3正常（合格水准，基本到位，分项5-7）4优秀（用心制作，超出预期，分项7-8.5）"
        "5极品（大师水准，令人赞叹，分项8.5-9.5）6绝品（百年难遇，传世之作，分项9.5-10）。"
        "品质等级是scores评分的核心依据，分项评分应落在对应等级范围内，允许相邻等级适当重叠但不得跨级。\n\n"
        "comment 字段要求（15-25字）：一句话点评，突出最突出的优点或缺点，风格风趣。\n\n"
        "scores 分项评分要求（各项1-10分，保留一位小数）：color（色泽）、aroma（香气）、taste（味道）、presentation（摆盘）。"
        "品质等级和烹饪难度是评分的核心参考。实际烹饪时长与标准烹饪时长的偏差也应纳入评分："
        "越接近标准时间说明火候掌控越好，可加分；偏差过大（过短可能没熟，过长可能糊了）应扣分。\n\n"
        "customerFeedback 字段要求（10-15字）：模拟食客一句话感叹，接地气。\n\n"
        "输出规范：直接输出JSON对象，不要使用markdown代码块，不要添加任何前缀或后缀文字。格式：" +
        json.dumps(back_json, ensure_ascii=False)
    )

    user_prompt = (
        f"请点评以下菜品：\n"
        f"- 菜名：{item_detail.item_name}\n"
        f"- 菜系：{cuisine}\n"
        f"- 菜品分类：{dish_category}\n"
        f"- 口味标签：{taste_profile}\n"
        f"- 上菜形式：{serving_style}\n"
        f"- 烹饪难度：{difficulty}（1简单 2中等 3困难）\n"
        f"- 标准烹饪时长：{default_cook_time}秒\n"
        f"- 实际烹饪时长：{cook_time}秒\n"
        f"- 品质等级：{dish_quality}（1略逊 2一般 3正常 4优秀 5极品 6绝品）\n"
        f"- 原料及配比：{slot.ingredient}\n"
        f"- 菜品描述：{item_detail.item_description or ''}\n\n"
        f"请根据以上信息给出你的专业点评和评分。品质等级应作为评分的重要参考依据。"
    )

    return system_prompt, user_prompt, quality_price


@api_view(["POST"])
def inventory_judge(request, character_id):
    """AI点评接口：接收instanceId，后端查询数据、调用AI、保存结果。"""
    from player.models import PlayerState
    from recipes.models import Recipe

    instance_id = request.data.get("instanceId")
    if not instance_id:
        return Response(error_response("缺少 instanceId"), status=400)

    bag = get_or_create_bag(request.user, character_id)
    slot = find_slot_by_instance(bag, instance_id)
    if not slot:
        return Response(error_response("物品未找到"), status=400)

    # 获取物品详情
    try:
        item_detail = Item.objects.get(item_id=slot.item_id)
    except Item.DoesNotExist:
        return Response(error_response("物品详情不存在"), status=400)

    # 验证是否食物
    if item_detail.item_type != ItemType.FOOD:
        return Response(error_response(f"{item_detail.item_name}不是食物，无法进行评价"), status=400)

    # 验证是否已点评
    if slot.rated:
        return Response(error_response(f"{item_detail.item_name}都已经评价过了，选择其他菜品吧！"), status=400)

    # 行动力检查
    player_id = request.user.pk
    player, _ = PlayerState.objects.get_or_create(player_id=player_id)
    judge_cost = 2  # 默认点评消耗
    if player.action_points < judge_cost:
        return Response(error_response("行动力不足，无法点评"), status=400)

    # 获取菜谱信息
    recipe = Recipe.objects.filter(item_id=slot.item_id).first()

    # 构建提示词
    system_prompt, user_prompt, quality_price = _build_judge_prompt(item_detail, slot, recipe)

    # 调用AI服务
    ai_url = settings.AI_SERVICE_URL
    ai_model = AiConfig.get_model()
    if not ai_url or not ai_model:
        return Response(error_response("AI服务未配置"), status=500)

    message_data = {
        "model": ai_model,
        "messages": [
            {"role": "system", "content": system_prompt, "tool_id": ""},
            {"role": "user", "content": user_prompt, "tool_id": ""}
        ],
        "temperature": 0.7,
        "max_tokens": 5000,
        "stream": False
    }

    try:
        headers = {}
        if settings.AI_API_KEY:
            headers["Authorization"] = f"Bearer {settings.AI_API_KEY}"

        with httpx.Client(timeout=settings.AI_REQUEST_TIMEOUT) as client:
            response = client.post(f"{ai_url}/menglong/chat", json=message_data, headers=headers)
            response.raise_for_status()
            res_data = response.json()
    except Exception as e:
        return Response(error_response(f"AI服务请求失败: {str(e)}"), status=500)

    # 解析AI返回
    try:
        content_text = res_data["output"]["content"]["text"]
        content = json.loads(content_text)
        data = content["data"]
        comment = data["comment"]
        scores = data["scores"]
        customer_feedback = data.get("customerFeedback", "")
    except Exception as e:
        return Response(error_response(f"AI返回数据解析失败: {str(e)}"), status=500)

    # 由后端按概率计算点评价值，不依赖AI
    rating_price = _calc_rating_price(quality_price, slot.dish_quality)

    # 计算总分
    difficulty = recipe.difficulty if recipe else 2
    overall_score = _calc_overall_score(scores, difficulty)

    # 保存结果（从数据库重新加载 slot，避免 compact 等操作导致的过期引用）
    slot = find_slot_by_instance(bag, instance_id)
    if not slot:
        return Response(error_response("物品未找到（可能已被整理操作移除）"), status=400)

    with transaction.atomic():
        if slot.item_amount > 1:
            # 拆分：原槽位减1，新槽位存放已点评物品
            InventorySlot.objects.filter(pk=slot.pk).update(item_amount=slot.item_amount - 1)
            new_slot = find_empty_slot(bag)
            new_slot.item_id = slot.item_id
            new_slot.item_amount = 1
            new_slot.rated = True
            new_slot.rating_price = rating_price
            new_slot.overall_score = overall_score
            new_slot.ingredient = slot.ingredient
            new_slot.item_bag_name = slot.item_bag_name
            new_slot.cook_time = slot.cook_time
            new_slot.dish_quality = slot.dish_quality
            new_slot.hide_in_shop = slot.hide_in_shop
            new_slot.review_comment = comment
            new_slot.save()
        else:
            InventorySlot.objects.filter(pk=slot.pk).update(
                rated=True,
                rating_price=rating_price,
                overall_score=overall_score,
                review_comment=comment,
            )

        # 扣除行动力
        player.action_points -= judge_cost
        player.save()

    return Response({
        "success": True,
        "message": "点评成功",
        "data": {
            "instanceId": str(instance_id),
            "comment": comment,
            "ratingPrice": rating_price,
            "overallScore": overall_score,
            "scores": scores,
            "customerFeedback": customer_feedback,
            "actionPoints": player.action_points,
            "actionPointsMax": player.action_points_max,
            "bagData": serialize_bag(bag),
        }
    })
