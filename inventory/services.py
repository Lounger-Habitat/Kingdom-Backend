import uuid
from django.db import transaction
from .models import InventoryBag, InventorySlot, BagTemplate, BagTemplateSlot


def init_player_bags(player):
    """从 ``player`` 模板初始化玩家自己的背包。

    NPC 库存属于 ``NpcGlobalInventory``，绝不能在这里为每个玩家复制一份。
    """
    if InventoryBag.objects.filter(player=player, character_id=player.username).exists():
        return 0

    template = BagTemplate.objects.prefetch_related("slots__item").filter(character_id="player").first()
    if template is None:
        return 0

    with transaction.atomic():
        bag = InventoryBag.objects.create(
            player=player,
            character_id=player.username,
            money=template.money,
        )
        for slot in template.slots.all():
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

    # 设置富豪榜日榜第一天基准金币和总榜快照
    if template.money > 0:
        from player.models import PlayerState
        ps, _ = PlayerState.objects.get_or_create(player_id=str(player.id))
        # 榜单改为按真实时间窗口统计 ReportEvent，不再维护日初金币快照。

    return 1


def restock_npc_bags(player):
    """每天推进时，将NPC背包中被买走的预设物品补回到模板数量。

    只补 BagTemplate 中定义的物品，玩家卖给NPC的物品不受影响。
    不重置金币。
    """
    # 已废弃：NPC 库存改为 NpcGlobalInventory，旧的按玩家库存只保留作历史备份。
    # 保留此入口以兼容外部旧调用，但禁止再读取或写入旧记录。
    return

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


def restock_npc_bags_global():
    """全服统一补充NPC全局库存到模板数量（凌晨4点重置时调用）。"""
    from .models import NpcGlobalInventory, NpcGlobalInventorySlot, BagTemplate
    from .village_policy import FORMAL_VILLAGE_NPC_IDS
    from django.conf import settings

    # 获取所有绑定NPC的模板
    templates = BagTemplate.objects.prefetch_related("slots__item").filter(
        npc__isnull=False,
        seed_only=False,
    ).exclude(npc__agent_id__in=FORMAL_VILLAGE_NPC_IDS)
    if not settings.VILLAGE_TEST_ASSET_ACTIONS_ENABLED:
        templates = templates.exclude(npc__is_test_only=True)
    if not templates.exists():
        return

    for tmpl in templates:
        npc = tmpl.npc
        # 仅首次创建时复制模板金币；后续每日补货不能重置交易后的 NPC 金币。
        bag, _ = NpcGlobalInventory.objects.get_or_create(
            npc=npc,
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
                    existing.hide_in_shop = tmpl_slot.hide_in_shop
                    existing.save()
            else:
                from django.db.models import Max
                max_idx = bag.slots.aggregate(Max("slot_index"))["slot_index__max"]
                next_idx = (max_idx + 1) if max_idx is not None else 0
                remaining_uses = None
                if tmpl_slot.item.item_type == "Usable":
                    remaining_uses = tmpl_slot.item.max_use_count
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
                    remaining_uses=remaining_uses,
                )
