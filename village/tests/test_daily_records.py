import copy
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition
from village.agent_queries import execute_agent_query
from village.command_catalog import agent_tool
from village.daily_records import READ_ACTIONS, WRITE_ACTIONS, query_daily_records, write_daily_record
from village.daily_record_views import public_records, write_record
from village.models import NpcDailyRecord, NpcProfile, NpcRuntimeState


class DailyRecordTests(TestCase):
    def setUp(self):
        self.clock = GlobalGameTimeState.get_instance()
        self.clock.apply_world_minute(GlobalGameTimeState.world_minute_from_calendar(2026, 3, 8, 23, 59))
        self.clock.save()
        self.npcs = {}
        for npc_id in ("10011", "10012"):
            npc, _ = NpcDefinition.objects.get_or_create(agent_id=npc_id, defaults={"name": npc_id})
            NpcProfile.objects.update_or_create(npc=npc, defaults={
                "display_name": npc_id, "is_active": True, "capabilities": list(WRITE_ACTIONS) + list(READ_ACTIONS),
            })
            NpcRuntimeState.objects.update_or_create(npc=npc, defaults={"action_status": "SLEEPING", "health_fp": 100})
            self.npcs[npc_id] = npc

    def envelope(self, action, params, sender="10011"):
        return {"envelope_id": str(uuid.uuid4()), "sender": sender, "content": {
            "schema_version": "village.command.v1", "id": str(uuid.uuid4()),
            "request_id": str(uuid.uuid4()), "name": action, "params": params,
        }}

    def test_create_read_update_without_changing_world_or_action(self):
        before = list(NpcRuntimeState.objects.values())
        for kind in ("SCHEDULE", "DIARY"):
            result = write_daily_record(self.envelope("WRITE_" + kind, {"content": "上午：吃饭\n下午：休息"}))
            self.assertTrue(result["success"], result)
            read = execute_agent_query("10011", "INSPECT_SCHEDULE" if kind == "SCHEDULE" else "INSPECT_DIARIES")
            self.assertEqual(read[2]["record"]["content"], "上午：吃饭\n下午：休息")
            self.assertNotIn("version", read[2]["record"])
            params = {"game_date": "2026-03-08", "content": "下午改为回家休息"}
            update = self.envelope("UPDATE_" + kind, params)
            updated = write_daily_record(update)
            self.assertTrue(updated["success"], updated)
            self.assertEqual(updated["data"]["record"]["content"], params["content"])
            self.assertNotIn("version", updated["data"]["record"])
            latest_params = {**params, "content": "下午回家休息，晚上整理今天的经历"}
            latest = write_daily_record(self.envelope("UPDATE_" + kind, latest_params))
            self.assertTrue(latest["success"], latest)
            replay = write_daily_record(update)
            self.assertTrue(replay["data"]["replayed"])
            self.assertEqual(replay["data"]["record"], updated["data"]["record"])
            self.assertEqual(query_daily_records(self.npcs["10011"], kind)["record"]["content"], latest_params["content"])
            self.assertIsNone(query_daily_records(self.npcs["10012"], kind)["record"])
        self.assertEqual(list(NpcRuntimeState.objects.values()), before)
        self.assertEqual(GlobalGameTimeState.get_instance().world_minute, self.clock.world_minute)

    def test_retry_after_midnight_keeps_original_record(self):
        envelope = self.envelope("WRITE_DIARY", {"content": "今天收获了玉米"})
        first = write_daily_record(envelope)
        self.clock.apply_world_minute(self.clock.world_minute + 2)
        self.clock.save()
        retry = copy.deepcopy(envelope)
        retry["envelope_id"] = str(uuid.uuid4())
        retry["content"]["expected_state_version"] = 999
        result = write_daily_record(retry)
        self.assertTrue(result["data"]["replayed"])
        self.assertEqual(result["data"]["record"], first["data"]["record"])
        self.assertEqual(NpcDailyRecord.objects.count(), 1)
        retry["content"]["params"]["content"] = "另一段文字"
        self.assertEqual(write_daily_record(retry)["result_code"], "DAILY_RECORD_REQUEST_CONFLICT")

    def test_duplicate_dates_invalid_content_and_identity(self):
        envelope = self.envelope("WRITE_DIARY", {"content": "今天的记录"})
        self.assertTrue(write_daily_record(envelope)["success"])
        self.assertEqual(write_daily_record(self.envelope("WRITE_DIARY", {"content": "另一篇"}))["result_code"], "DIARY_ALREADY_EXISTS")
        for params, code in [
            ({"content": "未来", "game_date": "2026-03-09"}, "DIARY_FUTURE_DATE"),
            ({"content": "错误日期", "game_date": "2026-02-30"}, "INVALID_GAME_DATE"),
            ({"content": "x" * 4001}, "INVALID_DAILY_RECORD_CONTENT"),
            ({"content": " ", "game_date": "2026-03-07"}, "INVALID_DAILY_RECORD_CONTENT"),
            ({"content": "冒用作者", "npc_id": "10012"}, "INVALID_DAILY_RECORD_SCHEMA"),
        ]:
            self.assertEqual(write_daily_record(self.envelope("WRITE_DIARY", params))["result_code"], code)
        self.assertEqual(write_daily_record(self.envelope("WRITE_DIARY", {"content": "伪造"}, "10099"))["result_code"], "SENDER_BINDING_INVALID")
        for kind in ("SCHEDULE", "DIARY"):
            self.assertEqual(write_daily_record(self.envelope("UPDATE_" + kind, {
                "game_date": "2026-03-07", "content": "尚未创建的记录",
            }))["result_code"], "DAILY_RECORD_NOT_FOUND")
            self.assertEqual(write_daily_record(self.envelope("UPDATE_" + kind, {
                "content": "未指定日期",
            }))["result_code"], "INVALID_DAILY_RECORD_SCHEMA")

    def test_history_pagination_and_real_command_descriptions(self):
        for day in (6, 7, 8):
            self.assertTrue(write_daily_record(self.envelope("WRITE_DIARY", {"game_date": f"2026-03-{day:02d}", "content": f"第{day}天"}))["success"])
        first = execute_agent_query("10011", "INSPECT_DIARIES", {"view": "list", "limit": 2})[2]
        self.assertEqual([r["game_date"] for r in first["records"]], ["2026-03-08", "2026-03-07"])
        last = query_daily_records(self.npcs["10011"], "DIARY", {"view": "list", "before_date": first["next_before_date"]})
        self.assertEqual(last["records"][0]["game_date"], "2026-03-06")
        self.assertIsNone(last["next_before_date"])
        self.assertEqual(execute_agent_query("10011", "INSPECT_DIARIES", {"game_date": "bad"})[1], "INVALID_QUERY_PARAMS")
        for action in set(READ_ACTIONS) | set(WRITE_ACTIONS):
            tool = agent_tool(action)
            self.assertGreater(len(tool["description"]), 70)
            self.assertIn("game_date", tool["parameters"]["properties"])
            self.assertEqual("read" in tool["tags"], action in READ_ACTIONS)
            if action.startswith("UPDATE_"):
                self.assertEqual(set(tool["parameters"]["properties"]), {"game_date", "content"})
                self.assertEqual(set(tool["parameters"]["required"]), {"game_date", "content"})

    def test_client_view_is_read_only_and_write_endpoint_requires_env_auth(self):
        factory = APIRequestFactory()
        player = SimpleNamespace(id="player", is_authenticated=True)
        with patch("village.daily_record_views.presentation_enabled", return_value=True):
            request = factory.get("/records")
            force_authenticate(request, user=player)
            self.assertEqual(public_records(request, "10011", "schedule").status_code, 200)
            request = factory.post("/records", {"content": "不能写入"}, format="json")
            force_authenticate(request, user=player)
            self.assertEqual(public_records(request, "10011", "schedule").status_code, 405)
        self.assertIn(write_record(factory.post("/write", {}, format="json")).status_code, (401, 403))
        from account.env_auth import EnvServerJWTAuthentication
        self.assertEqual(write_record.cls.authentication_classes, [EnvServerJWTAuthentication])
