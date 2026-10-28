"""Lucy onboarding over the existing personal commission and teaching records."""
from copy import deepcopy
import uuid

from django.db import transaction
from django.utils import timezone

from account.models import GameAccount
from .models import PlayerTutorialState

LUCY_ID = "10014"
LUCY_STEPS = ("basics_lucy_meet", "basics_lucy_commission", "basics_lucy_help", "basics_lucy_thanks")


def life_routes_unlocked(state):
    return bool(state and all(code in state.completed for code in LUCY_STEPS))


def publication_limit():
    from django.conf import settings
    return max(0, int(settings.LUCY_TUTORIAL_MAX_OPEN))


def template():
    # Each of these authored physical plants can be picked barehanded at level 1.
    from village.plant_catalog import PLANTS
    item_ids = sorted({plant["item_id"] for plant in PLANTS.values() if plant["level"] == 1})
    return dict(title="帮 Lucy 准备一小份食材", description="饭店需要准备食材。胡萝卜、土豆或平菇任选，合计交付2份即可；可在村边徒手采摘。",
        reward_gold=5, valid_for_minutes=1440, default_location_code="RESTAURANT",
        stages=[dict(id="supplies", title="为饭店准备食材", conditions=[dict(id="ingredients", type="item",
            item_ids=item_ids, target=2, min_quality=1)])])


def _state(player, *, lock=False):
    from .tutorials import _normalize
    query = PlayerTutorialState.objects.select_for_update(of=("self",)) if lock else PlayerTutorialState.objects
    state = query.select_related("lucy_commission").filter(player=player).first()
    if state:
        state = state if lock else deepcopy(state)
        _normalize(state)
    return state


def context(player, state=None):
    state = state or _state(player)
    row = state.lucy_commission if state and state.lucy_commission_id else None
    done = life_routes_unlocked(state)
    active = bool(state and state.started and not done)
    status = row.status if row else ""
    if done:
        stage = "completed"
    elif not state or "basics_lucy_meet" not in state.completed:
        stage = "meet_lucy"
    elif not row or status not in {"OPEN", "ACTIVE", "COMPLETED"}:
        stage = "offer_help"
    elif status == "OPEN":
        stage = "accept_commission"
    elif status == "ACTIVE":
        stage = "help_lucy"
    else:
        stage = "return_lucy"
    messages = dict(meet_lucy="你知道昨天有位旅行者在南巷旅人驿住了下来，现在这位旅行者来找你交谈，你想和对方认识一下。先自然介绍自己是经营饭店的 Lucy，欢迎对方，听听对方想聊什么，并回应关于村庄的问题。聊到饭店日常时，也可以顺势提到备料有些小忙可请对方帮一下，允许追问、暂缓或拒绝。先回应对方，完成一轮交谈；等环境确认已经认识、阶段变为 offer_help，且对方明确愿意帮忙后再发布委托。不要编造你昨晚亲自接待过对方或其他未发生的经历。",
        offer_help="你已经和这位在南巷旅人驿住下的旅行者完成一轮交谈、互相认识了。继续回应对方想了解的村庄生活，也可以顺着话题提起饭店准备食材的小忙。对方明确愿意帮助时，用引导标识发布这份个人委托；不必重新自我介绍，也不要催促对方。",
        accept_commission="这份委托已发布，请邀请当前旅人亲自接取；不要重复发布，也不要声称已经完成。",
        help_lucy="旅人正在执行这份委托。可以说明需求和交付地点，按真实进度回应。",
        return_lucy="旅人已经真实完成这份委托。下次与本人交谈时，请先结合具体帮助表达感谢。",
        completed="你已经向这位旅人表达过感谢，继续正常相处，无需重新发布入门委托。")
    spec = template()
    result = dict(active=active, player_id=str(player.pk), player_name=player.username, npc_id=LUCY_ID,
        stage=stage, context_id=str(state.lucy_context_id) if active else "",
        commission_id=str(row.pk) if row else "", commission_status=status,
        thanks_pending=active and stage == "return_lucy", template=spec,
        text=messages[stage])
    result["text"] += f" 当前旅人：{player.username}（玩家编号 {player.pk}）。"
    if active:
        result["text"] += f" 引导标识 tutorial_context_id={result['context_id']}。"
        result["text"] += " 入门委托需要胡萝卜、土豆或平菇任选合计2份，在饭店交付；报酬5金币，有效期1440世界分钟。"
    if row:
        result["text"] += f" 委托编号 {row.pk}，状态 {status}。"
    return result


def _save(state):
    from .tutorial_catalog import VERSION
    state.version = VERSION
    state.revision += 1
    state.save()


def _step(state, code, source):
    if code in state.completed:
        return False
    index = LUCY_STEPS.index(code)
    if not all(previous in state.completed for previous in LUCY_STEPS[:index]):
        return False
    state.completed[code] = timezone.now().isoformat()
    state.evidence[code] = {"lucy": [str(source)]}
    return True


@transaction.atomic
def handle(payload):
    from .tutorials import TutorialError
    if not isinstance(payload, dict) or set(payload) - {"operation", "player_id", "npc_id", "conversation_id", "speech_id", "commission_id", "player_speech_id", "tutorial_context_id"}:
        raise TutorialError("INVALID_PARAMS", "Lucy 引导参数无效。")
    if str(payload.get("npc_id", "")) != LUCY_ID:
        raise TutorialError("TUTORIAL_NPC_INVALID", "这段引导只由 Lucy 负责。")
    operation = payload.get("operation")
    if operation not in {"context", "meet", "exchange", "thanks"}:
        raise TutorialError("INVALID_PARAMS", "引导操作无效。")
    player_id = str(payload.get("player_id", ""))
    if not player_id.isdecimal():
        raise TutorialError("PLAYER_NOT_FOUND", "玩家不存在。")
    player = GameAccount.objects.filter(pk=player_id, is_active=True, is_ai=False).first()
    if player is None:
        raise TutorialError("PLAYER_NOT_FOUND", "玩家不存在。")
    if operation == "context":
        return context(player)
    conversation = payload.get("conversation_id")
    if not isinstance(conversation, str) or not conversation.strip() or len(conversation) > 256:
        raise TutorialError("INVALID_PARAMS", "需要真实的交谈编号。")
    # Keep the same clock -> account -> tutorial lock order as gameplay mutations.
    from game_time.models import GlobalGameTimeState
    GlobalGameTimeState.objects.select_for_update().get(pk=1)
    GameAccount.objects.select_for_update().get(pk=player.pk)
    state = _state(player, lock=True)
    if state is None or not state.started:
        raise TutorialError("TUTORIAL_NOT_STARTED", "玩家尚未开始入村教学。")
    if operation == "meet":
        # Opening a conversation registers presence, not a completed exchange.
        changed = False
        if state.lucy_conversation_id != conversation:
            state.lucy_conversation_id = conversation
            changed = True
        if state.lucy_commission_id and state.lucy_commission.status == "COMPLETED" and not state.lucy_thanks_speech_id:
            changed |= state.lucy_thanks_conversation_id != conversation
            state.lucy_thanks_conversation_id = conversation
        if changed:
            _save(state)
        return context(player, state)
    if operation == "exchange":
        player_speech, speech = payload.get("player_speech_id"), payload.get("speech_id")
        if any(not isinstance(value, str) or not value.strip() or len(value) > 256
               for value in (player_speech, speech)) or player_speech == speech:
            raise TutorialError("INVALID_PARAMS", "需要本轮玩家发言与 Lucy 已送达回复的不同编号。")
        if payload.get("tutorial_context_id") != str(state.lucy_context_id):
            raise TutorialError("TUTORIAL_CONTEXT_INVALID", "这轮交谈的引导标识已失效。")
        if state.lucy_conversation_id != conversation:
            raise TutorialError("TUTORIAL_EXCHANGE_NOT_READY", "这轮交谈与当前玩家的会话不匹配。")
        # Env verifies speech.refs and delivery before submitting this receipt.
        if _step(state, "basics_lucy_meet", speech):
            state.evidence["basics_lucy_meet"].update(
                conversation=[conversation], player_speech=[player_speech])
            _save(state)
        return context(player, state)
    speech = payload.get("speech_id")
    row = state.lucy_commission
    if not isinstance(speech, str) or not speech.strip() or len(speech) > 256:
        raise TutorialError("INVALID_PARAMS", "需要已发送的感谢发言编号。")
    if (row is None or row.status != "COMPLETED" or row.actor_type != "PLAYER" or row.actor_id != str(player.pk)
            or payload.get("commission_id") not in (None, "", str(row.pk))
            or state.lucy_thanks_conversation_id != conversation):
        raise TutorialError("TUTORIAL_THANKS_NOT_READY", "这份委托尚未完成，或感谢会话不匹配。")
    if state.lucy_thanks_speech_id:
        return context(player, state)
    if not _step(state, "basics_lucy_thanks", speech):
        raise TutorialError("TUTORIAL_THANKS_NOT_READY", "这份引导的完成记录尚未就绪。")
    state.lucy_thanks_speech_id = speech
    _save(state)
    return context(player, state)


def state_for_publication(npc, context_id):
    from village.personal_commissions import CommissionError
    if str(npc.agent_id) != LUCY_ID:
        raise CommissionError("TUTORIAL_NPC_INVALID", "只有 Lucy 能发布这份入村委托。")
    try:
        identity = uuid.UUID(str(context_id))
    except (ValueError, TypeError, AttributeError):
        raise CommissionError("TUTORIAL_CONTEXT_INVALID", "引导标识无效。") from None
    state = PlayerTutorialState.objects.select_for_update(of=("self",)).select_related("player", "lucy_commission").filter(lucy_context_id=identity).first()
    if not state:
        raise CommissionError("TUTORIAL_CONTEXT_INVALID", "引导标识不存在。")
    from .tutorials import _normalize
    _normalize(state)
    if not context(state.player, state)["active"] or "basics_lucy_meet" not in state.completed:
        raise CommissionError("TUTORIAL_NOT_READY", "请先与这位旅人交谈，再发布入村委托。")
    return state


def commission_changed(row):
    state = PlayerTutorialState.objects.select_for_update().filter(lucy_commission=row).first()
    if state is None:
        return
    changed = False
    if row.status == "ACTIVE":
        changed = _step(state, "basics_lucy_commission", row.pk)
    elif row.status == "COMPLETED":
        changed = _step(state, "basics_lucy_help", row.pk)
        # A fresh Env meet is required after completion; an old speech cannot thank.
        state.lucy_thanks_conversation_id = ""
    elif row.status in {"ABANDONED", "EXPIRED", "WITHDRAWN"}:
        for code in LUCY_STEPS[1:]:
            changed |= state.completed.pop(code, None) is not None
            state.evidence.pop(code, None)
        state.lucy_thanks_conversation_id = state.lucy_thanks_speech_id = ""
    if changed:
        _save(state)
