from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import uuid
from unittest.mock import patch

from django.core.management import call_command
from django.db import transaction
from django.test import TransactionTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition
from village.capability_gaps import ACTION, RECORD_KEY
from village.models import NpcCommandState, NpcProfile, NpcRuntimeState, VillageActionTask
from village.star_actions import accept_action
from village.views import commands_accept, npc_available_actions


class CapabilityGapTests(TransactionTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "logs" / "gaps.jsonl"
        override = self.settings(AGENT_CAPABILITY_GAP_LOG_PATH=str(self.path))
        override.enable()
        self.addCleanup(override.disable)
        self.npc, _ = NpcDefinition.objects.get_or_create(agent_id="10011", defaults={"name": "Bill"})
        NpcProfile.objects.update_or_create(npc=self.npc, defaults={
            "display_name": "Bill", "is_active": True, "capabilities": [],
        })
        self.state, _ = NpcRuntimeState.objects.update_or_create(npc=self.npc, defaults={
            "current_location_code": "BILL_FARM", "current_action": "PLANT",
            "action_status": "ACTING", "health_fp": 100, "energy_fp": 10,
        })
        self.clock = GlobalGameTimeState.get_instance()

    def message(self, action_id="gap-001", params=None):
        return {"id": str(uuid.uuid4()), "timestamp": int(time.time() * 1000), "type": "message",
                "sender": "10011", "recipient": "WuxiVillage", "payload": {"type": "action", "content": {
                    "id": action_id, "name": ACTION, "params": params if params is not None else {
                        "type": "information", "request": "查询最近七天的小麦成交价格\n按天列出",
                        "purpose": "决定下轮种什么", "blocker": "现有工具只有当前价格",
                    }}}}

    def request(self, method, body=None):
        factory = APIRequestFactory()
        request = factory.post("/", body, format="json") if method == "POST" else factory.get("/")
        force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
        return request

    def rows(self):
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]

    def test_discovery_and_http_report_during_work_write_a_real_jsonl_record(self):
        discovery = npc_available_actions(self.request("GET"), "10011").data["data"]
        tool = next(tool for tool in discovery["tools"] if tool["name"] == ACTION)
        self.assertIn(ACTION, discovery["available_actions"])
        self.assertNotIn("read", tool["tags"])
        self.assertIn(ACTION, "\n".join(discovery["agent_context"]["common_rules"]))
        original = self.message()
        before = deepcopy(original)
        response = commands_accept(self.request("POST", original))
        self.assertEqual(response.status_code, 200, response.data)
        result = response.data["data"]["result"]
        self.assertTrue(result["data"]["file_exported"])
        self.assertIn("动作尚未执行", result["data"]["message"])
        row, = self.rows()
        self.assertEqual(row["request"], original["payload"]["content"]["params"]["request"])
        self.assertEqual((row["agent_id"], row["environment_id"], row["location"]),
                         ("10011", "WuxiVillage", "BILL_FARM"))
        self.assertEqual(row["world_minute"], self.clock.world_minute)
        self.assertEqual(original, before)
        self.assertFalse(NpcCommandState.objects.exists())
        self.state.refresh_from_db()
        self.clock.refresh_from_db()
        self.assertEqual((self.state.current_action, self.state.action_status, self.state.energy_fp), ("PLANT", "ACTING", 10))
        self.assertEqual(self.clock.world_minute, row["world_minute"])

    def test_replay_conflict_and_interrupted_append_do_not_duplicate_reports(self):
        original = self.message()
        first = accept_action(original)
        self.assertTrue(first[0], first[1])
        with self.path.open("ab") as stream:
            stream.write(b'{"report_id":')
        retry = deepcopy(original)
        retry["id"] = str(uuid.uuid4())
        retry["timestamp"] += 1
        replay = accept_action(retry)
        self.assertTrue(replay[3])
        self.assertEqual(replay[2].pk, first[2].pk)
        self.assertEqual(len(self.rows()), 1)
        changed = deepcopy(original)
        changed["payload"]["content"]["params"]["request"] = "换一个需求"
        self.assertEqual(accept_action(changed)[:2], (False, "REQUEST_ID_CONFLICT"))
        self.assertTrue(accept_action(self.message("gap-action", {
            "type": "action", "request": "预约下周的运输", "purpose": "安排后续交货",
        }))[0])
        self.assertEqual([row["type"] for row in self.rows()], ["information", "action"])
        self.assertEqual(VillageActionTask.objects.filter(result__has_key=RECORD_KEY).count(), 2)

    def test_invalid_params_rollback_and_export_failure_remain_recoverable(self):
        for index, params in enumerate(({}, {"type": "other", "request": "x", "purpose": "y"},
                {"type": "action", "request": " ", "purpose": "y"},
                {"type": "action", "request": "x", "purpose": "y", "agent_id": "10012"})):
            self.assertEqual(accept_action(self.message(f"invalid-{index}", params))[:2],
                             (False, "INVALID_CAPABILITY_GAP_PARAMS"))
        with self.assertRaises(RuntimeError), transaction.atomic():
            accept_action(self.message("rollback"))
            raise RuntimeError("roll back the request")
        self.assertFalse(VillageActionTask.objects.filter(action_id="rollback").exists())
        self.assertFalse(self.path.exists())
        with patch("village.capability_gaps.append_report", side_effect=PermissionError("unwritable")), self.assertLogs("village.capability_gaps", level="ERROR"):
            ok, code, task, _ = accept_action(self.message())
        self.assertTrue(ok, code)
        self.assertFalse(task.result["data"]["file_exported"])
        self.assertIn("暂未写入", task.result["data"]["message"])
        self.assertIn(RECORD_KEY, VillageActionTask.objects.get(pk=task.pk).result)
        call_command("export_agent_capability_gaps", stdout=StringIO())
        self.assertEqual(self.rows(), [task.result[RECORD_KEY]])
        replay = accept_action(self.message())
        self.assertTrue(replay[2].result["data"]["file_exported"])
        self.assertEqual(len(self.rows()), 1)
