import uuid
import random
from django.db import transaction
from django.db.models import Max
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view
from rest_framework.response import Response
from .models import InventoryBag, InventorySlot, BagTemplate, BagTemplateSlot
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
        return Response(error_response("itemId is required"), status=400)

    # Check if item is food
    is_food = False
    item_name = ""
    try:
        item = Item.objects.get(item_id=item_id)
        is_food = (item.item_type == "食物")
        item_name = item.item_name
    except Item.DoesNotExist:
        pass

    if is_food and instance_id:
        # Food: always create a new slot (each dish is unique)
        slot = find_empty_slot(bag)
        slot.item_id = item_id
        slot.item_amount = amount
        slot.ingredient = ingredient
        slot.cook_time = cook_time
        slot.dish_quality = dish_quality
        slot.item_bag_name = item_name
        slot.save()
    else:
        # Non-food: stack on existing slot
        existing = bag.slots.filter(item_id=item_id, item_amount__gt=0).first()
        if existing:
            existing.item_amount += amount
            if ingredient:
                existing.ingredient = ingredient
            existing.save()
        else:
            slot = find_empty_slot(bag)
            slot.item_id = item_id
            slot.item_amount = amount
            slot.ingredient = ingredient
            slot.cook_time = cook_time
            slot.dish_quality = dish_quality
            slot.item_bag_name = item_name
            slot.save()

    return Response(success_response(bag))


@extend_schema(responses={200: InventoryResponseSerializer, 400: ErrorResponseSerializer})
@api_view(["POST"])
def inventory_rate(request, character_id):
    bag = get_or_create_bag(request.user, character_id)
    instance_id = request.data.get("instanceId")
    rating_price = request.data.get("ratingPrice", 0)
    overall_score = request.data.get("overallScore", 0)

    if not instance_id:
        return Response(error_response("instanceId is required"), status=400)

    slot = find_slot_by_instance(bag, instance_id)
    if not slot:
        return Response(error_response("item not found"), status=400)

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
        new_slot.save()
    else:
        # Update in place
        slot.rated = True
        slot.rating_price = rating_price
        slot.overall_score = overall_score
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
        return Response(error_response("instanceId or itemId required"), status=400)

    if not slot:
        return Response(error_response("item not found in bag"), status=400)

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
        return Response(error_response("fromIndex and toIndex required"), status=400)

    from_slot, _ = bag.slots.get_or_create(slot_index=from_index, defaults={"item_id": 0, "item_amount": 0})
    to_slot, _ = bag.slots.get_or_create(slot_index=to_index, defaults={"item_id": 0, "item_amount": 0})

    # Swap all fields
    fields = ["item_id", "item_amount", "rated", "rating_price", "ingredient", "item_bag_name", "cook_time", "dish_quality"]
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
        return Response(error_response("instanceId required"), status=400)

    slot = find_slot_by_instance(bag, instance_id)
    if not slot:
        return Response(error_response("item not found"), status=400)

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
            return Response(error_response("not enough items"), status=400)
        earnings = int(item_price * sell_pct * amount)
        slot.item_amount -= amount
        slot.save()
        clear_slot_if_empty(slot)
        bag.money += earnings
        bag.save()
    else:
        # Buy: deduct money, add item
        cost = item_price * amount
        if bag.money < cost:
            return Response(error_response("not enough money"), status=400)
        bag.money -= cost
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
        return Response(error_response("instanceId and targetCharacterId required"), status=400)

    source_slot = find_slot_by_instance(bag, instance_id)
    if not source_slot or source_slot.item_amount < amount:
        return Response(error_response("not enough items"), status=400)

    target_bag = get_or_create_bag(request.user, target_id)

    # Find or create target slot
    target_slot = target_bag.slots.filter(item_id=source_slot.item_id, item_amount__gt=0).first()
    if target_slot:
        target_slot.item_amount += amount
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
        empty.save()

    source_slot.item_amount -= amount
    clear_slot_if_empty(source_slot)

    return Response(dual_bag_response(bag, target_bag))


@extend_schema(responses={200: DualBagResponseSerializer, 400: ErrorResponseSerializer})
@api_view(["POST"])
def inventory_trade_with(request, character_id):
    bag = get_or_create_bag(request.user, character_id)
    other_id = request.data.get("otherCharacterId")
    give_items = request.data.get("giveItem", [])
    give_money = request.data.get("giveMoney", 0)
    get_items = request.data.get("getItem", [])
    get_money = request.data.get("getMoney", 0)

    if not other_id:
        return Response(error_response("otherCharacterId required"), status=400)

    other_bag = get_or_create_bag(request.user, other_id)

    # Validate money
    if bag.money < give_money:
        return Response(error_response("not enough money"), status=400)
    if other_bag.money < get_money:
        return Response(error_response("other not enough money"), status=400)

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
            return Response(error_response(f"not enough item {item_id}"), status=400)

    # Validate get items
    for gi in get_items:
        item_id = gi.get("itemID")
        amt = gi.get("amount", 1)
        slot = other_bag.slots.filter(item_id=item_id, item_amount__gt=0).first()
        if not slot or slot.item_amount < amt:
            return Response(error_response(f"other not enough item {item_id}"), status=400)

    # Execute transfers
    bag.money -= give_money
    other_bag.money += give_money
    other_bag.money -= get_money
    bag.money += get_money

    for gi in give_items:
        item_id = gi.get("itemID")
        inst_id = gi.get("instanceID")
        amt = gi.get("amount", 1)
        if inst_id:
            source_slot = find_slot_by_instance(bag, inst_id)
        else:
            source_slot = bag.slots.filter(item_id=item_id, item_amount__gt=0).first()

        # 判断是否为食物，食物出售后直接销毁，不写入NPC背包
        is_food = Item.objects.filter(item_id=item_id, item_type=ItemType.FOOD).exists()

        if not is_food:
            target_slot = other_bag.slots.filter(item_id=item_id, item_amount__gt=0).first()
            if target_slot:
                target_slot.item_amount += amt
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
            empty.save()
        source_slot.item_amount -= amt
        source_slot.save()
        clear_slot_if_empty(source_slot)

    bag.save()
    other_bag.save()

    return Response(dual_bag_response(bag, other_bag))


@extend_schema(responses={200: InventoryResponseSerializer, 400: ErrorResponseSerializer})
@api_view(["POST"])
def inventory_money(request, character_id):
    bag = get_or_create_bag(request.user, character_id)
    amount = request.data.get("amount", 0)
    bag.money += amount
    if bag.money < 0:
        bag.money = 0
    bag.save()
    return Response(success_response(bag))


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
                        cook_time=item.get("cookTime", 0),
                        dish_quality=item.get("dishQuality", 3),
                    )
                results.append({"characterId": character_id, "success": True, "message": "模板同步成功"})
            else:
                # ---- NPC 背包：写入当前玩家的 InventoryBag ----
                bag = get_or_create_bag(player, character_id)
                bag.money = bag_data.get("money", bag.money)
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
                    )
                results.append({"characterId": character_id, "success": True, "message": "同步成功"})

    return Response({"success": True, "message": "批量同步完成", "data": results})


# 采集类型 → 物品类型的映射
COLLECT_TYPE_MAP = {
    "采集肉类": ItemType.MEAT,
    "采集食材": ItemType.VEGETABLE,
}

COLLECT_ITEM_COUNT = 3      # 每次采集随机物品种类数
COLLECT_ACTION_COST = 15    # 采集消耗的行动力


@api_view(["POST"])
def inventory_collect(request, character_id):
    """采集接口：扣行动力 + 随机生成物品 + 写入背包，一步完成。"""
    from player.models import PlayerState

    collect_type = request.data.get("collectType", "")
    item_type = COLLECT_TYPE_MAP.get(collect_type)
    if item_type is None:
        return Response({
            "code": "INVALID_COLLECT_TYPE",
            "message": f"未知的采集类型: {collect_type}",
        }, status=400)

    # 获取玩家状态，校验行动力
    player_id = request.user.pk
    player, _ = PlayerState.objects.get_or_create(player_id=player_id)
    if player.action_points < COLLECT_ACTION_COST:
        return Response({
            "code": "INSUFFICIENT_ACTION_POINTS",
            "message": "行动力不足",
        }, status=200)

    # 从物品池中随机选取
    pool = list(Item.objects.filter(item_type=item_type, is_deleted=False, collectable=True))
    if not pool:
        return Response({
            "code": "NO_ITEMS",
            "message": f"该采集类型({collect_type})没有可生成的物品",
        }, status=200)

    count = min(COLLECT_ITEM_COUNT, len(pool))
    # 加权随机选取（不放回）
    weights = [item.collect_weight for item in pool]
    selected = []
    remaining_pool = list(pool)
    remaining_weights = list(weights)
    for _ in range(count):
        pick = random.choices(remaining_pool, weights=remaining_weights, k=1)[0]
        selected.append(pick)
        idx = remaining_pool.index(pick)
        remaining_pool.pop(idx)
        remaining_weights.pop(idx)

    # 生成采集结果（每物品独立数量范围）
    collected_items = []
    for item in selected:
        amount = random.randint(item.collect_min_amount, item.collect_max_amount)
        collected_items.append({"itemId": item.item_id, "itemName": item.item_name, "amount": amount})

    # 事务：扣行动力 + 写入背包
    bag = get_or_create_bag(request.user, character_id)
    with transaction.atomic():
        player.action_points -= COLLECT_ACTION_COST
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
                slot.item_bag_name = ci["itemName"]
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
