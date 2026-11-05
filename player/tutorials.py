"""Small teaching projection over existing authoritative actions and receipts.

Overview is read-only. Learning acknowledgements cannot issue assets, XP or
gameplay evidence. Evidence belongs to one unlocked task, never a global counter.
"""
from copy import deepcopy

from django.db import transaction
from django.utils import timezone

from .models import (PlayerTutorialState, PlayerSkillProgress, PlayerMilestoneProgress,
                     SkillRewardReceipt, EquipmentDefinition)
from .tutorial_catalog import VERSION, ROUTES, ROUTE_NAMES, TASKS, HINTS, SKILL_NAMES, SKILL_ACTIONS
from .lucy_tutorial import life_routes_unlocked

CONTROL_PARAMS = {"tutorial_start": set(), "tutorial_pause": set(),
    "tutorial_track": {"route_code"}, "tutorial_ack": {"hint_code", "task_code"}}
REWARD_EVENTS = {"collect": "collect", "advanced_collect": "collect", "professional_collect": "collect",
    "player_cooking": "cook", "farm_harvest": "harvest", "forestry": "wood",
    "wildlife_kill": "hunt", "stall_sale": "sale", "dish_review": "review",
    "starter_supply": "income", "personal_commission": "income"}
EVENT_NAMES = dict(collect="采摘植物", tool_collect="装备采集包采摘", income="出售或供货",
    cook="完成烹饪", harvest="收获作物", wood="砍倒树木", hunt="击败猎物", sale="顾客成交",
    review="完成料理点评", lease="租下田地", renew="续租田地", plant="播种作物", list="上架商品",
    open="开办摊位", withdraw="下架商品", process="领取加工产物", axe="挥动斧头", weapon="挥动武器",
    route="选择并追踪路线")
HINT_NAMES = dict(movement="走动约 3 米", camera="转动并缩放视角", bag="打开背包", growth="打开成长",
    recipe="查看配方", review="查看点评记录", business="查看摊位", farm_time="查看成熟时间",
    equipment="查看装备", training="命中训练木桩", training_block="成功格挡木桩攻击", training_dodge="成功闪避木桩攻击",
    cooking_preview="核对食材和炊具")


class TutorialError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def _normalize(state):
    if state.version >= VERSION:
        return
    # V1 global evidence has no task ownership. V2 already records each task;
    # preserve its partial work while retiring removed teaching goals.
    old_basics_done = state.version < 6 and "basics_route" in state.completed
    old_lucy = state.version < 6
    retired_basics = {"basics_move", "basics_collect", "basics_bag", "basics_income"}
    evidence = {} if state.version < 2 else {code: rows for code, rows in state.evidence.items() if code in TASKS or code in retired_basics}
    previous = dict(state.completed)
    if old_basics_done:
        stamp = state.completed["basics_route"]
        for row in ROUTES["basics"]:
            previous.setdefault(row["code"], stamp)
    completed = {}
    for rows in ROUTES.values():
        for row in rows:
            code, rule = row["code"], row["condition"]
            stamp = previous.get(code)
            if stamp is None and rule[0] == "hints" and all(evidence.get(code, {}).get("hint:" + hint) for hint in rule[1:]):
                stamp = timezone.now().isoformat()
            if stamp is None:
                break
            completed[code] = stamp
    # Earlier independent routes retain their own progress. New Lucy prerequisites
    # must not discard old growth/route evidence for an in-progress basics player.
    if old_lucy:
        for code in retired_basics | {"basics_growth", "basics_route"}:
            if code in previous:
                completed[code] = previous[code]
    state.completed, state.evidence, state.version = completed, evidence, VERSION


def _effective_route(state):
    return state.route_code if life_routes_unlocked(state) else "basics"


def _current_task(state):
    if not state.started:
        return None
    return next((TASKS[row["code"]] for row in ROUTES[_effective_route(state)]
                 if row["code"] not in state.completed), None)


def _events(row):
    rule = row["condition"]
    if rule[0] == "event":
        return {rule[1]}
    if rule[0] == "any_event":
        return set(rule[1:])
    if rule[0] == "hints":
        return {"hint:" + hint for hint in rule[1:]}
    if rule[0] == "hint_and_event":
        return {"hint:" + rule[1], rule[2]}
    return set()


def _remember(state, event, source):
    row = _current_task(state)
    if row is None or event not in _events(row):
        return
    source = str(source)
    if not event.startswith("hint:") and any(source in sources
            for code, evidence in state.evidence.items() if code != row["code"]
            for sources in evidence.values()):
        return
    rows = state.evidence.setdefault(row["code"], {}).setdefault(event, [])
    if source not in rows and len(rows) < 2:
        rows.append(source)


def _context(player, bag=None):
    from inventory.models import InventoryBag
    from restaurant.models import PlayerRestaurantState
    bag = bag or InventoryBag.objects.filter(player=player, character_id=player.username).first()
    tool_ids = dict(EquipmentDefinition.objects.filter(enabled=True, item__is_deleted=False).values_list("item_id", "category"))
    slots = list(bag.slots.filter(item_amount__gt=0).select_related("usage")) if bag and bag.pk else []
    tools = {tool_ids[slot.item_id] for slot in slots if slot.item_id in tool_ids and (slot.remaining_uses or 0) > 0}
    from village.phase3_catalog import FARM_CROP_RULES
    seed_ids = {rule["seed_item_id"] for rule in FARM_CROP_RULES.values()}
    milestones = list(PlayerMilestoneProgress.objects.filter(player=player).values("code", "source_type"))
    return dict(levels={s.skill_code: s.level for s in PlayerSkillProgress.objects.filter(player=player)},
        milestones={row["code"] for row in milestones},
        has_gameplay=any(row["source_type"] != "initial_equipment" for row in milestones),
        tools=tools,
        farm_ready="farming" in tools and any(slot.item_id in seed_ids for slot in slots),
        stall_open=PlayerRestaurantState.objects.filter(player=player, stall_opened_world_minute__isnull=False).exists())


def _met(row, state, context):
    rule = row["condition"]
    kind = rule[0]
    evidence = state.evidence.get(row["code"], {})
    if row["code"] in state.completed:
        return True
    if kind == "event":
        return len(evidence.get(rule[1], [])) >= rule[2]
    if kind == "any_event":
        return any(evidence.get(event) for event in rule[1:])
    if kind == "hints":
        return all(evidence.get("hint:" + hint) for hint in rule[1:])
    if kind == "level":
        return context["levels"].get(rule[1], 1) >= rule[2]
    if kind == "tool":
        return rule[1] in context["tools"] or "first_gathering_tool" in context["milestones"]
    if kind == "farm_ready":
        return context["farm_ready"]
    if kind == "stall_open":
        return context["stall_open"]
    if kind == "hint_and_event":
        return bool(evidence.get("hint:" + rule[1])) and len(evidence.get(rule[2], [])) >= rule[3]
    return False


def _complete(state, context):
    if not state.started:
        return
    stamp = timezone.now().isoformat()
    for definition in ROUTES[_effective_route(state)]:
        row = TASKS[definition["code"]]
        if not _met(row, state, context):
            break
        state.completed.setdefault(row["code"], stamp)


def _save_changed(state, before, context=None):
    if context is not None:
        _complete(state, context)
    after = _before(state)
    if before != after:
        state.revision += 1
        state.version = VERSION
        state.save()


def _before(state):
    return deepcopy((state.version, state.started, state.paused, state.route_code, state.evidence, state.completed))


def _action_evidence(state, action, result, source):
    event = None
    if action == "sell_store" and result.get("gold_amount", 0) > 0:
        event = "income"
    elif action == "lease_farm_plot":
        event = "lease"
    elif action == "renew_farm_plot":
        event = "renew"
    elif action == "plant_farm_plot":
        event = "plant"
    elif action == "list_stall_dish":
        event = "list"
    elif action == "open_player_stall" and result.get("is_open"):
        event = "open"
    elif action == "withdraw_stall_dish":
        event = "withdraw"
    elif action == "claim_game_processing" and result.get("job", {}).get("status") == "CLAIMED":
        event = "process"
    if event:
        _remember(state, event, source)


def _skill_evidence(state, source_type, source_id, result):
    event = REWARD_EVENTS.get(source_type)
    if not event:
        return
    source = f"{source_type}:{source_id}"
    _remember(state, event, source)
    if event in {"sale", "wood", "hunt"}:
        _remember(state, {"sale": "income", "wood": "axe", "hunt": "weapon"}[event], source)
    if event == "collect" and (result.get("facts") or {}).get("tool_item_id"):
        _remember(state, "tool_collect", source)


@transaction.atomic
def control(player, action, params, bag):
    from account.models import GameAccount
    if action == "tutorial_track" and (not isinstance(params.get("route_code"), str) or params["route_code"] not in ROUTES):
        raise TutorialError("INVALID_PARAMS", "请选择一条已开放的教学路线。")
    if action == "tutorial_ack" and (not isinstance(params.get("hint_code"), str) or params["hint_code"] not in HINTS
            or not isinstance(params.get("task_code"), str) or params["task_code"] not in TASKS):
        raise TutorialError("INVALID_PARAMS", "教学提示编号无效。")
    GameAccount.objects.select_for_update().get(pk=player.pk)
    state, _ = PlayerTutorialState.objects.select_for_update().get_or_create(player=player, defaults={"version": VERSION})
    before = _before(state)
    _normalize(state)
    if not life_routes_unlocked(state):
        if action == "tutorial_track" and params["route_code"] != "basics":
            raise TutorialError("TUTORIAL_LUCY_REQUIRED", "先完成 Lucy 的入村引导，听她表达感谢后即可选择生活路线。")
        state.route_code = "basics"
    acknowledged = False
    if action == "tutorial_pause":
        state.paused = True
    elif action == "tutorial_ack":
        if not state.started:
            raise TutorialError("TUTORIAL_NOT_STARTED", "请先开始或选择教学路线。")
        current = _current_task(state)
        if current is not None and current["code"] == params["task_code"] and "hint:" + params["hint_code"] in _events(current):
            _remember(state, "hint:" + params["hint_code"], "read")
            acknowledged = True
    else:
        state.started, state.paused = True, False
        if action == "tutorial_track":
            if params["route_code"] != "basics":
                _remember(state, "route", params["route_code"])
                _complete(state, _context(player, bag))
            state.route_code = params["route_code"]
    _save_changed(state, before, _context(player, bag))
    return dict(tutorial_updated=True, tutorial_backfilled=False, tutorial_acknowledged=acknowledged)


def observe_action(player, bag, action, result, request_id):
    """The player action caller owns the savepoint for evidence and asset changes."""
    state = PlayerTutorialState.objects.select_for_update().filter(player=player, started=True).first()
    if state is None:
        return False
    before = _before(state)
    _normalize(state)
    _action_evidence(state, action, result, str(request_id))
    _save_changed(state, before, _context(player, bag))
    return before != _before(state)


@transaction.atomic
def observe_skill(player, source_type, source_id, result):
    state = PlayerTutorialState.objects.select_for_update().filter(player=player, started=True).first()
    if state is None:
        return
    before = _before(state)
    _normalize(state)
    context = _context(player)
    # Cooking starts immediately after preview. Its successful settlement also
    # proves preparation, even if the client's learning acknowledgement is late.
    current = _current_task(state)
    if (source_type == "player_cooking" and current is not None and current["code"] == "cooking_prepare"
            and not any(f"{source_type}:{source_id}" in sources for evidence in state.evidence.values()
                        for sources in evidence.values())):
        _remember(state, "hint:cooking_preview", f"{source_type}:{source_id}:prepared")
        _complete(state, context)
    _skill_evidence(state, source_type, source_id, result)
    _save_changed(state, before, context)


@transaction.atomic
def observe_weapon(player, category, source_id):
    state = PlayerTutorialState.objects.select_for_update().filter(player=player, started=True).first()
    if state is None:
        return
    before = _before(state)
    _normalize(state)
    _remember(state, "axe" if category == "axe" else "weapon", str(source_id))
    _save_changed(state, before, _context(player))


def _waiting(row, state, context, overview):
    rule = row["condition"]
    if rule[0] == "level":
        return f"继续{SKILL_ACTIONS[rule[1]]}来获得经验；“成长 → {SKILL_NAMES[rule[1]]}”可查看升级进度。"
    if row["code"] == "farming_harvest":
        plots = (overview.get("farm") or {}).get("plots", [])
        mine = [p for p in plots if str(p.get("lessee_player_id")) == overview.get("player_id")]
        if mine and not any(p.get("status") == "MATURE" for p in mine):
            return "作物还在生长。查看预计成熟时间后，可以先去做其他任务，成熟后再回田边。"
    if row["code"] == "business_sale":
        return "上架成功后还需等顾客购买。确认摊位正在营业、货架有货，再等待成交。"
    if row["code"] == "cooking_review":
        return "提交送评后还需等 Mark 完成点评。可先做其他任务，稍后回议事厅查看。"
    if row["condition"][0] == "tool":
        return "需要一个可用的采集工具包。可到诺拉杂货店购买；缺货时先体验其他路线。"
    return ""


def _progress_text(row, state, context, done):
    if done:
        return "已完成"
    rule = row["condition"]
    if rule[0] == "lucy":
        return {"meet": "靠近 Lucy，开始交谈", "accept": "与 Lucy 聊清需求后接取她发布的委托",
            "complete": "以这份委托的真实交付结果为准", "thanks": "回去和 Lucy 聊聊"}[rule[1]]
    evidence = state.evidence.get(row["code"], {})
    if rule[0] == "event":
        count = min(rule[2], len(evidence.get(rule[1], [])))
        return f"{EVENT_NAMES[rule[1]]} {count} / {rule[2]} 次"
    if rule[0] == "level":
        return f"{SKILL_NAMES[rule[1]]}等级 {context['levels'].get(rule[1], 1)} / {rule[2]} 级"
    if rule[0] == "hints":
        pending = [HINT_NAMES[hint] for hint in rule[1:] if not evidence.get("hint:" + hint)]
        if row["code"] == "business_accounts" and pending:
            pending = ["查看营业记录"]
        return "还需：" + "、".join(pending)
    if rule[0] == "hint_and_event":
        read = "已查看" if evidence.get("hint:" + rule[1]) else "未查看"
        count = min(rule[3], len(evidence.get(rule[2], [])))
        return f"配方{read} · 本步采摘 {count} / {rule[3]} 次"
    if rule[0] == "any_event":
        return "任选一项：" + " / ".join(EVENT_NAMES[event] for event in rule[1:])
    if rule[0] == "tool":
        return "还需：一个可用采集工具包"
    if rule[0] == "farm_ready":
        return "还需：在主背包备齐种子和可用农具"
    if rule[0] == "stall_open":
        return "还需：在集市开办自己的摊位"
    return "按当前步骤操作"


def tutorial_overview(player, bag, minute, overview, read_only=False):
    saved = PlayerTutorialState.objects.filter(player=player).first()
    state = deepcopy(saved) if saved else PlayerTutorialState(player=player, version=VERSION)
    _normalize(state)
    unlocked = life_routes_unlocked(state)
    state.route_code = _effective_route(state)
    from .lucy_tutorial import context as lucy_context
    lucy = lucy_context(player, state)
    context = _context(player, bag)
    completed = set(state.completed)
    starter_goals = {g["code"]: g for g in (overview.get("starter") or {}).get("goals", [])}
    routes = []
    current = None
    for route, rows in ROUTES.items():
        if route != "basics" and not unlocked:
            continue
        projected, pending = [], False
        for definition in rows:
            row = TASKS[definition["code"]]
            done = row["code"] in completed
            status = "COMPLETED" if done else "LOCKED" if pending else "AVAILABLE"
            detail = {k: deepcopy(row[k]) for k in ("code", "title", "description", "location_code", "panel", "route_code", "index", "legacy_goal_code")}
            detail.update(completed=done, status=status, skill_name=SKILL_NAMES.get(route, ""), chapter="入门体验",
                waiting_reason="" if done or pending else _waiting(row, state, context, overview),
                learning_hint=row["condition"][1] if row["condition"][0] == "hints" and len(row["condition"]) == 2 else "",
                learning_hints=sorted(event[5:] for event in _events(row) if event.startswith("hint:")
                    and not state.evidence.get(row["code"], {}).get(event)))
            if row["condition"][0] == "lucy":
                detail.update(npc_id="10014", commission_id=lucy["commission_id"], panel="lucy")
            detail["progress_text"] = _progress_text(row, state, context, done)
            legacy = starter_goals.get(row["legacy_goal_code"])
            if legacy:
                detail["legacy_reward"] = deepcopy(legacy)
            if not done and not pending and route == state.route_code:
                current = detail
            pending = pending or not done
            projected.append(detail)
        routes.append(dict(code=route, title=ROUTE_NAMES[route], tasks=projected, completed_count=sum(t["completed"] for t in projected),
            total_count=len(rows), intro_completed=all(t["completed"] for t in projected)))
    has_history = context["has_gameplay"] or SkillRewardReceipt.objects.filter(player=player).exists()
    return dict(schema_version=VERSION, revision=state.revision, observed_world_minute=minute,
        started=state.started, paused=state.paused, route_code=state.route_code,
        show_welcome=saved is None and not has_history and not read_only,
        can_write=not read_only, current_task=current, routes=routes, lucy=lucy, life_routes_unlocked=unlocked,
        route_completed=current is None and state.started,
        reward_policy="原有五项入门金币奖励只结算一次；教学阅读不增加金币或技能经验。")
