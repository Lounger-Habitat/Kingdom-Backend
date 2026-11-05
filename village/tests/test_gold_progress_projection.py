from copy import deepcopy
import uuid

from django.test import TestCase

from village.affairs import _material_usage, complete_labor, create_resource_affair, legacy_definition, project_affair
from village.models import VillageAffair, VillageAffairContribution
from village.tests.test_player_actions import PlayerFixture


class GoldProgressProjectionTests(PlayerFixture, TestCase):
    @staticmethod
    def stage(*conditions):
        return dict(conditions=list(conditions))

    @staticmethod
    def gold(identity, target):
        return dict(id=identity, type="gold", target=target)

    @staticmethod
    def labor(identity, target):
        return dict(id=identity, type="labor", target=target)

    def test_bridge_uses_25_gold_per_global_unit_without_changing_funding_or_money(self):
        requirements = legacy_definition(dict(materials={}, meals=0, gold=500, labor=20))
        row = VillageAffair.objects.create(code="BRIDGE_REPAIR", title="桥施工资金", requirements=requirements,
            progress=dict(conditions=dict(legacy_gold=500, legacy_labor=1)), status="ACTIVE", opened_world_minute=100)
        before_progress = deepcopy(row.progress)
        before_money = self.bag_a.money
        with self.assertNumQueries(0):
            projection = project_affair(row, item_names={})
        condition = projection["stages"][0]["conditions"][0]
        self.assertEqual((condition["submitted"], condition["remaining"], condition["used_quantity"],
                          condition["stock_quantity"]), (500, 0, 25, 475))
        self.assertEqual(projection["gold"], dict(kind="gold", label="金币", target=500, submitted=500,
                         remaining=0, used_quantity=25, stock_quantity=475))
        self.assertEqual(projection["progress"]["gold"], 500)
        row.refresh_from_db()
        self.bag_a.refresh_from_db()
        self.assertEqual(row.progress, before_progress)
        self.assertEqual(self.bag_a.money, before_money)

    def test_integer_rounding_finishes_exactly_and_never_exceeds_supplied_gold(self):
        stages = legacy_definition(dict(materials={}, meals=0, gold=5, labor=3))["stages"]
        progress = dict(legacy_gold=5, legacy_labor=0)
        used = []
        with self.assertNumQueries(0):
            for completed in range(4):
                progress["legacy_labor"] = completed
                used.append(_material_usage("BRIDGE_REPAIR", stages, progress)["legacy_gold"]["used_quantity"])
            progress.update(legacy_gold=2, legacy_labor=99)
            clipped = _material_usage("BRIDGE_REPAIR", stages, progress)["legacy_gold"]
        self.assertEqual(used, [0, 1, 3, 5])
        self.assertEqual(clipped, dict(used_quantity=2, stock_quantity=0))

    def test_repeat_and_saved_progress_restore_the_same_read_only_stock(self):
        row = VillageAffair.objects.create(code="GOLD_RESTORE", title="资金恢复", status="ACTIVE",
            requirements=legacy_definition(dict(materials={}, meals=0, gold=5, labor=3)),
            progress=dict(conditions=dict(legacy_gold=5, legacy_labor=2)), opened_world_minute=100)
        restored = VillageAffair.objects.get(pk=row.pk)
        with self.assertNumQueries(0):
            first = project_affair(row, item_names={})
            repeated = project_affair(row, item_names={})
            recovered = project_affair(restored, item_names={})
        self.assertEqual(first, repeated)
        self.assertEqual(first, recovered)
        self.assertEqual(first["gold"]["stock_quantity"], 2)

    def test_replayed_labor_receipt_does_not_consume_gold_twice(self):
        row = create_resource_affair("GOLD_RECEIPT", "履约资金", dict(stages=[dict(id="work",
            conditions=[self.gold("fund", 5), self.labor("work", 3)])]), [], self.clock.world_minute)
        row.progress = dict(conditions=dict(fund=5, work=0))
        row.save(update_fields=["progress"])
        receipt = uuid.uuid4()
        first = complete_labor(row, condition_id="work", request_id=receipt,
            world_minute=self.clock.world_minute, player_id=str(self.a.pk))
        repeated = complete_labor(row, condition_id="work", request_id=receipt,
            world_minute=self.clock.world_minute, player_id=str(self.a.pk))
        restored = VillageAffair.objects.get(pk=row.pk)
        with self.assertNumQueries(0):
            recovered = project_affair(restored, item_names={})
        self.assertEqual(first["gold"], repeated["gold"])
        self.assertEqual(repeated["gold"], recovered["gold"])
        self.assertEqual(recovered["gold"]["used_quantity"], 1)
        self.assertEqual(recovered["gold"]["stock_quantity"], 4)
        self.assertEqual(VillageAffairContribution.objects.filter(affair=row).count(), 1)

    def test_career_and_farm_gold_follow_their_associated_labor(self):
        career = [self.stage(self.gold("gold", 80)), self.stage(self.labor("labor", 3))]
        farm = [self.stage(self.labor("unrelated", 9)),
                self.stage(self.gold("water_gold", 20), self.labor("water_labor", 2))]
        with self.assertNumQueries(0):
            career_usage = _material_usage("CAREER_JACK_1_PREP", career, dict(gold=80, labor=1))
            farm_usage = _material_usage("FARM_EXPANSION_E_PREP", farm,
                dict(unrelated=9, water_gold=20, water_labor=1))
        self.assertEqual(career_usage["gold"], dict(used_quantity=26, stock_quantity=54))
        self.assertEqual(farm_usage["water_gold"], dict(used_quantity=10, stock_quantity=10))

    def test_canal_funding_ignores_finished_clearing_and_waits_for_the_gate(self):
        stages = [self.stage(*(self.labor("clear_" + str(index), 1) for index in range(4))),
                  self.stage(self.gold("canal_gold", 100)), self.stage(self.labor("water_labor", 1))]
        progress = {"clear_" + str(index): 1 for index in range(4)}
        progress.update(canal_gold=100, water_labor=0)
        with self.assertNumQueries(0):
            before_gate = _material_usage("FARM_IRRIGATION", stages, progress)["canal_gold"]
            progress["water_labor"] = 1
            after_gate = _material_usage("FARM_IRRIGATION", stages, progress)["canal_gold"]
        self.assertEqual(before_gate, dict(used_quantity=0, stock_quantity=100))
        self.assertEqual(after_gate, dict(used_quantity=100, stock_quantity=0))

    def test_custom_gold_prefers_same_stage_then_later_work_then_all_work(self):
        stages = [self.stage(self.labor("early", 4)), self.stage(self.gold("fund", 10)),
                  self.stage(self.gold("local", 6), self.labor("later", 2)), self.stage(self.gold("last", 8))]
        progress = dict(early=4, fund=10, local=6, later=0, last=8)
        with self.assertNumQueries(0):
            usage = _material_usage("CUSTOM", stages, progress)
        self.assertEqual(usage["fund"], dict(used_quantity=0, stock_quantity=10))
        self.assertEqual(usage["local"], dict(used_quantity=0, stock_quantity=6))
        self.assertEqual(usage["last"], dict(used_quantity=5, stock_quantity=3))

    def test_no_labor_keeps_funding_and_custom_item_associations_stay_unchanged(self):
        item = dict(id="decoration", type="item", item_ids=[1002], target=7)
        stages = [self.stage(self.gold("fund", 10), item)]
        with self.assertNumQueries(0):
            no_work = _material_usage("CUSTOM", stages, dict(fund=9, decoration=7))
            stages.append(self.stage(self.labor("work", 1)))
            worked = _material_usage("CUSTOM", stages, dict(fund=9, decoration=7, work=1))
        self.assertEqual(no_work["fund"], dict(used_quantity=0, stock_quantity=9))
        self.assertEqual(worked["fund"], dict(used_quantity=9, stock_quantity=0))
        self.assertEqual(worked["decoration"], dict(used_quantity=0, stock_quantity=7))

    def test_gold_summary_aggregates_historical_and_active_conditions_without_changing_remaining(self):
        row = VillageAffair(code="GOLD_SUM", title="分阶段资金", status="ACTIVE", opened_world_minute=100,
            requirements=dict(stages=[dict(id="first", conditions=[self.gold("first_gold", 10), self.labor("first_work", 2)]),
                                      dict(id="second", conditions=[self.gold("second_gold", 5), self.labor("second_work", 3)])]),
            progress=dict(conditions=dict(first_gold=10, first_work=2, second_gold=4, second_work=1)))
        with self.assertNumQueries(0):
            projection = project_affair(row, item_names={})
        historical = projection["stages"][0]["conditions"][0]
        self.assertEqual(historical["stock_quantity"], 0)
        self.assertEqual(historical["submitted"], 10)
        self.assertEqual(projection["gold"], dict(kind="gold", label="金币", target=15, submitted=14,
                         remaining=1, used_quantity=11, stock_quantity=3))
