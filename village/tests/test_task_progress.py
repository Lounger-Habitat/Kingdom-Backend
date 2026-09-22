import uuid
from copy import deepcopy
from datetime import timedelta

from django.db import connection
from django.test import TestCase
from django.utils import timezone

from account.models import GameAccount
from inventory.models import NpcDefinition
from village.models import (NpcCommandState, NpcRuntimeState, VillageAffair,
    VillageAffairContribution, VillagePlayerAffairLabor)
from village.personal_commissions import complete_commission_labor, project_commission
from village.task_progress import decorate_task_progress
from village.tests.test_personal_commissions import CommissionFixture


class TaskProgressTests(TestCase):
    def setUp(self):
        self.player = GameAccount.objects.create(username="进度玩家")
        self.npc = NpcDefinition.objects.create(agent_id="progress-npc", name="Bill")
        self.affair = VillageAffair.objects.create(code="PROGRESS", title="清淤", status="ACTIVE", opened_world_minute=0)

    def task(self, submitted=0, target=2):
        return dict(affair_id=self.affair.pk, status="ACTIVE", stages=[dict(conditions=[dict(
            condition_id="work", type="labor", target=target, submitted=submitted, available=submitted < target)])])

    def test_contributors_and_first_completion_time_are_read_only_and_condition_scoped(self):
        for actor_type, actor_id, minute in (("PLAYER", str(self.player.pk), 100), ("NPC", self.npc.agent_id, 130)):
            VillageAffairContribution.objects.create(affair=self.affair, request_id=uuid.uuid4(), condition_id="work",
                actor_type=actor_type, actor_id=actor_id, kind="labor", quantity=1, world_minute=minute)
        task = self.task(2)
        task["stages"].append(dict(conditions=[dict(condition_id="second-work", type="labor", target=2, submitted=2, available=False)]))
        def forbid_write(execute, sql, params, many, context):
            self.assertFalse(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")), sql)
            return execute(sql, params, many, context)
        with connection.execute_wrapper(forbid_write):
            decorate_task_progress([task], [])
        first, second = [stage["conditions"][0] for stage in task["stages"]]
        self.assertEqual([(p["actor_name"], p["quantity"]) for p in first["contributors"]], [("进度玩家", 1), ("Bill", 1)])
        self.assertEqual(first["completed_world_minute"], 130)
        self.assertEqual([r["world_minute"] for r in first["contributions"]], [100, 130])
        self.assertTrue(first["history_complete"])
        self.assertEqual(second["contributors"], [])
        self.assertIsNone(second["completed_world_minute"])
        self.assertFalse(second["history_complete"])

    def test_active_workers_include_players_and_running_npcs_but_not_expired_or_queued(self):
        session = VillagePlayerAffairLabor.objects.create(player=self.player, affair=self.affair, condition_id="work",
            authority_session_id=uuid.uuid4(), location_code="", last_world_minute=100, last_time_revision=0,
            last_seen_at=timezone.now())
        command = NpcCommandState.objects.create(npc=self.npc, message_id=uuid.uuid4(), request_id=uuid.uuid4(),
            sender_id="test", recipient_id="env", kind="CONTRIBUTE_TO_AFFAIR", status="RUNNING",
            sanitized_parameters=dict(affair_id=self.affair.pk, condition_id="work", kind="labor"),
            started_world_minute=100, payload_sha256="test")
        NpcRuntimeState.objects.create(npc=self.npc, active_command_id=command.pk, action_status="ACTING")
        task = self.task()
        decorate_task_progress([task], [])
        self.assertEqual([w["actor_name"] for w in task["stages"][0]["conditions"][0]["active_workers"]], ["进度玩家", "Bill"])
        self.assertNotIn("authority_session_id", str(task))
        session.last_seen_at -= timedelta(seconds=20)
        session.save(update_fields=["last_seen_at"])
        command.status = "QUEUED"
        command.save(update_fields=["status"])
        decorate_task_progress([task], [])
        self.assertEqual(task["stages"][0]["conditions"][0]["active_workers"], [])
        command.status = "RUNNING"
        command.save(update_fields=["status"])
        completed = self.task(2)
        decorate_task_progress([completed], [])
        self.assertEqual(completed["stages"][0]["conditions"][0]["active_workers"], [])


class CommissionProgressTests(CommissionFixture, TestCase):
    def test_receipts_preserve_each_condition_time_and_do_not_duplicate_replays(self):
        row = self.publish(); self.accept(row); self.stock()
        rid = uuid.uuid4()
        self.give(row, amount=1, minute=105, rid=rid)
        self.give(row, amount=1, minute=105, rid=rid)
        self.give(row, amount=1, minute=110)
        complete_commission_labor(row.pk, "PLAYER", str(self.player.pk), "labor", uuid.uuid4(), 140, location="COMMISSION_YARD")
        row.refresh_from_db()
        task = project_commission(row, actor_type="PLAYER", actor_id=self.player.pk)
        original = deepcopy(task["progress"])
        decorate_task_progress([], [task])
        wood, labor = [stage["conditions"][0] for stage in task["stages"]]
        self.assertEqual(task["progress"], original)
        self.assertEqual((wood["completed_world_minute"], labor["completed_world_minute"]), (110, 140))
        self.assertEqual(wood["contributors"][0]["actor_name"], self.player.username)
        self.assertEqual(wood["contributors"][0]["quantity"], 2)
        self.assertEqual(len(wood["contributions"]), 2)
        self.assertEqual(labor["contributors"][0]["quantity"], 1)
