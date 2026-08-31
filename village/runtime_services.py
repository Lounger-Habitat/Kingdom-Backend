"""
阶段一：Backend 运行时服务

实现：
- 时间 CAS (Compare-And-Swap)
- Runtime CAS
- Command accept/start 生命周期
- Receipt
- WorldEvent
- Reservation
- Outbox
- Bootstrap
- Checkpoint
- 时间推进
- 动作完成
"""

import hashlib
import json
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from django.conf import settings
from django.db import IntegrityError, models, transaction
from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone

from .models import (
    NpcActionReservation,
    NpcCommandState,
    NpcDialogueSession,
    NpcProfile,
    NpcRuntimeState,
    VillageActionDurationConfig,
    VillageActionReceipt,
    VillageDeliveryCursor,
    VillageDeliveryOutbox,
    VillageSimulationConfig,
    VillageWorldEvent,
)
from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition

from .command_catalog import COMMAND_CATALOG


def _fast_integration_enabled() -> bool:
    return bool(getattr(settings, "VILLAGE_FAST_COMMAND_INTEGRATION", False))


# ============================================================================
# 稳定错误码
# ============================================================================

class ResultCode:
    # 成功码
    COMMAND_ACCEPTED = "COMMAND_ACCEPTED"
    COMMAND_REPLAYED = "COMMAND_REPLAYED"
    MOVE_COMPLETED = "MOVE_COMPLETED"
    WAIT_COMPLETED = "WAIT_COMPLETED"
    GO_HOME_COMPLETED = "GO_HOME_COMPLETED"
    COMMAND_CANCELLED_ON_RECOVERY = "COMMAND_CANCELLED_ON_RECOVERY"
    LIFE_ACTION_COMPLETED = "LIFE_ACTION_COMPLETED"
    DIALOGUE_STARTED = "DIALOGUE_STARTED"
    DIALOGUE_SPOKEN = "DIALOGUE_SPOKEN"
    DIALOGUE_COMPLETED = "DIALOGUE_COMPLETED"

    # 失败码 - 协议和身份
    PROTOCOL_VERSION_UNSUPPORTED = "PROTOCOL_VERSION_UNSUPPORTED"
    INVALID_ENVELOPE = "INVALID_ENVELOPE"
    INVALID_COMMAND_SCHEMA = "INVALID_COMMAND_SCHEMA"
    SENDER_BINDING_INVALID = "SENDER_BINDING_INVALID"
    WRONG_RECIPIENT = "WRONG_RECIPIENT"

    # 失败码 - ID 和幂等
    MESSAGE_ID_CONFLICT = "MESSAGE_ID_CONFLICT"
    COMMAND_ID_CONFLICT = "COMMAND_ID_CONFLICT"
    REQUEST_ID_CONFLICT = "REQUEST_ID_CONFLICT"

    # 失败码 - TTL
    MESSAGE_EXPIRED = "MESSAGE_EXPIRED"
    CLOCK_SKEW = "CLOCK_SKEW"

    # 失败码 - 动作和队列
    UNKNOWN_ACTION = "UNKNOWN_ACTION"
    ACTION_NOT_ALLOWED = "ACTION_NOT_ALLOWED"
    QUEUE_FULL = "QUEUE_FULL"
    NPC_BUSY = "NPC_BUSY"

    # 失败码 - 版本
    STALE_STATE_VERSION = "STALE_STATE_VERSION"
    STALE_TIME_REVISION = "STALE_TIME_REVISION"
    WORLD_TIME_REGRESSION = "WORLD_TIME_REGRESSION"

    # 失败码 - 执行
    INVALID_LOCATION = "INVALID_LOCATION"
    TARGET_UNREACHABLE = "TARGET_UNREACHABLE"
    ACTION_TIMEOUT = "ACTION_TIMEOUT"

    # 失败码 - 预约
    RESERVATION_CONFLICT = "RESERVATION_CONFLICT"

    # 失败码 - Backend
    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    CHECKPOINT_FIELD_NOT_ALLOWED = "CHECKPOINT_FIELD_NOT_ALLOWED"
    BOOTSTRAP_NPC_SET_MISMATCH = "BOOTSTRAP_NPC_SET_MISMATCH"

    # 阶段二需求与生活动作
    VILLAGE_NEEDS_DISABLED = "VILLAGE_NEEDS_DISABLED"
    NEEDS_FIELDS_NOT_ALLOWED = "NEEDS_FIELDS_NOT_ALLOWED"
    INVALID_ACTIVITY_INTERVAL = "INVALID_ACTIVITY_INTERVAL"
    STALE_NEEDS_MINUTE = "STALE_NEEDS_MINUTE"
    HUNGER_TOO_HIGH = "HUNGER_TOO_HIGH"
    ENERGY_TOO_LOW = "ENERGY_TOO_LOW"
    HEALTH_TOO_LOW = "HEALTH_TOO_LOW"
    INVALID_ACTION_DURATION = "INVALID_ACTION_DURATION"
    INVALID_ACTION_LOCATION = "INVALID_ACTION_LOCATION"
    LOCATION_CLOSED = "LOCATION_CLOSED"
    FOOD_NOT_AVAILABLE = "FOOD_NOT_AVAILABLE"
    FOOD_NOT_ALLOWED = "FOOD_NOT_ALLOWED"
    ASSET_VERSION_CONFLICT = "ASSET_VERSION_CONFLICT"
    SOCIAL_TARGET_BUSY = "SOCIAL_TARGET_BUSY"
    SOCIAL_TARGET_INVALID = "SOCIAL_TARGET_INVALID"
    DIALOGUE_NOT_FOUND = "DIALOGUE_NOT_FOUND"
    DIALOGUE_NOT_ACTIVE = "DIALOGUE_NOT_ACTIVE"
    DIALOGUE_PARTICIPANT_INVALID = "DIALOGUE_PARTICIPANT_INVALID"
    DIALOGUE_TEXT_INVALID = "DIALOGUE_TEXT_INVALID"
    DIALOGUE_NOT_COLOCATED = "DIALOGUE_NOT_COLOCATED"
    DIALOGUE_ALREADY_ENDED_SELF = "DIALOGUE_ALREADY_ENDED_SELF"

    # 阶段边界
    ASSET_EFFECT_NOT_ALLOWED = "ASSET_EFFECT_NOT_ALLOWED"

    # Outbox
    OUTBOX_ACK_UNKNOWN = "OUTBOX_ACK_UNKNOWN"
    OUTBOX_DELIVERY_FAILED = "OUTBOX_DELIVERY_FAILED"
    COMMAND_EXPIRED = "COMMAND_EXPIRED"
    COMMAND_NOT_FOUND = "COMMAND_NOT_FOUND"
    COMMAND_NOT_CANCELLABLE = "COMMAND_NOT_CANCELLABLE"
    SLEEP_INTERRUPTED = "SLEEP_INTERRUPTED"
    NPC_NOT_SLEEPING = "NPC_NOT_SLEEPING"
    SLEEP_COMMAND_MISMATCH = "SLEEP_COMMAND_MISMATCH"
    SETTLEMENT_IN_PROGRESS = "SETTLEMENT_IN_PROGRESS"
    HANDSHAKE_REQUIRED = "HANDSHAKE_REQUIRED"


_OUTCOME_ACTION_LABELS = {
    "MOVE": "移动", "WAIT": "等待", "GO_HOME": "回家", "EAT": "进食",
    "REST": "休息", "SLEEP": "睡眠", "SOCIALIZE": "社交",
    "WORK_PLACEHOLDER": "工作", "PLANT": "播种", "HARVEST": "收获",
    "HUNT": "狩猎", "COOK": "烹饪", "CREATE_ORDER": "创建订单",
    "CANCEL_ORDER": "取消订单", "START_DELIVERY": "配送",
    "IMPORT_GOODS": "进货", "EXPORT_GOODS": "出货", "RATE_DISH": "菜品评分",
    "PUBLISH_PRICE_INDEX": "发布价格指数", "AUDIT_PUBLIC_FUND": "公共资金审计",
    "PROPOSE_TRADE": "发起交易", "RESPOND_TRADE": "响应交易",
    "CANCEL_TRADE_OFFER": "取消交易报价", "LIST_STORE_ITEM": "商品上架",
    "UPDATE_STORE_PRICE": "修改商品价格", "DELIST_STORE_ITEM": "商品下架",
    "BUY_FROM_STORE": "购买商品",
    "BUY_FROM_MARKETPLACE": "购买全服商品",
    "SELL_ON_MARKETPLACE": "上架全服商品",
    "DELIST_FROM_MARKETPLACE": "下架全服商品",
    "BROWSE_MARKETPLACE": "浏览全服市场",
    "CHECK_MY_MARKETPLACE_LISTINGS": "读取我的全服挂单",
    "CHECK_MARKETPLACE_HISTORY": "读取全服成交历史",
    "INSPECT_STORE_INVENTORY": "读取商店库存",
    "OBSERVE": "读取状态", "INSPECT_NEARBY": "读取附近信息", "INSPECT_INVENTORY": "读取背包",
    "INSPECT_TRADE_OFFERS": "读取交易报价", "INSPECT_STORY_STATE": "读取剧情状态",
    "INSPECT_RELEVANT_STORIES": "读取相关剧情", "INSPECT_STORY_DETAIL": "读取剧情详情",
    "INSPECT_ORDERS": "读取订单", "INSPECT_MARKET_STATE": "读取市场状态",
    "INSPECT_FARM_STATE": "读取农田状态", "INSPECT_RESTAURANT_STATE": "读取餐厅状态",
    "INSPECT_RATEABLE_DISHES": "读取可评分菜品", "INSPECT_PUBLIC_ECONOMY_STATE": "读取公共经济状态",
    "INSPECT_SOCIAL_STATE": "读取社交状态",
    "CREATE_COMMITMENT": "创建承诺", "CANCEL_COMMITMENT": "取消承诺",
    "PERFORM_COMMITMENT": "履行承诺", "STORY_VOTE": "剧情投票",
    "STORY_ACT": "剧情行动", "UPDATE_SUBJECTIVE_GOAL": "更新主观目标",
}

_OUTCOME_REASONS = {
    "BACKEND_UNAVAILABLE": "Backend 暂不可用，尚未确认最终结果",
    "INVALID_ENVELOPE": "消息信封格式无效", "INVALID_COMMAND_SCHEMA": "命令字段或参数格式无效",
    "UNKNOWN_ACTION": "动作名称未知", "ACTION_NOT_ALLOWED": "当前状态不允许执行此动作",
    "QUEUE_FULL": "命令队列已满", "NPC_BUSY": "NPC 正在执行其他动作",
    "NPC_NOT_SLEEPING": "NPC 当前没有睡眠", "SLEEP_COMMAND_MISMATCH": "唤醒请求与睡眠命令不匹配",
    "STALE_STATE_VERSION": "客户端状态版本已过期", "STALE_TIME_REVISION": "时间版本已过期",
    "WORLD_TIME_REGRESSION": "世界时间不能倒退", "INVALID_LOCATION": "当前位置不满足动作要求",
    "INVALID_ACTION_LOCATION": "当前位置不满足动作要求", "P3_WRONG_LOCATION": "当前位置不满足动作要求",
    "TRADE_WRONG_LOCATION": "当前位置不满足交易要求", "TARGET_UNREACHABLE": "目标地点不可达",
    "ACTION_TIMEOUT": "动作执行超时", "FOOD_NOT_AVAILABLE": "指定食物不存在或数量不足",
    "FOOD_NOT_ALLOWED": "指定物品不是允许食用的食物", "ENERGY_TOO_LOW": "能量不足",
    "HEALTH_TOO_LOW": "生命值过低", "LOCATION_CLOSED": "目标地点当前关闭",
    "P3_FEATURE_DISABLED": "经济功能未启用", "P3_FORBIDDEN_ACTOR": "当前 NPC 无权执行该经济动作",
    "P3_INVALID_PARAMS": "经济动作参数无效", "P3_INSUFFICIENT_ITEM": "物品或材料不足",
    "P3_INSUFFICIENT_MONEY": "金币不足", "P3_TOOL_BROKEN": "所需工具已损坏",
    "P3_PLOT_NOT_READY": "地块尚未准备好", "P3_RECIPE_NOT_ALLOWED": "当前菜谱不允许制作",
    "P3_ORDER_NOT_OPEN": "订单不存在或已关闭", "P3_DELIVERY_NOT_READY": "配送尚未准备好",
    "P3_PRICE_UNAVAILABLE": "当前价格不可用", "P3_CARAVAN_COOLDOWN": "商队仍在冷却中",
    "P3_ASSET_VERSION_CONFLICT": "经济资产版本已变化，请刷新后重试",
    "P3_REQUEST_CONFLICT": "经济请求冲突，请刷新后重试", "P3_DAY_NOT_READY": "当天经济数据尚未准备好",
    "TRADE_TARGET_NOT_FOUND": "交易对象不存在", "TRADE_TARGET_REJECTED": "交易对象拒绝了报价",
    "TRADE_OFFER_NOT_FOUND": "交易报价不存在", "TRADE_OFFER_EXPIRED": "交易报价已过期",
    "TRADE_OFFER_ALREADY_RESOLVED": "交易报价已经处理过", "TRADE_RESPONSE_FORBIDDEN": "无权响应该交易报价",
    "TRADE_CANCEL_FORBIDDEN": "无权取消该交易报价", "TRADE_NOT_COLOCATED": "交易双方不在同一地点",
    "TRADE_STORE_CLOSED": "商店当前关闭", "TRADE_STORE_FORBIDDEN": "无权操作该商店",
    "TRADE_LISTING_NOT_FOUND": "商品不存在", "TRADE_LISTING_NOT_ACTIVE": "商品未在售",
    "TRADE_ITEM_NOT_FOR_SALE": "该物品不可出售", "TRADE_INSUFFICIENT_MONEY": "金币不足",
    "TRADE_INSUFFICIENT_ITEM": "库存物品不足", "TRADE_ASSET_VERSION_CONFLICT": "交易资产版本已变化，请刷新后重试",
    "TRADE_STORE_VERSION_CONFLICT": "商店库存版本已变化，请刷新后重试", "TRADE_REQUEST_CONFLICT": "交易请求冲突，请刷新后重试",
    "MARKETPLACE_LISTING_NOT_FOUND": "全服挂单不存在",
    "MARKETPLACE_LISTING_NOT_ACTIVE": "全服挂单未在售",
    "MARKETPLACE_FORBIDDEN": "无权操作该全服挂单",
    "MARKETPLACE_INSUFFICIENT_MONEY": "金币不足",
    "MARKETPLACE_INSUFFICIENT_ITEM": "库存物品不足",
    "MARKETPLACE_ASSET_VERSION_CONFLICT": "资产版本已变化，请刷新后重试",
    "VILLAGE_SOCIAL_DISABLED": "社交功能未启用", "SOCIAL_INVALID_PARAMS": "社交参数无效",
    "COMMITMENT_NOT_FOUND": "承诺不存在", "COMMITMENT_ALREADY_RESOLVED": "承诺已经处理过",
    "COMMITMENT_EXPIRED": "承诺已过期", "SOCIAL_NOT_COLOCATED": "双方不在同一地点",
    "SOCIAL_PARTICIPANT_UNAVAILABLE": "社交参与者当前不可用", "INSUFFICIENT_MONEY": "金币不足",
    "INSUFFICIENT_ITEM": "物品不足", "STORY_FEATURE_DISABLED": "剧情功能未启用",
    "STORY_VERSION_CONFLICT": "剧情版本已变化，请刷新后重试", "STORY_PHASE_INVALID": "当前剧情阶段不允许此操作",
    "STORY_PLAN_INVALID": "剧情方案无效", "STORY_FACT_NOT_UNLOCKED": "相关剧情事实尚未解锁",
    "STORY_MEETING_NOT_OPEN": "剧情会议尚未开放", "STORY_MEETING_ALREADY_SETTLED": "剧情会议已经结算",
    "STORY_NOT_ENOUGH_ATTENDANCE": "出席人数不足", "STORY_VOTE_INVALID": "剧情投票参数无效",
    "STORY_FORBIDDEN_ACTOR": "当前 NPC 无权执行该剧情操作", "STORY_ACTION_INVALID": "剧情行动不适用于当前计划",
    "STORY_LOCATION_INVALID": "剧情行动地点不正确", "STORY_RESOURCE_INSUFFICIENT": "剧情资源不足",
    "STORY_FUND_INSUFFICIENT": "剧情资金不足", "STORY_TRUST_TOO_LOW": "信任度不足",
    "STORY_NOT_READY": "剧情当前尚未准备好", "STORY_REQUEST_CONFLICT": "剧情请求冲突，请刷新后重试",
    "STORY_RATION_LIMIT_REACHED": "剧情行动额度已用尽", "GOAL_VERSION_CONFLICT": "目标版本已变化，请刷新后重试",
    "INVALID_GOAL_SCHEMA": "主观目标格式无效", "HANDSHAKE_REQUIRED": "尚未完成 Hub 会话握手",
    "COMMAND_EXPIRED": "命令已过期", "COMMAND_NOT_FOUND": "命令不存在",
    "COMMAND_NOT_CANCELLABLE": "命令当前不可取消", "SETTLEMENT_IN_PROGRESS": "命令正在结算中",
}


def _outcome_message(action: str, result_code: str, success: bool) -> str:
    action = str(action or "UNKNOWN").upper()
    label = _OUTCOME_ACTION_LABELS.get(action, action)
    if result_code == ResultCode.COMMAND_ACCEPTED:
        return f"已接受「{label}」指令，正在执行。"
    if result_code == ResultCode.COMMAND_REPLAYED:
        return "该指令已处理过，返回已保存的结果。"
    if result_code == ResultCode.SLEEP_INTERRUPTED:
        return "已提前唤醒 NPC，睡眠被中断。"
    if success:
        return f"「{label}」已完成。"
    reason = _OUTCOME_REASONS.get(result_code)
    if reason is None:
        reason = f"服务端返回 {result_code or '未知结果'}"
    return f"「{label}」失败：{reason}。"


def _outcome_next_action(result_code: str) -> Optional[str]:
    if result_code == ResultCode.BACKEND_UNAVAILABLE:
        return "RETRY"
    if result_code in {ResultCode.NPC_BUSY, ResultCode.QUEUE_FULL, ResultCode.SETTLEMENT_IN_PROGRESS}:
        return "WAIT"
    if result_code in {"FOOD_NOT_AVAILABLE", "FOOD_NOT_ALLOWED", "P3_INSUFFICIENT_ITEM", "P3_INSUFFICIENT_MONEY", "TRADE_INSUFFICIENT_ITEM", "TRADE_INSUFFICIENT_MONEY"}:
        return "INSPECT_INVENTORY"
    if result_code in {"P3_PLOT_NOT_READY"}:
        return "INSPECT_FARM_STATE"
    if result_code in {"P3_RECIPE_NOT_ALLOWED", "LOCATION_CLOSED"}:
        return "INSPECT_RESTAURANT_STATE"
    if result_code in {"TRADE_LISTING_NOT_FOUND", "TRADE_LISTING_NOT_ACTIVE", "TRADE_STORE_CLOSED", "TRADE_STORE_FORBIDDEN", "TRADE_STORE_VERSION_CONFLICT"}:
        return "INSPECT_STORE_INVENTORY"
    if result_code in {"MARKETPLACE_LISTING_NOT_FOUND", "MARKETPLACE_LISTING_NOT_ACTIVE", "MARKETPLACE_FORBIDDEN"}:
        return "BROWSE_MARKETPLACE"
    if result_code in {"TRADE_OFFER_NOT_FOUND", "TRADE_OFFER_EXPIRED", "TRADE_OFFER_ALREADY_RESOLVED"}:
        return "INSPECT_TRADE_OFFERS"
    if result_code in {"P3_WRONG_LOCATION", "INVALID_ACTION_LOCATION", "TRADE_WRONG_LOCATION", "TRADE_NOT_COLOCATED", "SOCIAL_NOT_COLOCATED"}:
        return "MOVE"
    if result_code in {"STORY_VERSION_CONFLICT", "STORY_PHASE_INVALID", "STORY_NOT_READY", "STORY_REQUEST_CONFLICT"}:
        return "INSPECT_STORY_STATE"
    if result_code in {"INVALID_COMMAND_SCHEMA", "INVALID_ENVELOPE", "P3_INVALID_PARAMS", "SOCIAL_INVALID_PARAMS", "STORY_VOTE_INVALID", "STORY_ACTION_INVALID"}:
        return "FIX_COMMAND"
    return None


# ============================================================================
# 时间服务
# ============================================================================

class TimeService:
    """权威时间推进服务。"""

    @staticmethod
    @transaction.atomic
    def advance_time(
        world_minute: int,
        expected_time_revision: int,
        request_id: Optional[uuid.UUID] = None,
    ) -> Tuple[bool, str, Optional[VillageActionReceipt]]:
        """推进世界时间（CAS）。

        .. deprecated::
            保留仅供旧单元测试使用，不挂生产路由。生产时间写入统一走
            :meth:`receive_time`（Unity 权威，单调被动接收）。

        Returns:
            (success, result_code, receipt)
        """
        # 检查幂等
        if request_id:
            existing = VillageActionReceipt.objects.filter(request_id=request_id).first()
            if existing:
                return True, ResultCode.COMMAND_REPLAYED, existing

        # 锁定单例
        try:
            state = GlobalGameTimeState.objects.select_for_update().get(pk=1)
        except GlobalGameTimeState.DoesNotExist:
            return False, ResultCode.BACKEND_UNAVAILABLE, None

        # 校验时间不倒退
        if world_minute < state.world_minute:
            return False, ResultCode.WORLD_TIME_REGRESSION, None

        # 校验 revision
        if expected_time_revision != state.time_revision:
            return False, ResultCode.STALE_TIME_REVISION, None

        # 校验 world_minute 递增
        if world_minute != state.world_minute + 1:
            # 同一分钟重复提交返回第一次结果
            if world_minute == state.world_minute:
                existing = VillageActionReceipt.objects.filter(
                    action_kind="TIME_ADVANCE",
                    world_minute=world_minute,
                ).first()
                if existing:
                    return True, ResultCode.COMMAND_REPLAYED, existing
            return False, ResultCode.STALE_TIME_REVISION, None

        # 更新时间
        state.world_minute = world_minute
        state.time_revision += 1

        # 从 world_minute 推导日历
        from datetime import date
        total_minutes = world_minute
        ordinal = total_minutes // 1440 + 1
        minute_of_day = total_minutes % 1440
        d = date.fromordinal(ordinal)
        state.game_year = d.year
        state.game_month = d.month
        state.game_day = d.day
        state.game_hour = minute_of_day // 60
        state.game_minute = minute_of_day % 60

        state.save()
        from .dialogue_service import DialogueService
        DialogueService.expire_active(state.world_minute)

        # 创建 Receipt
        receipt = VillageActionReceipt.objects.create(
            request_id=request_id or uuid.uuid4(),
            action_kind="TIME_ADVANCE",
            success=True,
            result_code=ResultCode.COMMAND_ACCEPTED,
            world_minute=world_minute,
            time_revision=state.time_revision,
        )

        return True, ResultCode.COMMAND_ACCEPTED, receipt

    @staticmethod
    @transaction.atomic
    def receive_time(
        world_minute: int,
        time_revision: int,
    ) -> Tuple[bool, str, Optional[GlobalGameTimeState]]:
        """单调被动接收 Unity 权威时间（生产唯一时间写入入口）。

        - world_minute 前进：要求 time_revision > 当前 revision，保存并做日历投影；
        - world_minute == 当前：幂等成功，不修改 revision；
        - world_minute 回退：返回 WORLD_TIME_REGRESSION，不修改全局时间。

        Returns:
            (success, result_code, state)
        """
        try:
            state = GlobalGameTimeState.objects.select_for_update().get(pk=1)
        except GlobalGameTimeState.DoesNotExist:
            return False, ResultCode.BACKEND_UNAVAILABLE, None

        if world_minute < state.world_minute:
            return False, ResultCode.WORLD_TIME_REGRESSION, state
        if world_minute > state.world_minute:
            if time_revision <= state.time_revision:
                return False, ResultCode.STALE_TIME_REVISION, state
            state.apply_world_minute(world_minute)
            state.time_revision = time_revision
            state.save()
            from .dialogue_service import DialogueService
            DialogueService.expire_active(state.world_minute)
        return True, ResultCode.COMMAND_ACCEPTED, state


# ============================================================================
# Bootstrap 服务
# ============================================================================

class BootstrapService:
    """启动快照服务。"""

    @staticmethod
    @transaction.atomic
    def get_bootstrap() -> Dict[str, Any]:
        """获取启动快照。

        Returns:
            包含时间、NPC状态、配置等的字典
        """
        # Bootstrap is a recovery boundary.  The global time row is the first
        # lock acquired by every path that also changes/observes command
        # lifecycle state.  Holding it through the sweep and response build
        # makes the returned minute/revision a single authority snapshot, and
        # has the same lock order as TimeService.advance_time (time first).
        GlobalGameTimeState.get_instance()
        time_state = GlobalGameTimeState.objects.select_for_update().get(pk=1)
        from .dialogue_service import DialogueService
        DialogueService.expire_active(
            time_state.world_minute, include_unexpired=True, force_invalid=True
        )

        # Lock the formal ownership set before validating or serializing it.
        # Test-only NPCs are intentionally absent from this recovery boundary.
        npcs = list(
            NpcDefinition.objects.select_for_update()
            .filter(is_test_only=False)
            .order_by("agent_id")
        )
        formal_ids = {str(npc.agent_id) for npc in npcs}
        expected_formal_ids = {"10011", "10012", "10013", "10014", "10015"}
        if ((_fast_integration_enabled() and
             (not formal_ids or not formal_ids.issubset(expected_formal_ids))) or
                (not _fast_integration_enabled() and formal_ids != expected_formal_ids)):
            return {"error": ResultCode.BOOTSTRAP_NPC_SET_MISMATCH}

        runtime_states = list(
            NpcRuntimeState.objects.select_for_update()
            .filter(npc__is_test_only=False)
            .select_related("npc")
            .order_by("npc__agent_id")
        )
        if {str(rt.npc.agent_id) for rt in runtime_states} != formal_ids:
            return {"error": ResultCode.BOOTSTRAP_NPC_SET_MISMATCH}

        # Bootstrap is a recovery boundary, not a passive replay of commands
        # which are no longer executable.  Env may have crashed after accept
        # and before StartCommand.  Sweep that durable work through the same
        # authority path before exposing the recovery snapshot; it creates the
        # FAILED command, receipt, event and Outbox records atomically.
        CommandService.terminalize_expired_prestart_commands(
            time_state, formal_ids=formal_ids
        )

        # Terminalization may have changed runtime state.  Re-read it while
        # retaining the authority-time lock; this is the snapshot Env restores.
        time_state = GlobalGameTimeState.objects.select_for_update().get(pk=1)
        runtime_states = list(
            NpcRuntimeState.objects.select_for_update()
            .filter(npc__is_test_only=False)
            .select_related("npc")
            .order_by("npc__agent_id")
        )

        # 获取配置。动作时长在 Admin 中维护；缺少行时保留默认值，
        # 以便旧数据库或尚未迁移的环境仍可安全启动。
        config = VillageSimulationConfig.objects.first()
        action_durations = VillageActionDurationConfig.default_durations()
        navigation_timeouts = VillageActionDurationConfig.default_navigation_timeouts()
        for action_name, duration_minutes, timeout_seconds in VillageActionDurationConfig.objects.values_list(
            "action_name", "duration_minutes", "navigation_timeout_seconds"
        ):
            if duration_minutes:
                action_durations[action_name] = duration_minutes
            if timeout_seconds:
                navigation_timeouts[action_name] = timeout_seconds

        active_commands = list(
            NpcCommandState.objects.select_for_update()
            .filter(status__in=["RECEIVED", "QUEUED", "RUNNING", "SETTLING"])
            .select_related("npc")
            .order_by("created_at")
        )

        def serialize_command(command: NpcCommandState) -> Dict[str, Any]:
            """Return the complete, replay-safe command wire data required by Env recovery."""
            return {
                "envelope_id": str(command.message_id),
                "timestamp": command.sent_at_ms,
                "sender": command.sender_id,
                "recipient": command.recipient_id,
                "content": {
                    "schema_version": command.schema_version,
                    "id": str(command.command_id),
                    "request_id": str(command.request_id),
                    "name": command.kind.lower(),
                    "target_id": command.target_id,
                    "params": command.sanitized_parameters,
                    "expected_state_version": command.validated_at_state_version,
                    "queue_mode": command.queue_mode,
                    "not_before_world_minute": command.not_before_world_minute,
                    "expires_world_minute": command.expires_world_minute,
                    "ttl_ms": command.ttl_ms,
                    "duration_minutes": command.duration_minutes,
                },
                "status": command.status,
                "started_world_minute": command.started_world_minute,
                "completed_world_minute": command.completed_world_minute,
                "state_version": command.state_version,
            }

        # 构建响应
        npc_data = []
        for rt in runtime_states:
            # 获取活动命令
            from .phase2_services import need_bands

            npc_commands = [c for c in active_commands if c.npc_id == rt.npc_id]
            active = next((c for c in npc_commands if c.status in {"RUNNING", "SETTLING"}), None)

            npc_data.append({
                "npc_id": str(rt.npc.agent_id),
                "name": rt.npc.name,
                "current_location_code": rt.current_location_code,
                "current_point_code": rt.current_point_code,
                "current_goal": "NONE",
                "current_action": active.kind if active else rt.current_action,
                "action_status": rt.action_status if active else "IDLE",
                "action_started_world_minute": active.started_world_minute if active else None,
                "action_ends_world_minute": (
                    active.started_world_minute + active.duration_minutes
                    if active and active.started_world_minute is not None and active.duration_minutes
                    else None
                ),
                "active_command_id": str(active.command_id) if active else None,
                "state_version": rt.state_version,
                "last_simulated_world_minute": rt.last_simulated_world_minute,
                "time_revision": rt.time_revision,
                "needs_world_minute": rt.needs_world_minute,
                "public_needs": {
                    "hunger": rt.hunger_fp / 10,
                    "energy": rt.energy_fp / 10,
                    "health": rt.health_fp / 10,
                    "social": rt.social_fp / 10,
                    "stress": rt.stress_fp / 10,
                },
                "need_bands": need_bands(rt),
                "active_reservation": None,
                "active_command": serialize_command(active) if active else None,
                "queued_commands": [serialize_command(c) for c in npc_commands if c is not active],
            })

        return {
            "world_minute": time_state.world_minute,
            "time_revision": time_state.time_revision,
            "game_year": time_state.game_year,
            "game_month": time_state.game_month,
            "game_day": time_state.game_day,
            "game_hour": time_state.game_hour,
            "game_minute": time_state.game_minute,
            "season": time_state.season,
            "npcs": npc_data,
            "commands": [serialize_command(command) for command in active_commands],
            "config": {
                "tick_interval_seconds": config.tick_interval_seconds if config else 5,
                "hub_command_timeout_seconds": config.hub_command_timeout_seconds if config else 30,
                "command_queue_limit": config.command_queue_limit if config else 10,
                "day_start_minute": config.day_start_minute if config else 360,
                "day_close_minute": config.day_close_minute if config else 1320,
                "village_simulation_enabled": config.village_simulation_enabled if config else False,
                "village_hub_command_adapter_enabled": config.village_hub_command_adapter_enabled if config else False,
                "village_needs_enabled": config.village_needs_enabled if config else False,
                "village_economy_enabled": config.village_economy_enabled if config else False,
                "village_social_enabled": config.village_social_enabled if config else False,
                "village_story_enabled": config.village_story_enabled if config else False,
                "village_presentation_enabled": config.village_presentation_enabled if config else False,
                # action_durations is the unified Phase 1/2/3 timer map.
                "action_durations": action_durations,
                "navigation_action_timeout_seconds": navigation_timeouts,
                # Compatibility for Env builds that only know Phase 3 durations.
                "economy_action_durations": {
                    action: action_durations[action]
                    for action, metadata in VillageActionDurationConfig.ACTION_METADATA.items()
                    if metadata[0] == "阶段三"
                    if action in action_durations
                },
            },
        }


# ============================================================================
# Checkpoint 服务
# ============================================================================

class CheckpointService:
    """批量状态检查点服务。"""

    # A checkpoint is a recovery hint from Env, not an authority-state update.
    # In particular it must never be able to claim ownership of a command or
    # change its lifecycle/time snapshot.  Keep this tuple ordered so both the
    # persisted update and validation behaviour remain deterministic.
    ALLOWED_FIELDS = (
        "current_location_code",
        "current_point_code",
        "current_goal",
        "last_simulated_world_minute",
    )
    _IDENTITY_FIELDS = {"npc_id", "expected_state_version"}
    _GOALS = {choice for choice, _label in NpcRuntimeState.GOAL_CHOICES}

    @staticmethod
    def _invalid_checkpoint(npc_id: Any) -> Dict[str, Any]:
        return {
            "npc_id": npc_id,
            "success": False,
            "result_code": ResultCode.INVALID_COMMAND_SCHEMA,
        }

    @classmethod
    def _validate_checkpoint(cls, checkpoint: Any) -> Optional[Dict[str, Any]]:
        """Return a stable rejection result for malformed checkpoint input."""
        if not isinstance(checkpoint, dict):
            return cls._invalid_checkpoint(None)

        npc_id = checkpoint.get("npc_id")
        expected_version = checkpoint.get("expected_state_version")
        if (
            any(not isinstance(field, str) for field in checkpoint)
            or
            isinstance(npc_id, bool)
            or not isinstance(npc_id, (str, int))
            or not str(npc_id).isascii()
            or not str(npc_id).isdigit()
            or len(str(npc_id)) > 100
            or isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 0
        ):
            return cls._invalid_checkpoint(npc_id)

        invalid_fields = sorted(
            set(checkpoint) - set(cls.ALLOWED_FIELDS) - cls._IDENTITY_FIELDS
        )
        if invalid_fields:
            return {
                "npc_id": npc_id,
                "success": False,
                "result_code": ResultCode.CHECKPOINT_FIELD_NOT_ALLOWED,
                "invalid_fields": invalid_fields,
            }

        submitted_fields = [field for field in cls.ALLOWED_FIELDS if field in checkpoint]
        if not submitted_fields:
            return cls._invalid_checkpoint(npc_id)

        for field in ("current_location_code", "current_point_code"):
            if field in checkpoint and (
                not isinstance(checkpoint[field], str)
                or not checkpoint[field]
                or len(checkpoint[field]) > 64
            ):
                return cls._invalid_checkpoint(npc_id)
        if "current_goal" in checkpoint and checkpoint["current_goal"] not in cls._GOALS:
            return cls._invalid_checkpoint(npc_id)
        if "last_simulated_world_minute" in checkpoint and (
            isinstance(checkpoint["last_simulated_world_minute"], bool)
            or not isinstance(checkpoint["last_simulated_world_minute"], int)
            or checkpoint["last_simulated_world_minute"] < 0
        ):
            return cls._invalid_checkpoint(npc_id)
        return None

    @staticmethod
    @transaction.atomic
    def submit_checkpoint(
        checkpoints: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """提交检查点。

        Args:
            checkpoints: 包含 npc_id、expected_state_version 和白名单字段的列表

        Returns:
            每个 NPC 的结果列表
        """
        if not isinstance(checkpoints, list):
            return [CheckpointService._invalid_checkpoint(None)]

        # A checkpoint that carries the simulation cursor observes authority
        # time.  Acquire that row before any NPC runtime row so it cannot form
        # an inverted lock order with Bootstrap recovery.
        locked_time_state = None
        if any(
            isinstance(checkpoint, dict)
            and "last_simulated_world_minute" in checkpoint
            for checkpoint in checkpoints
        ):
            GlobalGameTimeState.get_instance()
            try:
                locked_time_state = GlobalGameTimeState.objects.select_for_update().get(pk=1)
            except GlobalGameTimeState.DoesNotExist:
                return [{
                    "npc_id": checkpoint.get("npc_id") if isinstance(checkpoint, dict) else None,
                    "success": False,
                    "result_code": ResultCode.BACKEND_UNAVAILABLE,
                } for checkpoint in checkpoints]

        results = []

        for cp in checkpoints:
            validation_error = CheckpointService._validate_checkpoint(cp)
            if validation_error:
                results.append(validation_error)
                continue

            npc_id = cp["npc_id"]
            expected_version = cp["expected_state_version"]

            # 锁定 NPC 运行时状态
            try:
                rt = NpcRuntimeState.objects.select_for_update().get(
                    npc__agent_id=npc_id,
                    npc__is_test_only=False,
                )
            except NpcRuntimeState.DoesNotExist:
                results.append({
                    "npc_id": npc_id,
                    "success": False,
                    "result_code": ResultCode.BOOTSTRAP_NPC_SET_MISMATCH,
                })
                continue

            # select_for_update + this comparison is the checkpoint CAS.  The
            # row remains locked through the write, so a second checkpoint with
            # the same expected version deterministically observes this write.
            if rt.state_version != expected_version:
                results.append({
                    "npc_id": npc_id,
                    "success": False,
                    "result_code": ResultCode.STALE_STATE_VERSION,
                    "current_state_version": rt.state_version,
                })
                continue

            if "last_simulated_world_minute" in cp:
                # The local simulation cursor is permitted for recovery, but
                # it cannot be used to skip ahead of Backend's authority time.
                time_state = locked_time_state
                if cp["last_simulated_world_minute"] > time_state.world_minute:
                    results.append(CheckpointService._invalid_checkpoint(npc_id))
                    continue
                if cp["last_simulated_world_minute"] < rt.last_simulated_world_minute:
                    results.append(CheckpointService._invalid_checkpoint(npc_id))
                    continue

            # Update only recovery-safe fields and advance the runtime version
            # in the same write.  No lifecycle, command, authority-time, or
            # asset field can be altered by this endpoint.
            submitted_fields = []
            for field in CheckpointService.ALLOWED_FIELDS:
                if field in cp:
                    setattr(rt, field, cp[field])
                    submitted_fields.append(field)
            rt.state_version += 1
            rt.save(update_fields=[*submitted_fields, "state_version", "updated_at"])
            results.append({
                "npc_id": npc_id,
                "success": True,
                "state_version": rt.state_version,
            })

        return results


# ============================================================================
# Command 服务
# ============================================================================

class CommandService:
    """命令生命周期服务。"""

    ALLOWED_ACTIONS = frozenset(COMMAND_CATALOG)
    READ_QUERY_ACTIONS = frozenset({
        "OBSERVE", "INSPECT_NEARBY", "INSPECT_INVENTORY",
        "INSPECT_TRADE_OFFERS", "INSPECT_STORY_STATE", "INSPECT_RELEVANT_STORIES",
        "INSPECT_STORY_DETAIL", "INSPECT_ORDERS",
        "INSPECT_MARKET_STATE", "INSPECT_FARM_STATE",
        "INSPECT_RESTAURANT_STATE", "INSPECT_RATEABLE_DISHES",
        "INSPECT_PUBLIC_ECONOMY_STATE",
        "BROWSE_MARKETPLACE", "CHECK_MY_MARKETPLACE_LISTINGS",
        "CHECK_MARKETPLACE_HISTORY",
        "INSPECT_STORE_INVENTORY", "INSPECT_SOCIAL_STATE",
    })
    STORY_ACTIONS = {"STORY_VOTE", "STORY_ACT"}
    MAX_QUEUE_SIZE = 10
    MAX_CLOCK_SKEW_MS = 5 * 60 * 1000
    PHASE2_ACTIONS = {"EAT", "REST", "SLEEP", "SOCIALIZE", "WORK_PLACEHOLDER"}
    DIALOGUE_ACTIONS = {"START_DIALOGUE", "SPEAK"}
    PHASE3_ACTIONS = {
        "PLANT", "HARVEST", "HUNT", "COOK", "CREATE_ORDER", "CANCEL_ORDER",
        "START_DELIVERY", "IMPORT_GOODS", "EXPORT_GOODS", "RATE_DISH",
        "PUBLISH_PRICE_INDEX", "AUDIT_PUBLIC_FUND", "PROPOSE_TRADE",
        "RESPOND_TRADE", "CANCEL_TRADE_OFFER", "LIST_STORE_ITEM",
        "UPDATE_STORE_PRICE", "DELIST_STORE_ITEM", "BUY_FROM_STORE",
        "BUY_FROM_MARKETPLACE", "SELL_ON_MARKETPLACE", "DELIST_FROM_MARKETPLACE",
        "INSPECT_STORE_INVENTORY",
    }
    DIRECT_TRADE_ACTIONS = {
        "PROPOSE_TRADE", "RESPOND_TRADE", "CANCEL_TRADE_OFFER",
        "LIST_STORE_ITEM", "UPDATE_STORE_PRICE", "DELIST_STORE_ITEM",
        "BUY_FROM_STORE", "BUY_FROM_MARKETPLACE", "SELL_ON_MARKETPLACE",
        "DELIST_FROM_MARKETPLACE", "BROWSE_MARKETPLACE",
        "CHECK_MY_MARKETPLACE_LISTINGS", "CHECK_MARKETPLACE_HISTORY",
        "INSPECT_STORE_INVENTORY",
    }
    PHASE4_ACTIONS = {
        "INSPECT_SOCIAL_STATE", "CREATE_COMMITMENT", "CANCEL_COMMITMENT",
        "PERFORM_COMMITMENT",
    }
    NEEDS_FIELDS = {"hunger", "energy", "health", "social", "stress"}
    WORK_ACTIONS = {
        "WORK_PLACEHOLDER", "PLANT", "HARVEST", "HUNT", "COOK", "START_DELIVERY",
    }
    LOW_HEALTH_ACTIONS = READ_QUERY_ACTIONS | {"MOVE", "GO_HOME", "WAIT", "EAT", "REST", "SLEEP"}
    VARIABLE_DURATION_ACTIONS = {"REST", "SLEEP", "SOCIALIZE", "WORK_PLACEHOLDER"}

    @staticmethod
    def allowed_actions_for(npc: NpcDefinition) -> set[str]:
        """Return the configured command allow-list for one formal NPC."""
        try:
            configured = npc.village_profile.capabilities
        except NpcProfile.DoesNotExist:
            # Formal village bootstrap requires a profile. Keep isolated legacy
            # and test NPC definitions compatible outside that runtime.
            return set(CommandService.ALLOWED_ACTIONS)
        return {
            str(action).strip().upper()
            for action in configured
            if str(action).strip().upper() in CommandService.ALLOWED_ACTIONS
        }

    @staticmethod
    def unavailable_reason(action_name: str, runtime: NpcRuntimeState) -> Optional[str]:
        action_name = str(action_name).upper()
        if runtime.health_fp <= 300 and action_name not in CommandService.LOW_HEALTH_ACTIONS:
            return ResultCode.HEALTH_TOO_LOW
        if action_name in CommandService.WORK_ACTIONS:
            if runtime.hunger_fp >= 900:
                return ResultCode.HUNGER_TOO_HIGH
            if runtime.energy_fp <= 100:
                return ResultCode.ENERGY_TOO_LOW
        return None

    @staticmethod
    def apply_admin_duration(action_name: str, content: Dict[str, Any]) -> None:
        """Replace a wire duration with the Backend-managed value for known timers."""
        if action_name not in VillageActionDurationConfig.DEFAULT_DURATIONS:
            return
        if action_name in CommandService.VARIABLE_DURATION_ACTIONS and content.get("duration_minutes") is not None:
            return
        duration_minutes = VillageActionDurationConfig.default_durations()[action_name]
        configured = VillageActionDurationConfig.objects.filter(
            action_name=action_name
        ).values_list("duration_minutes", flat=True).first()
        if configured:
            duration_minutes = configured
        content["duration_minutes"] = duration_minutes

    @staticmethod
    def compute_payload_sha256(content: Dict[str, Any]) -> str:
        """计算 payload SHA256。"""
        canonical = json.dumps(content, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def validate_phase2_command(action_name: str, content: Dict[str, Any]) -> Optional[str]:
        if action_name not in CommandService.PHASE2_ACTIONS:
            return None
        params = content.get("params")
        if CommandService.NEEDS_FIELDS.intersection(content) or (
            isinstance(params, dict) and CommandService.NEEDS_FIELDS.intersection(params)
        ):
            return ResultCode.NEEDS_FIELDS_NOT_ALLOWED
        if _fast_integration_enabled():
            if not isinstance(params, dict):
                return ResultCode.INVALID_COMMAND_SCHEMA
            if action_name == "EAT":
                source = params.get("source")
                if source == "PERSONAL_INVENTORY":
                    try:
                        uuid.UUID(str(params.get("food_instance_id")))
                    except (ValueError, TypeError):
                        return ResultCode.INVALID_COMMAND_SCHEMA
                elif source != "COMMUNITY_MEAL":
                    return ResultCode.FOOD_NOT_ALLOWED
            if action_name == "SOCIALIZE":
                if not str(params.get("target_npc_id", "")):
                    return ResultCode.INVALID_COMMAND_SCHEMA
                try:
                    uuid.UUID(str(params.get("reservation_id")))
                except (ValueError, TypeError):
                    return ResultCode.INVALID_COMMAND_SCHEMA
            return None
        start = content.get("not_before_world_minute")
        expires = content.get("expires_world_minute")
        duration = content.get("duration_minutes")
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (start, expires, duration)):
            return ResultCode.INVALID_COMMAND_SCHEMA
        limits = {
            "EAT": (30, 30), "REST": (30, 120), "SLEEP": (30, 600),
            "SOCIALIZE": (30, 120), "WORK_PLACEHOLDER": (30, 240),
        }
        if start < 0 or expires < start or duration % 30 or not limits[action_name][0] <= duration <= limits[action_name][1]:
            return ResultCode.INVALID_ACTION_DURATION
        if action_name == "WORK_PLACEHOLDER" and start // 1440 != (start + duration - 1) // 1440:
            return ResultCode.INVALID_ACTION_DURATION
        allowed_params = {
            "EAT": {"source", "food_instance_id", "expected_asset_version"},
            "REST": set(), "SLEEP": set(), "WORK_PLACEHOLDER": set(),
            "SOCIALIZE": {"target_npc_id", "reservation_id"},
        }
        if not isinstance(params, dict) or not set(params).issubset(allowed_params[action_name]):
            return ResultCode.INVALID_COMMAND_SCHEMA
        if action_name == "EAT":
            source = params.get("source")
            if source == "PERSONAL_INVENTORY":
                try:
                    uuid.UUID(str(params.get("food_instance_id")))
                except (ValueError, TypeError):
                    return ResultCode.INVALID_COMMAND_SCHEMA
                if not {"source", "food_instance_id"}.issubset(params):
                    return ResultCode.INVALID_COMMAND_SCHEMA
                version = params.get("expected_asset_version")
                if version is not None and (isinstance(version, bool) or not isinstance(version, int) or version < 0):
                    return ResultCode.INVALID_COMMAND_SCHEMA
            elif source == "COMMUNITY_MEAL":
                if set(params) != {"source"}:
                    return ResultCode.INVALID_COMMAND_SCHEMA
            else:
                return ResultCode.FOOD_NOT_ALLOWED
        if action_name == "SOCIALIZE":
            if set(params) != allowed_params[action_name] or not str(params.get("target_npc_id", "")):
                return ResultCode.INVALID_COMMAND_SCHEMA
            try:
                uuid.UUID(str(params.get("reservation_id")))
            except (ValueError, TypeError):
                return ResultCode.INVALID_COMMAND_SCHEMA
        return None

    @staticmethod
    @transaction.atomic
    def accept_command(
        envelope_id: str,
        sender: str,
        recipient: str,
        content: Dict[str, Any],
        timestamp_ms: Optional[int] = None,
    ) -> Tuple[bool, str, Optional[NpcCommandState]]:
        """接受命令。

        Returns:
            (success, result_code, command_state)
        """
        # 校验 sender
        fast_integration = _fast_integration_enabled()
        content = dict(content or {})
        server_resolved_intent = content.pop("_server_resolved_intent", False) is True
        content["params"] = dict(content.get("params") or {})
        if fast_integration:
            content["schema_version"] = "village.command.v1"
            content["queue_mode"] = "STRICT"
            content["ttl_ms"] = 86_400_000
            content["not_before_world_minute"] = 0
            content["expires_world_minute"] = None
            timestamp_ms = timestamp_ms or int(timezone.now().timestamp() * 1000)
            action = str(content.get("name", "")).upper()
            if action == "EAT":
                content["duration_minutes"] = 30
                if content["params"].get("source") == "PERSONAL_INVENTORY":
                    content["params"].setdefault("expected_asset_version", 0)
            elif action in CommandService.PHASE2_ACTIONS:
                requested = content.get("duration_minutes")
                requested = requested if isinstance(requested, int) and not isinstance(requested, bool) else 30
                requested = max(30, ((max(1, requested) + 29) // 30) * 30)
                maximum = 600 if action == "SLEEP" else 240 if action == "WORK_PLACEHOLDER" else 120
                content["duration_minutes"] = min(requested, maximum)
                if action == "SOCIALIZE":
                    content["params"].setdefault("reservation_id", str(content.get("request_id", "")))

        try:
            message_id = uuid.UUID(str(envelope_id))
            command_id = uuid.UUID(str(content["id"]))
            request_id = uuid.UUID(str(content["request_id"]))
            timestamp_ms = int(timestamp_ms)
        except (KeyError, TypeError, ValueError, AttributeError):
            return False, ResultCode.INVALID_COMMAND_SCHEMA, None
        if (not fast_integration and timestamp_ms < 1_000_000_000_000) or not isinstance(content.get("params"), dict):
            return False, ResultCode.INVALID_COMMAND_SCHEMA, None
        if "parameters" in content or (not fast_integration and content.get("queue_mode") != "STRICT"):
            return False, ResultCode.INVALID_COMMAND_SCHEMA, None
        ttl_ms = content.get("ttl_ms")
        if not fast_integration and (not isinstance(ttl_ms, int) or ttl_ms <= 0):
            return False, ResultCode.INVALID_COMMAND_SCHEMA, None
        now_ms = int(timezone.now().timestamp() * 1000)
        if not fast_integration and timestamp_ms > now_ms + CommandService.MAX_CLOCK_SKEW_MS:
            return False, ResultCode.CLOCK_SKEW, None
        if not fast_integration and timestamp_ms + ttl_ms < now_ms:
            return False, ResultCode.MESSAGE_EXPIRED, None
        try:
            npc = NpcDefinition.objects.get(agent_id=int(sender), is_test_only=False)
        except (ValueError, NpcDefinition.DoesNotExist):
            return False, ResultCode.SENDER_BINDING_INVALID, None

        # 校验 schema_version
        schema_version = content.get("schema_version", "")
        if schema_version != "village.command.v1":
            return False, ResultCode.PROTOCOL_VERSION_UNSUPPORTED, None

        # 校验 action
        action_name = content.get("name", "").upper()
        if action_name not in CommandService.ALLOWED_ACTIONS:
            return False, ResultCode.UNKNOWN_ACTION, None
        if action_name not in CommandService.allowed_actions_for(npc):
            return False, ResultCode.ACTION_NOT_ALLOWED, None
        if action_name in CommandService.READ_QUERY_ACTIONS:
            return False, ResultCode.ACTION_NOT_ALLOWED, None
        if action_name in CommandService.DIALOGUE_ACTIONS:
            return False, ResultCode.ACTION_NOT_ALLOWED, None
        phase2_validation = CommandService.validate_phase2_command(action_name, content)
        if phase2_validation:
            return False, phase2_validation, None
        if action_name == "MOVE":
            params = content.get("params")
            if (not isinstance(params, dict) or set(params) not in (
                {"location_code"}, {"target_npc_id"}
            )):
                return False, ResultCode.INVALID_COMMAND_SCHEMA, None
            if "target_npc_id" in params and not NpcDefinition.objects.filter(
                agent_id=str(params["target_npc_id"]), is_test_only=False
            ).exists():
                return False, ResultCode.SOCIAL_TARGET_INVALID, None
            if str(params.get("target_npc_id", "")) == str(sender):
                return False, ResultCode.SOCIAL_TARGET_INVALID, None
        # Preserve protocol validation of the caller-provided value first, then
        # make normal commands use the Backend-administered duration.  Fast
        # integration intentionally keeps its caller duration for harnesses.
        if not fast_integration:
            CommandService.apply_admin_duration(action_name, content)
        if action_name in CommandService.PHASE2_ACTIONS and not fast_integration:
            if not VillageSimulationConfig.objects.filter(
                config_key="default", village_needs_enabled=True
            ).exists():
                return False, ResultCode.VILLAGE_NEEDS_DISABLED, None

        # 提取 ID
        message_id = uuid.UUID(envelope_id)
        command_id = uuid.UUID(content.get("id", str(uuid.uuid4())))
        request_id = uuid.UUID(content.get("request_id", str(uuid.uuid4())))

        # 检查 message_id 幂等
        existing = NpcCommandState.objects.filter(message_id=message_id).first()
        if existing:
            existing_sha = CommandService.compute_payload_sha256(content)
            if existing.payload_sha256 == existing_sha:
                return True, ResultCode.COMMAND_REPLAYED, existing
            else:
                return False, ResultCode.MESSAGE_ID_CONFLICT, None

        # 检查 command_id 幂等
        existing = NpcCommandState.objects.filter(command_id=command_id).first()
        if existing:
            existing_sha = CommandService.compute_payload_sha256(content)
            if existing.payload_sha256 == existing_sha:
                return True, ResultCode.COMMAND_REPLAYED, existing
            else:
                return False, ResultCode.COMMAND_ID_CONFLICT, None

        # 检查 request_id 幂等
        existing = NpcCommandState.objects.filter(request_id=request_id).first()
        if existing:
            existing_sha = CommandService.compute_payload_sha256(content)
            if existing.payload_sha256 == existing_sha:
                return True, ResultCode.COMMAND_REPLAYED, existing
            else:
                return False, ResultCode.REQUEST_ID_CONFLICT, None

        # 计算 payload hash
        payload_sha256 = CommandService.compute_payload_sha256(content)

        # Acquire authority time before runtime state.  Bootstrap uses this
        # same global -> command/runtime lock order for recovery snapshots.
        GlobalGameTimeState.get_instance()
        time_state = GlobalGameTimeState.objects.select_for_update().get(pk=1)

        # 校验队列上限
        queue_count = NpcCommandState.objects.filter(
            npc=npc,
            status__in=["RECEIVED", "QUEUED", "RUNNING"],
        ).count()
        if not fast_integration and queue_count >= CommandService.MAX_QUEUE_SIZE:
            return False, ResultCode.QUEUE_FULL, None

        # 获取当前时间
        # ``time_state`` remains the authority row locked above.

        # 解析参数
        try:
            runtime = NpcRuntimeState.objects.select_for_update().get(npc=npc)
        except NpcRuntimeState.DoesNotExist:
            return False, ResultCode.BOOTSTRAP_NPC_SET_MISMATCH, None
        expected_version = content.get("expected_state_version")
        if fast_integration or server_resolved_intent or expected_version is None:
            expected_version = runtime.state_version
            content["expected_state_version"] = expected_version
        if not isinstance(expected_version, int):
            return False, ResultCode.INVALID_COMMAND_SCHEMA, None
        if not fast_integration and expected_version != runtime.state_version:
            return False, ResultCode.STALE_STATE_VERSION, None
        if (not fast_integration and content.get("expires_world_minute") is not None and
                content["expires_world_minute"] < time_state.world_minute):
            return False, ResultCode.MESSAGE_EXPIRED, None
        params = content["params"]
        location_code = params.get("location_code", "")
        point_code = params.get("point_code", "")
        if action_name in CommandService.PHASE2_ACTIONS:
            location_code = runtime.current_location_code

        # 创建命令
        command = NpcCommandState.objects.create(
            command_id=command_id,
            message_id=message_id,
            request_id=request_id,
            npc=npc,
            schema_version=schema_version,
            sender_id=sender,
            recipient_id=recipient,
            kind=action_name,
            target_id=content.get("target_id", ""),
            location_code=location_code or point_code,
            sanitized_parameters=params,
            payload_sha256=payload_sha256,
            queue_mode=content.get("queue_mode", "STRICT"),
            status="RECEIVED",
            sent_at_ms=timestamp_ms,
            received_at_ms=now_ms,
            ttl_ms=ttl_ms,
            not_before_world_minute=content.get("not_before_world_minute"),
            expires_world_minute=content.get("expires_world_minute"),
            duration_minutes=content.get("duration_minutes"),
            received_world_minute=time_state.world_minute,
            validated_at_state_version=expected_version,
        )

        # Story mutations already have a Backend transaction service. Keep the
        # command row as the durable lifecycle anchor instead of introducing a
        # second executor in Unity.
        if action_name in CommandService.STORY_ACTIONS:
            from .phase5_story import dispatch_story_command
            story_payload = dict(content)
            story_payload["command"] = action_name
            story_payload["npc_id"] = str(sender)
            story_payload["current_location_code"] = runtime.current_location_code
            story_result = dispatch_story_command(story_payload)
            story_code = story_result.get("result_code", ResultCode.ACTION_NOT_ALLOWED)
            receipt = VillageActionReceipt.objects.filter(request_id=request_id).first()
            if receipt is not None:
                receipt.command = command
                receipt.save(update_fields=["command"])
                command.status = "SUCCEEDED" if receipt.success else "FAILED"
                command.result_code = receipt.result_code
                command.completed_world_minute = receipt.world_minute
                command.state_version = runtime.state_version
                command.save(update_fields=["status", "result_code", "completed_world_minute", "state_version", "updated_at"])
                OutboxService.ensure_outcome(receipt)
                return receipt.success, story_code, command
            ActionService.complete_action(
                command.command_id, command.request_id, False, story_code,
                message="Story command rejected by Backend story rules.",
                data=story_result, allow_prestart_failure=True,
            )
            return False, story_code, command

        return True, ResultCode.COMMAND_ACCEPTED, command

    @staticmethod
    def serialize_status(command: NpcCommandState) -> Dict[str, Any]:
        receipt = VillageActionReceipt.objects.filter(command=command).first()
        return {
            "command_id": str(command.command_id),
            "message_id": str(command.message_id),
            "request_id": str(command.request_id),
            "npc_id": str(command.npc.agent_id),
            "action": command.kind,
            "status": command.status,
            "result_code": command.result_code,
            "state_version": command.state_version,
            "received_world_minute": command.received_world_minute,
            "started_world_minute": command.started_world_minute,
            "completed_world_minute": command.completed_world_minute,
            "receipt": {
                "success": receipt.success,
                "result_code": receipt.result_code,
                "data": receipt.response_data,
            } if receipt else None,
        }

    @staticmethod
    @transaction.atomic
    def get_status(command_id: uuid.UUID) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        command = NpcCommandState.objects.select_related("npc").filter(command_id=command_id).first()
        if command is None:
            return False, ResultCode.COMMAND_NOT_FOUND, None
        return True, command.result_code or ResultCode.COMMAND_ACCEPTED, CommandService.serialize_status(command)

    @staticmethod
    @transaction.atomic
    def cancel_command(command_id: uuid.UUID) -> Tuple[bool, str, Optional[NpcCommandState]]:
        command = NpcCommandState.objects.select_for_update().filter(command_id=command_id).first()
        if command is None:
            return False, ResultCode.COMMAND_NOT_FOUND, None
        if command.status in {"SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED"}:
            return True, ResultCode.COMMAND_REPLAYED, command
        if command.status == "SETTLING":
            return False, ResultCode.SETTLEMENT_IN_PROGRESS, command
        if command.status not in {"RECEIVED", "QUEUED", "RUNNING"}:
            return False, ResultCode.COMMAND_NOT_CANCELLABLE, command
        ok, code, _receipt = ActionService.complete_action(
            command.command_id,
            command.request_id,
            False,
            ResultCode.COMMAND_CANCELLED_ON_RECOVERY,
            message="Command cancelled by the authority.",
            allow_prestart_failure=command.status in {"RECEIVED", "QUEUED"},
        )
        if ok:
            command.status = "CANCELLED"
            command.save(update_fields=["status", "updated_at"])
        return ok, code, command

    @staticmethod
    def terminalize_expired_prestart_commands(
        time_state: GlobalGameTimeState,
        *,
        formal_ids: Optional[set[str]] = None,
    ) -> None:
        """Use StartCommand to terminalize work that expired while Env was down.

        IDs are materialized before invoking the transactional StartCommand
        path.  Consequently recovery uses the normal receipt/event/Outbox
        transaction instead of permitting a Unity-generated outcome.
        """
        now_ms = int(timezone.now().timestamp() * 1000)
        formal_ids = formal_ids or {"10011", "10012", "10013", "10014", "10015"}
        expired_ids = list(
            NpcCommandState.objects.select_for_update()
            .filter(
                status__in=["RECEIVED", "QUEUED"],
                npc__is_test_only=False,
                npc__agent_id__in=[int(npc_id) for npc_id in formal_ids],
            )
            .filter(
                Q(
                    received_at_ms__isnull=False,
                    ttl_ms__isnull=False,
                    received_at_ms__lt=now_ms - F("ttl_ms"),
                )
                | Q(
                    expires_world_minute__isnull=False,
                    expires_world_minute__lt=time_state.world_minute,
                )
            )
            .order_by("created_at")
            .values_list("command_id", flat=True)
        )
        for command_id in expired_ids:
            # A concurrent StartCommand may have already claimed the command;
            # that durable terminal/running status remains authoritative.
            CommandService.start_command(command_id)

    @staticmethod
    @transaction.atomic
    def start_command(
        command_id: uuid.UUID,
        start_world_minute: Optional[int] = None,
    ) -> Tuple[bool, str, Optional[NpcCommandState]]:
        """开始命令执行。

        Args:
            command_id: 命令 ID。
            start_world_minute: Unity 权威的实际开始世界分钟；为 None 时（旧调用方/
                过期扫描等恢复路径）回退后端本地钟。

        Returns:
            (success, result_code, command_state)
        """
        # Global authority time is the canonical first lock for lifecycle
        # paths.  Bootstrap holds this same row while building recovery state,
        # so a StartCommand cannot observe or create a half-recovery snapshot.
        fast_integration = _fast_integration_enabled()
        GlobalGameTimeState.get_instance()
        time_state = GlobalGameTimeState.objects.select_for_update().get(pk=1)

        try:
            command = NpcCommandState.objects.select_for_update().get(command_id=command_id)
        except NpcCommandState.DoesNotExist:
            return False, ResultCode.INVALID_COMMAND_SCHEMA, None

        # 校验状态
        if command.status in {"SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED"}:
            return True, ResultCode.COMMAND_REPLAYED, command
        if command.status == "SETTLING":
            return False, ResultCode.SETTLEMENT_IN_PROGRESS, command
        if command.status not in ["RECEIVED", "QUEUED"]:
            return False, ResultCode.NPC_BUSY, None

        # SOCIALIZE acquires both runtime rows in stable agent-id order before
        # any reservation is created.  Every later start sees the HELD shared
        # reservation and refuses to claim either participant.
        social_runtimes = None
        social_reservation_id = None
        if command.kind == "SOCIALIZE":
            target_id = str(command.sanitized_parameters.get("target_npc_id", ""))
            try:
                social_reservation_id = uuid.UUID(str(command.sanitized_parameters.get("reservation_id")))
            except (ValueError, TypeError):
                return False, ResultCode.INVALID_COMMAND_SCHEMA, None
            participant_ids = sorted({str(command.npc.agent_id), target_id})
            if len(participant_ids) != 2:
                return False, ResultCode.SOCIAL_TARGET_INVALID, None
            social_runtimes = list(
                NpcRuntimeState.objects.select_for_update().select_related("npc")
                .filter(npc__agent_id__in=participant_ids, npc__is_test_only=False)
                .order_by("npc__agent_id")
            )
            if len(social_runtimes) != 2:
                return False, ResultCode.SOCIAL_TARGET_INVALID, None
            rt = next((row for row in social_runtimes if row.npc_id == command.npc_id), None)
            if rt is None or (not fast_integration and any(row.action_status != "IDLE" for row in social_runtimes)):
                return False, ResultCode.NPC_BUSY, None
            if (not fast_integration and
                    (len({row.current_location_code for row in social_runtimes}) != 1 or
                     command.location_code != rt.current_location_code)):
                return False, ResultCode.SOCIAL_TARGET_BUSY, None
            if not fast_integration and NpcActionReservation.objects.filter(
                participants__in=[row.npc for row in social_runtimes], status="HELD", active_lock_key__isnull=False
            ).exists():
                return False, ResultCode.NPC_BUSY, None
        else:
            try:
                rt = NpcRuntimeState.objects.select_for_update().get(npc=command.npc)
            except NpcRuntimeState.DoesNotExist:
                return False, ResultCode.BOOTSTRAP_NPC_SET_MISMATCH, None

        if (not fast_integration and
                command.kind in {"EAT", "REST", "SLEEP", "SOCIALIZE", "WORK_PLACEHOLDER"} and not VillageSimulationConfig.objects.filter(
            config_key="default", village_needs_enabled=True
        ).exists()):
            ActionService.complete_action(
                command.command_id, command.request_id, False,
                ResultCode.VILLAGE_NEEDS_DISABLED,
                message="Phase-two needs feature is disabled.",
                allow_prestart_failure=True,
            )
            return False, ResultCode.VILLAGE_NEEDS_DISABLED, None

        # STRICT 模式校验版本
        # Expiry has precedence over version/busy rejection for accepted
        # pre-start work.  This produces a deterministic Backend terminal fact
        # when a command ages out while Env is unavailable.
        now_ms = int(timezone.now().timestamp() * 1000)
        if (not fast_integration and ((command.received_at_ms is not None and command.ttl_ms is not None and
             command.received_at_ms + command.ttl_ms < now_ms) or
                (command.expires_world_minute is not None and
                 command.expires_world_minute < time_state.world_minute))):
            ActionService.complete_action(
                command.command_id, command.request_id, False,
                ResultCode.MESSAGE_EXPIRED,
                message="Command expired before command start.",
                allow_prestart_failure=True,
            )
            command.status = "EXPIRED"
            command.save(update_fields=["status", "updated_at"])
            return False, ResultCode.MESSAGE_EXPIRED, None

        if command.queue_mode == "STRICT" and not fast_integration:
            if (command.validated_at_state_version is not None and
                    command.validated_at_state_version != rt.state_version):
                ActionService.complete_action(
                    command.command_id, command.request_id, False,
                    ResultCode.STALE_STATE_VERSION,
                    message="State version changed before command start.",
                    allow_prestart_failure=True,
                )
                return False, ResultCode.STALE_STATE_VERSION, None

        # 校验 NPC 是否空闲
        if not fast_integration and rt.action_status != "IDLE":
            return False, ResultCode.NPC_BUSY, None
        if not fast_integration and NpcActionReservation.objects.filter(
            participants=rt.npc, status="HELD", active_lock_key__isnull=False
        ).exists():
            return False, ResultCode.NPC_BUSY, None

        if not fast_integration and command.kind not in CommandService.PHASE2_ACTIONS:
            unavailable = CommandService.unavailable_reason(command.kind, rt)
            if unavailable:
                ActionService.complete_action(
                    command.command_id, command.request_id, False, unavailable,
                    message="Action rejected by authoritative need threshold.",
                    allow_prestart_failure=True,
                )
                return False, unavailable, None

        # ``time_state`` remains locked from the first lifecycle read through
        # transition to RUNNING; do not re-read an unlocked minute here.
        now_ms = int(timezone.now().timestamp() * 1000)
        if (not fast_integration and command.not_before_world_minute is not None and
                command.not_before_world_minute > time_state.world_minute):
            # The command is durable and will be restored from Bootstrap.  Do
            # not consume it merely because Env attempted to start it early.
            command.status = "QUEUED"
            command.save(update_fields=["status", "updated_at"])
            return False, ResultCode.ACTION_NOT_ALLOWED, None
        if (not fast_integration and ((command.received_at_ms is not None and command.ttl_ms is not None and
             command.received_at_ms + command.ttl_ms < now_ms) or
                (command.expires_world_minute is not None and
                 command.expires_world_minute < time_state.world_minute))):
            ActionService.complete_action(
                command.command_id, command.request_id, False,
                ResultCode.MESSAGE_EXPIRED,
                message="Command expired before command start.",
                allow_prestart_failure=True,
            )
            command.status = "EXPIRED"
            command.save(update_fields=["status", "updated_at"])
            return False, ResultCode.MESSAGE_EXPIRED, None

        if command.kind in {"EAT", "REST", "SLEEP", "SOCIALIZE", "WORK_PLACEHOLDER"}:
            gate_runtimes = social_runtimes if social_runtimes is not None else [rt]
            if fast_integration:
                for row in gate_runtimes:
                    if row.needs_world_minute != time_state.world_minute:
                        row.needs_world_minute = time_state.world_minute
                        row.save(update_fields=["needs_world_minute", "updated_at"])
            if not fast_integration and any(row.needs_world_minute != time_state.world_minute for row in gate_runtimes):
                ActionService.complete_action(
                    command.command_id, command.request_id, False,
                    ResultCode.STALE_NEEDS_MINUTE,
                    message="Needs cursor must reach the authoritative start minute.",
                    allow_prestart_failure=True,
                )
                return False, ResultCode.STALE_NEEDS_MINUTE, None
            duration = command.duration_minutes or 0
            if not fast_integration and command.kind in {"SOCIALIZE", "WORK_PLACEHOLDER"} and (
                time_state.world_minute // 1440 !=
                (time_state.world_minute + duration - 1) // 1440
            ):
                ActionService.complete_action(
                    command.command_id, command.request_id, False,
                    ResultCode.INVALID_ACTION_DURATION,
                    message="This life action cannot cross an absolute day boundary.",
                    allow_prestart_failure=True,
                )
                return False, ResultCode.INVALID_ACTION_DURATION, None
            threshold_code = CommandService.unavailable_reason(command.kind, rt)
            if command.kind == "SOCIALIZE" and not threshold_code:
                threshold_code = next((
                    CommandService.unavailable_reason(command.kind, row)
                    for row in gate_runtimes
                    if CommandService.unavailable_reason(command.kind, row)
                ), None)
            if threshold_code and not fast_integration:
                ActionService.complete_action(
                    command.command_id, command.request_id, False,
                    threshold_code, message="Life action rejected by authoritative need threshold.",
                    allow_prestart_failure=True,
                )
                return False, threshold_code, None

        if social_runtimes is not None:
            lock_key = "social:" + ":".join(str(row.npc.agent_id) for row in social_runtimes)
            try:
                reservation = NpcActionReservation.objects.create(
                    reservation_id=social_reservation_id,
                    action_kind="SOCIALIZE",
                    location_code=rt.current_location_code,
                    resource_key=lock_key,
                    active_lock_key=lock_key,
                    status="HELD",
                    acquired_world_minute=time_state.world_minute,
                    expires_world_minute=command.expires_world_minute or time_state.world_minute + (command.duration_minutes or 120),
                    command_id=command.command_id,
                    request_id=command.request_id,
                )
            except IntegrityError:
                return False, ResultCode.RESERVATION_CONFLICT, None
            reservation.participants.set([row.npc for row in social_runtimes])

        # 更新命令状态
        command.status = "RUNNING"
        # Mark the durable command RUNNING only after any shared reservation
        # has been acquired successfully.
        command.status = "RUNNING"
        # Unity 是时间权威：命令武装区间以 Unity 上报的开始分钟为准，而非后端
        # 本地钟，保证结算（life-actions/execute 的身份校验）与 Unity 本地动作
        # 区间精确一致，避免本地钟偏差导致 NPC_BUSY。
        start_minute = start_world_minute if start_world_minute is not None else time_state.world_minute
        command.started_world_minute = start_minute
        command.state_version = rt.state_version
        command.save()

        # 更新 NPC 运行时状态
        rt.current_action = command.kind
        rt.action_status = "MOVING" if command.kind == "MOVE" else "SLEEPING" if command.kind == "SLEEP" else "ACTING"
        rt.action_started_world_minute = start_minute
        rt.action_ends_world_minute = (
            start_minute + command.duration_minutes
            if command.duration_minutes is not None else None
        )
        rt.active_command_id = command.command_id
        rt.save()

        return True, ResultCode.COMMAND_ACCEPTED, command


# ============================================================================
# 动作完成服务
# ============================================================================

class ActionService:
    """动作完成服务。"""

    @staticmethod
    @transaction.atomic
    def complete_action(
        command_id: uuid.UUID,
        request_id: uuid.UUID,
        success: bool,
        result_code: str,
        message: str = "",
        data: Optional[Dict[str, Any]] = None,
        allow_prestart_failure: bool = False,
    ) -> Tuple[bool, str, Optional[VillageActionReceipt]]:
        """完成动作。

        Returns:
            (success, result_code, receipt)
        """
        # 检查 request_id 幂等
        existing = VillageActionReceipt.objects.filter(request_id=request_id).first()
        if existing:
            if existing.command_id == command_id:
                return True, ResultCode.COMMAND_REPLAYED, existing
            return False, ResultCode.REQUEST_ID_CONFLICT, None

        # Lock authority time first.  Bootstrap, StartCommand and TimeService
        # use this order too, preventing a command/runtime lock from waiting
        # on Bootstrap while Bootstrap waits for that same command/runtime.
        try:
            time_state = GlobalGameTimeState.objects.select_for_update().get(pk=1)
        except GlobalGameTimeState.DoesNotExist:
            return False, ResultCode.BACKEND_UNAVAILABLE, None

        # 获取命令
        try:
            command = NpcCommandState.objects.select_for_update().get(command_id=command_id)
        except NpcCommandState.DoesNotExist:
            return False, ResultCode.INVALID_COMMAND_SCHEMA, None

        # Phase-two life actions are completed only by LifeActionService so
        # Unity cannot turn a local animation completion into an asset/needs
        # success fact through the stage-one generic endpoint.
        if command.kind in {"EAT", "REST", "SLEEP", "SOCIALIZE", "WORK_PLACEHOLDER"} and success:
            return False, ResultCode.ACTION_NOT_ALLOWED, None

        # 获取 NPC 运行时状态
        try:
            rt = NpcRuntimeState.objects.select_for_update().get(npc=command.npc)
        except NpcRuntimeState.DoesNotExist:
            return False, ResultCode.BOOTSTRAP_NPC_SET_MISMATCH, None

        # The time row remains locked through receipt/event/outbox creation, so
        # all three observe one authority minute/revision pair.

        # Only the command currently owned by this NPC can produce a terminal
        # outcome.  This prevents a duplicate/corrupted Outcome with a new
        # request_id from creating a second event, receipt and Outbox delivery.
        prestart_failure = (
            allow_prestart_failure
            and not success
            and command.status in ["RECEIVED", "QUEUED"]
        )
        if not prestart_failure and (
            command.status != "RUNNING" or
            (rt.active_command_id is not None and rt.active_command_id != command.command_id)
        ):
            return False, ResultCode.NPC_BUSY, None

        if success:
            from .phase2_services import settle_action_needs_locked
            started = command.started_world_minute
            ended = (
                min(time_state.world_minute, started + command.duration_minutes)
                if started is not None and command.duration_minutes else None
            )
            error = settle_action_needs_locked(
                rt, command.kind, started, ended, base_until=time_state.world_minute
            )
            if error:
                return False, error, None

        command.status = "SETTLING"
        command.save(update_fields=["status", "updated_at"])

        # 创建世界事件
        event_type = "NPC_ACTION_COMPLETED" if success else "NPC_ACTION_FAILED"
        event = VillageWorldEvent.objects.create(
            dedupe_key=f"{command_id}:{request_id}:{event_type}",
            event_type=event_type,
            world_minute=time_state.world_minute,
            time_revision=time_state.time_revision,
            actor_npc=command.npc,
            participant_npc_ids=[str(command.npc.agent_id)],
            location_code=command.location_code,
            command_id=command_id,
            request_id=request_id,
            visibility="PUBLIC",
            player_summary=message,
            hub_summary=message,
        )

        # 创建 Receipt
        receipt = VillageActionReceipt.objects.create(
            request_id=request_id,
            command=command,
            npc=command.npc,
            action_kind=command.kind,
            success=success,
            result_code=result_code,
            response_data=data or {},
            world_minute=time_state.world_minute,
            time_revision=time_state.time_revision,
            state_version=rt.state_version + 1,
            world_event=event,
        )

        # 更新命令状态
        command.status = "SUCCEEDED" if success else "FAILED"
        command.result_code = result_code
        command.completed_world_minute = time_state.world_minute
        command.save()

        # 更新 NPC 运行时状态
        rt.state_version += 1
        rt.current_action = ""
        rt.action_status = "IDLE"
        rt.action_started_world_minute = None
        rt.action_ends_world_minute = None
        rt.active_command_id = None
        rt.last_simulated_world_minute = time_state.world_minute
        rt.time_revision = time_state.time_revision
        rt.save()

        # A failed SOCIALIZE command is terminal only after its shared HELD
        # reservation is released.  This also lets normal phase-one restart
        # recovery clear both participants instead of leaving a permanent
        # busy target.
        if not success and command.kind == "SOCIALIZE":
            NpcActionReservation.objects.select_for_update().filter(
                command_id=command.command_id, status="HELD"
            ).update(status="RELEASED", active_lock_key=None, released_world_minute=time_state.world_minute)

        # 创建 Outbox 条目
        delivery_id = uuid.uuid4()
        outcome_payload = {
            "schema_version": "village.outcome.v1",
            "ref_id": str(command_id),
            "original_message_id": str(command.message_id),
            "request_id": str(request_id),
            "npc_id": str(command.npc.agent_id),
            "action": command.kind,
            "success": success,
            "result_code": result_code,
            "message": message,
            "world_event_id": str(event.event_id),
            "world_minute": time_state.world_minute,
            "time_revision": time_state.time_revision,
            "state_version": rt.state_version,
            "retryable": False,
            "effects": {},
            "data": data or {},
        }

        VillageDeliveryOutbox.objects.get_or_create(
            dedupe_key=f"outcome:{receipt.request_id}",
            defaults={
                "delivery_id": delivery_id,
                "recipient": command.sender_id,
                "message_kind": "OUTCOME",
                "command": command,
                "receipt": receipt,
                "event": event,
                "payload": outcome_payload,
                "priority": 10,
                "status": "PENDING",
            },
        )

        VillageDeliveryOutbox.objects.get_or_create(
            dedupe_key=f"event:{event.event_id}",
            defaults={
                "delivery_id": uuid.uuid4(),
                "recipient": command.sender_id,
                "message_kind": "EVENT",
                "command": command,
                "receipt": receipt,
                "event": event,
                "payload": {
                "schema_version": "village.event.v1",
                "event_id": str(event.event_id),
                "event_sequence": event.event_sequence,
                "event_type": event.event_type,
                "world_minute": event.world_minute,
                "time_revision": event.time_revision,
                "actor_npc_id": str(command.npc.agent_id),
                "participant_npc_ids": [str(command.npc.agent_id)],
                "location_code": event.location_code,
                "point_code": event.point_code,
                "command_id": str(command.command_id),
                "request_id": str(receipt.request_id),
                "cause_event_id": None,
                "visibility": event.visibility,
                "public_status_code": rt.action_status,
                "public_data": event.public_data,
                "summary": event.hub_summary,
                },
                "priority": 5,
                "status": "PENDING",
            },
        )

        return True, result_code, receipt


# ============================================================================
# Reservation service
# ============================================================================

class ReservationService:
    """Acquire/release phase-one multi-NPC reservations deterministically.

    Participant runtime rows are locked in ascending formal NPC id order.
    That order is part of the service contract: two overlapping requests then
    contend for the same first row instead of forming a lock cycle.
    """

    @staticmethod
    @transaction.atomic
    def acquire(
        *,
        action_kind: str,
        participant_ids: List[str],
        world_minute: int,
        expires_world_minute: int,
        resource_key: str = "",
        location_code: str = "",
        point_code: str = "",
        command_id: Optional[uuid.UUID] = None,
        request_id: Optional[uuid.UUID] = None,
    ) -> Tuple[bool, str, Optional[NpcActionReservation]]:
        normalized_ids = sorted({str(npc_id) for npc_id in participant_ids})
        if (not normalized_ids or expires_world_minute < world_minute or
                action_kind not in CommandService.ALLOWED_ACTIONS | {"DIALOGUE"}):
            return False, ResultCode.INVALID_COMMAND_SCHEMA, None

        # This query intentionally materializes locks in a stable total order.
        runtimes = list(
            NpcRuntimeState.objects.select_for_update()
            .select_related("npc")
            .filter(npc__agent_id__in=normalized_ids, npc__is_test_only=False)
            .order_by("npc__agent_id")
        )
        if [str(runtime.npc.agent_id) for runtime in runtimes] != normalized_ids:
            return False, ResultCode.BOOTSTRAP_NPC_SET_MISMATCH, None

        # Expired locks must not survive an Env/Backend restart.  Clearing the
        # active key makes a later acquisition of the same resource possible.
        expired = NpcActionReservation.objects.select_for_update().filter(
            status="HELD", expires_world_minute__lt=world_minute
        )
        expired.update(
            status="EXPIRED",
            released_world_minute=world_minute,
            active_lock_key=None,
        )

        held_by_participant = NpcActionReservation.objects.select_for_update().filter(
            status="HELD",
            participants__agent_id__in=normalized_ids,
        ).distinct().first()
        if held_by_participant:
            return False, ResultCode.RESERVATION_CONFLICT, None

        active_lock_key = (
            f"resource:{resource_key}" if resource_key
            else f"participants:{','.join(normalized_ids)}"
        )
        if NpcActionReservation.objects.select_for_update().filter(
            active_lock_key=active_lock_key, status="HELD"
        ).exists():
            return False, ResultCode.RESERVATION_CONFLICT, None

        try:
            # A disjoint participant set can still race on resource_key.  The
            # database unique key is the final arbiter for that case.
            with transaction.atomic():
                reservation = NpcActionReservation.objects.create(
                    action_kind=action_kind,
                    location_code=location_code,
                    point_code=point_code,
                    resource_key=resource_key,
                    active_lock_key=active_lock_key,
                    status="HELD",
                    acquired_world_minute=world_minute,
                    expires_world_minute=expires_world_minute,
                    command_id=command_id,
                    request_id=request_id,
                )
        except IntegrityError:
            return False, ResultCode.RESERVATION_CONFLICT, None
        reservation.participants.set([runtime.npc for runtime in runtimes])
        return True, ResultCode.COMMAND_ACCEPTED, reservation

    @staticmethod
    @transaction.atomic
    def release(reservation_id: uuid.UUID, world_minute: int) -> Tuple[bool, str]:
        try:
            reservation = NpcActionReservation.objects.select_for_update().get(
                reservation_id=reservation_id
            )
        except NpcActionReservation.DoesNotExist:
            return False, ResultCode.INVALID_COMMAND_SCHEMA

        if reservation.status in ["RELEASED", "EXPIRED"]:
            return True, ResultCode.COMMAND_REPLAYED

        reservation.status = "RELEASED"
        reservation.released_world_minute = world_minute
        reservation.active_lock_key = None
        reservation.save(update_fields=[
            "status", "released_world_minute", "active_lock_key", "updated_at"
        ])
        return True, ResultCode.COMMAND_ACCEPTED


# ============================================================================
# Outbox 服务
# ============================================================================

class OutboxService:
    """Outbox 领取和确认服务。"""

    LEASE_DURATION_MINUTES = 5

    @staticmethod
    @transaction.atomic
    def ensure_outcome(receipt: VillageActionReceipt) -> VillageDeliveryOutbox:
        """Create the durable final result for domain services that already made a receipt."""
        command = receipt.command
        recipient = command.sender_id if command else str(receipt.npc.agent_id) if receipt.npc else "env-server-001"
        payload = dict(receipt.response_data or {})
        payload["schema_version"] = "village.outcome.v1"
        payload.setdefault("request_id", str(receipt.request_id))
        payload.setdefault("npc_id", str(receipt.npc.agent_id) if receipt.npc else None)
        payload.setdefault("success", receipt.success)
        payload.setdefault("result_code", receipt.result_code)
        payload.setdefault("world_minute", receipt.world_minute)
        payload.setdefault("time_revision", receipt.time_revision)
        payload.setdefault("action", str(receipt.action_kind or "UNKNOWN").upper())
        payload.setdefault("phase", "COMPLETED")
        payload.setdefault("terminal", True)
        if command:
            payload_data = dict(payload.get("data") or {})
            if command.duration_minutes is not None:
                payload_data.setdefault("duration_minutes", command.duration_minutes)
            for key in ("target_npc_id", "location_code", "point_code"):
                value = (command.sanitized_parameters or {}).get(key)
                if value is not None:
                    payload_data.setdefault(key, value)
            payload["data"] = payload_data
        if not str(payload.get("message") or "").strip():
            payload["message"] = _outcome_message(
                payload.get("action"), payload.get("result_code"), bool(payload.get("success")))
        payload.setdefault("next_action", None if payload.get("success") else
                           _outcome_next_action(payload.get("result_code")))
        if command:
            payload.setdefault("ref_id", str(command.command_id))
            payload.setdefault("original_message_id", str(command.message_id))
        if receipt.world_event_id:
            payload.setdefault("world_event_id", str(receipt.world_event_id))
        entry, _ = VillageDeliveryOutbox.objects.get_or_create(
            dedupe_key=f"outcome:{receipt.request_id}",
            defaults={
                "recipient": recipient,
                "message_kind": "OUTCOME",
                "command": command,
                "receipt": receipt,
                "event": receipt.world_event,
                "payload": payload,
                "priority": 10,
                "status": "PENDING",
            },
        )
        if command and (entry.command_id is None or entry.receipt_id is None):
            payload.setdefault("ref_id", str(command.command_id))
            payload.setdefault("original_message_id", str(command.message_id))
            entry.command = command
            entry.receipt = receipt
            entry.recipient = command.sender_id
            entry.payload = payload
            entry.save(update_fields=["command", "receipt", "recipient", "payload"])
        else:
            existing_payload = dict(entry.payload or {})
            changed = False
            for key in ("action", "phase", "terminal", "message", "next_action"):
                if key not in existing_payload or existing_payload[key] in (None, ""):
                    existing_payload[key] = payload.get(key)
                    changed = True
            if changed:
                entry.payload = existing_payload
                entry.save(update_fields=["payload"])
        return entry

    @staticmethod
    @transaction.atomic
    def claim_pending(
        recipient: str, limit: int = 10, after_sequence: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """领取待发送的 Outbox 条目。

        Returns:
            领取的条目列表
        """
        now = timezone.now()
        lease_until = now + timedelta(minutes=OutboxService.LEASE_DURATION_MINUTES)

        limit = max(1, min(int(limit), 100))
        # The delivery row is the retry authority.  A cursor is only a diagnostic
        # safe watermark; it must never hide an unacknowledged row, including an
        # expired lease after an out-of-order ACK.
        qs = VillageDeliveryOutbox.objects.select_for_update(skip_locked=True).filter(
            recipient=recipient,
            status__in=["PENDING", "IN_FLIGHT"],
        ).filter(
            Q(status="PENDING") |
            Q(status="IN_FLIGHT", lease_until__lte=now)
        ).filter(
            Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now)
        )
        entries = list(qs.order_by("-priority", "created_at", "delivery_id")[:limit])

        results = []
        for entry in entries:
            entry.status = "IN_FLIGHT"
            entry.attempt_count += 1
            entry.lease_until = lease_until
            entry.last_sent_at = now
            entry.save()

            results.append({
                "delivery_id": str(entry.delivery_id),
                "message_kind": entry.message_kind,
                # Hub must preserve this envelope id.  Ling uses it as its ACK key.
                "envelope": {
                    "id": str(entry.delivery_id),
                    "timestamp": int(now.timestamp() * 1000),
                    "type": "message",
                    "sender": "env-server-001",
                    "recipient": entry.recipient,
                    "payload": {
                        "type": "outcome" if entry.message_kind == "OUTCOME" else "event",
                        "content": entry.payload,
                    },
                },
                "attempt_count": entry.attempt_count,
            })

        return results

    @staticmethod
    @transaction.atomic
    def ack_delivery(
        delivery_id: uuid.UUID,
        event_sequence: Optional[int] = None,
        recipient: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """确认投递。

        Returns:
            (success, result_code)
        """
        try:
            entry = VillageDeliveryOutbox.objects.select_for_update().get(
                delivery_id=delivery_id
            )
        except VillageDeliveryOutbox.DoesNotExist:
            return False, ResultCode.OUTBOX_ACK_UNKNOWN

        if recipient and entry.recipient != recipient:
            return False, ResultCode.SENDER_BINDING_INVALID

        canonical_sequence = (
            entry.event.event_sequence
            if entry.message_kind == "EVENT" and entry.event_id
            else None
        )
        if entry.message_kind == "EVENT" and event_sequence is not None:
            if isinstance(event_sequence, bool):
                return False, ResultCode.INVALID_COMMAND_SCHEMA
            try:
                submitted_sequence = int(event_sequence)
            except (TypeError, ValueError):
                return False, ResultCode.INVALID_COMMAND_SCHEMA
            if canonical_sequence is None or submitted_sequence != canonical_sequence:
                return False, ResultCode.INVALID_COMMAND_SCHEMA

        cursor = None
        if canonical_sequence is not None:
            cursor, _ = VillageDeliveryCursor.objects.select_for_update().get_or_create(
                recipient=entry.recipient
            )

        if entry.status == "ACKED":
            if cursor is not None:
                OutboxService._advance_safe_cursor(entry.recipient, cursor)
            return True, ResultCode.COMMAND_REPLAYED

        entry.status = "ACKED"
        entry.acked_at = timezone.now()
        entry.save(update_fields=["status", "acked_at"])
        if cursor is not None:
            OutboxService._advance_safe_cursor(entry.recipient, cursor)

        return True, ResultCode.COMMAND_ACCEPTED

    @staticmethod
    def _advance_safe_cursor(recipient: str, cursor: VillageDeliveryCursor) -> None:
        event_rows = VillageDeliveryOutbox.objects.filter(
            recipient=recipient,
            message_kind="EVENT",
            event__isnull=False,
        )
        first_unacked = (
            event_rows.exclude(status="ACKED")
            .aggregate(sequence=models.Min("event__event_sequence"))["sequence"]
        )
        acknowledged = event_rows.filter(status="ACKED")
        if first_unacked is not None:
            acknowledged = acknowledged.filter(event__event_sequence__lt=first_unacked)
        safe_sequence = acknowledged.aggregate(
            sequence=models.Max("event__event_sequence")
        )["sequence"] or 0
        if safe_sequence > cursor.last_event_sequence:
            cursor.last_event_sequence = safe_sequence
            cursor.save(update_fields=["last_event_sequence", "updated_at"])

    @staticmethod
    def get_cursor(recipient: str) -> int:
        return int(
            VillageDeliveryCursor.objects.filter(recipient=recipient)
            .values_list("last_event_sequence", flat=True).first() or 0
        )

    @staticmethod
    @transaction.atomic
    def cleanup(retention_days: int = 7, batch_size: int = 1000) -> Dict[str, int]:
        """Delete one bounded batch of retained, acknowledged deliveries."""
        cutoff = timezone.now() - timedelta(days=max(1, int(retention_days)))
        batch_size = max(1, min(int(batch_size), 1000))
        event_is_watermarked = VillageDeliveryCursor.objects.filter(
            recipient=OuterRef("recipient"),
            last_event_sequence__gte=OuterRef("event__event_sequence"),
        )
        entries = list(
            VillageDeliveryOutbox.objects.select_for_update()
            .filter(
                message_kind__in=("EVENT", "OUTCOME"),
                status="ACKED",
                acked_at__lt=cutoff,
            )
            .annotate(event_is_watermarked=Exists(event_is_watermarked))
            .filter(
                Q(message_kind="OUTCOME")
                | Q(event__isnull=True)
                | Q(event_is_watermarked=True)
            )
            .order_by("acked_at", "created_at", "delivery_id")[:batch_size]
        )
        counts = {
            "event_deleted": sum(entry.message_kind == "EVENT" for entry in entries),
            "outcome_deleted": sum(entry.message_kind == "OUTCOME" for entry in entries),
        }
        if entries:
            VillageDeliveryOutbox.objects.filter(
                delivery_id__in=[entry.delivery_id for entry in entries]
            ).delete()
        return {"summarized": 0, "deleted": len(entries), **counts}


# ============================================================================
# 事件查询服务
# ============================================================================

class EventService:
    """事件查询服务。"""

    @staticmethod
    def get_events(after_id: Optional[int] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """获取事件列表。

        Args:
            after_id: 获取此序列号之后的事件
            limit: 最大返回数量

        Returns:
            事件列表
        """
        qs = VillageWorldEvent.objects.all()

        if after_id is not None:
            qs = qs.filter(event_sequence__gt=after_id)

        qs = qs.order_by("event_sequence")[:limit]

        return [
            {
                "event_sequence": e.event_sequence,
                "event_id": str(e.event_id),
                "event_type": e.event_type,
                "world_minute": e.world_minute,
                "time_revision": e.time_revision,
                "actor_npc_id": str(e.actor_npc.agent_id) if e.actor_npc else None,
                "participant_npc_ids": e.participant_npc_ids,
                "location_code": e.location_code,
                "point_code": e.point_code,
                "command_id": str(e.command_id) if e.command_id else None,
                "request_id": str(e.request_id) if e.request_id else None,
                "visibility": e.visibility,
                "player_summary": e.player_summary,
                "hub_summary": e.hub_summary,
                "effects": e.effects,
                "public_data": e.public_data,
                "created_at": e.created_at.isoformat(),
            }
            for e in qs
        ]
