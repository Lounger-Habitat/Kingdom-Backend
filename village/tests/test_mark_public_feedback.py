"""Public feedback must let an Agent act on real meetings and receipts."""
import uuid

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from village.tests import test_mark_governance as fixtures
from village.models import (MarkDailySalary, NpcRuntimeState, VillageDeliveryOutbox,
                            VillagePublicDecision, VillageStoryMeeting, VillageStoryVote)
from village.agent_queries import _broken_trade_route_detail
from village.phase5_story import meeting_readiness, settle_story_meeting, submit_story_vote


class MarkMeetingFeedbackTests(TestCase):
    setUp = fixtures.MarkGovernanceTests.setUp
    story_arc = fixtures.MarkGovernanceTests.story_arc
    act = fixtures.MarkGovernanceTests.act

    def meeting(self, start=0, expiry=4000):
        result = self.act("CALL_MEETING", starts_world_minute=start)
        self.assertEqual(result["result_code"], "OK", result)
        row = VillageStoryMeeting.objects.get(pk=result["data"]["meeting_id"])
        result = self.act("REGISTER_PROPOSAL", meeting_id=str(row.pk), plan_code="BRIDGE_REPAIR",
                          budget_gold=100, executor_npc_id="10015", expires_world_minute=expiry)
        self.assertEqual(result["result_code"], "OK", result)
        row.refresh_from_db()
        return row

    def vote(self, row, npc="10013"):
        return submit_story_vote({"request_id":str(uuid.uuid4()), "npc_id":npc,
            "params":{"meeting_id":str(row.pk), "plan_code":"BRIDGE_REPAIR"}})

    def three_votes(self, row):
        for npc in ("10013", "10011", "10014"):
            self.assertEqual(self.vote(row, npc)["result_code"], "OK")

    def query(self):
        return _broken_trade_route_detail(self.npcs["10013"])["meetings"][0]

    def test_queries_are_read_only_and_two_votes_do_not_mean_ready(self):
        row = self.meeting()
        for npc in ("10013", "10011"):
            self.vote(row, npc)
        before = list(VillageStoryMeeting.objects.values())
        with CaptureQueriesContext(connection) as queries:
            for _ in range(3):
                data = self.query()
                self.assertTrue(data["terms_locked"])
                self.assertFalse(data["can_settle"])
                self.assertEqual(data["valid_npc_vote_count"], 2)
                self.assertTrue(data["own_vote"]["counted"])
                self.assertIn("NOT_ENOUGH_VALID_NPC_VOTES", data["unavailable_reasons"])
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")) for q in queries))
        self.assertEqual(before, list(VillageStoryMeeting.objects.values()))
        result = self.act("PUBLISH_DECISION", meeting_id=str(row.pk))
        self.assertEqual(result["result_code"], data["settlement_result_code"])
        self.assertEqual(result["data"]["valid_npc_vote_count"], 2)
        self.assertTrue(result["data"]["own_vote"]["counted"])
        self.assertIsNone(meeting_readiness(row, 0, actor_id="10014")["own_vote"])
        self.assertNotIn("own_vote", meeting_readiness(row, 0))
        self.assertEqual(before, list(VillageStoryMeeting.objects.values()))
        self.assertEqual(VillagePublicDecision.objects.count(), 0)
        self.assertEqual(self.vote(row, "10014")["result_code"], "OK")
        self.assertTrue(self.query()["can_settle"])
        published = self.act("PUBLISH_DECISION", meeting_id=str(row.pk))
        self.assertEqual(published["result_code"], "OK", published)
        self.assertEqual(published["data"]["decision"]["meeting_id"], str(row.pk))
        self.assertEqual(published["data"]["decision"]["terms"]["budget_gold"], 100)

    def test_away_sleep_injury_and_departure_remove_vote_from_effective_count(self):
        row = self.meeting()
        self.three_votes(row)
        for invalid in ({"action_status":"AWAY"}, {"action_status":"SLEEPING"},
                        {"health_fp":300}, {"away_until_world_minute":10},
                        {"current_location_code":"RESTAURANT"}):
            with self.subTest(invalid=invalid):
                NpcRuntimeState.objects.filter(npc=self.npcs["10013"]).update(
                    action_status="IDLE", health_fp=950, away_until_world_minute=None, current_location_code="RECORD_HALL")
                NpcRuntimeState.objects.filter(npc=self.npcs["10013"]).update(**invalid)
                data = meeting_readiness(row, 0, actor_id="10013")
                self.assertFalse(data["can_settle"])
                self.assertFalse(data["mark_present"])
                self.assertFalse(data["own_vote"]["counted"])
                self.assertEqual(data["valid_npc_vote_count"], 2)
                self.assertIn("MARK_NOT_PRESENT", data["unavailable_reasons"])
                result = settle_story_meeting(row.pk, record_failed_attempt=False)
                self.assertEqual(result["result_code"], data["settlement_result_code"])

    def test_invalidated_vote_and_expired_terms_match_settlement(self):
        row = self.meeting(expiry=100)
        self.three_votes(row)
        VillageStoryVote.objects.filter(meeting=row, voter_id="10011").update(is_valid=False)
        self.assertEqual(self.query()["valid_npc_vote_count"], 2)
        self.assertFalse(self.query()["can_settle"])
        VillageStoryVote.objects.filter(meeting=row).update(is_valid=True)
        self.clock.world_minute=100; self.clock.save()
        data = self.query()
        self.assertEqual(data["unavailable_reasons"], ["PUBLIC_AUTHORIZATION_EXPIRED"])
        self.assertEqual(self.act("PUBLISH_DECISION", meeting_id=str(row.pk))["result_code"], data["settlement_result_code"])
        self.assertEqual(VillagePublicDecision.objects.count(), 0)

    def test_missing_proposal_is_reported_not_approved(self):
        row = self.meeting()
        self.three_votes(row)
        # Simulate an inconsistent saved proposal, not an authorized edit after voting.
        row.proposals={}; row.save()
        data = self.query()
        self.assertEqual(data["unavailable_reasons"], ["PUBLIC_PROPOSAL_REQUIRED"])
        self.assertEqual(settle_story_meeting(row.pk)["result_code"], data["settlement_result_code"])

    def test_precise_unknown_not_started_closed_and_settled_errors(self):
        fake = uuid.uuid4()
        self.assertEqual(self.vote(type("Missing", (), {"pk":fake})())["result_code"], "STORY_MEETING_NOT_FOUND")
        self.assertEqual(settle_story_meeting(fake)["result_code"], "STORY_MEETING_NOT_FOUND")
        self.assertEqual(self.act("PUBLISH_DECISION", meeting_id=str(fake))["result_code"], "STORY_MEETING_NOT_FOUND")
        row = self.meeting(start=60)
        data = self.query()
        self.assertFalse(data["voting_open"])
        self.assertIn("STORY_MEETING_NOT_STARTED", data["unavailable_reasons"])
        self.assertEqual(self.vote(row)["result_code"], "STORY_MEETING_NOT_STARTED")
        self.assertEqual(self.act("PUBLISH_DECISION", meeting_id=str(row.pk))["result_code"], "STORY_MEETING_NOT_STARTED")
        self.clock.world_minute=60; self.clock.save()
        self.three_votes(row)
        self.assertEqual(self.act("PUBLISH_DECISION", meeting_id=str(row.pk))["result_code"], "OK")
        self.assertEqual(self.vote(row)["result_code"], "STORY_MEETING_ALREADY_SETTLED")
        row.status="FAILED"; row.save()
        self.assertEqual(self.vote(row)["result_code"], "STORY_MEETING_CLOSED")
        self.assertEqual(settle_story_meeting(row.pk)["result_code"], "STORY_MEETING_CLOSED")

    def test_voting_window_end_does_not_remove_existing_day_close_settlement(self):
        row = self.meeting()
        self.three_votes(row)
        self.clock.world_minute=1440; self.clock.save()
        data = self.query()
        self.assertFalse(data["voting_open"])
        self.assertEqual(data["voting_unavailable_reason"], "STORY_MEETING_CLOSED")
        self.assertTrue(data["can_settle"])
        self.assertEqual(self.vote(row)["result_code"], "STORY_MEETING_CLOSED")
        self.assertEqual(settle_story_meeting(row.pk)["result_code"], "OK")

    def test_notification_has_actual_start_time_and_other_npc_recipients(self):
        row = self.meeting(start=60)
        deliveries = VillageDeliveryOutbox.objects.filter(event__event_type="STORY_CALL_MEETING", message_kind="EVENT")
        self.assertTrue({"10011","10012","10014","10015"}.issubset(set(deliveries.values_list("recipient",flat=True))))
        for delivery in deliveries.exclude(recipient="ENV_CLIENTS"):
            content=delivery.payload
            self.assertEqual(content["public_data"]["meeting_id"], str(row.pk))
            self.assertEqual(content["public_data"]["starts_world_minute"], 60)
            self.assertIn("01:00", content["summary"])
            self.assertEqual(content["world_minute"], 0)
        self.assertEqual(MarkDailySalary.objects.count(), 0)

    def test_settled_event_is_not_an_authorization_to_spend_before_publication(self):
        row = self.meeting()
        self.three_votes(row)
        result=settle_story_meeting(row.pk)
        self.assertEqual(result["result_code"], "OK")
        event=VillageDeliveryOutbox.objects.filter(event__event_type="STORY_MEETING_SETTLED",recipient="10015").get()
        self.assertIsNone(event.payload["public_data"]["decision"]["published_world_minute"])
        self.assertIn("尚待正式公布", event.payload["summary"])
        self.assertEqual(event.payload["public_data"]["valid_npc_vote_count"], 3)
        published=self.act("PUBLISH_DECISION", decision_id=result["data"]["decision"]["decision_id"])
        self.assertEqual(published["result_code"], "OK")
        event=VillageDeliveryOutbox.objects.filter(event__event_type="STORY_PUBLISH_DECISION",recipient="10015").get()
        self.assertEqual(event.payload["public_data"]["decision"]["published_world_minute"], 0)
        self.assertIn("已正式公布", event.payload["summary"])


class MarkSalaryReceiptTests(TestCase):
    setUp = fixtures.MarkFinanceTests.setUp
    audit = fixtures.MarkFinanceTests.audit

    def test_receipt_distinguishes_new_entitlement_paid_and_outstanding(self):
        self.account.balance=0; self.account.audit_opening_balance=0; self.account.save()
        first=self.audit()["effects"]
        self.assertEqual(first["salary"]["new_amount_due"], 22)
        self.assertEqual(first["salary"]["paid"], 0)
        self.assertEqual(first["salary"]["outstanding"], 22)
        second=self.audit()["effects"]
        self.assertEqual(second["salary"]["new_amount_due"], 0)
        self.assertEqual(second["salary"]["amount_due_today"], 22)
        from village.models import EconomicLedgerEntry
        self.account.balance=30; self.account.save()
        EconomicLedgerEntry.objects.create(request_id=uuid.uuid4(),entry_type="DONATION",world_minute=self.time.world_minute,
                                          changes={"PUBLIC_FUND":{"money":30}})
        paid=self.audit()["effects"]
        self.assertEqual(paid["salary"]["paid"], 22)
        self.assertEqual(paid["salary"]["new_amount_due"], 0)
        self.assertEqual(paid["salary"]["amount_paid_for_today"], 22)
        self.assertEqual(paid["salary"]["outstanding"], 0)
        self.assertEqual(paid["salary"]["public_fund_balance"], 8)
        self.mark_inv.refresh_from_db()
        self.assertEqual(paid["salary"]["personal_balance"], self.mark_inv.money)
        repeat=self.audit()["effects"]
        self.assertEqual(repeat["salary"]["paid"], 0)
        self.assertEqual(repeat["salary"]["amount_paid_for_today"], 22)
        self.assertTrue(repeat["audit"]["balance_matches"])
        self.assertEqual(MarkDailySalary.objects.count(), 1)

    def test_paying_old_arrears_does_not_claim_today_wage_has_been_paid(self):
        self.account.balance=0; self.account.audit_opening_balance=0; self.account.save()
        self.assertEqual(self.audit()["effects"]["salary"]["outstanding"], 22)
        self.time.world_minute += 1440; self.time.save()
        self.account.balance=30; self.account.save()
        from village.models import EconomicLedgerEntry
        EconomicLedgerEntry.objects.create(request_id=uuid.uuid4(),entry_type="DONATION",world_minute=self.time.world_minute,
                                          changes={"PUBLIC_FUND":{"money":30}})
        salary=self.audit()["effects"]["salary"]
        self.assertEqual(salary["new_amount_due"], 22)
        self.assertEqual(salary["paid"], 22)
        self.assertEqual(salary["amount_paid_for_today"], 0)
        self.assertEqual(salary["outstanding"], 22)
