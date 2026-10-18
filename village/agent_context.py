"""Chinese environment rules and capability-specific action rules."""

from django.db.models import Q

from .dialogue_service import DialogueService
from .models import VillageActionDurationConfig, VillageLocationDefinition, VillageStore, VillageSimulationConfig
from .phase2_services import ACTION_DELTA_ACTIVITIES, NEEDS_SCALE, needs_rules
from . import phase3_catalog as economy
from .runtime_services import CommandService


NEED_LABELS = {"hunger": "饥饿", "energy": "精力", "health": "健康", "social": "社交需求", "stress": "压力"}


def _effects(values):
    return "、".join(f"{NEED_LABELS[name]}{value:+g}" for name, value in values.items() if value)


def _location_directory(profile):
    """Location codes visible to one NPC, mirroring the discover projection.

    Public locations without story-state requirements plus the NPC's own
    home/work/social codes. Locations are common knowledge, not private
    facts: agents need the codes to plan MOVE.
    """
    profile_codes = set()
    if profile is not None:
        profile_codes = {
            profile.home_location_code,
            profile.work_location_code,
            profile.social_location_code,
        }
    rows = (
        VillageLocationDefinition.objects.filter(is_active=True)
        .filter(
            Q(is_public=True, required_story_states=[])
            | Q(location_code__in=profile_codes)
        )
        .order_by("location_code")
    )
    return [(row.location_code, row.display_name) for row in rows]


def build_agent_context(actions, profile=None):
    """Environment rules for one NPC, plus its navigation location codes.

    Includes current identity and navigation context, without private facts or tool parameter schemas.
    Location codes (home/work/social) are navigation context, not private
    facts: they are part of the same discover projection that publishes
    ``npc_profile`` and ``locations``, and agents need them to plan MOVE.
    """
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
    if "INSPECT_NEARBY" in actions:
        common.append("INSPECT_NEARBY按场景中实际距离观察周围10米，包含人物、树木/树桩、动物、已登记设施和农田，带position、target_ref、状态和活动。categories筛选people/trees/animals/facilities，limit默认30、最多100；truncated表示未列完，sources中false表示该类信息未知，不代表没有。玩家仅包含在线、已初始化且参与村庄的角色；activity是公开状态，movement_state区分行走和奔跑，is_in_dialogue表示是否正在对话。普通变化经延迟合并以perception.updated通知，经过者可能已离开；是否回应由你决定，通知不会取消正在进行的动作。同一action.id只重放原观察，刷新使用新id；不必连续轮询。")
    if "MOVE" in actions:
        common.append('MOVE四选一：地点location_code、角色target_npc_id、观察所得target_ref，或世界坐标position（x/y/z，米）。例如{"position":{"x":12,"y":0,"z":8}}，或{"target_ref":"npc:10012"}。只前往可达位置，返回实际落点；对象消失或树木代次变化须重新观察。靠近对象不代表已经交谈、采集或取得经营权限。')
    if profile is not None:
        common.append(f"你的 NPC 编号：{profile.npc.agent_id}；姓名：{profile.display_name}。消息中的 actor_npc_id 与此编号相同表示你自己的事件。")
        location_parts = []
        for label, code in (
            ("家", profile.home_location_code),
            ("工作地点", profile.work_location_code),
            ("常去地点", profile.social_location_code),
        ):
            if code:
                location_parts.append(f"{label}：{code}")
        if location_parts:
            common.append(
                "你的地点代码：" + "；".join(location_parts) + "。使用 MOVE 前往地点时填写这些地点代码。"
            )
    directory = _location_directory(profile)
    if directory:
        common.append(
            "村庄地点目录：" + "；".join(f"{code}={name}" for code, name in directory)
            + "。MOVE 使用这些地点代码。"
        )
    percentages = "、".join(f"{value}%" for value in needs["socialize_daily_effect_percent"])
    common.extend([
        "对话通过event消息中的speech发送：payload.type=event、payload.content.name=speech；"
        "发言数据放在payload.content.data，包含唯一id、text、conversation.id及scope.recipient。"
        "使用现行speech会话协议续聊和结束，环境负责创建会话、转发、距离校验和社交结算。"
        "公开发言使用scope.mode=public，只广播，不建立双人对话或计双人社交收益。",
        f"对话是环境规则，不依赖具体行动指令：双方距离必须在{DialogueService.MAX_CHAT_DISTANCE_METERS}米以内才能开始或继续对话，"
        f"超出会被环境拒绝，离开范围会结束对话。"
        f"开口前先 INSPECT_NEARBY 看 distance 字段——注意'附近'半径（10 米）大于对话范围，"
        f"distance 超过{DialogueService.MAX_CHAT_DISTANCE_METERS}米的人必须先 MOVE 靠近再开口。"
        f"收到 conversation_failed（距离原因）时，只允许就近 MOVE 一次并重试一次，仍失败则记录原因并停止，不循环追逐。"
        f"单次发言不能为空且不能超过{DialogueService.MAX_TEXT_LENGTH}个字符；超出时该句话不会显示或发送，若是首句话，对话不会开始。"
        f"双方都发言且产生有效时长才获得社交收益。"
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
            f"当天至少成功进食一次、单段连续睡眠达到{needs['daily_health']['minimum_continuous_sleep_minutes']}世界分钟且未受伤，"
            f"每日健康变化{needs['daily_health']['ate_and_slept_at_least_360_minutes_and_not_injured']:+g}。"
        )

    rules = []
    if "PUBLISH_PERSONAL_COMMISSION" in actions:
        rules.extend([
            "个人委托由你根据实际需求主动发布，不会自动生成。先查询自己的库存、可用金币和已发布但尚未完成的委托，避免重复采购。",
            "PUBLISH_PERSONAL_COMMISSION：发布时预留全部金币报酬；发布者由你的身份确定。一个或多个阶段按顺序开放，同阶段全部条件满足才进入下一阶段；阶段地点未配置时继承委托默认地点。",
            "个人委托独立于商店、交易和村庄公共事务。个人委托仅由玩家认领并完成，每份同时只有一名玩家承接；NPC只能发布、查询和撤回尚未认领的自己的委托。发布后不能修改条件、报酬或期限。",
            "交付物品和金币先托管，全部阶段完成后转给发布者并把报酬付给承接者；发布者只能撤回未认领委托。放弃或到期会关闭委托并退回托管资产，劳动不退，保鲜期不刷新。",
            "当餐急需先考虑现有采购、交易或换菜；玩家委托可用于后续备料。即使同一阶段有多条条件，也须全部阶段完成后才能使用托管食材。已认领委托仍须按原承诺履行；自行买到原料不改变玩家的交付条件和报酬。",
        ])
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
        if activity in needs["effects_per_30_minutes"] and action != "SLEEP":
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
    if "BUY_FROM_STORE" in actions:
        rules.append("健康不高于30时仍可BUY_FROM_STORE购买可食用成品或领取公共餐，不能购买原料、种子、工具等非成品食物；仍须到店并满足库存、金币和限额条件。公共餐先零元领取，再EAT。")
    if "INSPECT_LIVING_OPTIONS" in actions:
        rules.append("缺饭或需要休息时可INSPECT_LIVING_OPTIONS远程查看公开生活资源及本人限制；摘要不含listing_id和批次，购买前仍须到店INSPECT_STORE_INVENTORY。available是资源条件快照，不代表已到场、空闲或已预留，最终以执行结果为准。")
    if "REST" in actions:
        rules.append("REST：只能在自己的住所或开放的餐厅休息。")
    if "SLEEP" in actions:
        sleep = needs["sleep"]
        rules.append(
            f"SLEEP：只能在自己的住所睡眠；连续睡眠前{sleep['full_effect_minutes']}分钟按平方曲线逐渐增加恢复，"
            f"连续满{sleep['full_effect_minutes']}分钟达到完整效果；之后至{sleep['no_gain_after_minutes']}分钟仅再增加10%，"
            "再继续睡不增加恢复。相邻睡眠间隔不超过"
            f"{sleep['continuation_gap_minutes']}世界分钟仍视为同一段睡眠；23:00到次日04:00完全不睡会使健康-5。"
            "可以使用WAKE_UP提前结束当前睡眠，只结算实际已睡的时间。"
        )
    if "WORK_PLACEHOLDER" in actions:
        rules.append("WORK_PLACEHOLDER：只能在自己的工作地点执行，且不能跨越世界日期边界。")
    if actions & {"PLANT", "HARVEST"}:
        rules.append(
            "农田：农田地点代码是 BILL_FARM；播种和收获必须先 MOVE 到 BILL_FARM。"
            "plot_code 从 INSPECT_FARM_STATE 返回的地块中选择；靠近某块农田可用 MOVE 的 target_ref=plot:地块代码，但耕作仍需满足农田地点和权限规则。"
            "只有空地可以播种，只有成熟地块可以收获；播种需要对应种子和可用农具，"
            "播种和收获都消耗农具使用次数。调用 INSPECT_FARM_STATE 获取当前地块状态；"
            "查询结果中的 path_distance 是从你当前位置到田边可达作业位置的导航路径距离（米），"
            "按距离从近到远排列；reachable=true 表示可达，false 表示不可达，null 表示导航信息未知，"
            "path_distance=null 不表示零距离。处理多块田地时，在满足播种或收获条件的可达田地中，"
            "优先选择 path_distance 最小的一块；每完成一块后重新调用 INSPECT_FARM_STATE，"
            "根据当前位置选择下一块，不能沿用移动前的距离顺序。"
            "调用 INSPECT_INVENTORY 确认实际持有的种子、可收获物、产量和种植时长。"
        )
    if "HUNT" in actions:
        rules.append("HUNT使用HUNT_ANIMALS：从猎场入口追击真实鸡/猪/牛，成功击杀后获得对应待处理猎物1件；数据库时长是狩猎期限，到时不自动发奖。PROCESS_GAME在屠宰棚每次处理1件：鸡→鸡肉3+羽毛1；猪→里脊1+排骨1+五花肉2+粗皮革1；牛→牛肉6+粗皮革2。两动作各耗1次猎具，时长只读数据库；肉与原猎物共用实际击杀起2880分钟鲜期。库存查询提供当日猎次、下一猎额外精力、可处理实例及阻碍；取消已开始的狩猎仍计次。肉可向Lucy报价，副产品可向Nora杂货店报价；采购、售卖和休息自主安排。")
    if "COOK" in actions:
        rules.append(f"COOK：需先 MOVE 到 RESTAURANT，使用允许的配方并消耗真实材料；配方编号、产量和材料清单查GET_RULE.cooking_recipes，个人可用材料查INSPECT_INVENTORY，菜品保鲜期为{economy.DISH_SPOILAGE_MINUTES}世界分钟。缺料会在开工前拒绝；按missing_items补齐本人可用材料后再重试该配方，未补齐时应采购、换配方或等待补货，不重复烹饪。")
    if actions & {"IMPORT_GOODS", "EXPORT_GOODS"}:
        rules.append("商队每日到访窗口为11:00–15:00，路线未开通不保证到访；先用INSPECT_MARKET_STATE查询caravan地点与时间，再MOVE；items的import_available_amount/import_unit_price是当趟进货余量与单价，export_unit_price是当趟出口收购单价；INSPECT_CARAVAN_REQUESTS单独返回完整补货目录requestable_items、requested=true登记标记、目标到访时间和配置进口总量，不能当作现货；CONTACT_CARAVAN提交完整item_ids并替换目标批次的旧登记。断桥使用 EMERGENCY_TRADE_POST，修好后后续到访使用 TRADE_ROAD，当趟以查询为准。CONTACT_CARAVAN 在杂货铺提交 item_ids，11点前完成针对本次、否则针对次日，合法需求登记100%成功；首次应急路线由Nora在允许地点个人付费开通，不要求森林侦察或补给。进口按配置总量配货并受本趟余货限制，出口受caravan.export_remaining_amount跨品种共享收购额度限制、收入归个人但保留村庄安全库存；商品目录和浮动报价每趟固定，后台变更从后续批次生效。进货仅扣个人可用金币，货进个人背包，不支持store_code；需要售卖时到自己的商店用LIST_STORE_ITEM、stock_source=PERSONAL自主上架。INSPECT_INVENTORY查看个人背包及自己商店的store_finances.unlisted_stock，INSPECT_STORE_INVENTORY查看指定商店的公开在售商品；提款须保留经营储备。")
    if actions & {"LIST_STORE_ITEM", "UPDATE_STORE_PRICE", "DELIST_STORE_ITEM"}:
        rules.append("商店经营：只能管理自己经营的商店。上架可将个人真实库存转入商店，stock_source=STORE可从店内未上架库存上架；下架只停止售卖，物品仍在商店。Nora店保留自由定价；Lucy饭店普通菜用ORIGINAL，后台按具体菜的默认价值或有效点评价定价，禁止CUSTOM。饭店必须传food_instance_id选择个人或店内未上架批次。")
    if "WITHDRAW_STORE_FUNDS" in actions:
        rules.append("顾客从你的商店购买后，你会收到ECONOMY_BUY_FROM_STORE事件（不是SALE），含store_code、listing_id、amount、store_net_amount、remaining_amount和world_time；公共餐领取也报告实际补贴入店款。它是成交事实和重新判断的信号，不自动替你补货。旧菜单快照收到销售通知后需核实；原始菜谱能做不等于货架现货。")
        rules.append("WITHDRAW_STORE_FUNDS：到自己商店，指定store_code和amount将店款提到个人钱包；查询可提款额。Lucy饭店储备0，个人自主采购做菜，不开放注资或商店采购；Nora仍按自己的经营储备执行。")
    if "INSPECT_INVENTORY" in actions:
        rules.append("库存按物品类型返回行动所需字段。sale.unit_price 是当前向 sale.target 出售每件可获得的金币；sale.allowed=false 时不可出售，reason说明原因。EAT须同时传source=PERSONAL_INVENTORY和个人可用熟食的food_instance_id。")
    if "INSPECT_RESTAURANT_STATE" in actions:
        rules.append("INSPECT_RESTAURANT_STATE只查询餐厅营业状态、在售菜单及本人的经营资料，不返回菜谱。public_meal_availability返回三种公共餐的补贴、实际公款余额、可制作/上架的真实食材和成品、每日剩余份额及阻碍。公共餐无需授权，没有预算和授权期限前置条件。该摘要不批准或预留公款，执行时仍复核本人状态、地点和最新钱货。")
    if "REQUEST_DISH_REVIEW" in actions:
        rules.append("Lucy制作产物先归个人，可留餐自食再上架。到RECORD_HALL用REQUEST_DISH_REVIEW(food_instance_id)提交具体1份给Mark；待评预留180分钟，不可重复吃卖，可CANCEL_DISH_REVIEW(review_id)。未评可按默认价值卖，评后按点评价卖。配方及产量查GET_RULE.cooking_recipes，再对照INSPECT_INVENTORY的可用量判断缺料；cook_time_seconds是工艺随机秒数，不是工作游戏分钟。")
    if "RATE_DISH" in actions:
        rules.append("Mark先INSPECT_RATEABLE_DISHES读取Lucy实际送来的review_id及完整菜品快照，用本人LLM判断色color、香aroma、味taste、摆盘presentation四项1–10分及comment。RATE_DISH提交review_id、scores对象、comment，不传总分/价格；后台按加权总分与默认价值计算价格。历史资料不完整须在评语说明，不编造原料。")
    if actions & {"BUY_FROM_STORE", "LIST_STORE_ITEM"}:
        rules.append("公共餐由Lucy实际制作后在饭店用ORIGINAL加public_meal_code=C1/C2/R1上架，无需授权，顾客价格0。按真实领取向饭店补贴9/12/9金；三类共享每人每个游戏日1份、全村每天10份，NPC（包括Lucy）和参与村庄生活的玩家共同占用总额度，游戏日零点重置。缺公款整笔失败。先BUY_FROM_STORE领取，再EAT个人食物，吃饭不再次发补贴。")
    if "DEPOSIT_STORE_FUNDS" in actions:
        rules.append("DEPOSIT_STORE_FUNDS：在自己的商店将个人可用资金存入店铺，不收费、不计作销售收入；WITHDRAW_STORE_FUNDS反向提款时须保留经营储备。")
    if "WITHDRAW_STORE_ITEM" in actions:
        rules.append("WITHDRAW_STORE_ITEM：在自己的商店取出未上架、未占用且未过期的库存到个人背包；已上架先下架。取货不转钱，保留批次属性；之后可自行到商队EXPORT_GOODS，收入归个人。出口失败货物仍在个人背包，也可重新上架。")
    if "CONTACT_CARAVAN" in actions:
        rules.append("CONTACT_CARAVAN登记下一趟需求，不自动开通路线。成功回执查看route_open和arrival_world_time；route_open=false时到访时间仅为计划窗口。Nora首次开通应急路线用CONTACT_EMERGENCY_CARAVAN(route_attempt_id=新UUID)，在GENERAL_STORE或MARKET_STREET由个人支付配置费用。无森林侦察、补给、会议或剧情阶段前置，已开通不重复收费。每日11:00到访、15:00离开，11:00后路线已冻结的当天不会补来，修桥改线同样从下次到访生效。")
    if actions & {"BUY_FROM_STORE", "INSPECT_STORE_INVENTORY"}:
        visible_location_codes = {code for code, _ in _location_directory(profile)}
        stores = VillageStore.objects.filter(
            location_code__in=visible_location_codes,
        ).order_by("store_code")
        store_locations = "; ".join(f"{store.store_code}={store.location_code}" for store in stores)
        if store_locations:
            rules.append(
                f"商店访问：先 MOVE 到商店地点（{store_locations}），再查询库存；查询或失败响应中的 location_code/required_location 是最终核对来源。"
                "购买按上架价格结算，并检查库存和资金。"
            )
        else:
            rules.append(
                "商店访问：先 MOVE 到已知商店地点，再查询库存；查询或失败响应中的 location_code/required_location 是最终核对来源。"
                "购买按上架价格结算，并检查库存和资金。"
            )
    if actions & {"BUY_FROM_MARKETPLACE", "SELL_ON_MARKETPLACE", "DELIST_FROM_MARKETPLACE"}:
        rules.append("全服市场交易：需到MARKET_STREET；只能上架自己的真实库存、下架自己的挂单，购买会检查库存和资金。")
    if "GIVE_ITEMS" in actions:
        rules.append("GIVE_ITEMS：向4米内的玩家或正式NPC单向赠送物品或金币，无需对方接受；先INSPECT_INVENTORY取得自己的instance_id和可用金币，再INSPECT_NEARBY取得目标的准确名字。target_type为PLAYER或NPC，target_name填写目标名字，由Unity解析ID；名字不存在或重名时不会执行，items为instance_id与amount列表。gold_amount为可选非负整数，省略为0；仅送金币传items=[]及正整数gold_amount，也可同时赠送物品和金币，二者不能同时为空。exchange_id必填小写标准UUID：回应PLAYER_GIFT时必须使用该事件的event_id，所有任务处理同一次交换都用这个ID，不得因换任务或重试生成新ID；只有独立的新赠送才使用新UUID。重复调用复用原动作/结果，can_await=false时只查询get_action_status，不await_action；unknown时先核对原动作，禁止另发赠送。全部物品和金币一次交付，可用余额不足整笔失败，失败不扣物不扣款；这是赠送，不保证对方回赠，不可用于从对方背包取物或取钱。")
    if actions & {"PROPOSE_TRADE", "RESPOND_TRADE", "CANCEL_TRADE_OFFER"}:
        rules.append("NPC个人交易及Nora主动发起的商店BUY采购：报价必须明确物品、数量和金额；接受时检查报价状态、双方地点、库存和资金。只有发起方能取消仍待处理的报价。NPC向商店SELL按下述直接售货规则执行。")
        rules.append('向Nora商店卖货：到GENERAL_STORE，PROPOSE_TRADE指定side=SELL、store_code=GENERAL_STORE、items=[{"item_id":1030,"amount":6},{"item_id":1049,"amount":5}]即可一次卖多种自己的物品；不需要target_npc_name、报价金额或报价期限。INSPECT_INVENTORY中sale.unit_price是每件实际售价；sale.allowed=false时不可出售，reason说明原因。合计直接成交，无需Nora回应或在场；不能自行定价。重复item_id合并数量；兼容原单品item_id+amount，但不能与items混填。商店账户付款、金币进卖方个人钱包、货进商店未上架库存；店款不足提示“商店金币不足，无法收购”。任一物品可售数量不足或店款不足，整批失败、全部钱货不变。商店须营业，过期或预留货不可重复卖。工具自动收购只接受配置满耐久批次；已损耗、耗尽、耐久不明或无效的工具不可收购。个人工具交易仍由双方议价并确认。不带store_code仍为双方回应的单品个人交易，须指定target_npc_name、item_id、amount和gold_amount；Nora主动商店BUY仍是单品询价。只能查看自己背包、商店公开货物及自己参与的报价，不能查看对方私人背包或钱包。')
    if actions & {"CREATE_ORDER", "CANCEL_ORDER", "START_DELIVERY"}:
        rules.append("订单交易：只能取消自己的开放订单；配送必须对应已匹配订单，并满足交付地点和资产条件。")
    if "RATE_DISH" in actions:
        rules.append("Mark是村庄从外部聘请的品鉴师，专属职责是菜品品鉴：先MOVE到RECORD_HALL，使用INSPECT_RATEABLE_DISHES查询真实待评样品，再通过RATE_DISH提交色、香、味、形四项1–10分评分和具体评语。系统据此更新该份菜品的评价和评价价格；单次评分不触发工资支付。")
        employment = VillageSimulationConfig.objects.filter(config_key="default").first()
        if employment and employment.mark_employed_from_minute is not None:
            rules.append(f"受聘起点为世界分钟{employment.mark_employed_from_minute}，从该游戏日开始每日工资{employment.mark_daily_salary}金币，由系统在日结时从公共资金自动支付到个人钱包，无需领取指令；当天没有菜品可评也计薪。资金不足记录新制欠薪，后续日结优先补发；旧制历史欠薪不补发。")
    if "CONTRIBUTE_TO_AFFAIR" in actions:
        rules.append("村庄事务由系统创建和定义，NPC自主决定是否参与。使用INSPECT_AFFAIRS查询；使用CONTRIBUTE_TO_AFFAIR指定affair_id、condition_id和kind，仅贡献available=true的条件。各事务独立推进，无全局剧情阶段、会议、投票或发布决议前置。")
        rules.append("kind=item提交item_id和amount，可用food_instance_id指定批次；kind=gold提交amount，扣个人可用金币。系统按条件品质、新鲜度、地点和剩余需求校验，不能重复贡献或使用公款。kind=labor不传amount，真实完成unit_minutes才计1份，中断不计。最后阶段完成后由系统结算完成效果。")
    opportunity_query = "GET_RULE 或 INSPECT_AFFAIRS" if "INSPECT_AFFAIRS" in actions else "GET_RULE"
    common.append(
        "current_daily_events 是与你有关的公开机会和近期结果，不是任务指令；你自主决定是否参与，并照常安排吃饭、休息和职业活动。"
        "status=OPEN表示邀请仍开放，RESOLVED表示已有实际结果，EXPIRED表示邀请窗口已结束。"
        "invite_until_world_minute 只控制邀请窗口，不会取消已认领委托、冻结交易或清零公共工程。"
        "summary 说明已知起因，resolution_summary 说明已结算事实；行动是否完成仍以真实业务回执为准。"
        f"使用{opportunity_query}可重新查询；已结束机会不要求再次执行。"
    )
    from .daily_events import project_daily_events
    context = {"common_rules": common, "npc_rules": rules}
    daily_events = project_daily_events(npc_id=profile.npc.agent_id) if profile else []
    if daily_events:
        context["current_daily_events"] = daily_events
    return context
