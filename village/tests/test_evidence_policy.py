"""Village declarations and external changes used by agent evidence freshness."""

import uuid
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase

from game_time.models import GlobalGameTimeState
from village.affairs import contribute, create_resource_affair
from village.command_catalog import agent_tool
from village.evidence_policy import policy_for
from village.models import VillageDeliveryOutbox, VillageWorldEvent
from village.phase4_visibility import ENV_CLIENTS, event_payload


class EvidencePolicyTests(SimpleTestCase):
    def test_tools_publish_runtime_policy_outside_model_parameters(self):
        tool = agent_tool("INSPECT_AFFAIRS")
        self.assertEqual(tool["evidence_policy"]["reads"], ["affair:{affair_id}"])
        self.assertNotIn("evidence_policy", tool["parameters"]["properties"])
        tool["evidence_policy"]["reads"].clear()
        self.assertEqual(policy_for("INSPECT_AFFAIRS")["reads"], ["affair:{affair_id}"])

    def test_eating_and_diaries_do_not_invalidate_shared_affairs(self):
        self.assertEqual(set(policy_for("EAT")["writes"]), {
            "inventory:{self}", "needs:{self}", "activity:{self}"})
        self.assertEqual(policy_for("UPDATE_DIARY")["writes"], ["diary:{self}:{game_date}"])
        self.assertIsNone(policy_for("UNDECLARED_ACTION"))

    def test_inventory_separates_money_from_items_and_keeps_filter_coverage(self):
        parts = policy_for("INSPECT_INVENTORY")["snapshots"]
        self.assertIn("filter", parts[0]["fields"])
        self.assertIn("has_more", parts[0]["fields"])
        self.assertEqual(parts[1]["domains"], ["wallet:{self}"])

    def test_event_changes_use_visible_fields_without_private_values(self):
        event = SimpleNamespace(
            event_id=uuid.uuid4(), event_sequence=1, event_type="VILLAGE_AFFAIR_PROGRESS",
            world_minute=1, time_revision=1, actor_npc=None, participant_npc_ids=[], witness_npc_ids=[],
            player_ids=[], location_code="", point_code="", visibility="PUBLIC",
            public_data={"affair_id": "FARM_IRRIGATION", "version": 2}, private_data={"secret": "hidden"},
            player_summary="progress", hub_summary="progress", command_id=None, request_id=None, cause_event_id=None,
        )
        packet = event_payload(event, "10011")
        self.assertEqual(packet["evidence_changes"], ["affair:FARM_IRRIGATION"])
        self.assertNotIn("hidden", str(packet))
        self.assertNotIn("evidence_changes", event_payload(event, ENV_CLIENTS))


class AffairEvidenceEventTests(TestCase):
    def test_partial_player_contribution_invalidates_progress_and_replay_deduplicates(self):
        clock = GlobalGameTimeState.get_instance()
        row = create_resource_affair("EVIDENCE_TEST", "Evidence test", {
            "stages": [{"id": "clear", "conditions": [
                {"id": "work", "type": "labor", "target": 2, "unit_minutes": 30},
            ]}],
        }, world_minute=clock.world_minute)
        request_id = uuid.uuid4()
        for _ in range(2):
            contribute(row, "labor", 1, condition_id="work", world_minute=clock.world_minute,
                       request_id=request_id, player_id="13")
        event = VillageWorldEvent.objects.get(event_type="VILLAGE_AFFAIR_PROGRESS")
        self.assertEqual(event.public_data["version"], 1)
        self.assertEqual(row.status, "ACTIVE")
        deliveries = VillageDeliveryOutbox.objects.filter(event=event, recipient="10011")
        self.assertEqual(deliveries.count(), 1)
        self.assertEqual(event_payload(event, "10011")["evidence_changes"], ["affair:EVIDENCE_TEST"])
