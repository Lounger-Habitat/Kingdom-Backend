"""Chinese environment rules and capability-specific action rules."""

from .dialogue_service import DialogueService
from .models import VillageActionDurationConfig
from .phase2_services import ACTION_DELTA_ACTIVITIES, NEEDS_SCALE, needs_rules
from . import phase3_catalog as economy
from .runtime_services import CommandService


NEED_LABELS = {"hunger": "饥饿", "energy": "精力", "health": "健康", "social": "社交需求", "stress": "压力"}


def _effects(values):
    return "、".join(f"{NEED_LABELS[name]}{value:+g}" for name, value in values.items() if value)


def build_agent_context(actions):
    """No identity, current state, private facts, or tool parameter schemas."""
    actions = set(actions) - {"SOCIALIZE"}
    needs = needs_rules()
    high = needs["bands"]["higher_is_worse"]
    low = needs["bands"]["lower_is_worse"]
    common = [
        "以下是适用于你的环境规则；可调用指令和参数以 specification 为准，当前状态通过已开放的查询指令获取。",
        "时间和行动时长以世界分钟计，导航超时使用现实秒。提交行动不代表行动完成，请以环境返回的执行结果为准。",
        f"五种需求值的范围都是{needs['scale']['minimum']}到{needs['scale']['maximum']}。饥饿、社交需求和压力越低越好；精力和健康越高越好。社交需求表示需要社交的程度，不代表社交能力。",
        f"饥饿、社交需求和压力达到{high['high_at']}时偏高，达到{high['critical_at']}时危急；精力和健康不高于{low['low_at']}时偏低，不高于{low['critical_at']}时危急。",
        f"世界时间每经过30分钟，基础变化为{_effects(needs['base_effects_per_30_minutes'])}。动作另按实际持续时间结算行为变化；以下活动总效果已包含基础变化，不要重复叠加。",
        "需求数值由环境计算，不能通过行动参数直接修改。普通行动受忙碌状态限制，只读查询不会占用行动队列或结算资产。",
    ]
    percentages = "、".join(f"{value}%" for value in needs["socialize_daily_effect_percent"])
    common.extend([
        f"对话是环境规则，不依赖具体行动指令：双方需在允许距离内；离开范围会结束对话。双方都发言且产生有效时长才获得社交收益。"
        f"同一对参与者当天第一次、第二次、第三次及以后的收益比例依次为{percentages}。"
        f"每次对话最长{DialogueService.MAX_DURATION_MINUTES}世界分钟，且不跨日。",
        f"有效对话首次每30分钟的需求总变化为{_effects(needs['effects_per_30_minutes']['SOCIALIZE'])}。",
    ])
    low_health_allowed = CommandService.LOW_HEALTH_ACTIONS | {"WAKE_UP"}
    low_health_actions = sorted(actions & low_health_allowed)
    if actions - low_health_allowed:
        common.append(
            f"健康不高于{CommandService.NEED_GATE_LIMITS['health'] / NEEDS_SCALE:g}时，"
            f"当前指令中仅允许{'、'.join(low_health_actions) or '无'}；其他行动需先恢复健康。"
        )
    common.append(
        f"整天严重饥饿会使每日健康变化{needs['daily_health']['severe_hunger_all_day']:+g}，"
        "严重饥饿期间还会额外增加压力。"
    )
    if {"EAT", "SLEEP"}.issubset(actions):
        common.append(
            f"当天至少成功进食一次、睡眠累计达到{needs['daily_health']['minimum_sleep_minutes']}世界分钟且未受伤，"
            f"每日健康变化{needs['daily_health']['ate_and_slept_at_least_360_minutes_and_not_injured']:+g}。"
        )

    rules = []
    durations = VillageActionDurationConfig.default_durations()
    timeouts = VillageActionDurationConfig.default_navigation_timeouts()
    for action, duration, timeout in VillageActionDurationConfig.objects.filter(
        action_name__in=actions
    ).values_list("action_name", "duration_minutes", "navigation_timeout_seconds"):
        if duration:
            durations[action] = duration
        if timeout:
            timeouts[action] = timeout

    for action in sorted(actions):
        if action in CommandService.WORK_ACTIONS:
            limits = CommandService.NEED_GATE_LIMITS
            rules.append(
                f"{action}：饥饿必须低于{limits['hunger'] / NEEDS_SCALE:g}、"
                f"精力高于{limits['energy'] / NEEDS_SCALE:g}、健康高于{limits['health'] / NEEDS_SCALE:g}。"
            )
        activity = ACTION_DELTA_ACTIVITIES.get(action, action)
        if activity in needs["effects_per_30_minutes"]:
            rules.append(f"{action}：每持续30分钟的需求总变化为{_effects(needs['effects_per_30_minutes'][activity])}。")
        if action in timeouts:
            rules.append(f"{action}：以实际寻路到达为完成条件；导航超过{timeouts[action]:g}现实秒会超时失败。")
        elif action in CommandService.VARIABLE_DURATION_ACTIONS:
            rules.append(
                f"{action}：可指定正整数世界分钟时长；省略时使用后端配置的{durations[action]}世界分钟。"
            )
        elif action in durations and action not in CommandService.READ_QUERY_ACTIONS:
            rules.append(f"{action}：当前环境配置的耗时为{durations[action]}世界分钟，实际耗时以执行结果为准。")

    if "EAT" in actions:
        rules.append(
            f"EAT：个人食物必须来自自己的真实库存并会被消耗；公共餐食需到餐厅且受开放状态和每日限额限制。"
            f"进食完成时额外获得{_effects(needs['eat_completion_effect'])}，时间流逝的基础变化另行计算。"
        )
    if "REST" in actions:
        rules.append("REST：只能在自己的住所或开放的餐厅休息。")
    if "SLEEP" in actions:
        rules.append("SLEEP：只能在自己的住所睡眠；可以使用WAKE_UP提前结束当前睡眠，只结算实际已睡的时间。")
    if "WORK_PLACEHOLDER" in actions:
        rules.append("WORK_PLACEHOLDER：只能在自己的工作地点执行，且不能跨越世界日期边界。")
    if actions & {"PLANT", "HARVEST"}:
        rules.append(
            f"农田：播种需要空地块、玉米种子和可用农具；玉米经过{economy.CORN_GROWTH_MINUTES}世界分钟成熟。"
            f"成熟后可收获{economy.CORN_YIELD_MIN}到{economy.CORN_YIELD_MAX}份玉米，并至少返还{economy.CORN_GUARANTEED_SEED_RETURN}份种子；播种和收获都消耗农具使用次数。"
        )
    if "HUNT" in actions:
        rules.append(f"HUNT：需到猎场入口并消耗可用猎具的使用次数；产出{economy.HUNT_MEAT_YIELD_MIN}到{economy.HUNT_MEAT_YIELD_MAX}份肉，保鲜期为{economy.HUNT_MEAT_SPOILAGE_MINUTES}世界分钟。")
    if "COOK" in actions:
        rules.append(f"COOK：需到餐厅，使用允许的配方并消耗真实材料；当前配方编号为{'、'.join(map(str, sorted(economy.RECIPE_WHITELIST)))}，菜品保鲜期为{economy.DISH_SPOILAGE_MINUTES}世界分钟。")
    if actions & {"IMPORT_GOODS", "EXPORT_GOODS"}:
        rules.append(f"商队交易：需到外部商路；进出口受物品白名单、库存和资金限制，商队冷却为{economy.CARAVAN_COOLDOWN_DAYS}天。当前商品和商队状态通过市场查询获取。")
    if actions & {"LIST_STORE_ITEM", "UPDATE_STORE_PRICE", "DELIST_STORE_ITEM"}:
        rules.append("商店经营：只能管理自己经营的商店。上架会将个人真实库存转入商店，下架会取回库存；上架价格使用CUSTOM策略，单价必须为正整数。")
    if actions & {"BUY_FROM_STORE", "INSPECT_STORE_INVENTORY"}:
        rules.append("商店访问：查看库存和购买前需到商店所在地点；GENERAL_STORE位于GENERAL_STORE，RESTAURANT_STORE位于RESTAURANT。购买按上架价格结算，并检查库存和资金。")
    if actions & {"BUY_FROM_MARKETPLACE", "SELL_ON_MARKETPLACE", "DELIST_FROM_MARKETPLACE"}:
        rules.append("全服市场交易：需到MARKET_STREET；只能上架自己的真实库存、下架自己的挂单，购买会检查库存和资金。")
    if actions & {"PROPOSE_TRADE", "RESPOND_TRADE", "CANCEL_TRADE_OFFER"}:
        rules.append("定向交易：报价必须明确物品、数量和金额；接受时检查报价状态、双方地点、库存和资金。只有发起方能取消仍待处理的报价。")
    if actions & {"CREATE_ORDER", "CANCEL_ORDER", "START_DELIVERY"}:
        rules.append("订单交易：只能取消自己的开放订单；配送必须对应已匹配订单，并满足交付地点和资产条件。")
    if actions & {"RATE_DISH", "PUBLISH_PRICE_INDEX", "AUDIT_PUBLIC_FUND"}:
        rules.append("公共经济事务：需到RECORD_HALL。评分针对真实且可评分的菜品；价格指数和公款审计受当日状态限制，结果由环境记录。")
    if actions & {"CREATE_COMMITMENT", "CANCEL_COMMITMENT", "PERFORM_COMMITMENT"}:
        rules.append("承诺：交付、还款、捐赠、共同工作和见面均需满足参与者、目标、地点和期限条件；履约和违约可能改变关系。")
    if actions & CommandService.STORY_ACTIONS:
        rules.append("剧情行动：投票受开放会议和候选方案限制，剧情行动受当前阶段、方案、角色权限、地点、已知事实和资源限制；进度变化后应重新查询，不要假定旧方案仍可执行。")

    return {"common_rules": common, "npc_rules": rules}
