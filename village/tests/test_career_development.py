"""Focused real progress, finite trials and review regressions on isolated SQLite."""
import uuid

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from village.affairs import AffairError, resolve_condition
from village.career_development import (content_access, ensure_career_development, project_career_development,
    procurement_candidates, record_business_evidence, recommended_item_ids,
    has_production_evidence, release_trial_production, reserve_trial_production, sync_career_development)
from village.models import (VillageAffair, VillageCareerDevelopment, VillageCareerEvidence,
    VillageCareerProductionReservation, VillageCaravanOrder, VillageLocationDefinition, VillageSimulationConfig, VillageWorldEvent)
from village.farm_expansion import preparation_code, plot_affair_code
from village.tests import test_farm_expansion as farm_fixtures


class CareerDevelopmentTests(TestCase):
    finish = farm_fixtures.FarmExpansionTests.finish

    def setUp(self):
        farm_fixtures.FarmExpansionTests.setUp(self)
        self.cfg.career_development_enabled = True
        self.cfg.save(update_fields=["career_development_enabled"])
        for code in ("BILL_FARM", "BUTCHER_YARD", "RESTAURANT", "RECORD_HALL", "TRADE_ROAD"):
            VillageLocationDefinition.objects.get_or_create(location_code=code, defaults=dict(display_name=code,
                center_anchor_code=code, location_type="work", bounds={}, opening_hours={"alwaysOpen": True}, is_active=True))
        ensure_career_development(self.clock.world_minute)

    def evidence(self, kind, items, metadata=None, actor_type="NPC", actor_id="10012", rid=None):
        return record_business_evidence(kind, rid or uuid.uuid4(), actor_type, actor_id, items,
            self.clock.world_minute, metadata)

    def test_paid_preparation_trial_reservation_cancellation_and_once_only_unlock(self):
        track = VillageCareerDevelopment.objects.get(pk="JACK_1")
        self.assertEqual(track.status, "PREPARING")
        self.assertFalse(content_access(29403)["can_produce"])
        with CaptureQueriesContext(connection) as queries:
            project_career_development(self.clock.world_minute)
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        self.finish(track.preparation_affair)
        track.refresh_from_db()
        self.assertEqual(track.status, "TRIAL")
        self.bag.refresh_from_db()
        self.assertEqual(self.bag.money, 220)
        cancelled = uuid.uuid4()
        self.assertTrue(reserve_trial_production(29403, 24, cancelled, self.clock.world_minute))
        self.assertTrue(reserve_trial_production(29403, 24, cancelled, self.clock.world_minute))
        self.assertFalse(content_access(29403)["can_produce"])
        self.assertIn(29403, [r["item_id"] for r in procurement_candidates(self.clock.world_minute)])
        self.assertEqual(release_trial_production(cancelled), 1)
        self.assertEqual(content_access(29403)["trial_remaining"], 24)
        produced = uuid.uuid4()
        self.assertTrue(reserve_trial_production(29403, 3, produced, self.clock.world_minute))
        self.assertTrue(self.evidence("PRODUCTION", {29403: 2}, rid=produced))
        self.assertFalse(self.evidence("PRODUCTION", {29403: 2}, rid=produced))
        self.assertEqual(content_access(29403)["trial_remaining"], 22)
        self.evidence("PRODUCTION", {29405: 1})
        self.evidence("SALE", {29403: 1}, dict(income=20, provenance_valid=True))
        self.assertTrue(content_access(29403)["unlocked"])
        self.assertEqual(VillageWorldEvent.objects.filter(event_type="CAREER_COMPLETED",
            dedupe_key="career:JACK_1:CAREER_COMPLETED").count(), 1)
        sync_career_development(None, self.clock.world_minute)
        self.assertEqual(VillageWorldEvent.objects.filter(dedupe_key="career:JACK_1:CAREER_COMPLETED").count(), 1)
        in_flight = uuid.uuid4()
        self.assertTrue(reserve_trial_production(29404, 2, in_flight, self.clock.world_minute))
        self.cfg.career_development_enabled = False
        self.cfg.save(update_fields=["career_development_enabled"])
        self.assertTrue(content_access(29403)["unlocked"])
        self.assertFalse(content_access(29404)["can_produce"])
        self.evidence("PRODUCTION", {29404: 1}, rid=in_flight)
        self.assertEqual(VillageCareerProductionReservation.objects.get(request_id=str(in_flight)).status, "CONSUMED")
        self.assertEqual(VillageCareerDevelopment.objects.get(pk="JACK_2").progress["production"]["29404"], 1)

    def test_bill_requires_real_ready_land_and_preserves_old_content(self):
        self.assertTrue(content_access(1030)["can_produce"])
        track = VillageCareerDevelopment.objects.get(pk="BILL_1")
        self.assertEqual(track.status, "LOCKED")
        with self.assertRaises(AffairError) as error:
            resolve_condition(track.preparation_affair, "gold")
        self.assertEqual(error.exception.code, "CAREER_IRRIGATION_REQUIRED")
        self.finish(self.irrigation)
        self.finish(VillageAffair.objects.get(pk=preparation_code("E")))
        self.finish(VillageAffair.objects.get(pk=plot_affair_code("FARM_PLOT_E_01")))
        track.refresh_from_db()
        self.assertEqual(track.status, "PREPARING")
        self.finish(track.preparation_affair)
        self.assertTrue(content_access(1047)["can_produce"])
        self.evidence("PRODUCTION", {1047: 5}, actor_id="10011")
        self.evidence("SALE", {1047: 1}, dict(income=5, provenance_valid=True), actor_id="10011")
        self.assertTrue(content_access(1047)["unlocked"])
        self.assertEqual(VillageCareerDevelopment.objects.get(pk="BILL_2").status, "PREPARING")
        same_rid = uuid.uuid4()
        self.evidence("PRODUCTION", {1047: 1}, actor_type="PLAYER", actor_id="player-a", rid=same_rid)
        self.evidence("PRODUCTION", {1047: 1}, actor_type="PLAYER", actor_id="player-b", rid=same_rid)
        self.assertEqual(VillageCareerEvidence.objects.filter(kind="PRODUCTION", request_id=str(same_rid)).count(), 2)

    def test_mark_low_scores_are_valid_and_only_external_new_sales_complete_nora(self):
        source = uuid.uuid4()
        self.evidence("PRODUCTION", {1038: 2}, {"source_request_id": str(source)},
            actor_id="10011", rid=source)
        self.assertTrue(has_production_evidence(source, 1038))
        self.assertFalse(has_production_evidence(source, 1047))
        self.evidence("REVIEW", {1053: 1}, {"review_id": str(uuid.uuid4()), "overall_score": 2}, actor_id="10013")
        self.evidence("REVIEW", {1067: 1}, {"review_id": str(uuid.uuid4()), "overall_score": 1}, actor_id="10013")
        mark = VillageCareerDevelopment.objects.get(code__startswith="MARK_")
        self.assertEqual(mark.status, "COMPLETED")
        self.assertEqual(recommended_item_ids(self.clock.world_minute), [1053, 1067])
        VillageCaravanOrder.objects.create(period_index=0, announcement_world_minute=self.clock.world_minute,
            deadline_world_minute=10000, budget_total=10, procurement_snapshot={"review_track_code": mark.code})
        self.assertEqual(recommended_item_ids(self.clock.world_minute), [])
        VillageAffair.objects.update_or_create(code="BRIDGE_REPAIR", defaults=dict(title="旧桥", status="COMPLETED",
            requirements={"materials": {"1002": 1}}, progress={"materials": {"1002": 1}},
            opened_world_minute=0, completed_world_minute=0))
        sync_career_development(None, self.clock.world_minute)
        self.evidence("SALE", {29403: 1}, {"external": False, "income": 20})
        self.assertEqual(VillageCareerDevelopment.objects.get(pk="NORA_1").status, "TRIAL")
        self.evidence("SALE", {29403: 1}, {"external": True, "income": 20, "provenance_valid": True})
        self.assertEqual(VillageCareerDevelopment.objects.get(pk="NORA_1").status, "COMPLETED")
        self.clock.world_minute += 6 * 1440
        self.clock.save(update_fields=["world_minute"])
        ensure_career_development(self.clock.world_minute)
        self.assertEqual(VillageCareerDevelopment.objects.filter(code__startswith="MARK_").count(), 2)
