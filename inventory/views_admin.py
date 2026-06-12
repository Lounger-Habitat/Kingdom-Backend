import uuid
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render, get_object_or_404, redirect
from django.db.models import Count
from account.models import GameAccount
from items.models import Item
from .models import InventoryBag, InventorySlot, BagTemplate, BagTemplateSlot
from .services import init_player_bags


@staff_member_required
def player_list(request):
    """第一层：玩家列表，显示所有 GameAccount 及其背包数量。"""
    qs = GameAccount.objects.annotate(bag_count=Count("inventorybag"))
    search = request.GET.get("q", "").strip()
    if search:
        qs = qs.filter(username__icontains=search)
    qs = qs.order_by("-created_at")

    return render(request, "inventory/admin/player_list.html", {
        "players": qs,
        "search": search,
        "title": "背包管理 - 玩家列表",
        "opts": GameAccount._meta,
    })


@staff_member_required
def player_bags(request, player_id):
    """第二层：指定玩家的所有背包列表。"""
    player = get_object_or_404(GameAccount, pk=player_id)

    # 从模板初始化 NPC 背包（仅在玩家没有任何背包时生效）
    if request.method == "POST" and request.POST.get("action") == "init_from_template":
        created = init_player_bags(player)
        if created > 0:
            messages.success(request, f"已从模板初始化 {created} 个背包。")
        else:
            templates = BagTemplate.objects.all()
            if not templates.exists():
                messages.warning(request, "模板表为空，请先推送背包数据。")
            else:
                # 模板存在但玩家已有背包，尝试补充缺失的 NPC 背包
                created = _init_missing_npc_bags(player)
                if created > 0:
                    messages.success(request, f"已补充 {created} 个缺失的 NPC 背包。")
                else:
                    messages.info(request, "所有 NPC 背包已存在，无需初始化。")
        return redirect("inventory-admin-player-bags", player_id=player_id)

    bags = InventoryBag.objects.filter(player=player).annotate(
        slot_count=Count("slots")
    ).order_by("character_id")

    # 玩家自己的背包：character_id == 玩家用户名
    player_bag = None
    npc_bags = []
    for bag in bags:
        if bag.character_id == player.username:
            player_bag = bag
        else:
            npc_bags.append(bag)

    # 检查模板中是否有尚未为该玩家创建的 NPC 背包
    existing_ids = {b.character_id for b in bags}
    missing_templates = BagTemplate.objects.exclude(
        character_id="player"
    ).exclude(
        character_id__in=existing_ids
    ).values_list("character_id", flat=True)

    return render(request, "inventory/admin/player_bags.html", {
        "player": player,
        "player_bag": player_bag,
        "npc_bags": npc_bags,
        "missing_templates": list(missing_templates),
        "title": f"背包管理 - {player.username} 的背包",
        "opts": InventoryBag._meta,
    })


@staff_member_required
def bag_detail(request, player_id, bag_id):
    """第三层：背包详情，显示和编辑物品列表。"""
    player = get_object_or_404(GameAccount, pk=player_id)
    bag = get_object_or_404(InventoryBag, pk=bag_id, player=player)

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "save_money":
            try:
                bag.money = int(request.POST.get("money", 0))
                bag.save()
            except (ValueError, TypeError):
                pass

        elif action == "save_slots":
            _save_slots(request, bag)

        elif action == "add_slot":
            _add_slot(request, bag)

        elif action == "delete_slot":
            _delete_slot(request, bag)

        return redirect("inventory-admin-bag-detail", player_id=player_id, bag_id=bag_id)

    # 批量获取物品名称，附加到 slot 对象上
    slots = list(bag.slots.all().order_by("slot_index"))
    item_ids = {s.item_id for s in slots if s.item_id != 0}
    if item_ids:
        name_map = dict(Item.objects.filter(item_id__in=item_ids).values_list("item_id", "item_name"))
    else:
        name_map = {}
    for slot in slots:
        slot.item_name = name_map.get(slot.item_id, "")

    return render(request, "inventory/admin/bag_detail.html", {
        "player": player,
        "bag": bag,
        "slots": slots,
        "title": f"背包管理 - {player.username} / {bag.character_id}",
        "opts": InventoryBag._meta,
    })


# ---- 辅助函数 ----

def _init_missing_npc_bags(player):
    """为已有玩家补充缺失的背包（从 BagTemplate 复制）。"""
    existing_ids = set(
        InventoryBag.objects.filter(player=player).values_list("character_id", flat=True)
    )
    templates = BagTemplate.objects.prefetch_related("slots").all()

    created = 0
    for tmpl in templates:
        # "player" 模板改为玩家用户名
        cid = player.username if tmpl.character_id == "player" else tmpl.character_id
        if cid in existing_ids:
            continue

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
                item_id=slot.item.item_id if slot.item else 0,
                item_amount=slot.item_amount,
                rated=slot.rated,
                rating_price=slot.rating_price,
                overall_score=slot.overall_score,
                ingredient=slot.ingredient,
                item_bag_name=slot.item.item_name if slot.item else "",
                cook_time=slot.cook_time,
                dish_quality=slot.dish_quality,
                hide_in_shop=slot.hide_in_shop,
            )
        created += 1
    return created


def _save_slots(request, bag):
    slot_ids = request.POST.getlist("slot_id")
    for slot_id in slot_ids:
        try:
            slot = InventorySlot.objects.get(pk=slot_id, bag=bag)
        except InventorySlot.DoesNotExist:
            continue

        prefix = f"slot_{slot_id}"
        try:
            slot.item_id = int(request.POST.get(f"{prefix}_item_id", 0))
            slot.item_amount = int(request.POST.get(f"{prefix}_item_amount", 0))
            slot.rated = request.POST.get(f"{prefix}_rated") == "on"
            slot.rating_price = int(request.POST.get(f"{prefix}_rating_price", -1))
            slot.overall_score = int(request.POST.get(f"{prefix}_overall_score", 0))
            slot.ingredient = request.POST.get(f"{prefix}_ingredient", "")
            slot.item_bag_name = request.POST.get(f"{prefix}_item_bag_name", "")
            slot.cook_time = int(request.POST.get(f"{prefix}_cook_time", 0))
            slot.dish_quality = int(request.POST.get(f"{prefix}_dish_quality", 3))
            slot.hide_in_shop = request.POST.get(f"{prefix}_hide_in_shop") == "on"
            slot.save()
        except (ValueError, TypeError):
            continue


def _add_slot(request, bag):
    max_index = bag.slots.count()
    InventorySlot.objects.create(bag=bag, slot_index=max_index)


def _delete_slot(request, bag):
    slot_id = request.POST.get("slot_id")
    if slot_id:
        InventorySlot.objects.filter(pk=slot_id, bag=bag).delete()
