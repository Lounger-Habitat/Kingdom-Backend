"""Chinese environment rules and capability-specific action rules."""

from django.db.models import Q

from .dialogue_service import DialogueService
from .models import VillageActionDurationConfig, VillageLocationDefinition, VillageStore
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
        f"开口前先 INSPECT_NEARBY 看 distance 字段——注意'附近'半径（20 米）大于对话范围，"
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
            "plot_code 从 INSPECT_FARM_STATE 返回的地块中选择，不能作为 MOVE 地点。"
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
        rules.append("HUNT使用HUNT_ANIMALS：猎场入口获得鸡/猪/牛待处理猎物1件；鸡60%、猪30%、牛10%。PROCESS_GAME在屠宰棚每次处理1件：鸡→鸡肉3+羽毛1；猪→里脊1+排骨1+五花肉2+粗皮革1；牛→牛肉6+粗皮革2。两动作各耗1次猎具，时长只读数据库；肉与原猎物共用猎获完成起2880分钟鲜期。库存查询提供当日猎次、下一猎额外精力、可处理实例及阻碍；取消已开始的狩猎仍计次。肉可向Lucy报价，副产品可向Nora杂货店报价；采购、售卖和休息自主安排。")
    if "COOK" in actions:
        rules.append(f"COOK：需先 MOVE 到 RESTAURANT，使用允许的配方并消耗真实材料；配方编号、产量和材料清单查GET_RULE.cooking_recipes，个人可用材料查INSPECT_INVENTORY，菜品保鲜期为{economy.DISH_SPOILAGE_MINUTES}世界分钟。")
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
        rules.append("INSPECT_RESTAURANT_STATE只查询餐厅营业状态、在售菜单及本人的经营资料，不返回菜谱。public_meal_availability.authorizations只列已公布且未撤销、未到期的公共餐授权，保留decision_id、批准商品、补贴、剩余预算、期限和当日份额；未到开始时间仍会说明阻碍。没有当前授权时只提示需授权。该摘要不批准或预留公款，执行时仍复核本人状态、地点和最新钱货。")
    if "REQUEST_DISH_REVIEW" in actions:
        rules.append("Lucy制作产物先归个人，可留餐自食再上架。到RECORD_HALL用REQUEST_DISH_REVIEW(food_instance_id)提交具体1份给Mark；待评预留180分钟，不可重复吃卖，可CANCEL_DISH_REVIEW(review_id)。未评可按默认价值卖，评后按点评价卖。配方及产量查GET_RULE.cooking_recipes，再对照INSPECT_INVENTORY的可用量判断缺料；cook_time_seconds是工艺随机秒数，不是工作游戏分钟。")
    if "RATE_DISH" in actions:
        rules.append("Mark先INSPECT_RATEABLE_DISHES读取Lucy实际送来的review_id及完整菜品快照，用本人LLM判断色color、香aroma、味taste、摆盘presentation四项1–10分及comment。RATE_DISH提交review_id、scores对象、comment，不传总分/价格；后台按加权总分与默认价值计算价格。历史资料不完整须在评语说明，不编造原料。")
    if actions & {"BUY_FROM_STORE", "LIST_STORE_ITEM"}:
        rules.append("公共餐由Lucy实际制作后在饭店用ORIGINAL加public_meal_code=C1/C2/R1及decision_id上架，顾客价格0。按真实领取向饭店补贴9/12/9金，受已公布的meal_service授权、期限和预算限制；三类共享每NPC每天1份、全村每天5份，Lucy同额，游客不领取。缺公款整笔失败。先BUY_FROM_STORE领取，再EAT个人食物，吃饭不再次发补贴。")
    if "DEPOSIT_STORE_FUNDS" in actions:
        rules.append("DEPOSIT_STORE_FUNDS：在自己的商店将个人可用资金存入店铺，不收费、不计作销售收入；WITHDRAW_STORE_FUNDS反向提款时须保留经营储备。")
    if "WITHDRAW_STORE_ITEM" in actions:
        rules.append("WITHDRAW_STORE_ITEM：在自己的商店取出未上架、未占用且未过期的库存到个人背包；已上架先下架。取货不转钱，保留批次属性；之后可自行到商队EXPORT_GOODS，收入归个人。出口失败货物仍在个人背包，也可重新上架。")
    if "CONTACT_CARAVAN" in actions:
        rules.append("CONTACT_CARAVAN登记下一趟需求，不自动开通路线。成功回执查看route_open和arrival_world_time；route_open=false时到访时间仅为计划窗口。Nora首次开通应急路线用STORY_ACT(action_code=CONTACT_EMERGENCY_CARAVAN,route_attempt_id=新UUID)，在GENERAL_STORE或MARKET_STREET由个人支付配置费用。无森林侦察、补给、会议或剧情阶段前置，已开通不重复收费。每日11:00到访、15:00离开，11:00后路线已冻结的当天不会补来，修桥改线同样从下次到访生效。")
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
    if actions & {"PROPOSE_TRADE", "RESPOND_TRADE", "CANCEL_TRADE_OFFER"}:
        rules.append("NPC个人交易及Nora主动发起的商店BUY采购：报价必须明确物品、数量和金额；接受时检查报价状态、双方地点、库存和资金。只有发起方能取消仍待处理的报价。NPC向商店SELL按下述直接售货规则执行。")
        rules.append('向Nora商店卖货：到GENERAL_STORE，PROPOSE_TRADE指定side=SELL、store_code=GENERAL_STORE、items=[{"item_id":1030,"amount":6},{"item_id":1049,"amount":5}]即可一次卖多种自己的物品；不需要target_npc_id、报价金额或报价期限。INSPECT_INVENTORY中sale.unit_price是每件实际售价；sale.allowed=false时不可出售，reason说明原因。合计直接成交，无需Nora回应或在场；不能自行定价。重复item_id合并数量；兼容原单品item_id+amount，但不能与items混填。商店账户付款、金币进卖方个人钱包、货进商店未上架库存；店款不足提示“商店金币不足，无法收购”。任一物品可售数量不足或店款不足，整批失败、全部钱货不变。商店须营业，过期或预留货不可重复卖。工具自动收购只接受配置满耐久批次；已损耗、耗尽、耐久不明或无效的工具不可收购。个人工具交易仍由双方议价并确认。不带store_code仍为双方回应的单品个人交易，须指定target_npc_id、item_id、amount和gold_amount；Nora主动商店BUY仍是单品询价。只能查看自己背包、商店公开货物及自己参与的报价，不能查看对方私人背包或钱包。')
    if actions & {"CREATE_ORDER", "CANCEL_ORDER", "START_DELIVERY"}:
        rules.append("订单交易：只能取消自己的开放订单；配送必须对应已匹配订单，并满足交付地点和资产条件。")
    if actions & {"RATE_DISH", "PUBLISH_PRICE_INDEX", "AUDIT_PUBLIC_FUND"}:
        rules.append("公共经济事务：需先MOVE到RECORD_HALL。评分针对真实且可评分菜品；物价发布、公款审计的day_index必须为当前游戏日。每天首次有效履职按最低有效成品餐公开参考价计一次日薪，无发布价则用基础价；公款不足记欠薪，不凭空发钱。当天重复审计或换工作不会重复计薪；可用STORY_ACT/CLAIM_PUBLIC_SALARY主动领取已有欠薪。审计对照独立期初和账目，历史缺基准时仅核对迁移之后。耗时以本次实际配置为准。")
    if actions & {"CREATE_COMMITMENT", "CANCEL_COMMITMENT", "PERFORM_COMMITMENT"}:
        rules.append("承诺：交付、还款、捐赠、共同工作和见面均需满足参与者、目标、地点和期限条件；履约和违约可能改变关系。")
        rules.append('CREATE_COMMITMENT按类型填写target：DELIVERY={"item_id":1030,"amount":1,"gold_amount":8}；REPAYMENT={"gold_amount":2}；DONATION赠款={"asset_kind":"GOLD","gold_amount":2}或赠物={"asset_kind":"ITEM","item_id":1030,"amount":1}；JOINT_WORK={"location_code":"RESTAURANT","work_code":"MEAL_PREPARATION"}；MEETING={"location_code":"RECORD_HALL"}。金额数量为正整数，物品按工具白名单，地点须存在；due_world_minute大于当前世界分钟，beneficiary_npc_id是他人。创建不扣资产；DONATION给个人而非公款；DELIVERY须个人PROPOSE_TRADE携带commitment_id，买卖双方和钱货与承诺一致、接受成交才履约；其他用PERFORM_COMMITMENT。会面/共同工作要求双方在目标地点并可参与，JOINT_WORK不代替COOK或公共工程结算。')
    if actions & CommandService.STORY_ACTIONS:
        rules.append("会议通知含meeting_id、starts_world_minute/starts_world_time；world_time只是通知发生时间。收到召集/提案/投票/公布事件后，按需查INSPECT_STORY_DETAIL.meetings：can_settle是当前结算条件快照，unavailable_reasons列缺项，valid_npc_vote_count与attendee_count分别是有效NPC票数和出席人数，own_vote只表示自己的投票；terms_locked不代表票已齐。voting_open只表示投票时间窗口，仍须符合在场和提案条件；投票截止后的OPEN会议可依既有规则结算。正式结算仍由后端重新核实，leading_plan不是已通过决定，不代投、不因通知自动到场。")
        rules.append("剧情行动：投票受开放会议和候选方案限制，剧情行动受当前阶段、方案、角色权限、地点、已知事实和资源限制；进度变化后应重新查询，不要假定旧方案仍可执行。")
        rules.append("REPAIR_LABOR：先到REPAIR_YARD或OLD_BRIDGE，使用STORY_ACT并填写有效decision_id。每次占用60游戏分钟，labor_hours省略或填1；完整完成后才记1人时，中断不计工时。受伤、AWAY、精力不足、离开工地或授权失效会停止。DONATE_REPAIR_RESOURCES的木材是1002，工程金属是4003，1003南瓜种子不能抵木材。")
        rules.append("公共决定：先查INSPECT_STORY_DETAIL中的会议提案与public_decisions。新决定须Mark在场、至少3人出席及3张有效票；提案一旦有人投票不能改。工程采购/贡献须带decision_id，采购者还须匹配执行人、物品、有效期和剩余预算。Mark缺席时不能新增决定，但已经公布的有限有效授权可继续执行。公共配给须先公布再由Nora按批准额度与期限启用；普通店铺限购可自主更严格，不能放宽有效公共规则；剧情结束不提前取消合法期限。")
    if profile and str(profile.npc.agent_id) == "10013" and "STORY_ACT" in actions:
        rules.append("公共履职回执保留会议/决定编号、条款、审核凭证与salary。salary.new_amount_due是本次新产生应付，paid是本次实际到账（可能含补发往日欠薪），amount_due_today/amount_paid_for_today只针对今天这份工资，outstanding是尚欠；不要把应付当现金。audit提供结构化对账范围和差额。回执缺字段时查询公共经济与剧情详情，不凭空补编号或条款。")
        rules.append("书记操作均通过STORY_ACT，在RECORD_HALL执行：CALL_MEETING(starts_world_minute)召集；REGISTER_PROPOSAL(meeting_id,plan_code,budget_gold,executor_npc_id,expires_world_minute,ration_daily_limits,meal_service)登记修桥/配给提案；PUBLISH_DECISION(meeting_id)核实真实出席与投票、结算并原样公布，或用decision_id公布已经结算的决定，配给也可用PUBLISH_RATION_RULES。公共配给公布后即受执行端强制保护，Nora按已批准范围启用。REVEAL_STORY_FACT(fact_code=DEFERRED_BRIDGE_BUDGET)公开本人已有的历史预算事实；MANAGE_REPAIR_LEDGER(decision_id,evidence_request_id)审核真实工程凭证，同一凭证只能审一次。CALL_MEETING/登记提案不计薪；已失能、睡眠或AWAY不可履职。召集、投票和采购均由各Agent自行决定。")

    return {"common_rules": common, "npc_rules": rules}
