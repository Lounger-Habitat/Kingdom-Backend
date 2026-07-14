import uuid
from django.db import transaction
from .models import InventoryBag, InventorySlot, BagTemplate, BagTemplateSlot


def init_player_bags(player):
    """根据 BagTemplate 为新玩家初始化所有背包。

    只在该玩家没有任何背包数据时执行，已存在则跳过。
    返回创建的背包数量。
    """
    if InventoryBag.objects.filter(player=player).exists():
        return 0

    templates = BagTemplate.objects.prefetch_related("slots").all()
    if not templates.exists():
        return 0

    created = 0
    player_own_bag_money = 0
    with transaction.atomic():
        for tmpl in templates:
            # character_id="player" 的模板改为玩家用户名，作为玩家自己的背包
            cid = player.username if tmpl.character_id == "player" else tmpl.character_id
            bag = InventoryBag.objects.create(
                player=player,
                character_id=cid,
                money=tmpl.money,
            )
            if tmpl.character_id == "player":
                player_own_bag_money = tmpl.money
            for slot in tmpl.slots.select_related("item").all():
                remaining_uses = None
                if slot.item and slot.item.item_type == "Usable":
                    remaining_uses = slot.item.max_use_count
                InventorySlot.objects.create(
                    bag=bag,
                    slot_index=slot.slot_index,
                    instance_id=uuid.uuid4(),
                    item_id=slot.item_id,
                    item_amount=slot.item_amount,
                    rated=slot.rated,
                    rating_price=slot.rating_price,
                    overall_score=slot.overall_score,
                    ingredient=slot.ingredient,
                    item_bag_name=f"{slot.item.item_name}_{slot.slot_index}" if slot.item else "",
                    cook_time=slot.cook_time,
                    dish_quality=slot.dish_quality,
                    hide_in_shop=slot.hide_in_shop,
                    remaining_uses=remaining_uses,
                )
            created += 1

    # 设置富豪榜日榜第一天基准金币和总榜快照
    if created > 0 and player_own_bag_money > 0:
        from player.models import PlayerState
        ps, _ = PlayerState.objects.get_or_create(player_id=str(player.id))
        ps.day_start_money = player_own_bag_money
        ps.day_start_money_game_day = 1
        ps.total_wealth_snapshot = player_own_bag_money
        ps.save(update_fields=["day_start_money", "day_start_money_game_day", "total_wealth_snapshot"])

    return created


def restock_npc_bags(player):
    """每天推进时，将NPC背包中被买走的预设物品补回到模板数量。

    只补 BagTemplate 中定义的物品，玩家卖给NPC的物品不受影响。
    不重置金币。
    """
    templates = {
        t.character_id: t
        for t in BagTemplate.objects.prefetch_related("slots__item").all()
        if t.character_id != "player"
    }
    if not templates:
        return

    npc_bags = InventoryBag.objects.filter(
        player=player,
    ).exclude(
        character_id=player.username,
    )

    with transaction.atomic():
        for bag in npc_bags:
            tmpl = templates.get(bag.character_id)
            if not tmpl:
                continue

            for tmpl_slot in tmpl.slots.select_related("item").all():
                if not tmpl_slot.item:
                    continue

                existing_by_item = bag.slots.filter(
                    item_id=tmpl_slot.item_id,
                    item_amount__gt=0,
                ).first()

                if existing_by_item:
                    deficit = tmpl_slot.item_amount - existing_by_item.item_amount
                    if deficit > 0:
                        existing_by_item.item_amount = tmpl_slot.item_amount
                        existing_by_item.hide_in_shop = tmpl_slot.hide_in_shop
                        existing_by_item.save()
                else:
                    remaining_uses = None
                    if tmpl_slot.item.item_type == "Usable":
                        remaining_uses = tmpl_slot.item.max_use_count

                    existing_at_slot = bag.slots.filter(
                        slot_index=tmpl_slot.slot_index,
                    ).first()

                    if existing_at_slot:
                        existing_at_slot.instance_id = uuid.uuid4()
                        existing_at_slot.item_id = tmpl_slot.item_id
                        existing_at_slot.item_amount = tmpl_slot.item_amount
                        existing_at_slot.rated = tmpl_slot.rated
                        existing_at_slot.rating_price = tmpl_slot.rating_price
                        existing_at_slot.overall_score = tmpl_slot.overall_score
                        existing_at_slot.ingredient = tmpl_slot.ingredient
                        existing_at_slot.item_bag_name = f"{tmpl_slot.item.item_name}_{tmpl_slot.slot_index}"
                        existing_at_slot.cook_time = tmpl_slot.cook_time
                        existing_at_slot.dish_quality = tmpl_slot.dish_quality
                        existing_at_slot.hide_in_shop = tmpl_slot.hide_in_shop
                        existing_at_slot.remaining_uses = remaining_uses
                        existing_at_slot.save()
                    else:
                        InventorySlot.objects.create(
                            bag=bag,
                            slot_index=tmpl_slot.slot_index,
                            instance_id=uuid.uuid4(),
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
