import uuid
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from village.models import (
    EconomicLedgerEntry,
    VillageActionReceipt,
    VillageDeliveryCursor,
    VillageDeliveryOutbox,
    VillageWorldEvent,
)
from village.runtime_services import OutboxService, ResultCode


class Phase7BoundedRetentionTests(TestCase):
    def test_out_of_order_event_ack_keeps_gap_reclaimable_and_cursor_safe(self):
        first_event = VillageWorldEvent.objects.create(
            dedupe_key="phase7-ack-first", event_type="FIRST", world_minute=1,
            time_revision=1,
        )
        second_event = VillageWorldEvent.objects.create(
            dedupe_key="phase7-ack-second", event_type="SECOND", world_minute=2,
            time_revision=1,
        )
        first = VillageDeliveryOutbox.objects.create(
            dedupe_key="phase7-ack-delivery-first", recipient="ENV_CLIENTS",
            message_kind="EVENT", event=first_event, payload={}, priority=10,
        )
        second = VillageDeliveryOutbox.objects.create(
            dedupe_key="phase7-ack-delivery-second", recipient="ENV_CLIENTS",
            message_kind="EVENT", event=second_event, payload={}, priority=10,
        )

        claimed = OutboxService.claim_pending("ENV_CLIENTS", limit=2)
        self.assertEqual(
            {row["delivery_id"] for row in claimed},
            {str(first.delivery_id), str(second.delivery_id)},
        )
        ok, code = OutboxService.ack_delivery(
            second.delivery_id,
            event_sequence=second_event.event_sequence,
            recipient="ENV_CLIENTS",
        )
        self.assertTrue(ok, code)
        self.assertEqual(OutboxService.get_cursor("ENV_CLIENTS"), 0)

        old = timezone.now() - timedelta(days=30)
        VillageDeliveryOutbox.objects.filter(pk=second.pk).update(acked_at=old)
        self.assertEqual(OutboxService.cleanup(retention_days=7)["deleted"], 0)
        self.assertTrue(VillageDeliveryOutbox.objects.filter(pk=second.pk).exists())
        self.assertTrue(VillageDeliveryOutbox.objects.filter(pk=first.pk).exists())

        VillageDeliveryOutbox.objects.filter(pk=first.pk).update(
            lease_until=timezone.now() - timedelta(seconds=1)
        )
        reclaimed = OutboxService.claim_pending(
            "ENV_CLIENTS", limit=10, after_sequence=second_event.event_sequence
        )
        self.assertEqual([row["delivery_id"] for row in reclaimed], [str(first.delivery_id)])

        ok, code = OutboxService.ack_delivery(
            first.delivery_id,
            event_sequence=second_event.event_sequence,
            recipient="ENV_CLIENTS",
        )
        self.assertFalse(ok)
        self.assertEqual(code, ResultCode.INVALID_COMMAND_SCHEMA)
        self.assertEqual(OutboxService.get_cursor("ENV_CLIENTS"), 0)

        ok, code = OutboxService.ack_delivery(first.delivery_id, recipient="ENV_CLIENTS")
        self.assertTrue(ok, code)
        self.assertEqual(
            VillageDeliveryCursor.objects.get(recipient="ENV_CLIENTS").last_event_sequence,
            second_event.event_sequence,
        )
        self.assertEqual(OutboxService.cleanup(retention_days=7)["deleted"], 1)
        self.assertFalse(VillageDeliveryOutbox.objects.filter(pk=second.pk).exists())

        third_event = VillageWorldEvent.objects.create(
            dedupe_key="phase7-ack-third", event_type="THIRD", world_minute=3,
            time_revision=1,
        )
        outcome = VillageDeliveryOutbox.objects.create(
            dedupe_key="phase7-ack-outcome", recipient="ENV_CLIENTS",
            message_kind="OUTCOME", event=third_event, payload={}, priority=10,
        )
        self.assertTrue(OutboxService.ack_delivery(
            outcome.delivery_id, event_sequence=third_event.event_sequence,
            recipient="ENV_CLIENTS",
        )[0])
        self.assertEqual(OutboxService.get_cursor("ENV_CLIENTS"), second_event.event_sequence)

    def test_cleanup_is_bounded_and_never_deletes_unacked_or_audit_event(self):
        audit_event = VillageWorldEvent.objects.create(
            dedupe_key="phase7-soak-audit",
            event_type="SOAK_AUDIT",
            world_minute=1,
            time_revision=1,
        )
        old = timezone.now() - timedelta(days=30)
        receipt = VillageActionReceipt.objects.create(
            request_id=uuid.uuid4(), action_kind="SOAK_AUDIT", success=True,
            result_code="OK", world_minute=1, time_revision=1, world_event=audit_event,
        )
        ledger = EconomicLedgerEntry.objects.create(
            request_id=uuid.uuid4(), entry_type="SOAK_AUDIT", changes={},
            world_minute=1, event=audit_event,
        )
        entries = [
            VillageDeliveryOutbox(
                delivery_id=uuid.uuid4(),
                dedupe_key=f"phase7-cleanup-{index}",
                recipient="10011",
                message_kind="EVENT" if index % 2 == 0 else "OUTCOME",
                event=audit_event if index == 0 else None,
                receipt=receipt if index == 0 else None,
                payload={},
                priority=10,
                status="ACKED",
                acked_at=old - timedelta(days=1) if index == 0 else old,
            )
            for index in range(1005)
        ]
        VillageDeliveryOutbox.objects.bulk_create(entries)
        pending = VillageDeliveryOutbox.objects.create(
            dedupe_key="phase7-unacked",
            recipient="10011",
            message_kind="OUTCOME",
            payload={},
            priority=10,
            status="PENDING",
        )
        VillageDeliveryCursor.objects.create(
            recipient="10011", last_event_sequence=audit_event.event_sequence
        )

        result = OutboxService.cleanup(retention_days=7, batch_size=5000)
        self.assertEqual(result["deleted"], 1000)
        self.assertEqual(result["event_deleted"] + result["outcome_deleted"], 1000)
        self.assertEqual(VillageDeliveryOutbox.objects.filter(status="ACKED").count(), 5)
        self.assertTrue(VillageDeliveryOutbox.objects.filter(delivery_id=pending.delivery_id).exists())
        self.assertFalse(VillageDeliveryOutbox.objects.filter(delivery_id=entries[0].delivery_id).exists())
        self.assertTrue(VillageWorldEvent.objects.filter(event_id=audit_event.event_id).exists())
        self.assertTrue(VillageActionReceipt.objects.filter(request_id=receipt.request_id).exists())
        self.assertTrue(EconomicLedgerEntry.objects.filter(request_id=ledger.request_id).exists())

    def test_claim_limit_remains_clamped_to_one_through_one_hundred(self):
        VillageDeliveryOutbox.objects.bulk_create([
            VillageDeliveryOutbox(
                delivery_id=uuid.uuid4(), dedupe_key=f"phase7-claim-{index}",
                recipient="ENV_CLIENTS", message_kind="EVENT", payload={}, status="PENDING",
            )
            for index in range(101)
        ])
        self.assertEqual(len(OutboxService.claim_pending("ENV_CLIENTS", limit=0)), 1)
        VillageDeliveryOutbox.objects.update(status="PENDING", lease_until=None)
        self.assertEqual(len(OutboxService.claim_pending("ENV_CLIENTS", limit=1000)), 100)
