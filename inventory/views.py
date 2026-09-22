import uuid
import random
from django.db import transaction
from django.db.models import Max
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from .models import (
    InventoryBag, InventorySlot, BagTemplate, BagTemplateSlot,
    CollectionPoint, CollectionPointLevel, CollectionPointLevelItem,
    CollectionPointUpgradeItemCost, PlayerCollectionPoint, PlayerCollectionOverride,
    AiConfig, NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot, EnvActionReceipt,
)
from .serializers import (
    success_response, dual_bag_response, error_response, serialize_bag,
    InventoryResponseSerializer, DualBagResponseSerializer, ErrorResponseSerializer,
    serialize_npc_global_bag,
)
from .components import BATCH_COPY_FIELDS, copy_batch_attributes, same_batch_attributes
from items.models import Item, ItemType
from account.env_auth import EnvServerJWTAuthentication
from account.authentication import GameJWTAuthentication
from account.models import GameAccount
from config.auth import get_player_id
from .village_policy import (
    ASSET_ACTION_BLOCKED,
    FORMAL_VILLAGE_NPC_IDS,
    LEGACY_WRITE_BLOCKED,
    NOT_BOOTSTRAPPED,
    TEST_ASSET_ACTIONS_DISABLED,
    blocked_response,
    is_formal_village_npc,
    test_asset_actions_enabled,
)


def get_or_create_bag(player, character_id):
    # "player" 只是 BagTemplate 的保留 ID；运行时玩家主背包始终按用户名存储。
    # NPC 库存只能走 api/npc-inventory 或 env/actions/npc-trade，禁止创建私有副本。
    if character_id not in {"player", player.username}:
        raise ValidationError({
            "characterId": "NPC 库存为全服共享数据，不能通过玩家背包接口访问"
        })
    bag, _ = InventoryBag.objects.get_or_create(player=player, character_id=player.username)
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
        slot.dish_metadata = {}
        slot.produced_world_minute = None
        slot.expires_world_minute = None
        slot.source_request_id = None
        slot.freshness_unknown = False
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
@transaction.atomic
def inventory_rate(request, character_id):
    bag = get_or_create_bag(request.user, character_id)
    bag = InventoryBag.objects.select_for_update().get(pk=bag.pk)
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
        copy_batch_attributes(slot, new_slot)
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
@transaction.atomic
def inventory_swap(request, character_id):
    bag = get_or_create_bag(request.user, character_id)
    bag = InventoryBag.objects.select_for_update().get(pk=bag.pk)
    from_index = request.data.get("fromIndex")
    to_index = request.data.get("toIndex")

    if from_index is None or to_index is None:
        return Response(error_response("需要 fromIndex 和 toIndex"), status=400)

    from_slot, _ = bag.slots.get_or_create(slot_index=from_index, defaults={"item_id": 0, "item_amount": 0})
    to_slot, _ = bag.slots.get_or_create(slot_index=to_index, defaults={"item_id": 0, "item_amount": 0})

    # Swap all fields
    fields = ["item_id", "item_amount", "item_bag_name", "hide_in_shop", *BATCH_COPY_FIELDS]
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
@transaction.atomic
def inventory_transfer(request, character_id):
    bag = get_or_create_bag(request.user, character_id)
    bag = InventoryBag.objects.select_for_update().get(pk=bag.pk)
    instance_id = request.data.get("instanceId")
    amount = request.data.get("amount", 1)
    target_id = request.data.get("targetCharacterId")

    if not instance_id or not target_id:
        return Response(error_response("需要 instanceId 和 targetCharacterId"), status=400)

    source_slot = find_slot_by_instance(bag, instance_id)
    if not source_slot or source_slot.item_amount < amount:
        return Response(error_response("物品数量不足"), status=400)

    target_bag = get_or_create_bag(request.user, target_id)
    if target_bag.pk == bag.pk:
        return Response(error_response("不能向同一个背包转移物品"), status=400)
    is_npc_target = (target_id != "player")

    # Find or create target slot
    target_slot = next((row for row in target_bag.slots.filter(item_id=source_slot.item_id, item_amount__gt=0)
                        if same_batch_attributes(row, source_slot)), None)
    if target_slot:
        target_slot.item_amount += amount
        # 不改变已有槽位的 hide_in_shop，避免覆盖NPC预设物品的可见性
        target_slot.save()
    else:
        empty = find_empty_slot(target_bag)
        empty.item_id = source_slot.item_id
        empty.item_amount = amount
        copy_batch_attributes(source_slot, empty)
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
    source_slot.save(update_fields=["item_amount"])
    clear_slot_if_empty(source_slot)

    return Response(dual_bag_response(bag, target_bag))


@extend_schema(responses={200: DualBagResponseSerializer, 400: ErrorResponseSerializer})
@api_view(["POST"])
def inventory_compact(request, character_id):
    """整理背包：删除空槽位，有物品的槽位重新从 0 开始连续编号。"""
    bag = get_or_create_bag(request.user, character_id)

    with transaction.atomic():
        bag = InventoryBag.objects.select_for_update().get(pk=bag.pk)
        filled_slots = list(bag.slots.select_for_update().filter(item_amount__gt=0).order_by("slot_index"))
        bag.slots.filter(item_amount__lte=0).delete()
        # Reindex in place: preserve parent PKs, batch IDs and component ownership.
        # A disjoint temporary range avoids the unique (bag, slot_index) constraint.
        temporary_start = min([0, *(row.slot_index for row in filled_slots)]) - len(filled_slots) - 1
        for offset, row in enumerate(filled_slots):
            row.slot_index = temporary_start + offset
            row.save(update_fields=["slot_index"])
        for index, row in enumerate(filled_slots):
            row.slot_index = index
            row.save(update_fields=["slot_index"])

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


def _consume_item_use(slot, item):
    """Use one copy; the remaining copies in this batch retain their full uses."""
    if slot.remaining_uses is None or slot.remaining_uses == -1:
        return
    slot.remaining_uses -= 1
    if slot.remaining_uses <= 0:
        if slot.item_amount <= 1:
            slot.delete()
            return
        slot.item_amount -= 1
        slot.remaining_uses = item.max_use_count
    slot.save()


@extend_schema(responses={200: InventoryResponseSerializer, 400: ErrorResponseSerializer})
@api_view(["POST"])
@transaction.atomic
def inventory_use(request, character_id):
    """使用背包中的可使用物品。"""
    bag = get_or_create_bag(request.user, character_id)
    bag = InventoryBag.objects.select_for_update().get(pk=bag.pk)
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
                _consume_item_use(slot, item)
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

            _consume_item_use(slot, item)

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

        _consume_item_use(slot, item)

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

        _consume_item_use(slot, item)

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
                # NPC 已改为 NpcGlobalInventory；拒绝旧协议，防止重新创建按玩家库存。
                results.append({
                    "characterId": character_id,
                    "success": False,
                    "message": "NPC库存为全服共享数据，不能通过 bulk-push 写入",
                })

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
    if collect_type == "采集肉类":
        from village.models import VillageSimulationConfig
        if VillageSimulationConfig.objects.filter(config_key="default", village_economy_enabled=True).exists():
            return Response({"code": "REAL_HUNT_REQUIRED", "message": "请在猎场攻击真实动物，击杀后自动获得猎物。"}, status=403)
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
    from player.services import recover_action_points
    recover_action_points(player)
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
        from django.utils import timezone
        player.action_points_recovered_at = timezone.now()
        player.save(update_fields=["action_points", "action_points_recovered_at"])

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


def _report_judge_confirmed(player_id, item_id, item_name, overall_score, comment):
    """上报 judge_confirmed 事件并刷新极鲜榜排行榜。"""
    from leaderboard.models import ReportEvent
    from leaderboard.services import refresh_leaderboard, BoardType
    from season.season_service import get_active_season
    season_id = get_active_season().season_id

    payload = {
        "dish_id": item_id,
        "dish_name": item_name or "",
        "judge_score": overall_score,
        "judge_comment": comment or "",
    }

    ReportEvent.objects.create(
        player_id=str(player_id),
        event_type="judge_confirmed",
        payload=payload,
        season_id=season_id,
    )

    refresh_leaderboard(BoardType.TOTAL_FRESHNESS, season_id)


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
    from cooking.dish_rules import overall_score
    return overall_score(scores, difficulty, random)



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
    from player.services import recover_action_points
    recover_action_points(player)
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
            copy_batch_attributes(slot, new_slot)
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
            slot.rated = True
            slot.rating_price = rating_price
            slot.overall_score = overall_score
            slot.review_comment = comment
            slot.save(update_fields=["rated", "rating_price", "overall_score", "review_comment"])

        # 扣除行动力
        player.action_points -= judge_cost
        from django.utils import timezone
        player.action_points_recovered_at = timezone.now()
        player.save(update_fields=["action_points", "action_points_recovered_at"])

    # 上报 judge_confirmed 事件（刷新极鲜榜）
    _report_judge_confirmed(player_user.pk, slot.item_id, item_detail.item_name, overall_score, comment)

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


@api_view(["POST"])
@authentication_classes([GameJWTAuthentication])
@permission_classes([IsAuthenticated])
def inventory_judge_by_agent(request, character_id):
    """Agent点评接口：接收Agent生成的评语和评分，后端计算并保存最终结果。"""
    from player.models import PlayerState
    from recipes.models import Recipe

    instance_id = request.data.get("instanceId")
    comment = request.data.get("comment", "")
    scores = request.data.get("scores", {})
    customer_feedback = request.data.get("customerFeedback", "")

    # 参数校验
    if not instance_id:
        return Response(error_response("缺少 instanceId"), status=400)
    if not comment:
        return Response(error_response("缺少 comment"), status=400)
    required_scores = ["color", "aroma", "taste", "presentation"]
    for key in required_scores:
        if key not in scores:
            return Response(error_response(f"缺少 scores.{key}"), status=400)

    # 分数范围校验（1-10）
    for key in required_scores:
        val = scores[key]
        if not isinstance(val, (int, float)) or val < 1 or val > 10:
            return Response(error_response(f"scores.{key} 必须在 1-10 之间"), status=400)

    # 通过 instanceId 查找物品所在背包，从而确定玩家
    slot_obj = InventorySlot.objects.filter(instance_id=instance_id, item_amount__gt=0).first()
    if not slot_obj:
        return Response(error_response("物品未找到"), status=400)
    bag = slot_obj.bag
    player_user = bag.player
    if not player_user:
        return Response(error_response("无法确定物品所属玩家"), status=400)
    if str(player_user.id) != str(get_player_id(request)):
        return Response(error_response("点评物品不属于当前玩家"), status=403)

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
    player, _ = PlayerState.objects.get_or_create(player_id=player_user.pk)
    from player.services import recover_action_points
    recover_action_points(player)
    judge_cost = 2
    if player.action_points < judge_cost:
        return Response(error_response("行动力不足，无法点评"), status=400)

    # 获取菜谱信息
    recipe = Recipe.objects.filter(item_id=slot.item_id).first()

    # 计算品质价格
    base_price = round(item_detail.item_price * item_detail.sell_percentage)
    quality_price = _calc_quality_price(base_price, slot.dish_quality)

    # 由后端计算点评价值
    rating_price = _calc_rating_price(quality_price, slot.dish_quality)

    # 计算总分
    difficulty = recipe.difficulty if recipe else 2
    overall_score = _calc_overall_score(scores, difficulty)

    # 保存结果
    slot = find_slot_by_instance(bag, instance_id)
    if not slot:
        return Response(error_response("物品未找到（可能已被整理操作移除）"), status=400)

    with transaction.atomic():
        if slot.item_amount > 1:
            InventorySlot.objects.filter(pk=slot.pk).update(item_amount=slot.item_amount - 1)
            new_slot = find_empty_slot(bag)
            new_slot.item_id = slot.item_id
            new_slot.item_amount = 1
            copy_batch_attributes(slot, new_slot)
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
            slot.rated = True
            slot.rating_price = rating_price
            slot.overall_score = overall_score
            slot.review_comment = comment
            slot.save(update_fields=["rated", "rating_price", "overall_score", "review_comment"])

        # 扣除行动力
        player.action_points -= judge_cost
        from django.utils import timezone
        player.action_points_recovered_at = timezone.now()
        player.save(update_fields=["action_points", "action_points_recovered_at"])

    # 上报 judge_confirmed 事件（刷新极鲜榜）
    _report_judge_confirmed(player_user.pk, slot.item_id, item_detail.item_name, overall_score, comment)

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


# ─────────────── Env Server 原子动作 API ───────────────


def _character_trade_owner(raw_owner, label):
    """Normalize a generic trade participant into a player or NPC owner."""
    if not isinstance(raw_owner, dict):
        return None, f"缺少 {label} 交易对象"

    owner_type = str(raw_owner.get("type") or "").strip().lower()
    owner_id = str(
        raw_owner.get("id")
        or raw_owner.get("characterId")
        or ""
    ).strip()
    if owner_type not in {"player", "npc"} or not owner_id:
        return None, f"{label} 交易对象格式错误"

    if owner_type == "player":
        try:
            player = GameAccount.objects.get(id=owner_id, is_active=True)
        except (GameAccount.DoesNotExist, ValueError, TypeError):
            player = GameAccount.objects.filter(
                username=owner_id, is_active=True
            ).first()
        if player is None:
            return None, f"玩家不存在或已禁用: {owner_id}"

        bag, _ = InventoryBag.objects.get_or_create(
            player=player, character_id=player.username
        )
        return {
            "type": "player",
            "id": str(player.id),
            "account": player,
            "bag": bag,
        }, None

    try:
        npc_bag = NpcGlobalInventory.objects.get(npc__agent_id=owner_id)
    except NpcGlobalInventory.DoesNotExist:
        # Keep the generic endpoint usable after a fresh deployment or when
        # GameInitializer has not loaded this NPC yet.  The initializer uses
        # the NPC's BagTemplate and returns None for an unknown agent ID.
        npc_bag = _init_npc_global_from_template(owner_id)
        if npc_bag is None:
            return None, f"NPC库存不存在: {owner_id}"

    return {
        "type": "npc",
        "id": owner_id,
        "account": None,
        "bag": npc_bag,
    }, None


def _character_trade_items(raw_items, label):
    """Parse the original giveItem/getItem shape for the generic endpoint."""
    if raw_items is None:
        return [], None
    if not isinstance(raw_items, list):
        return [], f"{label} 必须是数组"

    parsed = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            return [], f"{label} 物品格式错误"

        item_id = raw_item.get("itemID")
        if item_id is None:
            item_id = raw_item.get("itemId")
        instance_id = raw_item.get("instanceID")
        if instance_id is None:
            instance_id = raw_item.get("instanceId")

        try:
            item_id = int(item_id)
            amount = int(raw_item.get("amount", 0))
        except (TypeError, ValueError):
            return [], f"{label} 物品格式错误"

        if item_id <= 0 or amount <= 0:
            return [], f"{label} 物品格式错误"

        parsed.append({
            "item_id": item_id,
            "instance_id": str(instance_id or "").strip() or None,
            "amount": amount,
        })
    return parsed, None


def _character_trade_find_slot(owner, item):
    slots = owner["bag"].slots.select_for_update().filter(item_amount__gt=0)
    instance_id = item["instance_id"]
    if instance_id:
        slot = slots.filter(instance_id=instance_id).first()
        if slot is not None and slot.item_id != item["item_id"]:
            return None, "物品实例与物品ID不匹配"
    else:
        slot = slots.filter(item_id=item["item_id"]).first()

    if slot is None or slot.item_id != item["item_id"]:
        return None, "物品数量不足"
    return slot, None


def _character_trade_copy_slot(source_slot, target_owner, amount):
    """Copy one inventory slot to either a player bag or an NPC global bag."""
    target_bag = target_owner["bag"]
    target_is_npc = target_owner["type"] == "npc"
    target_hide_in_shop = False
    if target_is_npc:
        # NPC 模板是商店商品白名单。玩家卖出的物品仍进入 NPC 全局库存，
        # 但只有模板中配置为可见的物品才能出现在商店页面。
        target_hide_in_shop = not BagTemplateSlot.objects.filter(
            template__npc_id=target_bag.npc_id,
            item_id=source_slot.item_id,
            hide_in_shop=False,
        ).exists()

    candidates = target_bag.slots.select_for_update().filter(
        item_id=source_slot.item_id,
        item_amount__gt=0,
        quality=source_slot.quality,
    ).order_by("hide_in_shop", "slot_index", "id")
    existing = next((row for row in candidates if same_batch_attributes(row, source_slot)), None)
    if existing:
        existing.item_amount += amount
        # 同步模板白名单结果，同时修正历史上写错的可见性。
        existing.hide_in_shop = target_hide_in_shop
        existing.save(update_fields=["item_amount", "hide_in_shop"])
        return

    if target_is_npc:
        target = target_bag.slots.filter(item_amount=0).first()
        if target is None:
            max_idx = target_bag.slots.aggregate(Max("slot_index"))["slot_index__max"]
            target = NpcGlobalInventorySlot.objects.create(
                bag=target_bag,
                slot_index=(max_idx + 1) if max_idx is not None else 0,
            )
    else:
        target = find_empty_slot(target_bag)

    target.item_id = source_slot.item_id
    target.item_amount = amount
    copy_batch_attributes(source_slot, target)
    target.rated = source_slot.rated
    target.rating_price = source_slot.rating_price
    target.overall_score = source_slot.overall_score
    target.ingredient = source_slot.ingredient
    target.item_bag_name = source_slot.item_bag_name
    target.cook_time = source_slot.cook_time
    target.dish_quality = source_slot.dish_quality
    target.hide_in_shop = target_hide_in_shop
    target.remaining_uses = source_slot.remaining_uses
    target.review_comment = source_slot.review_comment
    target.save()


def _character_trade_remove_slot(slot, amount):
    slot.item_amount -= amount
    slot.save(update_fields=["item_amount"])
    clear_slot_if_empty(slot)


@api_view(["POST"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
@transaction.atomic
def character_trade(request):
    """Execute a generic atomic trade between a player and/or an NPC.

    The payload keeps the original giveItem/getItem semantics:
    initiator gives giveItem/giveMoney and receives getItem/getMoney.
    The other participant is the source of getItem/getMoney and receives the
    initiator's giveItem/giveMoney.
    """
    # import time as _time
    # _trade_start = _time.monotonic()
    request_id = str(request.data.get("requestId") or "").strip()
    requester_id = str(request.data.get("requesterId") or "").strip()
    # print(f"[TradePerf] character_trade 开始: requestId={request_id}")
    if not request_id:
        return Response(error_response("缺少 requestId"), status=400)

    initiator_raw = request.data.get("initiator")
    other_raw = request.data.get("other")
    if initiator_raw is None:
        initiator_raw = {
            "id": request.data.get("initiatorId"),
            "type": request.data.get("initiatorType"),
        }
    if other_raw is None:
        other_raw = {
            "id": request.data.get("otherId"),
            "type": request.data.get("otherType"),
        }

    participant_ids = {
        str(owner.get("id") or owner.get("characterId") or "").strip()
        for owner in (initiator_raw, other_raw)
        if isinstance(owner, dict)
        and str(owner.get("type") or "").strip().lower() == "npc"
    }
    player_to_formal_npc = (
        isinstance(initiator_raw, dict)
        and isinstance(other_raw, dict)
        and str(initiator_raw.get("type") or "").strip().lower() == "player"
        and str(other_raw.get("type") or "").strip().lower() == "npc"
        and str(other_raw.get("id") or other_raw.get("characterId") or "").strip() in FORMAL_VILLAGE_NPC_IDS
    )
    # This marker is set only by Env's authenticated, distance-checked NPC meeting route.
    dialogue_gift = player_to_formal_npc and request.data.get("giftContext") == "npc_dialogue"
    if dialogue_gift and not str(request.data.get("conversationId") or "").strip():
        return Response(error_response("缺少 NPC 会话编号"), status=400)
    blocked_ids = sorted(participant_ids & FORMAL_VILLAGE_NPC_IDS)
    if blocked_ids and not player_to_formal_npc:
        return Response(
            blocked_response(
                ASSET_ACTION_BLOCKED,
                "正式村庄 NPC 的资产动作将在阶段三原子结算启用后开放",
            ),
            status=409,
        )
    has_test_only_participant = NpcDefinition.objects.filter(
        agent_id__in=participant_ids,
        is_test_only=True,
    ).exists()
    if has_test_only_participant and not test_asset_actions_enabled():
        return Response(
            blocked_response(
                TEST_ASSET_ACTIONS_DISABLED,
                "TestOnly NPC 资产动作需要显式测试开关",
            ),
            status=409,
        )

    # A player client may only know its username.  Env Server supplies the
    # authenticated account ID as requesterId, so use it for the local player.
    if isinstance(initiator_raw, dict) and str(initiator_raw.get("type") or "").lower() == "player":
        if str(initiator_raw.get("id") or "").strip() in {"", "player", "self"} and requester_id:
            initiator_raw = dict(initiator_raw)
            initiator_raw["id"] = requester_id
    if isinstance(other_raw, dict) and str(other_raw.get("type") or "").lower() == "player":
        if str(other_raw.get("id") or "").strip() in {"", "player", "self"} and requester_id:
            other_raw = dict(other_raw)
            other_raw["id"] = requester_id

    initiator, error = _character_trade_owner(initiator_raw, "initiator")
    if error:
        return Response(error_response(error), status=400)
    other, error = _character_trade_owner(other_raw, "other")
    if error:
        return Response(error_response(error), status=400)
    if initiator["type"] == other["type"] and initiator["id"] == other["id"]:
        return Response(error_response("交易双方不能是同一角色"), status=400)

    player_owners = [owner for owner in (initiator, other) if owner["type"] == "player"]

    requester = None
    if requester_id:
        # The client may send either the account UUID or the username.  The
        # owner resolver normalizes both forms to the account UUID, so resolve
        # requesterId the same way before comparing it with the participants.
        try:
            requester_account = GameAccount.objects.get(
                id=requester_id, is_active=True
            )
        except (GameAccount.DoesNotExist, ValueError, TypeError):
            requester_account = GameAccount.objects.filter(
                username=requester_id, is_active=True
            ).first()

        if requester_account is not None:
            requester = next(
                (
                    owner for owner in player_owners
                    if owner["account"].id == requester_account.id
                ),
                None,
            )
            if requester is not None:
                requester_id = str(requester_account.id)

        if requester is None:
            return Response(error_response("requesterId 不是交易参与玩家"), status=400)
    else:
        requester = player_owners[0] if player_owners else None
        requester_id = str(requester["account"].id) if requester else ""

    receipt = None
    if requester is not None:
        receipt = EnvActionReceipt.objects.select_for_update().filter(
            player=requester["account"], request_id=request_id
        ).first()
    if receipt is not None:
        if receipt.response_data:
            return Response(receipt.response_data)
        receipt.delete()

    # Lock both owner rows before validating or mutating any slot.  The tables
    # are different for players and NPCs, but deterministic ordering prevents
    # two concurrent player/NPC transactions from waiting in opposite order.
    locked_owners = sorted(
        (initiator, other),
        key=lambda owner: (owner["type"], owner["id"]),
    )
    for owner in locked_owners:
        if owner["type"] == "player":
            owner["bag"] = InventoryBag.objects.select_for_update().get(pk=owner["bag"].pk)
        else:
            owner["bag"] = NpcGlobalInventory.objects.select_for_update().get(pk=owner["bag"].pk)

    # Re-check after locking both inventories.  A concurrent retry with the
    # same requestId may have waited on the owner lock while the first request
    # committed its receipt.
    if requester is not None:
        receipt = EnvActionReceipt.objects.select_for_update().filter(
            player=requester["account"], request_id=request_id
        ).first()
        if receipt is not None:
            if receipt.response_data:
                return Response(receipt.response_data)
            receipt.delete()

    give_items, error = _character_trade_items(request.data.get("giveItem"), "giveItem")
    if error:
        return Response(error_response(error), status=400)
    get_items, error = _character_trade_items(request.data.get("getItem"), "getItem")
    if error:
        return Response(error_response(error), status=400)

    try:
        give_money = int(request.data.get("giveMoney", 0) or 0)
        get_money = int(request.data.get("getMoney", 0) or 0)
    except (TypeError, ValueError):
        return Response(error_response("金币格式错误"), status=400)
    if give_money < 0 or get_money < 0:
        return Response(error_response("金币不能为负数"), status=400)
    if not give_items and not get_items and give_money == 0 and get_money == 0:
        return Response(error_response("交易内容不能为空"), status=400)

    if player_to_formal_npc:
        if get_items or get_money:
            return Response(error_response("正式 NPC 只接受玩家单向赠予"), status=409)
        from village.phase3_catalog import SAFE_STOCKS
        from village.phase3_asset_service import get_available_slot_amount
        from game_time.models import GlobalGameTimeState
        world_minute = GlobalGameTimeState.get_instance().world_minute
        for gift in give_items:
            stock = sum(
                get_available_slot_amount(slot)
                for slot in other["bag"].slots.filter(item_id=gift["item_id"], item_amount__gt=0)
                if slot.expires_world_minute is None or slot.expires_world_minute > world_minute
            )
            if not dialogue_gift and (gift["item_id"] not in SAFE_STOCKS or stock >= SAFE_STOCKS[gift["item_id"]]):
                return Response(error_response("该正式 NPC 当前不符合救急缺货条件"), status=409)

    def resolve_all(owner, items, label):
        resolved = {}
        for item in items:
            slot, slot_error = _character_trade_find_slot(owner, item)
            if slot_error:
                return None, f"{label}: {slot_error}"
            if dialogue_gift:
                if not item["instance_id"]:
                    return None, f"{label}: 请选择明确的物品批次"
                expiry = getattr(slot, "expires_world_minute", None)
                if slot.freshness_unknown or (expiry is not None and expiry <= world_minute):
                    return None, f"{label}: 物品已经过期"
            if slot.item_amount < item["amount"]:
                return None, f"{label}: 物品数量不足"
            item_config = Item.objects.filter(item_id=item["item_id"]).first()
            if item_config is None:
                return None, f"{label}: 物品配置不存在({item['item_id']})"
            key = slot.pk
            if key not in resolved:
                resolved[key] = {
                    "slot": slot,
                    "item": item_config,
                    "amount": 0,
                }
            resolved[key]["amount"] += item["amount"]

        for entry in resolved.values():
            available = entry["slot"].item_amount
            if available < entry["amount"]:
                return None, f"{label}: 物品数量不足"
        return list(resolved.values()), None

    resolved_give, error = resolve_all(initiator, give_items, "给予物品")
    if error:
        return Response(error_response(error), status=400)
    resolved_get, error = resolve_all(other, get_items, "获得物品")
    if error:
        return Response(error_response(error), status=400)

    if initiator["bag"].money < give_money:
        return Response(error_response("发起者金币不足"), status=400)
    if other["bag"].money < get_money:
        return Response(error_response("对方金币不足"), status=400)

    for entry in resolved_give:
        _character_trade_copy_slot(entry["slot"], other, entry["amount"])
        _character_trade_remove_slot(entry["slot"], entry["amount"])
    for entry in resolved_get:
        _character_trade_copy_slot(entry["slot"], initiator, entry["amount"])
        _character_trade_remove_slot(entry["slot"], entry["amount"])

    initiator["bag"].money -= give_money
    other["bag"].money += give_money
    other["bag"].money -= get_money
    initiator["bag"].money += get_money

    if initiator["type"] == "player":
        initiator["bag"]._money_change_source = "character_trade"
        initiator["bag"].save()
    else:
        initiator["bag"].save(update_fields=["money"])
    if other["type"] == "player":
        other["bag"]._money_change_source = "character_trade"
        other["bag"].save()
    else:
        other["bag"].save(update_fields=["money"])

    def serialize_owner(owner):
        if owner["type"] == "player":
            return serialize_bag(owner["bag"])
        return serialize_npc_global_bag(owner["bag"])

    response_data = {
        "success": True,
        "message": "交易完成",
        "data": {
            "requestId": request_id,
            "initiatorInventory": serialize_owner(initiator),
            "otherInventory": serialize_owner(other),
        },
    }
    if player_to_formal_npc:
        from game_time.models import GlobalGameTimeState
        from village.models import NpcRuntimeState, VillageWorldEvent
        from village.phase4_visibility import enqueue_event_deliveries

        clock = GlobalGameTimeState.objects.filter(pk=1).values("world_minute", "time_revision").first() or {}
        runtime = NpcRuntimeState.objects.filter(npc_id=other["bag"].npc_id).first()
        location = runtime.current_location_code if runtime else ""
        try:
            event_request_id = uuid.UUID(request_id)
        except (ValueError, TypeError, AttributeError):
            event_request_id = uuid.uuid5(uuid.NAMESPACE_URL, request_id)
        # Use the validated item definitions: transferring an entire batch may
        # already have cleared its source slot by this point.
        gift_items = {}
        for entry in resolved_give:
            item = entry["item"]
            detail = gift_items.setdefault(item.item_id, {
                "item_id": item.item_id, "item_name": item.item_name, "amount": 0,
            })
            detail["amount"] += entry["amount"]
        gift_items = list(gift_items.values())
        gift_description = [f"{item['item_name']}×{item['amount']}" for item in gift_items]
        if give_money:
            gift_description.append(f"{give_money} 金币")
        gift_description = "、".join(gift_description)
        # Summaries are capped at 256 characters; the structured receipt keeps
        # every item even when the readable list must be shortened.
        if len(gift_description) > 180:
            gift_description = gift_description[:177] + "…"
        gift_summary = f"玩家{initiator['account'].username}赠送给你：{gift_description}。已到账。"
        event = VillageWorldEvent.objects.create(
            dedupe_key=f"player-gift:{request_id}" if dialogue_gift else f"player-rescue-gift:{request_id}",
            event_type="PLAYER_GIFT" if dialogue_gift else "PLAYER_RESCUE_GIFT",
            world_minute=int(clock.get("world_minute", 0)),
            time_revision=int(clock.get("time_revision", 0)),
            participant_npc_ids=[str(other["id"])],
            witness_npc_ids=[],
            player_ids=[requester_id],
            location_code=location,
            request_id=event_request_id,
            visibility="PARTICIPANTS",
            hub_summary=gift_summary,
            player_summary="Gift delivered.",
            private_data={
                "npc_id": str(other["id"]),
                "player_id": str(initiator["account"].pk),
                "player_name": initiator["account"].username,
                "items": gift_items,
                "gold": give_money,
                "request_id": request_id,
                "conversation_id": str(request.data.get("conversationId") or "") if dialogue_gift else "",
            },
        )
        enqueue_event_deliveries(event)
    if requester is not None:
        EnvActionReceipt.objects.create(
            player=requester["account"],
            request_id=request_id,
            action="character_trade",
            response_data=response_data,
        )
    # _trade_elapsed = _time.monotonic() - _trade_start
    # print(f"[TradePerf] character_trade 完成: requestId={request_id}, elapsed={_trade_elapsed:.3f}s")
    return Response(response_data)


@api_view(["POST"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def character_trade_receipt(request):
    """Read only: resolve an uncertain transfer without submitting a new one."""
    request_id = str(request.data.get("requestId") or "").strip()
    requester_id = str(request.data.get("requesterId") or "").strip()
    try:
        uuid.UUID(request_id)
        account = GameAccount.objects.get(id=requester_id, is_active=True)
    except (ValueError, TypeError, GameAccount.DoesNotExist):
        return Response(error_response("回执查询参数无效"), status=400)
    receipt = EnvActionReceipt.objects.filter(
        player=account, request_id=request_id, action="character_trade"
    ).first()
    bag = InventoryBag.objects.filter(player=account, character_id=account.username).first() if receipt else None
    return Response({"success": True, "found": bool(receipt and receipt.response_data),
                     "receipt": receipt.response_data if receipt else None,
                     "currentInventory": serialize_bag(bag) if bag else None})


# ─────────────── NPC 全局库存 API ───────────────


@api_view(["GET"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def npc_global_inventory_detail(request, character_id):
    """获取NPC全局库存。Env Server启动时调用加载库存。"""
    try:
        bag = NpcGlobalInventory.objects.select_related("npc").get(npc__agent_id=character_id)
    except NpcGlobalInventory.DoesNotExist:
        return Response(
            blocked_response(
                NOT_BOOTSTRAPPED,
                f"NPC {character_id} 尚未通过受控 Seed 初始化",
            ),
            status=409 if is_formal_village_npc(character_id) else 404,
        )
    return Response({
        "success": True,
        "message": "获取成功",
        "data": serialize_npc_global_bag(bag),
    })


@api_view(["PUT"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
@transaction.atomic
def npc_global_inventory_update(request, character_id):
    """更新NPC全局库存（Env Server交易后写回）。

    请求体: {"money": 100, "items": [{"slotIndex": 0, "itemId": 1, "itemAmount": 5, ...}, ...]}
    """
    if is_formal_village_npc(character_id):
        return Response(
            blocked_response(
                LEGACY_WRITE_BLOCKED,
                "正式村庄 NPC 禁止使用旧式全量库存覆盖接口",
            ),
            status=409,
        )

    if NpcDefinition.objects.filter(agent_id=character_id, is_test_only=True).exists() and not test_asset_actions_enabled():
        return Response(
            blocked_response(TEST_ASSET_ACTIONS_DISABLED, "TestOnly NPC 资产动作需要显式测试开关"),
            status=409,
        )

    npc = NpcDefinition.objects.filter(agent_id=character_id).first()
    if not npc:
        return Response(error_response(f"NPC {character_id} 不存在"))
    bag, _ = NpcGlobalInventory.objects.get_or_create(npc=npc)
    money = request.data.get("money")
    if money is not None:
        bag.money = money
        bag.save()

    items = request.data.get("items")
    if items is not None:
        # 清除旧slots，全量写入
        bag.slots.all().delete()
        for item_data in items:
            NpcGlobalInventorySlot.objects.create(
                bag=bag,
                slot_index=item_data.get("slotIndex", 0),
                item_id=item_data.get("itemId", 0),
                item_amount=item_data.get("itemAmount", 0),
                rated=item_data.get("rated", False),
                rating_price=item_data.get("ratingPrice", -1),
                overall_score=item_data.get("overallScore", 0),
                ingredient=item_data.get("ingredient", ""),
                item_bag_name=item_data.get("itemBagName", ""),
                cook_time=item_data.get("cookTime", 0),
                dish_quality=item_data.get("dishQuality", 3),
                hide_in_shop=item_data.get("hideInShop", False),
                remaining_uses=item_data.get("remainingUses"),
                review_comment=item_data.get("reviewComment", ""),
            )

    return Response({
        "success": True,
        "message": "更新成功",
        "data": serialize_npc_global_bag(bag),
    })


@api_view(["POST"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
@transaction.atomic
def npc_global_inventory_restock(request):
    """批量补充NPC库存到模板数量（凌晨4点重置时调用）。

    请求体: {"characterIds": ["zhplb", "hmzs"]} 或 {} 表示全部
    """
    character_ids = request.data.get("characterIds", [])
    requested_ids = {str(value).strip() for value in character_ids if str(value).strip()}
    if requested_ids & FORMAL_VILLAGE_NPC_IDS:
        return Response(
            blocked_response(
                LEGACY_WRITE_BLOCKED,
                "正式村庄 NPC 禁止使用模板补货接口",
            ),
            status=409,
        )

    if requested_ids and NpcDefinition.objects.filter(
        agent_id__in=requested_ids,
        is_test_only=True,
    ).exists() and not test_asset_actions_enabled():
        return Response(
            blocked_response(TEST_ASSET_ACTIONS_DISABLED, "TestOnly NPC 资产动作需要显式测试开关"),
            status=409,
        )

    templates = BagTemplate.objects.prefetch_related("slots__item").filter(seed_only=False)
    if character_ids:
        templates = templates.filter(npc__agent_id__in=requested_ids)
    templates = templates.filter(npc__isnull=False).exclude(
        npc__agent_id__in=FORMAL_VILLAGE_NPC_IDS
    )
    if not test_asset_actions_enabled():
        templates = templates.exclude(npc__is_test_only=True)

    restocked = 0
    for tmpl in templates:
        bag, _ = NpcGlobalInventory.objects.select_for_update().get_or_create(
            npc=tmpl.npc,
            defaults={"money": tmpl.money},
        )
        for tmpl_slot in tmpl.slots.select_related("item").all():
            if not tmpl_slot.item:
                continue
            existing = bag.slots.filter(item_id=tmpl_slot.item_id, item_amount__gt=0).first()
            if existing:
                deficit = tmpl_slot.item_amount - existing.item_amount
                if deficit > 0:
                    existing.item_amount = tmpl_slot.item_amount
                    existing.save()
            else:
                max_idx = bag.slots.aggregate(Max("slot_index"))["slot_index__max"]
                next_idx = (max_idx + 1) if max_idx is not None else 0
                NpcGlobalInventorySlot.objects.create(
                    bag=bag,
                    slot_index=next_idx,
                    item_id=tmpl_slot.item_id,
                    item_amount=tmpl_slot.item_amount,
                    rated=tmpl_slot.rated,
                    rating_price=tmpl_slot.rating_price,
                    overall_score=tmpl_slot.overall_score,
                    ingredient=tmpl_slot.ingredient,
                    item_bag_name=f"{tmpl_slot.item.item_name}_{next_idx}",
                    cook_time=tmpl_slot.cook_time,
                    dish_quality=tmpl_slot.dish_quality,
                    hide_in_shop=tmpl_slot.hide_in_shop,
                )
        restocked += 1

    return Response({"success": True, "restocked": restocked})


def _init_npc_global_from_template(character_id):
    """从BagTemplate初始化NPC全局库存。无模板时创建空库存。"""
    npc = NpcDefinition.objects.filter(agent_id=character_id).first()
    if not npc:
        return None

    tmpl = BagTemplate.objects.prefetch_related("slots__item").filter(npc=npc).first()

    bag = NpcGlobalInventory.objects.create(npc=npc, money=tmpl.money if tmpl else 0)
    if tmpl:
        for tmpl_slot in tmpl.slots.select_related("item").all():
            if not tmpl_slot.item:
                continue
            remaining_uses = None
            if tmpl_slot.item.item_type == "Usable":
                remaining_uses = tmpl_slot.item.max_use_count
            NpcGlobalInventorySlot.objects.create(
                bag=bag,
                slot_index=tmpl_slot.slot_index,
                item_id=tmpl_slot.item_id,
                item_amount=tmpl_slot.item_amount,
                rated=tmpl_slot.rated,
                rating_price=tmpl_slot.rating_price,
                overall_score=tmpl_slot.overall_score,
                ingredient=tmpl_slot.ingredient,
                item_bag_name=f"{tmpl_slot.item.item_name}_{tmpl_slot.slot_index}",
                cook_time=tmpl_slot.cook_time,
                dish_quality=tmpl_slot.dish_quality,
                hide_in_shop=tmpl_slot.hide_in_shop,
                remaining_uses=remaining_uses,
            )
    return bag
