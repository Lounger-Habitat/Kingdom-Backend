"""Audience correction must preserve history and suppress stale retry deliveries."""
import importlib
import uuid
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

from django.apps import apps
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from village.models import VillageDailyEvent, VillageDailyEventTemplate, VillageDeliveryOutbox, VillageWorldEvent
from village.phase4_visibility import ENV_CLIENTS, event_payload
from village.runtime_services import OutboxService


class DailyEventScopeMigrationTests(TestCase):
    def event(self, *, scope=None):
        template = VillageDailyEventTemplate.objects.create(code=f"SCOPE_{uuid.uuid4()}", title="饭店备料",
            rule_type="RECIPE_PREPARATION", parameters={"npc_id": "10014"}, summary="缺料", resolved_summary="已补货")
        snapshot = {"rule_type": "RECIPE_PREPARATION", "parameters": {"npc_id": "10014"}}
        if scope is not None:
            snapshot["npc_scope"] = scope
        row = VillageDailyEvent.objects.create(template=template, source_id="10014", title="饭店备料",
            npc_id="10014", status="RESOLVED", opened_world_minute=600, invite_until_world_minute=1200,
            closed_world_minute=610, rule_snapshot=snapshot, facts={"keep": "business facts"})
        story = {"id": str(row.pk), "npc_id": "10014", "source_type": "RECIPE_PREPARATION",
            "status": "OPEN", "title": "饭店备料", "opened_world_minute": 600, "invite_until_world_minute": 1200}
        if scope is not None:
            story["npc_scope"] = scope
        event = VillageWorldEvent.objects.create(dedupe_key=f"old-scope:{uuid.uuid4()}",
            event_type="STORY_DAILY_OPENED", visibility="PUBLIC", world_minute=600, time_revision=1,
            public_data={"story_event": story, "keep": "frozen opening data"})
        return row, event

    def delivery(self, event, recipient, status="PENDING", *, expired=False):
        return VillageDeliveryOutbox.objects.create(dedupe_key=f"event:{event.pk}:{recipient}",
            event=event, recipient=recipient, message_kind="EVENT", status=status,
            lease_until=timezone.now() + timedelta(minutes=-1 if expired else 10) if status == "IN_FLIGHT" else None,
            payload=event_payload(event, recipient))

    def migrate_scope(self):
        migration = importlib.import_module("village.migrations.0073_daily_event_npc_scope")
        migration.backfill_scope(apps, SimpleNamespace(connection=connection))

    def test_migration_preserves_opening_facts_and_closes_only_unsent_wrong_audience(self):
        row, event = self.event()
        bill = self.delivery(event, "10011")
        jack = self.delivery(event, "10012", "IN_FLIGHT")
        acknowledged = self.delivery(event, "10013", "ACKED")
        dead = self.delivery(event, "10015", "DEAD_LETTER")
        owner = self.delivery(event, "10014")
        player = self.delivery(event, ENV_CLIENTS)
        original_acked = deepcopy(acknowledged.payload)
        original_business = deepcopy(row.facts)
        self.migrate_scope()
        row.refresh_from_db()
        event.refresh_from_db()
        self.assertEqual(row.rule_snapshot["npc_scope"], "PERSONAL")
        self.assertEqual((row.status, row.facts), ("RESOLVED", original_business))
        self.assertEqual(event.public_data["story_event"]["status"], "OPEN")
        self.assertEqual(event.public_data["story_event"]["npc_scope"], "PERSONAL")
        self.assertEqual(event.public_data["keep"], "frozen opening data")
        for wrong in (bill, jack):
            wrong.refresh_from_db()
            self.assertEqual(wrong.status, "ACKED")
            self.assertEqual(wrong.last_error_code, "DAILY_NPC_SCOPE_SUPPRESSED")
            self.assertIsNone(wrong.lease_until)
        acknowledged.refresh_from_db()
        dead.refresh_from_db()
        self.assertEqual(acknowledged.payload, original_acked)
        self.assertEqual(dead.status, "DEAD_LETTER")
        owner.refresh_from_db()
        player.refresh_from_db()
        self.assertEqual(owner.payload["public_data"]["story_event"]["status"], "OPEN")
        self.assertEqual(owner.payload["public_data"]["story_event"]["npc_scope"], "PERSONAL")
        self.assertEqual(player.payload["story_event"]["npc_scope"], "PERSONAL")
        self.assertEqual(len(OutboxService.claim_pending("10014")), 1)
        self.assertEqual(len(OutboxService.claim_pending(ENV_CLIENTS)), 1)
        self.assertEqual(OutboxService.claim_pending("10011"), [])
        count = VillageDeliveryOutbox.objects.count()
        self.migrate_scope()
        self.assertEqual(VillageDeliveryOutbox.objects.count(), count)

    def test_claim_rechecks_personal_scope_for_stale_pending_and_expired_leases(self):
        _, event = self.event(scope="PERSONAL")
        for recipient, status in (("10011", "PENDING"), ("10012", "IN_FLIGHT")):
            with self.subTest(status=status):
                wrong = self.delivery(event, recipient, status, expired=True)
                self.assertEqual(OutboxService.claim_pending(recipient), [])
                wrong.refresh_from_db()
                self.assertEqual(wrong.status, "ACKED")
                self.assertEqual(wrong.last_error_code, "DAILY_NPC_SCOPE_SUPPRESSED")
                self.assertEqual(wrong.attempt_count, 0)

    def test_migration_preserves_explicit_public_scope_and_unrelated_deliveries(self):
        row, event = self.event(scope="PUBLIC")
        public_delivery = self.delivery(event, "10011")
        unrelated_event = VillageWorldEvent.objects.create(dedupe_key=f"other:{uuid.uuid4()}",
            event_type="VILLAGE_AFFAIR_PROGRESS", visibility="PUBLIC", world_minute=600, time_revision=1)
        unrelated = self.delivery(unrelated_event, "10012")
        before = deepcopy(unrelated.payload)
        self.migrate_scope()
        row.refresh_from_db()
        public_delivery.refresh_from_db()
        unrelated.refresh_from_db()
        self.assertEqual(row.rule_snapshot["npc_scope"], "PUBLIC")
        self.assertEqual(public_delivery.status, "PENDING")
        self.assertEqual(len(OutboxService.claim_pending("10011")), 1)
        self.assertEqual((unrelated.status, unrelated.payload), ("PENDING", before))

    def test_suppressed_delivery_does_not_block_cursor_or_skip_a_real_pending_event(self):
        _, wrong_event = self.event(scope="PERSONAL")
        self.delivery(wrong_event, "10011")
        _, first_event = self.event(scope="PUBLIC")
        first = self.delivery(first_event, "10011")
        _, last_event = self.event(scope="PUBLIC")
        last = self.delivery(last_event, "10011")
        claimed = OutboxService.claim_pending("10011", limit=2)
        self.assertEqual([entry["delivery_id"] for entry in claimed], [str(first.pk)])
        self.assertTrue(OutboxService.ack_delivery(first.pk, recipient="10011", event_sequence=first_event.event_sequence)[0])
        self.assertEqual(OutboxService.get_cursor("10011"), first_event.event_sequence)
        self.assertEqual([entry["delivery_id"] for entry in OutboxService.claim_pending("10011")], [str(last.pk)])
        self.assertTrue(OutboxService.ack_delivery(last.pk, recipient="10011", event_sequence=last_event.event_sequence)[0])
        self.assertEqual(OutboxService.get_cursor("10011"), last_event.event_sequence)
