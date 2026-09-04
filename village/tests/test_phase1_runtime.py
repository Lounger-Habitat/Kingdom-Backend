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
    NpcActionReservation,
    NpcProfile,
    NpcRuntimeState,
    VillageActionDurationConfig,
    VillageActionReceipt,
    VillageDeliveryOutbox,
    VillageSimulationConfig,
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
        runtime = NpcRuntimeState.objects.get(npc__agent_id=10011)
        runtime.world_position = {"x": 1.25, "y": 2.5, "z": -3.75}
        runtime.save(update_fields=["world_position"])
        data = BootstrapService.get_bootstrap()
        npc = next(row for row in data["npcs"] if row["npc_id"] == "10011")
        self.assertEqual(npc["world_position"], {"x": 1.25, "y": 2.5, "z": -3.75})

    def test_bootstrap_exposes_economy_feature_flag(self):
        for i, name in enumerate(["bill", "jack", "mark", "lucy", "nora"], start=10011):
            npc, _ = NpcDefinition.objects.get_or_create(
                agent_id=i, defaults={"name": name, "is_test_only": False},
            )
            NpcRuntimeState.objects.get_or_create(
                npc=npc,
                defaults={"current_location_code": "VILLAGE_SQUARE", "action_status": "IDLE"},
            )
        config, _ = VillageSimulationConfig.objects.get_or_create(config_key="default")
        config.village_economy_enabled = True
        config.save(update_fields=["village_economy_enabled"])

        data = BootstrapService.get_bootstrap()

        self.assertIs(data["config"]["village_economy_enabled"], True)

    def test_bootstrap_exposes_social_feature_flag(self):
        for i, name in enumerate(["bill", "jack", "mark", "lucy", "nora"], start=10011):
            npc, _ = NpcDefinition.objects.get_or_create(
                agent_id=i, defaults={"name": name, "is_test_only": False},
            )
            NpcRuntimeState.objects.get_or_create(
                npc=npc,
                defaults={"current_location_code": "VILLAGE_SQUARE", "action_status": "IDLE"},
            )
        config, _ = VillageSimulationConfig.objects.get_or_create(config_key="default")
        config.village_social_enabled = True
        config.save(update_fields=["village_social_enabled"])

        data = BootstrapService.get_bootstrap()

        self.assertIs(data["config"]["village_social_enabled"], True)

    def test_bootstrap_exposes_all_admin_managed_action_configs(self):
        for i, name in enumerate(["bill", "jack", "mark", "lucy", "nora"], start=10011):
            npc, _ = NpcDefinition.objects.get_or_create(
                agent_id=i, defaults={"name": name, "is_test_only": False},
            )
            NpcRuntimeState.objects.get_or_create(
                npc=npc,
                defaults={"current_location_code": "VILLAGE_SQUARE", "action_status": "IDLE"},
            )
        VillageActionDurationConfig.objects.update_or_create(
            action_name="PLANT", defaults={"duration_minutes": 5},
        )
        VillageActionDurationConfig.objects.update_or_create(
            action_name="MOVE", defaults={"navigation_timeout_seconds": 45},
        )

        data = BootstrapService.get_bootstrap()

        durations = data["config"]["action_durations"]
        self.assertEqual(durations["PLANT"], 5)
        self.assertEqual(set(durations), set(VillageActionDurationConfig.default_durations()))
        self.assertEqual(data["config"]["navigation_action_timeout_seconds"]["MOVE"], 45)
        self.assertEqual(data["config"]["economy_action_durations"]["PLANT"], 5)

    def test_variable_command_duration_is_preserved(self):
        VillageActionDurationConfig.objects.update_or_create(
            action_name="REST", defaults={"duration_minutes": 60},
        )
        content = {"duration_minutes": 30}

        CommandService.apply_admin_duration("REST", content)

        self.assertEqual(content["duration_minutes"], 30)

    def test_bootstrap_exposes_need_bands_and_shared_active_reservation(self):
        npcs = []
        for i, name in enumerate(["bill", "jack", "mark", "lucy", "nora"], start=10011):
            npc, _ = NpcDefinition.objects.get_or_create(
                agent_id=i, defaults={"name": name, "is_test_only": False},
            )
            NpcRuntimeState.objects.get_or_create(
                npc=npc, defaults={"current_location_code": "VILLAGE_SQUARE"},
            )
            npcs.append(npc)
        reservation = NpcActionReservation.objects.create(
            reservation_id=uuid.uuid4(), action_kind="SOCIALIZE", location_code="VILLAGE_SQUARE",
            active_lock_key="social:10011:10012", status="HELD", acquired_world_minute=0,
            expires_world_minute=30,
        )
        reservation.participants.set(npcs[:2])
        data = BootstrapService.get_bootstrap()
        by_id = {row["npc_id"]: row for row in data["npcs"]}
        self.assertIn("need_bands", by_id["10011"])
        self.assertEqual(
            by_id["10011"]["active_reservation"],
            by_id["10012"]["active_reservation"],
        )


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
            "world_position": {"x": 1.25, "y": 2.5, "z": -3.75},
        }])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["success"])

        self.rt.refresh_from_db()
        self.assertEqual(self.rt.current_location_code, "IRONSMITH")
        self.assertEqual(self.rt.world_position, {"x": 1.25, "y": 2.5, "z": -3.75})
        self.assertEqual(self.rt.state_version, 11)

        # The same expected version must not be able to overwrite the first
        # checkpoint.  This exercises the persisted, public-service CAS.
        replay = CheckpointService.submit_checkpoint([{
            "npc_id": "10011",
            "expected_state_version": 10,
            "current_location_code": "TAVERN",
        }])[0]
        self.assertFalse(replay["success"])
        self.assertEqual(replay["result_code"], ResultCode.STALE_STATE_VERSION)
        self.rt.refresh_from_db()
        self.assertEqual(self.rt.current_location_code, "IRONSMITH")
        self.assertEqual(self.rt.state_version, 11)

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

    def test_checkpoint_rejects_malformed_world_position(self):
        for position in (
            {"x": 1, "y": 2},
            {"x": 1, "y": 2, "z": True},
            {"x": 1, "y": 2, "z": float("inf")},
        ):
            with self.subTest(position=position):
                result = CheckpointService.submit_checkpoint([{
                    "npc_id": "10011",
                    "expected_state_version": 10,
                    "world_position": position,
                }])[0]
                self.assertFalse(result["success"])
                self.assertEqual(result["result_code"], ResultCode.INVALID_COMMAND_SCHEMA)

    def test_checkpoint_cannot_mutate_lifecycle_or_authority_fields(self):
        """Checkpoint is restricted to recovery-safe location/goal state."""
        forbidden_fields = {
            "action_status": "MOVING",
            "current_action": "MOVE",
            "action_started_world_minute": 5,
            "action_ends_world_minute": 10,
            "active_command_id": str(uuid.uuid4()),
            "state_version": 999,
            "time_revision": 999,
            "world_minute": 999,
            "inventory": {"gold": 999},
        }
        for field, value in forbidden_fields.items():
            with self.subTest(field=field):
                result = CheckpointService.submit_checkpoint([{
                    "npc_id": "10011",
                    "expected_state_version": 10,
                    field: value,
                }])[0]
                self.assertFalse(result["success"])
                self.assertEqual(result["result_code"], ResultCode.CHECKPOINT_FIELD_NOT_ALLOWED)
                self.rt.refresh_from_db()
                self.assertEqual(self.rt.state_version, 10)
                self.assertEqual(self.rt.action_status, "IDLE")
                self.assertEqual(self.rt.current_action, "")


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
        NpcProfile.objects.create(
            npc=self.npc, display_name="Bill", occupation="farmer",
            home_location_code="VILLAGE_SQUARE", work_location_code="VILLAGE_SQUARE",
            social_location_code="VILLAGE_SQUARE", capabilities=["MOVE"],
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

    def test_accept_command_rejects_unconfigured_action(self):
        content = {
            "schema_version": "village.command.v1",
            "id": str(uuid.uuid4()),
            "request_id": str(uuid.uuid4()),
            "name": "WAIT",
            "params": {},
            "queue_mode": "STRICT",
            "expected_state_version": 10,
            "ttl_ms": 30000,
        }

        ok, code, cmd = CommandService.accept_command(
            envelope_id=str(uuid.uuid4()), sender="10011", recipient="env-001",
            content=content, timestamp_ms=int(time.time() * 1000),
        )

        self.assertFalse(ok)
        self.assertEqual(code, ResultCode.ACTION_NOT_ALLOWED)
        self.assertIsNone(cmd)

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
