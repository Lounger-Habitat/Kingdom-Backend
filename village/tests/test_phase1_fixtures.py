"""
阶段一：验证 P0 fixture 一致性
"""
import json
from django.test import TestCase

from .fixtures import (
    NPC_IDS,
    LOCATION_CODES,
    COMMAND_FIXTURE,
    OUTCOME_FIXTURE,
    EVENT_FIXTURE,
    DELIVERY_ACK_FIXTURE,
    SUCCESS_CODES,
    FAILURE_CODES,
    ALLOWED_ACTIONS,
    EVENT_TYPES,
)


class Phase1FixtureConsistencyTest(TestCase):
    """验证阶段一 fixture 数据一致性"""

    def test_npc_ids_count(self):
        """应该恰好有 5 个正式 NPC"""
        self.assertEqual(len(NPC_IDS), 5)
        self.assertIn("10011", NPC_IDS.values())
        self.assertIn("10012", NPC_IDS.values())
        self.assertIn("10013", NPC_IDS.values())
        self.assertIn("10014", NPC_IDS.values())
        self.assertIn("10015", NPC_IDS.values())

    def test_location_codes_count(self):
        """应该恰好有 15 个地点码"""
        self.assertEqual(len(LOCATION_CODES), 15)
        self.assertIn("VILLAGE_SQUARE", LOCATION_CODES)
        self.assertIn("IRONSMITH", LOCATION_CODES)
        self.assertIn("JACK_HOME", LOCATION_CODES)

    def test_command_fixture_structure(self):
        """Command fixture 应该有正确的结构"""
        self.assertIn("id", COMMAND_FIXTURE)
        self.assertIn("timestamp", COMMAND_FIXTURE)
        self.assertEqual(COMMAND_FIXTURE["type"], "message")
        self.assertIn("sender", COMMAND_FIXTURE)
        self.assertIn("recipient", COMMAND_FIXTURE)
        self.assertIn("payload", COMMAND_FIXTURE)

        payload = COMMAND_FIXTURE["payload"]
        self.assertEqual(payload["type"], "action")
        self.assertIn("content", payload)

        content = payload["content"]
        self.assertEqual(content["schema_version"], "village.command.v1")
        self.assertIn("id", content)
        self.assertIn("request_id", content)
        self.assertIn("name", content)
        self.assertIn("params", content)
        self.assertIn("expected_state_version", content)
        self.assertEqual(content["queue_mode"], "STRICT")
        self.assertIn("not_before_world_minute", content)
        self.assertIn("expires_world_minute", content)
        self.assertIn("ttl_ms", content)
        self.assertIn("reason_code", content)

    def test_outcome_fixture_structure(self):
        """Outcome fixture 应该有正确的结构"""
        self.assertIn("id", OUTCOME_FIXTURE)
        self.assertIn("timestamp", OUTCOME_FIXTURE)
        self.assertEqual(OUTCOME_FIXTURE["type"], "message")
        self.assertIn("sender", OUTCOME_FIXTURE)
        self.assertIn("recipient", OUTCOME_FIXTURE)
        self.assertIn("payload", OUTCOME_FIXTURE)

        payload = OUTCOME_FIXTURE["payload"]
        self.assertEqual(payload["type"], "event")
        self.assertIn("content", payload)

        content = payload["content"]
        self.assertEqual(content["schema_version"], "village.outcome.v1")
        self.assertIn("ref_id", content)
        self.assertIn("original_message_id", content)
        self.assertIn("request_id", content)
        self.assertIn("npc_id", content)
        self.assertIn("success", content)
        self.assertIn("result_code", content)
        self.assertIn("message", content)
        self.assertIn("world_event_id", content)
        self.assertIn("world_minute", content)
        self.assertIn("time_revision", content)
        self.assertIn("state_version", content)
        self.assertIn("retryable", content)
        self.assertIn("effects", content)
        self.assertIn("data", content)

    def test_event_fixture_structure(self):
        """Event fixture 应该有正确的结构"""
        self.assertIn("id", EVENT_FIXTURE)
        self.assertIn("timestamp", EVENT_FIXTURE)
        self.assertEqual(EVENT_FIXTURE["type"], "message")
        self.assertIn("sender", EVENT_FIXTURE)
        self.assertIn("recipient", EVENT_FIXTURE)
        self.assertIn("payload", EVENT_FIXTURE)

        payload = EVENT_FIXTURE["payload"]
        self.assertEqual(payload["type"], "event")
        self.assertIn("content", payload)

        content = payload["content"]
        self.assertEqual(content["schema_version"], "village.event.v1")
        self.assertIn("event_id", content)
        self.assertIn("event_sequence", content)
        self.assertIn("event_type", content)
        self.assertIn("world_minute", content)
        self.assertIn("time_revision", content)
        self.assertIn("actor_npc_id", content)
        self.assertIn("participant_npc_ids", content)
        self.assertIn("location_code", content)
        self.assertIn("point_code", content)
        self.assertIn("command_id", content)
        self.assertIn("request_id", content)
        self.assertIn("cause_event_id", content)
        self.assertIn("visibility", content)
        self.assertIn("public_status_code", content)
        self.assertIn("public_data", content)
        self.assertIn("summary", content)

    def test_delivery_ack_fixture_structure(self):
        """VILLAGE_DELIVERY_ACK fixture 应该有正确的结构"""
        self.assertEqual(DELIVERY_ACK_FIXTURE["type"], "message")
        self.assertIn("sender", DELIVERY_ACK_FIXTURE)
        self.assertIn("recipient", DELIVERY_ACK_FIXTURE)
        self.assertIn("payload", DELIVERY_ACK_FIXTURE)

        payload = DELIVERY_ACK_FIXTURE["payload"]
        self.assertEqual(payload["type"], "event")
        self.assertIn("content", payload)

        content = payload["content"]
        self.assertIn("id", content)
        self.assertEqual(content["name"], "VILLAGE_DELIVERY_ACK")
        self.assertIn("data", content)
        self.assertIn("delivery_id", content["data"])

    def test_success_codes_count(self):
        """应该有 6 个成功码"""
        self.assertEqual(len(SUCCESS_CODES), 6)
        self.assertIn("COMMAND_ACCEPTED", SUCCESS_CODES)
        self.assertIn("MOVE_COMPLETED", SUCCESS_CODES)
        self.assertIn("WAIT_COMPLETED", SUCCESS_CODES)
        self.assertIn("GO_HOME_COMPLETED", SUCCESS_CODES)

    def test_failure_codes_count(self):
        """应该有足够的失败码"""
        self.assertGreaterEqual(len(FAILURE_CODES), 20)
        self.assertIn("PROTOCOL_VERSION_UNSUPPORTED", FAILURE_CODES)
        self.assertIn("SENDER_BINDING_INVALID", FAILURE_CODES)
        self.assertIn("STALE_STATE_VERSION", FAILURE_CODES)
        self.assertIn("ASSET_EFFECT_NOT_ALLOWED", FAILURE_CODES)

    def test_allowed_actions(self):
        """阶段一应该只有 MOVE、WAIT、GO_HOME"""
        self.assertEqual(len(ALLOWED_ACTIONS), 3)
        self.assertIn("MOVE", ALLOWED_ACTIONS)
        self.assertIn("WAIT", ALLOWED_ACTIONS)
        self.assertIn("GO_HOME", ALLOWED_ACTIONS)

    def test_event_types(self):
        """应该有正确的事件类型"""
        self.assertIn("NPC_MOVED", EVENT_TYPES)
        self.assertIn("NPC_ACTION_COMPLETED", EVENT_TYPES)
        self.assertIn("DAY_CHANGED", EVENT_TYPES)

    def test_fixture_ids_unique(self):
        """fixture 中的 UUID 应该唯一（除了 request_id 可以跨 fixture 复用）"""
        ids = set()
        # Command IDs
        ids.add(COMMAND_FIXTURE["id"])
        ids.add(COMMAND_FIXTURE["payload"]["content"]["id"])
        ids.add(COMMAND_FIXTURE["payload"]["content"]["request_id"])
        # Outcome IDs
        ids.add(OUTCOME_FIXTURE["id"])
        # request_id 在 Outcome 中应该与 Command 相同（幂等键）
        self.assertEqual(
            OUTCOME_FIXTURE["payload"]["content"]["request_id"],
            COMMAND_FIXTURE["payload"]["content"]["request_id"],
        )
        # Event IDs
        ids.add(EVENT_FIXTURE["id"])
        ids.add(EVENT_FIXTURE["payload"]["content"]["event_id"])
        # ACK IDs
        ids.add(DELIVERY_ACK_FIXTURE["payload"]["content"]["id"])

        # 应该有 7 个唯一 ID（request_id 跨 fixture 复用）
        self.assertEqual(len(ids), 7)

    def test_fixture_timestamps(self):
        """fixture 时间戳应该是毫秒"""
        self.assertGreater(COMMAND_FIXTURE["timestamp"], 1_000_000_000_000)
        self.assertGreater(OUTCOME_FIXTURE["timestamp"], 1_000_000_000_000)
        self.assertGreater(EVENT_FIXTURE["timestamp"], 1_000_000_000_000)

    def test_fixture_world_minutes(self):
        """fixture world_minute 应该是正整数"""
        self.assertGreater(
            COMMAND_FIXTURE["payload"]["content"]["not_before_world_minute"], 0
        )
        self.assertGreater(
            OUTCOME_FIXTURE["payload"]["content"]["world_minute"], 0
        )
        self.assertGreater(
            EVENT_FIXTURE["payload"]["content"]["world_minute"], 0
        )
