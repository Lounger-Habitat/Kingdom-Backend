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

from django.db import IntegrityError, models, transaction
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

        # Lock the formal ownership set before validating or serializing it.
        # Test-only NPCs are intentionally absent from this recovery boundary.
        npcs = list(
            NpcDefinition.objects.select_for_update()
            .filter(is_test_only=False)
            .order_by("agent_id")
        )
        formal_ids = {str(npc.agent_id) for npc in npcs}
        if formal_ids != {"10011", "10012", "10013", "10014", "10015"}:
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

        queued_commands = list(
            NpcCommandState.objects.select_for_update()
            .filter(npc__is_test_only=False, status__in=["RECEIVED", "QUEUED"])
            .order_by("npc__agent_id", "received_world_minute", "created_at")
        )

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

        # Acquire authority time before runtime state.  Bootstrap uses this
        # same global -> command/runtime lock order for recovery snapshots.
        GlobalGameTimeState.get_instance()
        time_state = GlobalGameTimeState.objects.select_for_update().get(pk=1)

        # 校验队列上限
        queue_count = NpcCommandState.objects.filter(
            npc=npc,
            status__in=["RECEIVED", "QUEUED", "RUNNING"],
        ).count()
        if queue_count >= CommandService.MAX_QUEUE_SIZE:
            return False, ResultCode.QUEUE_FULL, None

        # 获取当前时间
        # ``time_state`` remains the authority row locked above.

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
    def start_command(command_id: uuid.UUID) -> Tuple[bool, str, Optional[NpcCommandState]]:
        """开始命令执行。

        Returns:
            (success, result_code, command_state)
        """
        # Global authority time is the canonical first lock for lifecycle
        # paths.  Bootstrap holds this same row while building recovery state,
        # so a StartCommand cannot observe or create a half-recovery snapshot.
        GlobalGameTimeState.get_instance()
        time_state = GlobalGameTimeState.objects.select_for_update().get(pk=1)

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
        # Expiry has precedence over version/busy rejection for accepted
        # pre-start work.  This produces a deterministic Backend terminal fact
        # when a command ages out while Env is unavailable.
        now_ms = int(timezone.now().timestamp() * 1000)
        if ((command.received_at_ms is not None and command.ttl_ms is not None and
             command.received_at_ms + command.ttl_ms < now_ms) or
                (command.expires_world_minute is not None and
                 command.expires_world_minute < time_state.world_minute)):
            ActionService.complete_action(
                command.command_id, command.request_id, False,
                ResultCode.MESSAGE_EXPIRED,
                message="Command expired before command start.",
                allow_prestart_failure=True,
            )
            return False, ResultCode.MESSAGE_EXPIRED, None

        if command.queue_mode == "STRICT":
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
        if rt.action_status != "IDLE":
            return False, ResultCode.NPC_BUSY, None

        # ``time_state`` remains locked from the first lifecycle read through
        # transition to RUNNING; do not re-read an unlocked minute here.
        now_ms = int(timezone.now().timestamp() * 1000)
        if (command.not_before_world_minute is not None and
                command.not_before_world_minute > time_state.world_minute):
            # The command is durable and will be restored from Bootstrap.  Do
            # not consume it merely because Env attempted to start it early.
            command.status = "QUEUED"
            command.save(update_fields=["status", "updated_at"])
            return False, ResultCode.ACTION_NOT_ALLOWED, None
        if ((command.received_at_ms is not None and command.ttl_ms is not None and
             command.received_at_ms + command.ttl_ms < now_ms) or
                (command.expires_world_minute is not None and
                 command.expires_world_minute < time_state.world_minute)):
            ActionService.complete_action(
                command.command_id, command.request_id, False,
                ResultCode.MESSAGE_EXPIRED,
                message="Command expired before command start.",
                allow_prestart_failure=True,
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
                action_kind not in CommandService.ALLOWED_ACTIONS):
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
