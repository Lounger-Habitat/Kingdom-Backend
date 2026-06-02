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
    with transaction.atomic():
        for tmpl in templates:
            # character_id="player" 的模板改为玩家用户名，作为玩家自己的背包
            cid = player.username if tmpl.character_id == "player" else tmpl.character_id
            bag = InventoryBag.objects.create(
                player=player,
                character_id=cid,
                money=tmpl.money,
            )
            for slot in tmpl.slots.select_related("item").all():
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
                    item_bag_name=slot.item.item_name if slot.item else "",
                    cook_time=slot.cook_time,
                    dish_quality=slot.dish_quality,
                )
            created += 1

    return created
