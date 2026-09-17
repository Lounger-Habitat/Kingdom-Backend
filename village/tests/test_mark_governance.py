"""Current Mark policy: real wages, auditable funds and voted bounded authority."""
import time
import uuid
from types import SimpleNamespace
from django.test import TestCase
from inventory.models import NpcGlobalInventory
from items.models import Item
from village.models import (DailyPriceSnapshot, EconomicLedgerEntry, MarkDailySalary, NpcProfile,
                            NpcRuntimeState, NpcStoryFactAccess, VillageAccount, VillageProjectReview,
                            VillagePublicDecision, VillageStoryMeeting, VillageStoryVote, VillageSimulationConfig)
from village.tests import test_phase3_public_economy as economy_fixtures, test_phase5_story as story_fixtures
from village.tests.test_phase3_public_economy import _payload
from village.phase3_asset_service import commit_economy_action
from village.phase5_story import complete_story_action, settle_story_meeting, submit_story_vote, get_store_purchase_limit
from village.public_finance import public_fund_delta, reconcile, meal_reference
from village.agent_queries import _public_economy, _broken_trade_route_detail
from village.runtime_services import CommandService


class MarkFinanceTests(TestCase):
    def setUp(self):
        economy_fixtures.PublicEconomyTestBase.setUp(self)
        self.mark_inv, _ = NpcGlobalInventory.objects.get_or_create(npc=self.mark, defaults={"money":120})
        self.account.audit_opening_balance = self.account.balance
        self.account.save(update_fields=["audit_opening_balance"])

    def audit(self, request=None):
        return commit_economy_action(_payload("AUDIT_PUBLIC_FUND",10013,"RECORD_HALL",{"day_index":self.time.world_minute//1440},request))

    def test_one_daily_meal_reference_across_new_requests_and_actions(self):
        before=self.mark_inv.money
        payload=_payload("AUDIT_PUBLIC_FUND",10013,"RECORD_HALL",{"day_index":1})
        for _ in range(2):
            self.assertEqual(commit_economy_action(payload)["result_code"],"OK")
        self.assertEqual(self.audit()["result_code"],"OK")
        DailyPriceSnapshot.objects.create(day_index=1,prices={"1053":31,"1067":26},status="DRAFT")
        self.assertEqual(commit_economy_action(_payload("PUBLISH_PRICE_INDEX",10013,"RECORD_HALL",{"day_index":1}))["result_code"],"OK")
        self.mark_inv.refresh_from_db();self.account.refresh_from_db()
        self.assertEqual(self.mark_inv.money-before,22)
        self.assertEqual(self.account.balance,478)
        self.assertEqual(MarkDailySalary.objects.count(),1)
        self.assertEqual(MarkDailySalary.objects.get().amount_due,22)
        self.assertGreater(self.mark_inv.asset_version,0)
        self.assertGreater(self.account.asset_version,0)
        self.assertTrue(reconcile(self.account)["balance_matches"])

    def test_next_day_uses_new_published_price_without_repricing_debt(self):
        self.account.balance=0;self.account.audit_opening_balance=0;self.account.save()
        self.audit()
        self.time.world_minute=3000;self.time.save()
        DailyPriceSnapshot.objects.create(day_index=2,prices={"1053":30,"1067":19},status="PUBLISHED")
        result=self.audit()
        self.assertEqual(result["effects"]["salary"]["outstanding"],41)
        self.assertEqual(list(MarkDailySalary.objects.order_by("day_index").values_list("amount_due",flat=True)),[22,19])
        self.account.refresh_from_db();self.assertEqual(self.account.balance,0)

    def test_audit_detects_unrecorded_change_and_reports_partial_history(self):
        self.audit();self.account.refresh_from_db()
        self.account.balance+=7;self.account.save(update_fields=["balance"])
        result=self.audit()
        self.assertFalse(result["effects"]["audit"]["balance_matches"])
        self.assertEqual(result["effects"]["audit"]["difference"],7)
        self.account.audit_baseline_kind="MIGRATION";self.account.save()
        self.assertFalse(_public_economy(self.mark)["reconciliation"]["historical_verified"])

    def test_arbitrary_day_cannot_create_salary(self):
        for day in [0,999999,True]:
            self.assertNotEqual(commit_economy_action(_payload("AUDIT_PUBLIC_FUND",10013,"RECORD_HALL",{"day_index":day}))["result_code"],"OK")
        self.assertEqual(MarkDailySalary.objects.count(),0)

    def test_legacy_account_classification_and_no_double_count(self):
        cases=[({"mark_salary":3},"AUDIT_PUBLIC_FUND",0,-3),
               ({"eat":{"source":"COMMUNITY_MEAL","payment":22}},"EAT",0,-22),
               ({"action_code":"DONATE_REPAIR_RESOURCES","gold":50},"STORY_STORY_ACT",0,50),
               ({"PUBLIC_FUND":{"money":-22},"mark_salary":22},"AUDIT_PUBLIC_FUND",0,-22),
               ({},"EXPORT_GOODS",42,42),
               ({"income_account":"10015"},"EXPORT_GOODS",42,0)]
        for changes,kind,external,expected in cases:
            self.assertEqual(public_fund_delta(SimpleNamespace(changes=changes,entry_type=kind,external_delta=external)),expected)

    def test_migration_preserves_money_inventory_and_duration_and_marks_history(self):
        import importlib
        from django.apps import apps
        from django.db import connection
        from village.models import VillageActionDurationConfig
        duration,_=VillageActionDurationConfig.objects.update_or_create(action_name="AUDIT_PUBLIC_FUND",defaults={"duration_minutes":5})
        before=list(NpcGlobalInventory.objects.order_by("pk").values("pk","money","asset_version"))
        EconomicLedgerEntry.objects.create(request_id=uuid.uuid4(),entry_type="AUDIT_PUBLIC_FUND",actor_npc=self.mark,
                                          world_minute=1000,changes={"mark_salary":2})
        module=importlib.import_module("village.migrations.0035_mark_public_governance")
        module.initialize_existing_accounts(apps,SimpleNamespace(connection=connection))
        self.assertEqual(before,list(NpcGlobalInventory.objects.order_by("pk").values("pk","money","asset_version")))
        duration.refresh_from_db();self.assertEqual(duration.duration_minutes,5)
        self.account.refresh_from_db();self.assertEqual(self.account.balance,500)
        self.assertEqual(self.account.audit_baseline_kind,"MIGRATION")
        self.assertEqual(MarkDailySalary.objects.get(day_index=0).amount_paid,2)
        self.assertTrue(reconcile(self.account)["balance_matches"])


class MarkGovernanceTests(TestCase):
    story_arc=story_fixtures.PhaseFiveStoryTest.story_arc

    def setUp(self):
        story_fixtures.PhaseFiveStoryTest.setUp(self)
        self.config.village_economy_enabled=True;self.config.save()
        self.arc,self.fact=self.story_arc(phase="SHORTAGE_VISIBLE",plan="")
        for npc in self.npcs.values():
            NpcProfile.objects.update_or_create(npc=npc,defaults={"display_name":npc.name,"occupation":"clerk","home_location_code":"RECORD_HALL","work_location_code":"RECORD_HALL","social_location_code":"RECORD_HALL","capabilities":["STORY_ACT","STORY_VOTE","INSPECT_STORY_DETAIL"]})
        self.fund=VillageAccount.objects.get(account_code="PUBLIC_FUND")
        self.fund.balance=500;self.fund.audit_opening_balance=500;self.fund.save()
        Item.objects.update_or_create(item_id=1049,defaults={"item_name":"salt","item_type":"食材","item_price":2})

    def act(self, action, npc="10013", **params):
        self.arc.refresh_from_db()
        payload={"request_id":str(uuid.uuid4()),"npc_id":npc,"current_location_code":"RECORD_HALL" if npc=="10013" else "GENERAL_STORE",
                 "params":{"action_code":action,"expected_story_version":self.arc.story_version,**params}}
        return complete_story_action(payload)

    def prepare(self,plan="BRIDGE_REPAIR",publish=True,budget=100,expiry=4000):
        result=self.act("CALL_MEETING")
        self.assertEqual(result["result_code"],"OK",result)
        meeting=VillageStoryMeeting.objects.get(status="OPEN")
        result=self.act("REGISTER_PROPOSAL",meeting_id=str(meeting.pk),plan_code=plan,budget_gold=budget if plan=="BRIDGE_REPAIR" else 0,
                        executor_npc_id="10015",expires_world_minute=expiry,**({"ration_daily_limits":{"1049":2}} if plan=="RATION_AND_WAIT" else {}))
        self.assertEqual(result["result_code"],"OK",result)
        for npc in ("10011","10012","10013"):
            self.assertEqual(submit_story_vote({"request_id":str(uuid.uuid4()),"npc_id":npc,"params":{"meeting_id":str(meeting.pk),"plan_code":plan}})["result_code"],"OK")
        result=settle_story_meeting(meeting.pk)
        self.assertEqual(result["result_code"],"OK",result)
        decision=VillagePublicDecision.objects.get(meeting=meeting)
        if publish:
            result=self.act("PUBLISH_DECISION",decision_id=str(decision.pk))
            self.assertEqual(result["result_code"],"OK",result)
        return decision

    def test_voted_budget_bounds_purchase_and_existing_authority_survives_away(self):
        decision=self.prepare(budget=75)
        NpcRuntimeState.objects.filter(npc=self.npcs["10013"]).update(away_until_world_minute=5000)
        before=VillageAccount.objects.get(pk=self.fund.pk).balance
        result=self.act("PURCHASE_REPAIR_MATERIALS","10015",decision_id=str(decision.pk),amount=1)
        self.assertEqual(result["result_code"],"OK",result)
        self.assertEqual(VillageAccount.objects.get(pk=self.fund.pk).balance,before-50)
        self.assertEqual(self.act("PURCHASE_REPAIR_MATERIALS","10015",decision_id=str(decision.pk),amount=1)["result_code"],"PUBLIC_BUDGET_EXCEEDED")
        decision.refresh_from_db();self.assertEqual(decision.spent_gold,50)

    def test_plan_alone_is_not_purchase_authority(self):
        self.arc.current_plan="BRIDGE_REPAIR";self.arc.current_phase="EXECUTION";self.arc.save()
        result=self.act("PURCHASE_REPAIR_MATERIALS","10015",amount=1)
        self.assertEqual(result["result_code"],"PUBLIC_AUTHORIZATION_REQUIRED")
        self.fund.refresh_from_db();self.assertEqual(self.fund.balance,500)

    def test_three_other_votes_cannot_create_a_plan_without_mark(self):
        self.act("CALL_MEETING");meeting=VillageStoryMeeting.objects.get(status="OPEN")
        self.act("REGISTER_PROPOSAL",meeting_id=str(meeting.pk),plan_code="BRIDGE_REPAIR",budget_gold=100,executor_npc_id="10015",expires_world_minute=4000)
        NpcRuntimeState.objects.filter(npc=self.npcs["10013"]).update(away_until_world_minute=5000)
        for npc in ("10011","10012","10014"):
            self.assertEqual(submit_story_vote({"request_id":str(uuid.uuid4()),"npc_id":npc,"params":{"meeting_id":str(meeting.pk),"plan_code":"BRIDGE_REPAIR"}})["result_code"],"OK")
        for _ in range(2):self.assertEqual(settle_story_meeting(meeting.pk)["result_code"],"STORY_NOT_ENOUGH_ATTENDANCE")
        self.arc.refresh_from_db();self.assertEqual(self.arc.current_plan,"")
        self.assertEqual(VillagePublicDecision.objects.count(),0)

    def test_terms_freeze_after_first_vote(self):
        self.act("CALL_MEETING");meeting=VillageStoryMeeting.objects.get(status="OPEN")
        params={"meeting_id":str(meeting.pk),"plan_code":"BRIDGE_REPAIR","budget_gold":100,"executor_npc_id":"10015","expires_world_minute":4000}
        self.assertEqual(self.act("REGISTER_PROPOSAL",**params)["result_code"],"OK")
        submit_story_vote({"request_id":str(uuid.uuid4()),"npc_id":"10011","params":{"meeting_id":str(meeting.pk),"plan_code":"BRIDGE_REPAIR"}})
        params["budget_gold"]=999
        self.assertEqual(self.act("REGISTER_PROPOSAL",**params)["result_code"],"PUBLIC_PROPOSAL_LOCKED")

    def test_unpublished_or_overlong_public_limit_rejected_and_expiry_survives_ending(self):
        decision=self.prepare("RATION_AND_WAIT",publish=False)
        params={"decision_id":str(decision.pk),"limit_item_id":1049,"daily_limit":2}
        self.assertEqual(self.act("ENABLE_STORE_LIMITS","10015",**params)["result_code"],"PUBLIC_DECISION_NOT_PUBLISHED")
        self.assertEqual(self.act("PUBLISH_DECISION",decision_id=str(decision.pk))["result_code"],"OK")
        self.assertEqual(self.act("ENABLE_STORE_LIMITS","10015",expires_world_minute=99999999,**params)["result_code"],"PUBLIC_LIMIT_OUTSIDE_AUTHORIZATION")
        self.assertEqual(self.act("ENABLE_STORE_LIMITS","10015",**params)["result_code"],"OK")
        self.arc.refresh_from_db();self.arc.is_completed=True;self.arc.save()
        self.assertEqual(get_store_purchase_limit("10011",1049,3999),2)
        self.assertIsNone(get_store_purchase_limit("10011",1049,4000))

    def test_actual_purchase_evidence_reviewed_once_and_no_empty_trust_gain(self):
        decision=self.prepare()
        result=self.act("PURCHASE_REPAIR_MATERIALS","10015",decision_id=str(decision.pk),amount=1)
        self.assertEqual(result["result_code"],"OK")
        entry=EconomicLedgerEntry.objects.filter(changes__action_code="PURCHASE_REPAIR_MATERIALS").get()
        self.fact.refresh_from_db();trust=self.fact.public_trust
        params={"decision_id":str(decision.pk),"evidence_request_id":str(entry.request_id)}
        self.assertEqual(self.act("MANAGE_REPAIR_LEDGER",**params)["result_code"],"OK")
        self.assertEqual(self.act("MANAGE_REPAIR_LEDGER",**params)["result_code"],"PUBLIC_EVIDENCE_ALREADY_REVIEWED")
        self.fact.refresh_from_db();self.assertEqual(self.fact.public_trust,trust+1)
        self.assertEqual(VillageProjectReview.objects.count(),1)
        self.assertEqual(self.act("MANAGE_REPAIR_LEDGER",decision_id=str(decision.pk))["result_code"],"PUBLIC_EVIDENCE_INVALID")

    def test_formal_command_cannot_execute_while_away_or_dead(self):
        for fields in ({"away_until_world_minute":5000,"health_fp":950},{"away_until_world_minute":None,"health_fp":0}):
            NpcRuntimeState.objects.filter(npc=self.npcs["10013"]).update(**fields)
            ok,code,command=CommandService.accept_command(envelope_id=str(uuid.uuid4()),sender="10013",recipient="WuxiVillage",timestamp_ms=int(time.time()*1000),content={
                "schema_version":"village.command.v1","id":str(uuid.uuid4()),"request_id":str(uuid.uuid4()),"name":"STORY_ACT",
                "params":{"action_code":"CALL_MEETING"},"queue_mode":"STRICT","not_before_world_minute":0,"expires_world_minute":120,"ttl_ms":120000})
            self.assertFalse(ok,code)
        self.assertEqual(VillageStoryMeeting.objects.count(),0)

    def test_salary_debt_claim_does_not_create_new_entitlement(self):
        self.fund.balance=0;self.fund.audit_opening_balance=0;self.fund.save()
        result=commit_economy_action(_payload("AUDIT_PUBLIC_FUND",10013,"RECORD_HALL",{"day_index":0}))
        self.assertEqual(result["result_code"],"OK",result)
        self.assertEqual(MarkDailySalary.objects.get().amount_due,22)
        self.fund.balance=50;self.fund.save(update_fields=["balance"])
        EconomicLedgerEntry.objects.create(request_id=uuid.uuid4(),entry_type="DONATION",world_minute=0,changes={"PUBLIC_FUND":{"money":50}})
        self.assertEqual(self.act("CLAIM_PUBLIC_SALARY")["result_code"],"OK")
        self.assertEqual(self.act("CLAIM_PUBLIC_SALARY")["result_code"],"PUBLIC_NO_SALARY_DUE")
        self.fund.refresh_from_db();self.assertEqual(self.fund.balance,28)
        self.assertTrue(reconcile(self.fund)["balance_matches"])

    def test_discovery_exposes_terms_and_role_specific_admin_entries(self):
        self.act("CALL_MEETING")
        mark=_broken_trade_route_detail(self.npcs["10013"])
        nora=_broken_trade_route_detail(self.npcs["10015"])
        self.assertIn("REGISTER_PROPOSAL",mark["available_actions"])
        self.assertNotIn("REGISTER_PROPOSAL",nora["available_actions"])
        self.assertEqual(len(mark["meetings"]),1)
        self.assertEqual(self.act("REGISTER_PROPOSAL","10015")["result_code"],"STORY_FORBIDDEN_ACTOR")

    def test_private_budget_fact_must_exist_before_publication(self):
        self.assertEqual(self.act("REVEAL_STORY_FACT",fact_code="DEFERRED_BRIDGE_BUDGET")["result_code"],"STORY_FACT_NOT_UNLOCKED")
        from village.phase4_social import grant_npc_fact
        grant_npc_fact("10013","DEFERRED_BRIDGE_BUDGET",0)
        self.assertEqual(self.act("REVEAL_STORY_FACT",fact_code="DEFERRED_BRIDGE_BUDGET")["result_code"],"OK")
        self.fact.refresh_from_db();self.assertTrue(self.fact.mark_budget_delay_revealed)
        self.assertEqual(self.act("REVEAL_STORY_FACT",fact_code="DEFERRED_BRIDGE_BUDGET")["result_code"],"PUBLIC_FACT_ALREADY_PUBLISHED")

    def test_mark_can_conclude_and_publish_real_votes_without_advancing_world(self):
        self.act("CALL_MEETING")
        meeting=VillageStoryMeeting.objects.get(status="OPEN")
        self.act("REGISTER_PROPOSAL",meeting_id=str(meeting.pk),plan_code="BRIDGE_REPAIR",budget_gold=100,executor_npc_id="10015",expires_world_minute=4000)
        for npc in ("10011","10012","10013"):
            submit_story_vote({"request_id":str(uuid.uuid4()),"npc_id":npc,"params":{"meeting_id":str(meeting.pk),"plan_code":"BRIDGE_REPAIR"}})
        ok,code,command=CommandService.accept_command(envelope_id=str(uuid.uuid4()),sender="10013",recipient="WuxiVillage",timestamp_ms=int(time.time()*1000),content={
            "schema_version":"village.command.v1","id":str(uuid.uuid4()),"request_id":str(uuid.uuid4()),"name":"STORY_ACT",
            "params":{"action_code":"PUBLISH_DECISION","meeting_id":str(meeting.pk)},"queue_mode":"STRICT","not_before_world_minute":0,"expires_world_minute":120,"ttl_ms":120000})
        self.assertTrue(ok,code)
        self.assertEqual(command.status,"SUCCEEDED")
        decision=VillagePublicDecision.objects.get(meeting=meeting)
        self.assertEqual(decision.published_world_minute,0)
        self.assertEqual(decision.terms["budget_gold"],100)
        self.clock.refresh_from_db();self.assertEqual(self.clock.world_minute,0)

    def test_expired_authority_rejects_purchase_without_changing_budget(self):
        decision=self.prepare(expiry=100)
        self.clock.world_minute=100;self.clock.save()
        before=VillageAccount.objects.get(pk=self.fund.pk).balance
        self.assertEqual(self.act("PURCHASE_REPAIR_MATERIALS","10015",decision_id=str(decision.pk),amount=1)["result_code"],"PUBLIC_AUTHORIZATION_EXPIRED")
        decision.refresh_from_db();self.assertEqual(decision.spent_gold,0)
        self.assertEqual(VillageAccount.objects.get(pk=self.fund.pk).balance,before)

    def test_older_unpublished_decision_cannot_override_same_minute_newer_meeting(self):
        older=self.prepare(publish=False)
        meeting=VillageStoryMeeting.objects.create(arc=self.arc,meeting_type="PLAN_REVIEW",opened_day=0,closes_day=0,
            status="SETTLED",selected_plan="BRIDGE_REPAIR")
        VillagePublicDecision.objects.create(meeting=meeting,plan_code="BRIDGE_REPAIR",terms=older.terms,approved_world_minute=0)
        self.assertEqual(self.act("PUBLISH_DECISION",decision_id=str(older.pk))["result_code"],"PUBLIC_AUTHORIZATION_EXPIRED")
        older.refresh_from_db();self.assertIsNone(older.published_world_minute)

    def test_post_story_public_capabilities_remain_discoverable_until_expiry(self):
        self.prepare("RATION_AND_WAIT")
        self.arc.refresh_from_db();self.arc.is_completed=True;self.arc.save()
        mark=_broken_trade_route_detail(self.npcs["10013"])
        nora=_broken_trade_route_detail(self.npcs["10015"])
        self.assertIn("CALL_MEETING",mark["available_actions"])
        self.assertIn("ENABLE_STORE_LIMITS",nora["available_actions"])
        NpcRuntimeState.objects.filter(npc=self.npcs["10013"]).update(action_status="AWAY")
        self.assertEqual(_broken_trade_route_detail(self.npcs["10013"])["available_actions"],[])

    def test_mark_attendance_does_not_replace_three_valid_votes(self):
        self.act("CALL_MEETING");meeting=VillageStoryMeeting.objects.get(status="OPEN")
        self.act("REGISTER_PROPOSAL",meeting_id=str(meeting.pk),plan_code="BRIDGE_REPAIR",budget_gold=100,executor_npc_id="10015",expires_world_minute=4000)
        for npc in ("10011","10013"):
            submit_story_vote({"request_id":str(uuid.uuid4()),"npc_id":npc,"params":{"meeting_id":str(meeting.pk),"plan_code":"BRIDGE_REPAIR"}})
        self.assertEqual(settle_story_meeting(meeting.pk)["result_code"],"STORY_NOT_ENOUGH_ATTENDANCE")
        self.assertEqual(VillagePublicDecision.objects.count(),0)

    def test_away_status_without_deadline_cannot_count_as_mark_attendance(self):
        from village.phase5_story import _attendees
        NpcRuntimeState.objects.filter(npc=self.npcs["10013"]).update(action_status="AWAY",away_until_world_minute=None)
        self.assertNotIn("10013",_attendees())
