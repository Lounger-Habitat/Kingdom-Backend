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
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from django.db import models, transaction
from django.db.models import F, Q
from django.utils import timezone

from .models import (
    NpcActionReservation,
    NpcCommandState,
    NpcProfile,
    NpcRuntimeState,
    VillageActionReceipt,
    VillageDeliveryOutbox,
    VillageSimulationConfig,
    VillageWorldEvent,
)
from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition


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

    # 阶段边界
    ASSET_EFFECT_NOT_ALLOWED = "ASSET_EFFECT_NOT_ALLOWED"

    # Outbox
    OUTBOX_ACK_UNKNOWN = "OUTBOX_ACK_UNKNOWN"
    OUTBOX_DELIVERY_FAILED = "OUTBOX_DELIVERY_FAILED"


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


# ============================================================================
# Bootstrap 服务
# ============================================================================

class BootstrapService:
    """启动快照服务。"""

    @staticmethod
    def get_bootstrap() -> Dict[str, Any]:
        """获取启动快照。

        Returns:
            包含时间、NPC状态、配置等的字典
        """
        # 获取时间
        time_state = GlobalGameTimeState.get_instance()

        # 获取五名正式 NPC
        npcs = NpcDefinition.objects.filter(is_test_only=False).order_by("agent_id")
        formal_ids = {str(npc.agent_id) for npc in npcs}
        if formal_ids != {"10011", "10012", "10013", "10014", "10015"}:
            return {"error": ResultCode.BOOTSTRAP_NPC_SET_MISMATCH}

        # 获取运行时状态
        runtime_states = NpcRuntimeState.objects.filter(
            npc__is_test_only=False
        ).select_related("npc").order_by("npc__agent_id")
        if {str(rt.npc.agent_id) for rt in runtime_states} != formal_ids:
            return {"error": ResultCode.BOOTSTRAP_NPC_SET_MISMATCH}

        # 获取配置
        config = VillageSimulationConfig.objects.first()

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
            }

        # 构建响应
        npc_data = []
        for rt in runtime_states:
            # 获取活动命令
            active_cmd = None
            if rt.active_command_id:
                active_cmd = NpcCommandState.objects.filter(
                    command_id=rt.active_command_id
                ).first()

            npc_data.append({
                "npc_id": str(rt.npc.agent_id),
                "name": rt.npc.name,
                "current_location_code": rt.current_location_code,
                "current_point_code": rt.current_point_code,
                "current_goal": rt.current_goal,
                "current_action": rt.current_action,
                "action_status": rt.action_status,
                "action_started_world_minute": rt.action_started_world_minute,
                "action_ends_world_minute": rt.action_ends_world_minute,
                "active_command_id": str(rt.active_command_id) if rt.active_command_id else None,
                "state_version": rt.state_version,
                "last_simulated_world_minute": rt.last_simulated_world_minute,
                "time_revision": rt.time_revision,
                "active_command": serialize_command(active_cmd) if active_cmd else None,
            })

        queued_commands = NpcCommandState.objects.filter(
            npc__is_test_only=False, status__in=["RECEIVED", "QUEUED"]
        ).order_by("npc__agent_id", "received_world_minute", "created_at")

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
            "commands": [serialize_command(command) for command in queued_commands],
            "config": {
                "tick_interval_seconds": config.tick_interval_seconds if config else 5,
                "hub_command_timeout_seconds": config.hub_command_timeout_seconds if config else 30,
                "command_queue_limit": config.command_queue_limit if config else 10,
                "day_start_minute": config.day_start_minute if config else 360,
                "day_close_minute": config.day_close_minute if config else 1320,
                "village_simulation_enabled": config.village_simulation_enabled if config else False,
                "village_hub_command_adapter_enabled": config.village_hub_command_adapter_enabled if config else False,
            },
        }


# ============================================================================
# Checkpoint 服务
# ============================================================================

class CheckpointService:
    """批量状态检查点服务。"""

    # 白名单字段
    ALLOWED_FIELDS = {
        "current_location_code",
        "current_point_code",
        "current_goal",
        "current_action",
        "action_status",
        "action_started_world_minute",
        "action_ends_world_minute",
        "active_command_id",
        "last_simulated_world_minute",
    }

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
        results = []

        for cp in checkpoints:
            npc_id = cp.get("npc_id")
            expected_version = cp.get("expected_state_version")

            if not npc_id or expected_version is None:
                results.append({
                    "npc_id": npc_id,
                    "success": False,
                    "result_code": ResultCode.INVALID_COMMAND_SCHEMA,
                })
                continue

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

            # 校验版本
            if rt.state_version != expected_version:
                results.append({
                    "npc_id": npc_id,
                    "success": False,
                    "result_code": ResultCode.STALE_STATE_VERSION,
                    "current_state_version": rt.state_version,
                })
                continue

            # 校验白名单字段
            invalid_fields = set(cp.keys()) - CheckpointService.ALLOWED_FIELDS - {"npc_id", "expected_state_version"}
            if invalid_fields:
                results.append({
                    "npc_id": npc_id,
                    "success": False,
                    "result_code": ResultCode.CHECKPOINT_FIELD_NOT_ALLOWED,
                    "invalid_fields": list(invalid_fields),
                })
                continue

            # 更新字段
            for field in CheckpointService.ALLOWED_FIELDS:
                if field in cp:
                    setattr(rt, field, cp[field])

            rt.save()
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

    ALLOWED_ACTIONS = {"MOVE", "WAIT", "GO_HOME"}
    MAX_QUEUE_SIZE = 10
    MAX_CLOCK_SKEW_MS = 5 * 60 * 1000

    @staticmethod
    def compute_payload_sha256(content: Dict[str, Any]) -> str:
        """计算 payload SHA256。"""
        canonical = json.dumps(content, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode()).hexdigest()

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
        try:
            message_id = uuid.UUID(str(envelope_id))
            command_id = uuid.UUID(str(content["id"]))
            request_id = uuid.UUID(str(content["request_id"]))
            timestamp_ms = int(timestamp_ms)
        except (KeyError, TypeError, ValueError, AttributeError):
            return False, ResultCode.INVALID_COMMAND_SCHEMA, None
        if timestamp_ms < 1_000_000_000_000 or not isinstance(content.get("params"), dict):
            return False, ResultCode.INVALID_COMMAND_SCHEMA, None
        if "parameters" in content or content.get("queue_mode") != "STRICT":
            return False, ResultCode.INVALID_COMMAND_SCHEMA, None
        ttl_ms = content.get("ttl_ms")
        if not isinstance(ttl_ms, int) or ttl_ms <= 0:
            return False, ResultCode.INVALID_COMMAND_SCHEMA, None
        now_ms = int(timezone.now().timestamp() * 1000)
        if timestamp_ms > now_ms + CommandService.MAX_CLOCK_SKEW_MS:
            return False, ResultCode.CLOCK_SKEW, None
        if timestamp_ms + ttl_ms < now_ms:
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

        # 校验队列上限
        queue_count = NpcCommandState.objects.filter(
            npc=npc,
            status__in=["RECEIVED", "QUEUED", "RUNNING"],
        ).count()
        if queue_count >= CommandService.MAX_QUEUE_SIZE:
            return False, ResultCode.QUEUE_FULL, None

        # 获取当前时间
        time_state = GlobalGameTimeState.get_instance()

        # 解析参数
        try:
            runtime = NpcRuntimeState.objects.select_for_update().get(npc=npc)
        except NpcRuntimeState.DoesNotExist:
            return False, ResultCode.BOOTSTRAP_NPC_SET_MISMATCH, None
        expected_version = content.get("expected_state_version")
        if not isinstance(expected_version, int):
            return False, ResultCode.INVALID_COMMAND_SCHEMA, None
        if expected_version != runtime.state_version:
            return False, ResultCode.STALE_STATE_VERSION, None
        if (content.get("expires_world_minute") is not None and
                content["expires_world_minute"] < time_state.world_minute):
            return False, ResultCode.MESSAGE_EXPIRED, None
        params = content["params"]
        location_code = params.get("location_code", "")
        point_code = params.get("point_code", "")

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

        return True, ResultCode.COMMAND_ACCEPTED, command

    @staticmethod
    @transaction.atomic
    def start_command(command_id: uuid.UUID) -> Tuple[bool, str, Optional[NpcCommandState]]:
        """开始命令执行。

        Returns:
            (success, result_code, command_state)
        """
        try:
            command = NpcCommandState.objects.select_for_update().get(command_id=command_id)
        except NpcCommandState.DoesNotExist:
            return False, ResultCode.INVALID_COMMAND_SCHEMA, None

        # 校验状态
        if command.status not in ["RECEIVED", "QUEUED"]:
            return False, ResultCode.NPC_BUSY, None

        # 获取 NPC 运行时状态
        try:
            rt = NpcRuntimeState.objects.select_for_update().get(npc=command.npc)
        except NpcRuntimeState.DoesNotExist:
            return False, ResultCode.BOOTSTRAP_NPC_SET_MISMATCH, None

        # STRICT 模式校验版本
        if command.queue_mode == "STRICT":
            if (command.validated_at_state_version is not None and
                    command.validated_at_state_version != rt.state_version):
                ActionService.complete_action(
                    command.command_id, command.request_id, False,
                    ResultCode.STALE_STATE_VERSION,
                    message="State version changed before command start.",
                )
                return False, ResultCode.STALE_STATE_VERSION, None

        # 校验 NPC 是否空闲
        if rt.action_status != "IDLE":
            return False, ResultCode.NPC_BUSY, None

        # 获取当前时间
        time_state = GlobalGameTimeState.get_instance()
        now_ms = int(timezone.now().timestamp() * 1000)
        if ((command.received_at_ms is not None and command.ttl_ms is not None and
             command.received_at_ms + command.ttl_ms < now_ms) or
                (command.expires_world_minute is not None and
                 command.expires_world_minute < time_state.world_minute)):
            ActionService.complete_action(
                command.command_id, command.request_id, False,
                ResultCode.MESSAGE_EXPIRED,
                message="Command expired before command start.",
            )
            return False, ResultCode.MESSAGE_EXPIRED, None

        # 更新命令状态
        command.status = "RUNNING"
        command.started_world_minute = time_state.world_minute
        command.state_version = rt.state_version
        command.save()

        # 更新 NPC 运行时状态
        rt.current_action = command.kind
        rt.action_status = "MOVING" if command.kind == "MOVE" else "ACTING"
        rt.action_started_world_minute = time_state.world_minute
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
    ) -> Tuple[bool, str, Optional[VillageActionReceipt]]:
        """完成动作。

        Returns:
            (success, result_code, receipt)
        """
        # 检查 request_id 幂等
        existing = VillageActionReceipt.objects.filter(request_id=request_id).first()
        if existing:
            return True, ResultCode.COMMAND_REPLAYED, existing

        # 获取命令
        try:
            command = NpcCommandState.objects.select_for_update().get(command_id=command_id)
        except NpcCommandState.DoesNotExist:
            return False, ResultCode.INVALID_COMMAND_SCHEMA, None

        # 获取 NPC 运行时状态
        try:
            rt = NpcRuntimeState.objects.select_for_update().get(npc=command.npc)
        except NpcRuntimeState.DoesNotExist:
            return False, ResultCode.BOOTSTRAP_NPC_SET_MISMATCH, None

        # 获取当前时间
        time_state = GlobalGameTimeState.get_instance()

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
        rt.save()

        # 创建 Outbox 条目
        delivery_id = uuid.uuid4()
        outcome_payload = {
            "schema_version": "village.outcome.v1",
            "ref_id": str(command_id),
            "original_message_id": str(command.message_id),
            "request_id": str(request_id),
            "npc_id": str(command.npc.agent_id),
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

        VillageDeliveryOutbox.objects.create(
            delivery_id=delivery_id,
            dedupe_key=f"outcome:{receipt.request_id}",
            recipient=command.sender_id,
            message_kind="OUTCOME",
            command=command,
            receipt=receipt,
            event=event,
            payload=outcome_payload,
            priority=10,
            status="PENDING",
        )

        VillageDeliveryOutbox.objects.create(
            delivery_id=uuid.uuid4(),
            dedupe_key=f"event:{event.event_id}",
            recipient=command.sender_id,
            message_kind="EVENT",
            command=command,
            receipt=receipt,
            event=event,
            payload={
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
            priority=5,
            status="PENDING",
        )

        return True, result_code, receipt


# ============================================================================
# Outbox 服务
# ============================================================================

class OutboxService:
    """Outbox 领取和确认服务。"""

    LEASE_DURATION_MINUTES = 5

    @staticmethod
    @transaction.atomic
    def claim_pending(recipient: str, limit: int = 10) -> List[Dict[str, Any]]:
        """领取待发送的 Outbox 条目。

        Returns:
            领取的条目列表
        """
        now = timezone.now()
        lease_until = now + timedelta(minutes=OutboxService.LEASE_DURATION_MINUTES)

        # 领取 PENDING 或租约过期的 IN_FLIGHT 条目
        entries = VillageDeliveryOutbox.objects.select_for_update(skip_locked=True).filter(
            recipient=recipient,
            status__in=["PENDING", "IN_FLIGHT"],
        ).filter(
            Q(status="PENDING") |
            Q(status="IN_FLIGHT", lease_until__lt=now)
        ).order_by(
            "-priority", "created_at"
        )[:limit]

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
    def ack_delivery(delivery_id: uuid.UUID) -> Tuple[bool, str]:
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

        if entry.status == "ACKED":
            return True, ResultCode.COMMAND_REPLAYED

        entry.status = "ACKED"
        entry.acked_at = timezone.now()
        entry.save()

        return True, ResultCode.COMMAND_ACCEPTED


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
                "created_at": e.created_at.isoformat(),
            }
            for e in qs
        ]
