from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count
from django.shortcuts import render, get_object_or_404, redirect
from account.models import GameAccount
from inventory.models import InventoryBag, InventorySlot
from .models import PlayerState, PlayerRecipe, PlayerTitle
from .services import reset_player_data


@staff_member_required
def player_reset_list(request):
    """玩家重置列表页，显示所有玩家及其数据统计。"""
    qs = GameAccount.objects.annotate(bag_count=Count("inventorybag"))
    search = request.GET.get("q", "").strip()
    if search:
        qs = qs.filter(username__icontains=search)
    qs = qs.order_by("-created_at")

    # 批量获取玩家统计数据，合并到列表中
    account_ids = [a.id for a in qs]
    states = PlayerState.objects.filter(player_id__in=[str(i) for i in account_ids])
    state_map = {s.player_id: s for s in states}

    # 批量统计玩家自己背包的金币（character_id == username）
    player_bag_map = {}
    for bag in InventoryBag.objects.filter(player__in=qs):
        if bag.character_id == bag.player.username:
            player_bag_map[bag.player_id] = bag

    # 批量统计玩家自己背包的物品数量
    player_bag_ids = [b.pk for b in player_bag_map.values()]
    item_counts = (
        InventorySlot.objects
        .filter(bag__pk__in=player_bag_ids, item_id__gt=0)
        .values("bag__player_id")
        .annotate(cnt=Count("id"))
    )
    item_count_map = {row["bag__player_id"]: row["cnt"] for row in item_counts}

    player_list = []
    for account in qs:
        state = state_map.get(str(account.id))
        pb = player_bag_map.get(account.id)
        player_list.append({
            "account": account,
            "item_count": item_count_map.get(account.id, 0),
            "money": pb.money if pb else 0,
            "recipe_count": state.recipes.count() if state else 0,
            "action_points": state.action_points if state else 100,
            "title_count": state.titles.count() if state else 0,
        })

    return render(request, "admin/player/reset/player_list.html", {
        "player_list": player_list,
        "search": search,
        "title": "玩家重置",
    })


@staff_member_required
def player_reset_detail(request, player_id):
    """玩家重置详情页，展示数据统计并执行重置操作。"""
    account = get_object_or_404(GameAccount, pk=player_id)
    player_state, _ = PlayerState.objects.get_or_create(player_id=str(player_id))

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "reset":
            options = {
                "inventory": "opt_inventory" in request.POST,
                "money": "opt_money" in request.POST,
                "recipes": "opt_recipes" in request.POST,
                "action_points": "opt_action_points" in request.POST,
                "titles": "opt_titles" in request.POST,
                "game_day": "opt_game_day" in request.POST,
            }
            if not any(options.values()):
                messages.warning(request, "请至少选择一项要重置的数据。")
            else:
                try:
                    stats = reset_player_data(player_id, options)
                    parts = []
                    if "inventory" in stats:
                        parts.append(f"背包: 重置了 {stats['inventory']} 个背包的物品")
                    if "money" in stats:
                        parts.append(f"金币: 重置为 {stats['money']}")
                    if "recipes" in stats:
                        parts.append(f"菜谱: 删除 {stats['recipes']['deleted']} 个, 恢复 {stats['recipes']['created']} 个")
                    if "action_points" in stats:
                        parts.append(f"行动力: 重置为 {stats['action_points']}")
                    if "titles" in stats:
                        parts.append(f"称号: 删除 {stats['titles']} 个")
                    if "game_day" in stats:
                        parts.append("游戏天数: 重置为第 1 天")
                    messages.success(request, "重置完成！" + "；".join(parts))
                except Exception as e:
                    messages.error(request, f"重置失败: {e}")
            return redirect("player-admin-reset-detail", player_id=player_id)

    # 当前数据统计 —— 玩家背包 vs NPC 背包分开
    bags = list(InventoryBag.objects.filter(player=account))
    player_bag = None
    npc_bags = []
    for bag in bags:
        if bag.character_id == account.username:
            player_bag = bag
        else:
            npc_bags.append(bag)

    # 玩家背包统计
    player_item_count = 0
    player_money = 0
    if player_bag:
        player_item_count = InventorySlot.objects.filter(bag=player_bag, item_id__gt=0).count()
        player_money = player_bag.money

    # NPC 背包统计
    npc_stats = []
    total_npc_item_count = 0
    total_npc_money = 0
    for nb in npc_bags:
        cnt = InventorySlot.objects.filter(bag=nb, item_id__gt=0).count()
        total_npc_item_count += cnt
        total_npc_money += nb.money
        npc_stats.append({
            "character_id": nb.character_id,
            "item_count": cnt,
            "money": nb.money,
        })

    recipe_count = player_state.recipes.count()
    title_count = player_state.titles.count()

    return render(request, "admin/player/reset/reset_detail.html", {
        "account": account,
        "player_state": player_state,
        "player_item_count": player_item_count,
        "player_money": player_money,
        "npc_stats": npc_stats,
        "total_npc_item_count": total_npc_item_count,
        "total_npc_money": total_npc_money,
        "recipe_count": recipe_count,
        "title_count": title_count,
        "title": f"重置 - {account.username}",
    })
