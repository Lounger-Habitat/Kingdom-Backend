"""Real commission completion, finite professional demand and permanent world recap."""
import uuid

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from inventory.models import NpcDefinition, NpcGlobalInventory
from items.models import Item
from player.models import PlayerGrowthState, PlayerSkillProgress
from village.cooperation import cooperation_overview, cooperation_score, record_commission_completion, post_bridge_recap
from village.models import (NpcProfile, VillageAffair, VillageAffairContribution, VillageCaravanVisit,
    VillagePersonalCommission, VillagePlayerNpcRelationship, VillagePlayerNpcRelationshipChange,
    VillageProfessionalContractLink, VillageLocationDefinition)
from village.personal_commissions import (CommissionError, publish_commission, accept_commission,
    contribute_commission, abandon_commission, project_commission, withdraw_commission)
from village.professional_contracts import (ProfessionalContractsError, professional_contracts_overview,
    execute_prepare_professional_contracts, completed_professional_contracts)
from village.tests.test_player_actions import PlayerFixture


class ProfessionalCooperationTests(PlayerFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.clock.world_minute = 700; self.clock.save()
        npc, _ = NpcDefinition.objects.get_or_create(agent_id="10015", defaults=dict(name="Nora"))
        self.nora, _ = NpcGlobalInventory.objects.update_or_create(npc=npc, defaults=dict(money=400))
        self.nora.slots.all().delete()
        NpcProfile.objects.update_or_create(npc=npc, defaults=dict(display_name="Nora", occupation="商人",
            home_location_code="GENERAL_STORE", work_location_code="GENERAL_STORE", social_location_code="VILLAGE_SQUARE",
            capabilities=["PUBLISH_PERSONAL_COMMISSION", "WITHDRAW_PERSONAL_COMMISSION"]))
        VillageLocationDefinition.objects.update_or_create(location_code="GENERAL_STORE", defaults=dict(
            display_name="杂货铺", center_anchor_code="GENERAL_STORE", location_type="PUBLIC", is_active=True))
        Item.objects.get_or_create(pk=1001, defaults=dict(item_name="南瓜", item_type="蔬菜", item_price=8))
        self.visit = VillageCaravanVisit.objects.create(day_index=0, created_world_minute=660, route="NORMAL",
            location_code="TRADE_ROAD", stock={"1030": 60}, initial_stock={"1030": 60},
            import_prices={"1030": 6}, export_prices={"1030": 5}, export_capacity=60)
        PlayerSkillProgress.objects.create(player=self.a, skill_code="business", total_xp=1980)

    def prepare(self, player=None, bag=None):
        return execute_prepare_professional_contracts(player or self.a, bag or self.bag_a, {}, uuid.uuid4(), self.clock)

    def relationship(self, score):
        return VillagePlayerNpcRelationship.objects.update_or_create(player=self.a, npc=self.nora.npc, defaults=dict(score=score))[0]

    def ordinary(self, publisher=None):
        npc = publisher or self.nora.npc
        return VillagePersonalCommission.objects.get(pk=publish_commission(npc,
            dict(title="真实小额备料", reward_gold=1, valid_for_minutes=1000, default_location_code="GENERAL_STORE",
                stages=[dict(id="stock", conditions=[dict(id="corn", type="item", item_ids=[1030], target=1)])]),
            uuid.uuid4(), self.clock.world_minute)["commission_id"])

    def complete(self, row, *, expiry=2000):
        row.refresh_from_db()
        if row.status != "ACTIVE":
            accept_commission(row.pk, "PLAYER", self.a.pk, uuid.uuid4(), self.clock.world_minute)
        for stage in row.requirements["stages"]:
            for condition in stage["conditions"]:
                slot = self.slot(condition["target"], item_id=condition["item_ids"][0], quality=4,
                    expires_world_minute=expiry if condition["requires_freshness"] else None)
                result = contribute_commission(row.pk, "PLAYER", self.a.pk, condition["id"], condition["target"],
                    uuid.uuid4(), self.clock.world_minute, item_id=slot.item_id, instance_id=slot.instance_id, location="GENERAL_STORE")
        row.refresh_from_db()
        return result

    def test_all_queries_select_only_and_zero_relationship_does_not_lock_first_template(self):
        with CaptureQueriesContext(connection) as queries:
            coop = cooperation_overview(self.a, 700)
            view = professional_contracts_overview(self.a, 700)
            readonly = professional_contracts_overview(self.a, 700, read_only=True)
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries), queries.captured_queries)
        self.assertTrue(view["can_prepare"])
        self.assertFalse(readonly["can_prepare"])
        self.assertFalse(VillagePlayerNpcRelationship.objects.exists())
        self.assertFalse(PlayerGrowthState.objects.exists())
        self.assertFalse(VillagePersonalCommission.objects.exists())
        self.assertEqual(next(t for t in view["templates"] if t["code"] == "EQUIPMENT_SUPPLY")["reason"], "COOPERATION_SCORE_REQUIRED")
        self.assertFalse(coop["post_bridge_recap"]["old_rewards_claimable"])

    def test_actual_completed_commissions_only_gain_two_daily_four_and_replay_once(self):
        for expected in (2, 4, 4):
            row = self.ordinary(); self.complete(row)
            self.assertEqual(cooperation_score(self.a, "10015"), expected)
            record_commission_completion(row, self.clock.world_minute)
            self.assertEqual(cooperation_score(self.a, "10015"), expected)
        self.assertEqual(VillagePlayerNpcRelationshipChange.objects.count(), 3)
        row = self.ordinary()
        accept_commission(row.pk, "PLAYER", self.a.pk, uuid.uuid4(), 700)
        abandon_commission(row.pk, "PLAYER", self.a.pk, uuid.uuid4(), 700)
        self.assertEqual(cooperation_score(self.a, "10015"), 4)
        self.clock.world_minute = 1500; self.clock.save()
        self.complete(self.ordinary())
        self.assertEqual(cooperation_score(self.a, "10015"), 6)
        self.relationship(99)
        self.clock.world_minute = 3000; self.clock.save()
        self.complete(self.ordinary())
        self.assertEqual(cooperation_score(self.a, "10015"), 100)

    def test_real_wallet_reserve_no_republish_after_refund_and_visit_stays_frozen(self):
        self.relationship(4)
        before = (dict(self.visit.stock), dict(self.visit.import_prices), dict(self.visit.export_prices), self.visit.export_capacity)
        result = self.prepare()
        self.assertEqual((result["spent_gold"], len(result["published_commission_ids"])), (140, 2))
        self.nora.refresh_from_db(); self.assertEqual(self.nora.money, 260)
        self.assertEqual(sum(VillagePersonalCommission.objects.values_list("reserved_reward_gold", flat=True)), 140)
        row = VillageProfessionalContractLink.objects.get(template_code="FRESH_SUPPLY").commission
        withdraw_commission(row.pk, "NPC", self.nora.npc_id, uuid.uuid4(), 700)
        self.assertEqual(self.prepare()["spent_gold"], 0)
        self.assertEqual(professional_contracts_overview(self.a, 700)["published_gold"], 140)
        self.assertEqual(VillageProfessionalContractLink.objects.count(), 2)
        self.visit.refresh_from_db()
        self.assertEqual((self.visit.stock, self.visit.import_prices, self.visit.export_prices, self.visit.export_capacity), before)

    def test_partial_gap_is_exact_and_price_is_proportional_not_full_pack(self):
        self.nora.slots.create(slot_index=0, item_id=1001, item_amount=1, quality=4,
            produced_world_minute=600, expires_world_minute=2000)
        view = professional_contracts_overview(self.a, 700)
        fresh = next(t for t in view["templates"] if t["code"] == "FRESH_SUPPLY")
        self.assertEqual((fresh["reward_gold"], sum(r["quantity"] for r in fresh["requirements"])), (61, 5))
        self.assertEqual(self.prepare()["spent_gold"], 61)
        row = VillageProfessionalContractLink.objects.get().commission
        self.assertEqual(next(c for c in row.requirements["stages"][0]["conditions"] if c["item_ids"] == [1001])["target"], 1)

    def test_wallet_normal_visit_and_real_accept_qualification_are_enforced(self):
        self.nora.money = 71; self.nora.save()
        with self.assertRaises(ProfessionalContractsError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, "NORA_BUDGET_UNAVAILABLE")
        self.nora.money = 400; self.nora.save()
        self.visit.route = "EMERGENCY"; self.visit.save()
        with self.assertRaises(ProfessionalContractsError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, "NORMAL_CARAVAN_REQUIRED")
        self.visit.route = "NORMAL"; self.visit.save()
        self.prepare()
        row = VillageProfessionalContractLink.objects.get().commission
        with self.assertRaises(CommissionError) as caught:
            accept_commission(row.pk, "PLAYER", self.b.pk, uuid.uuid4(), 700)
        self.assertEqual(caught.exception.code, "PROFESSIONAL_LEVEL_REQUIRED")
        data = project_commission(row, actor_type="PLAYER", actor_id=self.b.pk)
        self.assertTrue(data["professional_contract"])
        self.assertFalse(data["can_accept"])

    def test_completion_transfers_real_batches_and_exposes_mastery_link_without_export_income(self):
        self.relationship(4); self.prepare()
        for row in list(VillagePersonalCommission.objects.filter(publisher=self.nora.npc)):
            self.complete(row)
        self.nora.refresh_from_db(); self.bag_a.refresh_from_db(); self.visit.refresh_from_db()
        self.assertEqual((self.nora.money, self.bag_a.money, self.visit.exported_amount), (260, 240, 0))
        self.assertEqual(completed_professional_contracts(self.a, since_minute=700).count(), 2)
        self.assertEqual(completed_professional_contracts(self.a, since_minute=701).count(), 0)
        self.assertEqual(professional_contracts_overview(self.a, 700)["completed_today"], 2)
        self.assertEqual(self.nora.slots.filter(item_amount__gt=0).count(), 6)
        from player.starter import record_order_completion
        reward = record_order_completion(self.a, "personal_commission", row.pk)
        facts = reward["facts"]
        self.assertEqual((facts["publisher_npc_id"], facts["completed_world_minute"], facts["professional_contract"]), ("10015", 700, True))
        self.assertEqual(sum(r["quantity"] for r in facts["delivered_batches"]), 6)
        self.assertTrue(all(r["source_instance_id"] and r["quality"] >= 3 for r in facts["delivered_batches"]))
        from village.agent_queries import _social_state
        own = _social_state(self.nora.npc)["player_cooperations"]
        other = _social_state(self.npc)["player_cooperations"]
        self.assertEqual(own[0]["completed_count"], 2)
        self.assertEqual(other, [])

    def test_earlier_held_fresh_supply_is_rechecked_before_final_delivery(self):
        self.prepare(); row = VillageProfessionalContractLink.objects.get().commission
        accept_commission(row.pk, "PLAYER", self.a.pk, uuid.uuid4(), 700)
        conditions = row.requirements["stages"][0]["conditions"]
        first = conditions[0]
        slot = self.slot(2, item_id=first["item_ids"][0], quality=4, expires_world_minute=1100)
        contribute_commission(row.pk, "PLAYER", self.a.pk, first["id"], 2, uuid.uuid4(), 700,
            item_id=slot.item_id, instance_id=slot.instance_id, location="GENERAL_STORE")
        self.clock.world_minute = 850; self.clock.save()
        for index, condition in enumerate(conditions[1:]):
            material = self.slot(2, item_id=condition["item_ids"][0], quality=4, expires_world_minute=2000)
            if index == 0:
                contribute_commission(row.pk, "PLAYER", self.a.pk, condition["id"], 2, uuid.uuid4(), 850,
                    item_id=material.item_id, instance_id=material.instance_id, location="GENERAL_STORE")
            else:
                with self.assertRaises(CommissionError) as caught:
                    contribute_commission(row.pk, "PLAYER", self.a.pk, condition["id"], 2, uuid.uuid4(), 850,
                        item_id=material.item_id, instance_id=material.instance_id, location="GENERAL_STORE")
                self.assertEqual(caught.exception.code, "HELD_BATCH_NO_LONGER_ELIGIBLE")
        row.refresh_from_db(); self.assertEqual(row.status, "ACTIVE")
        self.assertFalse(completed_professional_contracts(self.a).exists())
        self.assertEqual(cooperation_score(self.a, "10015"), 0)

    def test_late_player_recap_uses_permanent_contributions_without_reopening_world_work(self):
        bridge = VillageAffair.objects.create(code="BRIDGE_REPAIR", title="旧桥", status="COMPLETED",
            requirements={}, progress={}, opened_world_minute=0, completed_world_minute=500)
        VillageAffairContribution.objects.create(affair=bridge, request_id=uuid.uuid4(), actor_type="PLAYER",
            actor_id=str(self.a.pk), kind="item", item_id=1002, quantity=5, world_minute=100)
        recap = post_bridge_recap(self.b, 700)
        self.assertTrue(recap["bridge"]["completed"])
        self.assertEqual(recap["first_normal_visit"]["day_index"], 0)
        self.assertEqual(recap["contributions"]["totals"][0]["quantity"], 5)
        self.assertEqual(recap["contributions"]["mine"], [])
        self.assertFalse(recap["old_rewards_claimable"])
        self.assertEqual(post_bridge_recap(self.a, 700)["contributions"]["mine"][0]["quantity"], 5)

    def test_one_active_two_completions_and_real_contract_is_business_mastery_evidence(self):
        from player.mastery import execute_mastery_action, mastery_overview
        from village.professional_contracts import professional_contract_eligibility
        execute_mastery_action(self.a, self.bag_a, "start_mastery", dict(skill_code="business"), uuid.uuid4(), self.clock)
        self.relationship(4); self.prepare()
        rows = [link.commission for link in VillageProfessionalContractLink.objects.select_related("commission")]
        first, second = rows
        accept_commission(first.pk, "PLAYER", self.a.pk, uuid.uuid4(), 700)
        with self.assertRaises(CommissionError) as caught:
            accept_commission(second.pk, "PLAYER", self.a.pk, uuid.uuid4(), 700)
        self.assertEqual(caught.exception.code, "PROFESSIONAL_CONTRACT_ACTIVE")
        self.complete(first)
        view = next(c for c in mastery_overview(self.a, self.bag_a, 700)["challenges"] if c["skill_code"] == "business")
        evidence = next(e for e in view["evidence"] if e["code"] == "professional_contract")
        self.assertEqual((evidence["quantity"], evidence["complete"]), (1, True))
        self.assertFalse(view["can_claim"])
        self.complete(second)
        self.assertEqual(professional_contract_eligibility(second, self.a, 700), "PROFESSIONAL_DAILY_LIMIT")

    def test_equipment_quote_weights_catalog_value_not_number_of_missing_pieces(self):
        from village.professional_catalog import PROFESSIONAL_WOOD
        from village.phase3_catalog import FEATHER, ROUGH_LEATHER
        self.relationship(4)
        for item_id, price in ((PROFESSIONAL_WOOD, 12), (ROUGH_LEATHER, 5), (FEATHER, 2)):
            Item.objects.filter(pk=item_id).update(item_price=price)
        for missing, expected, missing_value in ((PROFESSIONAL_WOOD, 42, 24), (FEATHER, 7, 4)):
            with self.subTest(missing=missing):
                self.nora.slots.all().delete()
                for index, item_id in enumerate((PROFESSIONAL_WOOD, ROUGH_LEATHER, FEATHER)):
                    if item_id != missing:
                        self.nora.slots.create(slot_index=index, item_id=item_id, item_amount=2, quality=3)
                view = professional_contracts_overview(self.a, 700)
                equipment = next(t for t in view["templates"] if t["code"] == "EQUIPMENT_SUPPLY")
                self.assertEqual(sum(r["quantity"] for r in equipment["requirements"]), 2)
                self.assertEqual((equipment["reward_gold"], equipment["full_catalog_value"],
                                  equipment["missing_catalog_value"]), (expected, 38, missing_value))

    def test_missing_deleted_or_nonpositive_catalog_price_cannot_publish(self):
        from village.professional_catalog import PROFESSIONAL_MUSHROOM
        for price in (0, -1):
            Item.objects.filter(pk=PROFESSIONAL_MUSHROOM).update(item_price=price)
            view = professional_contracts_overview(self.a, 700)
            self.assertEqual(view["templates"][0]["reason"], "ITEM_PRICE_UNAVAILABLE")
            with self.assertRaises(ProfessionalContractsError) as caught:
                self.prepare()
            self.assertEqual(caught.exception.code, "ITEM_PRICE_UNAVAILABLE")
        Item.objects.filter(pk=PROFESSIONAL_MUSHROOM).update(item_price=10, is_deleted=True)
        with self.assertRaises(ProfessionalContractsError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, "ITEM_CATALOG_MISSING")
        self.assertFalse(VillageProfessionalContractLink.objects.exists())
        self.nora.refresh_from_db(); self.assertEqual(self.nora.money, 400)

    def test_pre_g7_signed_52_gold_contract_keeps_its_requirements_price_and_budget(self):
        from village.professional_contracts import templates
        self.relationship(4)
        full = templates()[0]
        old = publish_commission(self.nora.npc, dict(title="G6已签鲜供", reward_gold=52,
            valid_for_minutes=200, default_location_code="GENERAL_STORE", stages=[dict(id="stock", conditions=[
                dict(id=f"item_{item_id}", type="item", item_ids=[item_id], target=amount,
                     min_quality=4, requires_freshness=True, min_remaining_minutes=300)
                for item_id, amount in full["items"]])]), uuid.uuid4(), 700)
        row = VillagePersonalCommission.objects.get(pk=old["commission_id"])
        VillageProfessionalContractLink.objects.create(commission=row, visit=self.visit, template_code="FRESH_SUPPLY")
        import copy
        frozen = copy.deepcopy(row.requirements)
        Item.objects.filter(pk=full["items"][0][0]).update(item_price=20)
        self.assertEqual(self.prepare()["spent_gold"], 68)
        row.refresh_from_db()
        self.assertEqual((row.reward_gold, row.requirements), (52, frozen))
        self.assertEqual(professional_contracts_overview(self.a, 700)["published_gold"], 120)
        self.nora.refresh_from_db(); self.assertEqual(self.nora.money, 280)
