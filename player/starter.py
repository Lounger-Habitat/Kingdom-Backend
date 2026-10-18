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
    from copy import deepcopy
    from village.models import VillageProfessionalContractLink, EconomicLedgerEntry
    facts = dict(source_type=source_type, order_id=str(order.pk), player_id=str(player.pk),
                 professional_contract=False, publisher_npc_id=None, completed_world_minute=None,
                 delivered_batches=[])
    if source_type == "personal_commission":
        facts.update(publisher_npc_id=str(order.publisher.agent_id), completed_world_minute=order.completed_world_minute)
        facts["delivered_batches"] = [dict(deepcopy(batch.batch_snapshot), condition_id=batch.condition_id,
            item_id=batch.item_id, quantity=batch.quantity, source_instance_id=str(batch.source_instance_id))
            for batch in order.escrow_batches.filter(status="DELIVERED").order_by("pk")]
        link = VillageProfessionalContractLink.objects.filter(commission=order).first()
        if link:
            facts.update(professional_contract=True, template_code=link.template_code, visit_day_index=link.visit_id)
    else:
        # Starter orders predate a completed-minute column; only their actual paid ledger proves the time.
        paid = EconomicLedgerEntry.objects.filter(entry_type="STARTER_DELIVERY", changes__order_id=str(order.pk),
            changes__STARTER_ESCROW__money__lt=0).order_by("world_minute", "pk").first()
        facts["completed_world_minute"] = paid.world_minute if paid else None
        facts["publisher_account_code"] = order.batch.fund_id
        from inventory.components import BATCH_COPY_FIELDS
        for delivery in order.deliveries.filter(status="RESERVED").select_related("slot").order_by("pk"):
            attrs = {key: deepcopy(getattr(delivery.slot, key)) for key in BATCH_COPY_FIELDS}
            if attrs["source_request_id"] is not None:
                attrs["source_request_id"] = str(attrs["source_request_id"])
            facts["delivered_batches"].append(dict(attrs, item_id=delivery.slot.item_id, quantity=delivery.quantity,
                source_instance_id=str(delivery.source_instance_id)))
    return award_skill(player, source_type=source_type, source_id=str(order.pk), skill_code="business",
        xp=BUSINESS_ORDER_XP, milestone=dict(code="first_supply", title=GOAL_MAP["first_supply"][0], xp=0), facts=facts)


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
