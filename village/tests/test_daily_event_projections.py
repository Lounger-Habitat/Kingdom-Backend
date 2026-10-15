"""Public event replay and NPC recovery use the same authoritative facts."""
import uuid
from unittest.mock import patch

from django.test import TestCase

from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition
from items.models import Item
from village.affairs import contribute, ensure_bridge_affair
from village.agent_context import build_agent_context
from village.agent_queries import _affairs
from village.models import (NpcProfile, NpcRuntimeState, VillageDailyEvent, VillageDailyEventTemplate,
                            VillageSimulationConfig, VillageWorldEvent)
from village.phase4_visibility import ENV_CLIENTS, event_payload, public_story_event_fields
from village.phase7_services import public_journal, public_overview


class DailyEventProjectionTests(TestCase):
    def setUp(self):
        self.clock = GlobalGameTimeState.get_instance()
        self.clock.apply_world_minute(600)
        self.clock.time_revision = 1
        self.clock.save()
        VillageSimulationConfig.objects.update_or_create(config_key="default", defaults={
            "village_story_enabled": True, "caravan_normal_capacity": 37})
        self.npc, _ = NpcDefinition.objects.get_or_create(agent_id="10011", defaults={"name": "Bill"})
        self.profile, _ = NpcProfile.objects.update_or_create(npc=self.npc, defaults={
            "display_name": "Bill", "occupation": "farmer", "capabilities": ["INSPECT_AFFAIRS", "CONTRIBUTE_TO_AFFAIR"]})
        NpcRuntimeState.objects.update_or_create(npc=self.npc, defaults={"current_location_code": "REPAIR_YARD"})
        Item.objects.update_or_create(pk=1002, defaults={"item_name": "木材"})
        self.bridge = ensure_bridge_affair(self.clock.world_minute)

    def add(self, kind, quantity, condition_id, item_id=None, request_id=None):
        return contribute(self.bridge, kind, quantity, condition_id=condition_id, item_id=item_id,
            request_id=request_id or uuid.uuid4(), world_minute=self.clock.world_minute, actor_npc=self.npc)

    def test_bridge_event_freezes_actual_remaining_needs_and_contribution(self):
        first_request = uuid.uuid4()
        self.add("item", 3, "legacy_material_1002", 1002, first_request)
        first = VillageWorldEvent.objects.get(request_id=first_request)
        self.assertEqual(first.public_data["current_stage_title"], "筹备")
        need = next(row for row in first.public_data["open_needs"] if row["condition_id"] == "legacy_material_1002")
        self.assertEqual(need, {"condition_id": "legacy_material_1002", "label": "木材", "kind": "item",
            "item_id": 1002, "remaining": 97, "unit_minutes": None, "locations": ["REPAIR_YARD"]})
        self.assertIn("Bill贡献了木材3份", first.player_summary)
        self.add("item", 1, "legacy_material_1002", 1002)
        first.refresh_from_db()
        self.assertEqual(next(row["remaining"] for row in first.public_data["open_needs"]
            if row["condition_id"] == "legacy_material_1002"), 97)

    def test_bridge_completion_uses_config_and_request_replay_keeps_one_event(self):
        self.bridge.progress = {"conditions": {"legacy_material_1002": 100, "legacy_meals": 20,
            "legacy_gold": 500, "legacy_labor": 19}}
        self.bridge.save(update_fields=["progress"])
        request_id = uuid.uuid4()
        self.add("labor", 1, "legacy_labor", request_id=request_id)
        self.add("labor", 1, "legacy_labor", request_id=request_id)
        event = VillageWorldEvent.objects.get(request_id=request_id)
        self.assertEqual(event.event_type, "VILLAGE_AFFAIR_COMPLETED")
        self.assertEqual(event.public_data["open_needs"], [])
        self.assertIsNone(event.public_data["stage_id"])
        self.assertEqual(event.public_data["current_stage_title"], "已完成")
        self.assertIn("后续商队", event.player_summary)
        self.assertIn("37件", event.player_summary)
        self.bridge.refresh_from_db()
        self.assertEqual(self.bridge.progress["conditions"]["legacy_labor"], 20)
        self.assertEqual(VillageWorldEvent.objects.filter(request_id=request_id).count(), 1)

    def test_valid_long_bridge_labels_cannot_overflow_event_summary(self):
        self.bridge.title = "桥" * 100
        self.bridge.requirements["stages"][0]["title"] = "筹" * 100
        for condition in self.bridge.requirements["stages"][0]["conditions"]:
            condition["label"] = "材" * 100
        self.bridge.save(update_fields=["title", "requirements"])
        request_id = uuid.uuid4()
        self.add("item", 1, "legacy_material_1002", 1002, request_id)
        event = VillageWorldEvent.objects.get(request_id=request_id)
        self.assertLessEqual(len(event.player_summary), 256)
        self.assertEqual(event.public_data["current_stage_title"], "筹" * 100)
        self.assertEqual(event.public_data["open_needs"][0]["remaining"], 99)

    def test_live_and_journal_share_only_explicit_story_fields(self):
        story = {"id": str(uuid.uuid4()), "template_code": "PANTRY", "title": "备料", "status": "OPEN",
            "summary": "准备下一顿饭", "npc_id": "10014", "source_type": "COMMISSION",
            "source_id": str(uuid.uuid4()), "private_wallet": 999}
        event = VillageWorldEvent.objects.create(dedupe_key="projection:daily", event_type="STORY_DAILY_OPENED",
            world_minute=600, time_revision=1, visibility="PUBLIC", player_summary="备料机会",
            public_data={"story_event": story, "private_inventory": ["secret-batch"]},
            private_data={"secret": "never"})
        live = event_payload(event, ENV_CLIENTS)
        history = public_journal("42", {"limit": 20})["events"][0]
        self.assertEqual(live["story_event"], history["story_event"])
        self.assertNotIn("private_wallet", history["story_event"])
        self.assertNotIn("public_data", history)
        self.assertNotIn("private_inventory", str(live))
        self.assertNotIn("private_data", str(history))
        live["story_event"]["title"] = "changed-client-copy"
        self.assertEqual(event.public_data["story_event"]["title"], "备料")

    def test_bridge_live_and_history_share_needs_and_hide_unlisted_fields(self):
        request_id = uuid.uuid4()
        self.add("item", 1, "legacy_material_1002", 1002, request_id)
        event = VillageWorldEvent.objects.get(request_id=request_id)
        event.public_data["private_wallet"] = 999
        event.public_data["open_needs"][0]["private_batch"] = "never"
        event.save(update_fields=["public_data"])
        live = event_payload(event, ENV_CLIENTS)
        history = public_journal("42", {"limit": 20})["events"][0]
        for key in ("affair_id", "stage_id", "current_stage_title", "open_needs", "completion_effect", "version"):
            self.assertEqual(live[key], history[key])
        self.assertNotIn("private_wallet", str(live))
        self.assertNotIn("private_batch", str(history))

    def test_other_or_private_events_keep_existing_player_field_contract(self):
        event = VillageWorldEvent(event_type="STORY_TRANSITION", visibility="PUBLIC",
            public_data={"story_event": {"title": "not-a-daily-event"}, "affair_id": "BRIDGE_REPAIR"})
        self.assertEqual(public_story_event_fields(event), {})
        event.event_type, event.visibility = "STORY_DAILY_OPENED", "PARTICIPANTS"
        self.assertEqual(public_story_event_fields(event), {})

    @patch("village.daily_events.project_daily_events")
    def test_npc_current_context_and_affairs_query_restore_relevant_opportunities(self, project):
        project.return_value = [{"id": str(uuid.uuid4()), "title": "修桥筹备", "npc_id": ""}]
        context = build_agent_context(self.profile.capabilities, profile=self.profile)
        self.assertEqual(context["current_daily_events"], project.return_value)
        project.assert_called_with(npc_id="10011")
        result = _affairs(self.npc)
        self.assertEqual(result["daily_events"], project.return_value)
        self.assertEqual(result["affairs"][0]["affair_id"], "BRIDGE_REPAIR")
        self.assertEqual(result["world_minute"], 600)
        self.bridge.refresh_from_db()
        self.assertEqual(self.bridge.progress["conditions"]["legacy_material_1002"], 0)

    @patch("village.phase7_services._public_economy", return_value={})
    @patch("village.daily_events.project_daily_events")
    def test_player_overview_restores_opportunities_without_old_story_arc(self, project, _economy):
        project.return_value = [{"id": str(uuid.uuid4()), "title": "修桥筹备"}]
        overview = public_overview("42")
        self.assertEqual(overview["story"]["daily_events"], project.return_value)
        self.assertEqual(overview["story"]["affairs"][0]["affair_id"], "BRIDGE_REPAIR")
        project.assert_called_with(600)

    def test_persisted_opportunities_restore_for_each_audience_without_mutation(self):
        template = VillageDailyEventTemplate.objects.create(code="RECOVERY_TEST", title="补货",
            rule_type="INVENTORY_SHORTAGE", parameters={"npc_id": "10011", "item_ids": [1002], "minimum": 2},
            summary="补充材料", resolved_summary="已补充材料")
        rows = []
        for index, npc_id in enumerate(("10011", "10014", "")):
            rows.append(VillageDailyEvent.objects.create(template=template, source_id=f"source-{index}",
                active_key=f"recovery-{index}", title="补货", summary="补充材料", npc_id=npc_id,
                rule_snapshot={"rule_type": "AFFAIR_STAGE" if not npc_id else "INVENTORY_SHORTAGE",
                    "npc_scope": "PUBLIC" if not npc_id else "PERSONAL"}, opened_world_minute=500,
                invite_until_world_minute=1100))
        before = list(VillageDailyEvent.objects.order_by("id").values("id", "status", "active_key", "facts"))
        context = build_agent_context(self.profile.capabilities, profile=self.profile)
        query = _affairs(self.npc)
        expected = {str(rows[0].pk), str(rows[2].pk)}
        self.assertEqual({row["id"] for row in context["current_daily_events"]}, expected)
        self.assertEqual({row["id"] for row in query["daily_events"]}, expected)
        with patch("village.phase7_services._public_economy", return_value={}):
            overview = public_overview("42")
        self.assertEqual({row["id"] for row in overview["story"]["daily_events"]}, {str(row.pk) for row in rows})
        after = list(VillageDailyEvent.objects.order_by("id").values("id", "status", "active_key", "facts"))
        self.assertEqual(before, after)
