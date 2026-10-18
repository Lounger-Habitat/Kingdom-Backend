"""Five personal introductory goals derived from authoritative successful actions."""
from django.db import transaction

from .models import PlayerGrowthState, PlayerMilestoneProgress, PlayerSkillProgress
from .progression import award_skill, ensure_growth
from .progression_rules import BUSINESS_ORDER_XP, level_for_xp
from village.models import StarterSupplyOrder, VillagePersonalCommission
from village.starter_supply import supply_overview

GOALS = (
    ("first_gathering", "完成第一次采集", "到森林边缘采集食材。", "FOREST_EDGE"),
    ("first_supply", "完成一次供货或出售", "交付真实需求订单，或到杂货店出售合格物品。", "GENERAL_STORE"),
    ("first_gathering_tool", "准备基础采集工具包", "到杂货店购买采集工具包。", "GENERAL_STORE"),
    ("gathering_level_2", "采集技能达到 2 级", "继续进行实际采集，累计 80 点经验。", "FOREST_EDGE"),
    ("first_targeted_gathering", "完成一次类别采集", "达到采集 2 级后，携带工具包按食材类别采集。", "FOREST_EDGE"),
)
GOAL_MAP = {code: (title, description, location) for code, title, description, location in GOALS}


class StarterError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def starter_overview(player, bag, minute, read_only=False):
    """Pure projection; a recommended goal is never persisted over a choice."""
    state = PlayerGrowthState.objects.filter(player=player).first()
    completed = set(PlayerMilestoneProgress.objects.filter(player=player).values_list("code", flat=True))
    tracked = state.tracked_goal_code if state else ""
    if not tracked:
        tracked = next((code for code, *_ in GOALS if code not in completed), "")
    return dict(revision=state.revision if state else 0, tracked_goal_code=tracked,
        goals=[dict(code=code, title=title, description=description, completed=code in completed,
                    location_code=location) for code, title, description, location in GOALS],
        supply=supply_overview(player, bag, minute, read_only=read_only))


@transaction.atomic
def track_goal(player, goal_code):
    if not isinstance(goal_code, str) or goal_code not in GOAL_MAP:
        raise StarterError("INVALID_PARAMS", "请选择一个已开放的入门目标。")
    state = ensure_growth(player)
    state.tracked_goal_code = goal_code
    state.revision += 1
    state.save(update_fields=["tracked_goal_code", "revision"])
    return dict(tracked_goal_code=goal_code)


def _record_milestone(player, code, action, rid):
    title = GOAL_MAP[code][0]
    row, created = PlayerMilestoneProgress.objects.get_or_create(player=player, code=code,
        defaults=dict(title=title, source_type=action, source_id=str(rid), rules_version=1))
    return dict(code=row.code, title=row.title) if created else None


@transaction.atomic
def record_order_completion(player, source_type, order_id):
    """Shared by item delivery and authoritative timed-labor completion."""
    if source_type == "starter_supply":
        order = StarterSupplyOrder.objects.filter(pk=order_id, actor_player=player, status="COMPLETED").first()
    elif source_type == "personal_commission":
        order = VillagePersonalCommission.objects.filter(pk=order_id, actor_type="PLAYER",
            actor_id=str(player.pk), status="COMPLETED").first()
    else:
        raise StarterError("INVALID_PARAMS", "经营经验来源无效。")
    if not order:
        raise StarterError("ORDER_NOT_COMPLETED", "订单尚未由该玩家完成。")
    return award_skill(player, source_type=source_type, source_id=str(order.pk), skill_code="business",
        xp=BUSINESS_ORDER_XP, milestone=dict(code="first_supply", title=GOAL_MAP["first_supply"][0], xp=0))


@transaction.atomic
def post_action_progress(player, bag, action, params, result, rid, minute):
    """Called once inside the original successful action savepoint, before receipt.

    Result flags are produced by trusted services. For repeatable paid orders,
    verify the durable final order too and award by its stable identity.
    """
    if action == "overview":
        return {}
    ensure_growth(player)
    milestones, rewards = [], []
    code = None
    if action == "collect" and result.get("collected_items"):
        if params.get("category") and params.get("tool_instance_id"):
            code = "first_targeted_gathering"
    elif action == "buy_store" and result.get("item_id") == 4101 and result.get("purchased_batches"):
        code = "first_gathering_tool"
    elif action == "sell_store" and result.get("quantity", 0) > 0 and result.get("gold_amount", 0) > 0:
        code = "first_supply"
    if code:
        new = _record_milestone(player, code, action, rid)
        if new:
            milestones.append(new)
    source = None
    if action == "supply_deliver" and result.get("status") == "COMPLETED":
        order = StarterSupplyOrder.objects.filter(pk=result.get("order_id"), actor_player=player, status="COMPLETED").first()
        if order:
            source = ("starter_supply", str(order.pk))
    elif action == "commission_contribute" and result.get("status") == "COMPLETED":
        order = VillagePersonalCommission.objects.filter(pk=params.get("commission_id"), actor_type="PLAYER",
            actor_id=str(player.pk), status="COMPLETED").first()
        if order:
            source = ("personal_commission", str(order.pk))
    if source:
        reward = record_order_completion(player, source[0], source[1])
        rewards.append(reward)
        milestones.extend(reward["milestones"])
    xp = PlayerSkillProgress.objects.filter(player=player, skill_code="gathering").values_list("total_xp", flat=True).first() or 0
    if level_for_xp(xp) >= 2:
        new = _record_milestone(player, "gathering_level_2", action, rid)
        if new:
            milestones.append(new)
    # Even a sale or tool purchase with no new milestone changes the starter
    # inventory view. Its snapshot must supersede an older saved action receipt.
    state = PlayerGrowthState.objects.select_for_update().get(player=player)
    state.revision += 1
    state.save(update_fields=["revision"])
    return dict(growth_rewards=rewards, starter_milestones=milestones)
