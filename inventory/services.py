import uuid
from django.db import transaction
from .models import InventoryBag, InventorySlot, BagTemplate, BagTemplateSlot
from .storage import add_inventory_items, TOOL_TYPES


def add_template_items(bag, slot, amount=None):
    if not slot.item_id or (slot.item_amount if amount is None else amount) <= 0:
        return []
    return add_inventory_items(bag, slot.item_id, slot.item_amount if amount is None else amount,
        attributes=dict(rated=slot.rated, rating_price=slot.rating_price,
            overall_score=slot.overall_score, ingredient=slot.ingredient,
            item_bag_name=slot.item.item_name, cook_time=slot.cook_time,
            dish_quality=slot.dish_quality, hide_in_shop=slot.hide_in_shop,
            remaining_uses=slot.item.max_use_count if slot.item.item_type == "Usable" or slot.item.item_type in TOOL_TYPES else None))


@transaction.atomic
def ensure_initial_equipment(player, bag):
    """One grant per player; login also upgrades older saves without duplicating gear."""
    from items.models import Item
    from player.models import PlayerMilestoneProgress
    from game_time.models import GlobalGameTimeState
    InventoryBag.objects.select_for_update().get(pk=bag.pk)
    minute = GlobalGameTimeState.objects.values_list("world_minute", flat=True).first() or 0
    for item_id, family, marker, legacy in (
        (4114, (4114,), "initial_sword_granted", "starter_hunting_kit_claimed"),
        (4104, (4104, 4105, 4122), "initial_axe_granted", "starter_axe_claimed"),
    ):
        if PlayerMilestoneProgress.objects.filter(player=player, code=marker).exists():
            continue
        item = Item.objects.filter(pk=item_id, is_deleted=False).first()
        if item is None:
            continue
        owned = bag.slots.filter(item_id__in=family, item_amount__gt=0).exists()
        claimed = PlayerMilestoneProgress.objects.filter(player=player, code=legacy).exists()
        if not owned and not claimed:
            add_inventory_items(bag, item_id, 1, attributes=dict(item_bag_name=item.item_name,
                quality=3, remaining_uses=item.max_use_count, produced_world_minute=minute))
        PlayerMilestoneProgress.objects.create(player=player, code=marker, title="初始装备",
            source_type="initial_equipment", source_id=str(player.pk))


def init_player_bags(player):
    """从 ``player`` 模板初始化玩家自己的背包。

    NPC 库存属于 ``NpcGlobalInventory``，绝不能在这里为每个玩家复制一份。
    """
    existing = InventoryBag.objects.filter(player=player, character_id=player.username).first()
    if existing:
        ensure_initial_equipment(player, existing)
        return 0

    template = BagTemplate.objects.prefetch_related("slots__item").filter(character_id="player").first()
    if template is None:
        bag, _ = InventoryBag.objects.get_or_create(player=player, character_id=player.username)
        ensure_initial_equipment(player, bag)
        return 1

    with transaction.atomic():
        bag = InventoryBag.objects.create(
            player=player,
            character_id=player.username,
            money=template.money,
        )
        for slot in template.slots.all():
            add_template_items(bag, slot)
        ensure_initial_equipment(player, bag)

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
            from django.db.models import Sum
            owned = bag.slots.filter(item_id=tmpl_slot.item_id, item_amount__gt=0).aggregate(total=Sum("item_amount"))["total"] or 0
            deficit = tmpl_slot.item_amount - owned
            if deficit > 0:
                add_template_items(bag, tmpl_slot, deficit)
