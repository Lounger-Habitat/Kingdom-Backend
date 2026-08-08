import uuid
import logging
from django.db import transaction
from django.utils import timezone
from account.models import GameAccount
from inventory.models import InventoryBag, InventorySlot, BagTemplate, BagTemplateSlot
from .models import PlayerState, PlayerRecipe, PlayerTitle, DefaultRecipe

NATURAL_ACTION_POINT_CAP = 200
ACTION_POINT_RECOVERY_SECONDS = 180


def recover_action_points(player_state, now=None):
    """按现实时间恢复行动力；自然恢复上限固定为 200。"""
    now = now or timezone.now()
    if player_state.action_points >= NATURAL_ACTION_POINT_CAP:
        player_state.action_points_recovered_at = now
        player_state.save(update_fields=["action_points_recovered_at"])
        return player_state.action_points
    elapsed = max(0, int((now - player_state.action_points_recovered_at).total_seconds()))
    recovered = elapsed // ACTION_POINT_RECOVERY_SECONDS
    if recovered:
        player_state.action_points = min(NATURAL_ACTION_POINT_CAP, player_state.action_points + recovered)
        player_state.action_points_recovered_at += timezone.timedelta(seconds=recovered * ACTION_POINT_RECOVERY_SECONDS)
        player_state.save(update_fields=["action_points", "action_points_recovered_at"])
    return player_state.action_points


def spend_action_points(player_state, amount):
    recover_action_points(player_state)
    if player_state.action_points < amount:
        return False
    player_state.action_points -= amount
    player_state.action_points_recovered_at = timezone.now()
    player_state.save(update_fields=["action_points", "action_points_recovered_at"])
    return True

logger = logging.getLogger(__name__)

# PlayerState 时间字段默认值（重置到第一天：2026年3月8日 07:00）
_DEFAULTS = {
    "action_points": NATURAL_ACTION_POINT_CAP,
}


def reset_player_data(player_id: str, options: dict) -> dict:
    """重置玩家数据到模板状态。

    options 键: inventory, money, recipes, action_points, titles, game_day
    返回重置统计 dict。
    """
    stats = {}
    account = GameAccount.objects.filter(pk=player_id).first()
    if not account:
        raise ValueError(f"玩家 {player_id} 不存在")

    player_state, _ = PlayerState.objects.get_or_create(player_id=str(player_id))

    with transaction.atomic():
        if options.get("inventory"):
            stats["inventory"] = _reset_inventory(account)

        if options.get("money"):
            stats["money"] = _reset_money(account)

        if options.get("recipes"):
            stats["recipes"] = _reset_recipes(player_state)

        if options.get("action_points"):
            _reset_action_points(player_state)
            stats["action_points"] = NATURAL_ACTION_POINT_CAP

        if options.get("titles"):
            stats["titles"] = _reset_titles(player_state)

        if options.get("game_day"):
            _reset_game_day(player_state)
            stats["game_day"] = True

    return stats


def _reset_inventory(account):
    """清空该玩家所有背包的 slot，从 BagTemplate 重新复制。"""
    bags = InventoryBag.objects.filter(player=account)
    InventorySlot.objects.filter(bag__in=bags).delete()

    templates = BagTemplate.objects.prefetch_related("slots").all()
    if not templates.exists():
        return 0

    reset_count = 0
    for tmpl in templates:
        cid = account.username if tmpl.character_id == "player" else tmpl.character_id
        bag = bags.filter(character_id=cid).first()
        if not bag:
            continue
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
                item_bag_name=f"{slot.item.item_name}_{slot.slot_index}" if slot.item else "",
                cook_time=slot.cook_time,
                dish_quality=slot.dish_quality,
            )
        reset_count += 1

    return reset_count


def _reset_money(account):
    """将该玩家所有背包（含 NPC）的 money 重置为模板值。"""
    templates = {t.character_id: t.money for t in BagTemplate.objects.all()}
    player_money = templates.get("player", 0)

    bags = InventoryBag.objects.filter(player=account)
    reset_money = 0
    for bag in bags:
        cid = "player" if bag.character_id == account.username else bag.character_id
        tmpl_money = templates.get(cid, 0)
        bag.money = tmpl_money
        bag._money_change_source = "player_reset"
        bag.save(update_fields=["money"])
        if cid == "player":
            reset_money = tmpl_money

    return reset_money


def _reset_recipes(player_state):
    """删除该玩家所有 PlayerRecipe，从 DefaultRecipe 重新创建。"""
    deleted_count = player_state.recipes.count()
    player_state.recipes.all().delete()

    defaults = DefaultRecipe.objects.all()
    created = 0
    for dr in defaults:
        from recipes.models import Recipe
        catalog = Recipe.objects.filter(recipe_name=dr.recipe_name).first()
        PlayerRecipe.objects.create(
            player=player_state,
            recipe_name=dr.recipe_name,
            catalog_recipe=catalog,
        )
        created += 1

    return {"deleted": deleted_count, "created": created}


def _reset_action_points(player_state):
    """行动力重置为 100。"""
    player_state.action_points = NATURAL_ACTION_POINT_CAP
    player_state.action_points_recovered_at = timezone.now()
    player_state.save(update_fields=["action_points", "action_points_recovered_at"])


def _reset_titles(player_state):
    """删除该玩家所有称号。"""
    count = player_state.titles.count()
    player_state.titles.all().delete()
    return count


def _reset_game_day(player_state):
    """游戏时间重置到第一天。"""
    for field, value in _DEFAULTS.items():
        setattr(player_state, field, value)
    player_state.save()
