"""An invitation's NPC scope is independent of its public player visibility."""
import uuid

from django.test import TestCase

from inventory.models import NpcDefinition
from village.models import VillageDeliveryOutbox, VillageWorldEvent
from village.phase4_visibility import ENV_CLIENTS, FORMAL_NPC_IDS, enqueue_event_deliveries, event_payload
from village.phase7_services import public_journal


class DailyEventAudienceTests(TestCase):
    def setUp(self):
        self.bill, _ = NpcDefinition.objects.get_or_create(agent_id="10011", defaults={"name": "Bill"})

    def event(self, event_type="STORY_DAILY_OPENED", *, story=None, visibility="PUBLIC", **kwargs):
        return VillageWorldEvent.objects.create(dedupe_key=f"audience:{uuid.uuid4()}", event_type=event_type,
            world_minute=600, time_revision=1, actor_npc=self.bill,
            participant_npc_ids=kwargs.pop("participant_npc_ids", ["10011", "10012", "10013", "10015"]),
            witness_npc_ids=kwargs.pop("witness_npc_ids", list(FORMAL_NPC_IDS)),
            player_ids=kwargs.pop("player_ids", ["42"]), visibility=visibility,
            public_data={"story_event": story} if story is not None else {},
            private_data={"secret": "never"}, player_summary="公开参与机会", hub_summary="参与机会", **kwargs)

    def recipients(self, event):
        enqueue_event_deliveries(event)
        return set(VillageDeliveryOutbox.objects.filter(event=event).values_list("recipient", flat=True))

    def test_personal_open_resolved_expired_go_only_to_owner_and_players(self):
        for suffix, status in (("OPENED", "OPEN"), ("RESOLVED", "RESOLVED"), ("EXPIRED", "EXPIRED")):
            with self.subTest(status=status):
                event = self.event(f"STORY_DAILY_{suffix}", story={"id": str(uuid.uuid4()), "status": status,
                    "npc_id": "10014", "npc_scope": "PERSONAL", "source_type": "RECIPE_PREPARATION"})
                self.assertEqual(self.recipients(event), {"10014", ENV_CLIENTS})
                self.assertEqual(self.recipients(event), {"10014", ENV_CLIENTS})
                self.assertEqual(VillageDeliveryOutbox.objects.filter(event=event).count(), 2)
                live = VillageDeliveryOutbox.objects.get(event=event, recipient=ENV_CLIENTS).payload
                history = public_journal("900", {"limit": 1})["events"][0]
                self.assertEqual(live["story_event"], history["story_event"])
                self.assertEqual(live["story_event"]["npc_scope"], "PERSONAL")
                self.assertNotIn("private_data", str(live))
                self.assertEqual(history["event_id"], str(event.event_id))

    def test_public_scope_with_lucy_owner_reaches_all_five_in_every_state(self):
        for suffix in ("OPENED", "RESOLVED", "EXPIRED"):
            with self.subTest(suffix=suffix):
                event = self.event(f"STORY_DAILY_{suffix}", story={"npc_id": "10014", "npc_scope": "PUBLIC",
                    "source_type": "PUBLIC_MEAL"}, participant_npc_ids=[], witness_npc_ids=[])
                self.assertEqual(self.recipients(event), {*FORMAL_NPC_IDS, ENV_CLIENTS})

    def test_old_scope_inference_keeps_personal_stock_and_public_sources_distinct(self):
        cases = [("INVENTORY_SHORTAGE", "10014", "PERSONAL", {"10014", ENV_CLIENTS}),
                 ("PUBLIC_MEAL", "10014", "PUBLIC", {*FORMAL_NPC_IDS, ENV_CLIENTS}),
                 ("AFFAIR_STAGE", "10014", "PUBLIC", {*FORMAL_NPC_IDS, ENV_CLIENTS}),
                 ("NORMAL_CARAVAN_RETURN", "10015", "PUBLIC", {*FORMAL_NPC_IDS, ENV_CLIENTS}),
                 ("INVENTORY_SHORTAGE", "", "PERSONAL", {ENV_CLIENTS}),
                 ("UNKNOWN", "", "PERSONAL", {ENV_CLIENTS}),
                 (None, None, "PERSONAL", {ENV_CLIENTS})]
        for source, owner, scope, expected in cases:
            with self.subTest(source=source, owner=owner):
                event = self.event(story={"source_type": source, "npc_id": owner})
                self.assertEqual(self.recipients(event), expected)
                live = event_payload(event, ENV_CLIENTS)
                history = public_journal("42", {"limit": 1})["events"][0]
                self.assertEqual(live["story_event"], history["story_event"])
                self.assertEqual(live["story_event"]["npc_scope"], scope)

    def test_invalid_scope_and_unknown_personal_owner_never_expand_to_participants(self):
        for scope in (None, "", "GROUP", "public", ["PUBLIC"]):
            with self.subTest(scope=scope):
                event = self.event(story={"npc_id": "10014", "npc_scope": scope, "source_type": "PUBLIC_MEAL"})
                self.assertEqual(self.recipients(event), {ENV_CLIENTS})
        for owner in ("", "90001", None):
            with self.subTest(owner=owner):
                event = self.event(story={"npc_id": owner, "npc_scope": "PERSONAL", "source_type": "AFFAIR_STAGE"})
                self.assertEqual(self.recipients(event), {ENV_CLIENTS})
        self.assertEqual(self.recipients(self.event()), {ENV_CLIENTS})

    def test_explicit_scope_overrides_legacy_source_inference(self):
        personal = self.event(story={"npc_scope": "PERSONAL", "npc_id": "10014", "source_type": "PUBLIC_MEAL"})
        public = self.event(story={"npc_scope": "PUBLIC", "npc_id": "10014", "source_type": "INVENTORY_SHORTAGE"})
        ownerless_public = self.event(story={"npc_scope": "PUBLIC", "npc_id": "", "source_type": "UNKNOWN"})
        self.assertEqual(self.recipients(personal), {"10014", ENV_CLIENTS})
        self.assertEqual(self.recipients(public), {*FORMAL_NPC_IDS, ENV_CLIENTS})
        self.assertEqual(self.recipients(ownerless_public), {*FORMAL_NPC_IDS, ENV_CLIENTS})

    def test_other_business_events_keep_existing_audiences(self):
        cases = [
            ("VILLAGE_AFFAIR_READY", "PUBLIC", {}, {*FORMAL_NPC_IDS, ENV_CLIENTS}),
            ("ECONOMY_PROPOSE_TRADE", "PARTICIPANTS", {"participant_npc_ids": ["10011", "10015"]}, {"10015", ENV_CLIENTS}),
            ("PLAYER_GIFT", "PARTICIPANTS", {"participant_npc_ids": ["10011"]}, {"10011"}),
            ("PLAYER_PLANT_FARM_PLOT", "PUBLIC", {"participant_npc_ids": ["10011", "10014"]}, {"10011", ENV_CLIENTS}),
            ("NPC_ACTION_COMPLETED", "PUBLIC", {}, set()),
        ]
        for event_type, visibility, kwargs, expected in cases:
            with self.subTest(event_type=event_type):
                self.assertEqual(self.recipients(self.event(event_type, visibility=visibility, **kwargs)), expected)
