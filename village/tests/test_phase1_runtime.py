"""
阶段一：Backend Runtime API 测试
"""
import uuid
import time
from datetime import timedelta
from django.test import TestCase
from django.utils import timezone

from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition
from village.models import (
    NpcCommandState,
    NpcRuntimeState,
    VillageActionReceipt,
    VillageDeliveryOutbox,
    VillageWorldEvent,
)
from village.runtime_services import (
    ActionService,
    BootstrapService,
    CheckpointService,
    CommandService,
    EventService,
    OutboxService,
    TimeService,
    ResultCode,
)


class TimeServiceTest(TestCase):
    """时间服务测试"""

    def setUp(self):
        self.time_state = GlobalGameTimeState.get_instance()
        self.time_state.world_minute = 1000
        self.time_state.time_revision = 5
        self.time_state.save()

    def test_advance_time_success(self):
        """正常时间推进"""
        success, code, receipt = TimeService.advance_time(
            world_minute=1001,
            expected_time_revision=5,
        )
        self.assertTrue(success)
        self.assertEqual(code, ResultCode.COMMAND_ACCEPTED)
        self.assertIsNotNone(receipt)

        self.time_state.refresh_from_db()
        self.assertEqual(self.time_state.world_minute, 1001)
        self.assertEqual(self.time_state.time_revision, 6)

    def test_advance_time_regression(self):
        """时间倒退应拒绝"""
        success, code, receipt = TimeService.advance_time(
            world_minute=999,
            expected_time_revision=5,
        )
        self.assertFalse(success)
        self.assertEqual(code, ResultCode.WORLD_TIME_REGRESSION)

    def test_advance_time_stale_revision(self):
        """旧 revision 应拒绝"""
        success, code, receipt = TimeService.advance_time(
            world_minute=1001,
            expected_time_revision=4,
        )
        self.assertFalse(success)
        self.assertEqual(code, ResultCode.STALE_TIME_REVISION)

    def test_advance_time_idempotent(self):
        """同一分钟重复提交应返回第一次结果"""
        # 第一次
        success1, code1, receipt1 = TimeService.advance_time(
            world_minute=1001,
            expected_time_revision=5,
        )
        self.assertTrue(success1)

        # 第二次（revision 已更新）
        success2, code2, receipt2 = TimeService.advance_time(
            world_minute=1001,
            expected_time_revision=5,
        )
        self.assertFalse(success2)
        self.assertEqual(code2, ResultCode.STALE_TIME_REVISION)


class BootstrapServiceTest(TestCase):
    """Bootstrap 服务测试"""

    def test_bootstrap_returns_five_npcs(self):
        """应该返回五名 NPC"""
        # 创建五名正式 NPC
        for i, name in enumerate(["bill", "jack", "mark", "lucy", "nora"], start=10011):
            npc, _ = NpcDefinition.objects.get_or_create(
                agent_id=i,
                defaults={"name": name, "is_test_only": False},
            )
            NpcRuntimeState.objects.get_or_create(
                npc=npc,
                defaults={
                    "current_location_code": "VILLAGE_SQUARE",
                    "action_status": "IDLE",
                },
            )

        data = BootstrapService.get_bootstrap()
        self.assertNotIn("error", data)
        self.assertEqual(len(data["npcs"]), 5)


class CheckpointServiceTest(TestCase):
    """检查点服务测试"""

    def setUp(self):
        self.npc = NpcDefinition.objects.create(agent_id=10011, name="bill", is_test_only=False)
        self.rt = NpcRuntimeState.objects.create(
            npc=self.npc,
            current_location_code="VILLAGE_SQUARE",
            action_status="IDLE",
            state_version=10,
        )

    def test_checkpoint_success(self):
        """正常检查点提交"""
        results = CheckpointService.submit_checkpoint([{
            "npc_id": "10011",
            "expected_state_version": 10,
            "current_location_code": "IRONSMITH",
        }])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["success"])

        self.rt.refresh_from_db()
        self.assertEqual(self.rt.current_location_code, "IRONSMITH")

    def test_checkpoint_stale_version(self):
        """旧版本应拒绝"""
        results = CheckpointService.submit_checkpoint([{
            "npc_id": "10011",
            "expected_state_version": 9,
            "current_location_code": "IRONSMITH",
        }])
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["success"])
        self.assertEqual(results[0]["result_code"], ResultCode.STALE_STATE_VERSION)

    def test_checkpoint_field_not_allowed(self):
        """非白名单字段应拒绝"""
        results = CheckpointService.submit_checkpoint([{
            "npc_id": "10011",
            "expected_state_version": 10,
            "invalid_field": "value",
        }])
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["success"])
        self.assertEqual(results[0]["result_code"], ResultCode.CHECKPOINT_FIELD_NOT_ALLOWED)


class CommandServiceTest(TestCase):
    """命令服务测试"""

    def setUp(self):
        self.npc = NpcDefinition.objects.create(agent_id=10011, name="bill", is_test_only=False)
        self.rt = NpcRuntimeState.objects.create(
            npc=self.npc,
            current_location_code="VILLAGE_SQUARE",
            action_status="IDLE",
            state_version=10,
        )
        self.time_state = GlobalGameTimeState.get_instance()
        self.time_state.world_minute = 1000
        self.time_state.time_revision = 5
        self.time_state.save()

    def test_accept_command_success(self):
        """正常命令接受"""
        content = {
            "schema_version": "village.command.v1",
            "id": str(uuid.uuid4()),
            "request_id": str(uuid.uuid4()),
            "name": "move",
            "params": {"location_code": "IRONSMITH"},
            "queue_mode": "STRICT",
            "expected_state_version": 10,
            "ttl_ms": 30000,
        }

        ok, code, cmd = CommandService.accept_command(
            envelope_id=str(uuid.uuid4()),
            sender="10011",
            recipient="env-001",
            content=content,
            timestamp_ms=int(time.time() * 1000),
        )
        self.assertTrue(ok)
        self.assertEqual(code, ResultCode.COMMAND_ACCEPTED)
        self.assertIsNotNone(cmd)

    def test_accept_command_unknown_action(self):
        """未知动作应拒绝"""
        content = {
            "schema_version": "village.command.v1",
            "id": str(uuid.uuid4()),
            "request_id": str(uuid.uuid4()),
            "name": "invalid_action",
            "params": {},
            "queue_mode": "STRICT",
            "expected_state_version": 10,
            "ttl_ms": 30000,
        }

        ok, code, cmd = CommandService.accept_command(
            envelope_id=str(uuid.uuid4()),
            sender="10011",
            recipient="env-001",
            content=content,
            timestamp_ms=int(time.time() * 1000),
        )
        self.assertFalse(ok)
        self.assertEqual(code, ResultCode.UNKNOWN_ACTION)

    def test_accept_command_sender_binding(self):
        """非正式 sender 应拒绝"""
        content = {
            "schema_version": "village.command.v1",
            "id": str(uuid.uuid4()),
            "request_id": str(uuid.uuid4()),
            "name": "move",
            "params": {"location_code": "IRONSMITH"},
            "queue_mode": "STRICT",
            "expected_state_version": 10,
            "ttl_ms": 30000,
        }

        ok, code, cmd = CommandService.accept_command(
            envelope_id=str(uuid.uuid4()),
            sender="99999",
            recipient="env-001",
            content=content,
            timestamp_ms=int(time.time() * 1000),
        )
        self.assertFalse(ok)
        self.assertEqual(code, ResultCode.SENDER_BINDING_INVALID)

    def test_start_command_success(self):
        """正常命令开始"""
        # 先接受命令
        content = {
            "schema_version": "village.command.v1",
            "id": str(uuid.uuid4()),
            "request_id": str(uuid.uuid4()),
            "name": "move",
            "params": {"location_code": "IRONSMITH"},
            "queue_mode": "STRICT",
            "expected_state_version": 10,
            "ttl_ms": 30000,
        }

        ok, code, cmd = CommandService.accept_command(
            envelope_id=str(uuid.uuid4()),
            sender="10011",
            recipient="env-001",
            content=content,
            timestamp_ms=int(time.time() * 1000),
        )
        self.assertTrue(ok)

        # 开始命令
        ok, code, cmd = CommandService.start_command(cmd.command_id)
        self.assertTrue(ok)
        self.assertEqual(code, ResultCode.COMMAND_ACCEPTED)

        # 检查 NPC 状态
        self.rt.refresh_from_db()
        self.assertEqual(self.rt.action_status, "MOVING")
        self.assertEqual(self.rt.current_action, "MOVE")


class ActionServiceTest(TestCase):
    """动作完成服务测试"""

    def setUp(self):
        self.npc = NpcDefinition.objects.create(agent_id=10011, name="bill", is_test_only=False)
        self.rt = NpcRuntimeState.objects.create(
            npc=self.npc,
            current_location_code="VILLAGE_SQUARE",
            action_status="MOVING",
            current_action="MOVE",
            state_version=10,
        )
        self.time_state = GlobalGameTimeState.get_instance()
        self.time_state.world_minute = 1000
        self.time_state.time_revision = 5
        self.time_state.save()

        # 创建命令
        self.command = NpcCommandState.objects.create(
            command_id=uuid.uuid4(),
            message_id=uuid.uuid4(),
            request_id=uuid.uuid4(),
            npc=self.npc,
            schema_version="village.command.v1",
            sender_id="10011",
            kind="MOVE",
            location_code="IRONSMITH",
            payload_sha256="test",
            queue_mode="STRICT",
            status="RUNNING",
        )

    def test_complete_action_success(self):
        """正常动作完成"""
        request_id = uuid.uuid4()
        ok, code, receipt = ActionService.complete_action(
            command_id=self.command.command_id,
            request_id=request_id,
            success=True,
            result_code=ResultCode.MOVE_COMPLETED,
            message="已到达",
        )
        self.assertTrue(ok)
        self.assertEqual(code, ResultCode.MOVE_COMPLETED)

        # 检查 Receipt
        self.assertIsNotNone(receipt)
        self.assertTrue(receipt.success)

        # 检查 NPC 状态
        self.rt.refresh_from_db()
        self.assertEqual(self.rt.action_status, "IDLE")
        self.assertEqual(self.rt.current_action, "")
        self.assertEqual(self.rt.state_version, 11)

        # 检查 Outbox
        outbox = VillageDeliveryOutbox.objects.filter(command=self.command).first()
        self.assertIsNotNone(outbox)
        self.assertEqual(outbox.status, "PENDING")

    def test_complete_action_idempotent(self):
        """重复完成应返回第一次结果"""
        request_id = uuid.uuid4()

        # 第一次
        ok1, code1, receipt1 = ActionService.complete_action(
            command_id=self.command.command_id,
            request_id=request_id,
            success=True,
            result_code=ResultCode.MOVE_COMPLETED,
        )
        self.assertTrue(ok1)

        # 第二次
        ok2, code2, receipt2 = ActionService.complete_action(
            command_id=self.command.command_id,
            request_id=request_id,
            success=True,
            result_code=ResultCode.MOVE_COMPLETED,
        )
        self.assertTrue(ok2)
        self.assertEqual(code2, ResultCode.COMMAND_REPLAYED)


class OutboxServiceTest(TestCase):
    """Outbox 服务测试"""

    def setUp(self):
        self.npc = NpcDefinition.objects.create(agent_id=10011, name="bill", is_test_only=False)
        self.command = NpcCommandState.objects.create(
            command_id=uuid.uuid4(),
            message_id=uuid.uuid4(),
            request_id=uuid.uuid4(),
            npc=self.npc,
            schema_version="village.command.v1",
            sender_id="10011",
            kind="MOVE",
            payload_sha256="test",
            status="SUCCEEDED",
        )
        self.receipt = VillageActionReceipt.objects.create(
            request_id=uuid.uuid4(),
            command=self.command,
            npc=self.npc,
            action_kind="MOVE",
            success=True,
            result_code="MOVE_COMPLETED",
            world_minute=1000,
            time_revision=5,
        )

    def test_claim_pending(self):
        """领取待发送条目"""
        # 创建 Outbox 条目
        delivery_id = uuid.uuid4()
        VillageDeliveryOutbox.objects.create(
            delivery_id=delivery_id,
            dedupe_key=f"test:{delivery_id}",
            recipient="10011",
            message_kind="OUTCOME",
            command=self.command,
            receipt=self.receipt,
            payload={"test": True},
            status="PENDING",
        )

        entries = OutboxService.claim_pending(recipient="10011", limit=10)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["delivery_id"], str(delivery_id))

        # 检查状态已更新
        outbox = VillageDeliveryOutbox.objects.get(delivery_id=delivery_id)
        self.assertEqual(outbox.status, "IN_FLIGHT")

    def test_ack_delivery(self):
        """确认投递"""
        delivery_id = uuid.uuid4()
        VillageDeliveryOutbox.objects.create(
            delivery_id=delivery_id,
            dedupe_key=f"test:{delivery_id}",
            recipient="10011",
            message_kind="OUTCOME",
            command=self.command,
            receipt=self.receipt,
            payload={"test": True},
            status="IN_FLIGHT",
        )

        ok, code = OutboxService.ack_delivery(delivery_id)
        self.assertTrue(ok)
        self.assertEqual(code, ResultCode.COMMAND_ACCEPTED)

        # 检查状态已更新
        outbox = VillageDeliveryOutbox.objects.get(delivery_id=delivery_id)
        self.assertEqual(outbox.status, "ACKED")
        self.assertIsNotNone(outbox.acked_at)

    def test_ack_unknown_delivery(self):
        """未知 delivery_id 应返回错误"""
        ok, code = OutboxService.ack_delivery(uuid.uuid4())
        self.assertFalse(ok)
        self.assertEqual(code, ResultCode.OUTBOX_ACK_UNKNOWN)

    def test_expired_lease_is_reclaimed_with_the_same_hub_envelope_id(self):
        delivery_id = uuid.uuid4()
        VillageDeliveryOutbox.objects.create(
            delivery_id=delivery_id,
            dedupe_key=f"reclaim:{delivery_id}",
            recipient="10011",
            message_kind="OUTCOME",
            command=self.command,
            receipt=self.receipt,
            payload={"schema_version": "village.outcome.v1"},
            status="IN_FLIGHT",
            lease_until=timezone.now() - timedelta(seconds=1),
        )

        entries = OutboxService.claim_pending(recipient="10011", limit=1)
        self.assertEqual(entries[0]["delivery_id"], str(delivery_id))
        self.assertEqual(entries[0]["envelope"]["id"], str(delivery_id))
        self.assertGreater(entries[0]["envelope"]["timestamp"], 1_000_000_000_000)


class EventServiceTest(TestCase):
    """事件服务测试"""

    def test_get_events(self):
        """获取事件列表"""
        # 创建事件
        VillageWorldEvent.objects.create(
            dedupe_key="test1",
            event_type="NPC_MOVED",
            world_minute=1000,
            time_revision=5,
            visibility="PUBLIC",
            player_summary="测试事件",
        )
        VillageWorldEvent.objects.create(
            dedupe_key="test2",
            event_type="NPC_ACTION_COMPLETED",
            world_minute=1001,
            time_revision=6,
            visibility="PUBLIC",
            player_summary="测试事件2",
        )

        events = EventService.get_events()
        self.assertEqual(len(events), 2)

    def test_get_events_after_id(self):
        """获取指定 ID 之后的事件"""
        e1 = VillageWorldEvent.objects.create(
            dedupe_key="test1",
            event_type="NPC_MOVED",
            world_minute=1000,
            time_revision=5,
            visibility="PUBLIC",
        )
        e2 = VillageWorldEvent.objects.create(
            dedupe_key="test2",
            event_type="NPC_ACTION_COMPLETED",
            world_minute=1001,
            time_revision=6,
            visibility="PUBLIC",
        )

        events = EventService.get_events(after_id=e1.event_sequence)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_sequence"], e2.event_sequence)
