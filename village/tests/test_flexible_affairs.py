import importlib
import time
import uuid
from types import SimpleNamespace
from django.apps import apps
from django.db import connection
from django.test import TestCase
from items.models import Item
from village.affairs import (AffairError, contribute, complete_labor, create_resource_affair, project_affair,
                            resolve_condition, normalize_definition)
from village.models import VillageAffair, VillageAffairContribution, VillageWorldEvent, VillageLocationDefinition
from village.phase2_services import TickService
from village.runtime_services import ActionService, CommandService
from village.tests import test_village_affairs as bridge_tests


class FlexibleAffairTests(TestCase):
    def setUp(self):
        bridge_tests.VillageAffairTests.setUp(self)
        VillageLocationDefinition.objects.update_or_create(location_code="REPAIR_YARD", defaults={
            "display_name": "施工场", "center_anchor_code": "test_yard", "location_type": "PUBLIC", "is_active": True})
    slot = bridge_tests.VillageAffairTests.slot
    def action(self, code, **params):
        from village.models import NpcProfile
        NpcProfile.objects.filter(npc=self.npc).update(capabilities=["CONTRIBUTE_TO_AFFAIR"])
        kind = "gold" if params.get("gold_amount") else "item"
        args = {"affair_id": params["affair_id"], "condition_id":params["condition_id"], "kind":kind,
            "amount":params.get("gold_amount", params.get("amount"))}
        if kind == "item":
            args["item_id"] = params["item_id"]
            if "food_instance_id" in params:
                args["food_instance_id"] = params["food_instance_id"]
        ok, result, _ = CommandService.accept_command(str(uuid.uuid4()), self.npc.agent_id, "env", {
            "schema_version":"village.command.v1", "id":str(uuid.uuid4()), "request_id":str(uuid.uuid4()),
            "name":"CONTRIBUTE_TO_AFFAIR", "params":args, "queue_mode":"STRICT", "ttl_ms":120000}, int(time.time()*1000))
        return {"result_code":result}

    def create(self, conditions=None, *, stages=None, locations=None, code="TEST_FLEX"):
        for item_id in (1002, 1030):
            Item.objects.update_or_create(pk=item_id, defaults={"item_name": f"物品{item_id}", "item_price": 5})
        requirements = {"stages": stages or [{"id": "s1", "title": "完成条件", "conditions": conditions}]}
        return create_resource_affair(code, "灵活事务", requirements, locations or [], 0)

    def add(self, row, kind, quantity, condition_id=None, item_id=None, request_id=None):
        return contribute(row, kind, quantity, condition_id=condition_id, item_id=item_id,
            request_id=request_id or uuid.uuid4(), world_minute=0, actor_npc=self.npc)

    def test_item_only_accepts_alternatives_and_finishes_without_other_types(self):
        row = self.create([dict(id="items", type="item", item_ids=[1002, 1030], target=3)])
        self.assertEqual(project_affair(row)["conditions"][0]["item_names"], ["物品1002", "物品1030"])
        self.add(row, "item", 1, item_id=1002)
        self.add(row, "item", 2, item_id=1030)
        self.assertEqual((row.status, row.progress["conditions"]), ("COMPLETED", {"items": 3}))
        self.assertFalse(project_affair(row)["labor_available"])
        self.assertEqual(VillageWorldEvent.objects.filter(event_type="VILLAGE_AFFAIR_COMPLETED").count(), 1)

    def test_all_conditions_required_before_each_of_three_stages_opens(self):
        row = self.create(stages=[
            dict(id="prepare", title="交付", conditions=[dict(id="wood", type="item", item_ids=[1002], target=1), dict(id="fund", type="gold", target=5)]),
            dict(id="work", title="施工", conditions=[dict(id="labor", type="labor", target=1, unit_minutes=15)]),
            dict(id="finish", title="验收材料", conditions=[dict(id="corn", type="item", item_ids=[1030], target=1)])])
        with self.assertRaises(AffairError) as error:
            self.add(row, "item", 1, "corn", 1030)
        self.assertEqual(error.exception.code, "AFFAIR_CONDITION_LOCKED")
        self.add(row, "item", 1, "wood", 1002)
        with self.assertRaises(AffairError):
            complete_labor(row, condition_id="labor", world_minute=0, request_id=uuid.uuid4(), actor_npc=self.npc)
        self.add(row, "gold", 5, "fund")
        self.assertEqual(project_affair(row)["active_stage_id"], "work")
        complete_labor(row, condition_id="labor", world_minute=15, request_id=uuid.uuid4(), actor_npc=self.npc)
        self.assertEqual(project_affair(row)["active_stage_id"], "finish")
        self.add(row, "item", 1, "corn", 1030)
        self.assertEqual(row.status, "COMPLETED")
        self.assertEqual(VillageWorldEvent.objects.filter(dedupe_key__startswith="affair:TEST_FLEX:").count(), 3)

    def test_same_stage_labor_does_not_invent_preparation_requirement(self):
        row = self.create([dict(id="wood", type="item", item_ids=[1002], target=1), dict(id="work", type="labor", target=1, unit_minutes=10)])
        self.assertTrue(project_affair(row)["labor_available"])
        complete_labor(row, condition_id="work", world_minute=10, request_id=uuid.uuid4(), actor_npc=self.npc)
        self.assertEqual(row.status, "ACTIVE")
        self.add(row, "item", 1, "wood", 1002)
        self.assertEqual(row.status, "COMPLETED")

    def test_ambiguous_conditions_require_id_and_replay_cannot_credit_next_stage(self):
        row = self.create(stages=[
            dict(id="first", conditions=[dict(id="first_item", type="item", item_ids=[1002], target=1)]),
            dict(id="second", conditions=[dict(id="second_item", type="item", item_ids=[1002], target=2)])])
        rid = uuid.uuid4()
        self.add(row, "material", 1, item_id=1002, request_id=rid)
        self.add(row, "item", 1, item_id=1002, request_id=rid)
        self.assertEqual(row.progress["conditions"], {"first_item": 1})
        self.assertEqual(VillageAffairContribution.objects.filter(affair=row).count(), 1)
        other = self.create([dict(id="a", type="item", item_ids=[1002], target=1), dict(id="b", type="item", item_ids=[1002], target=1)], code="AMBIGUOUS")
        with self.assertRaises(AffairError) as error:
            resolve_condition(other, kind="item", item_id=1002)
        self.assertEqual(error.exception.code, "AFFAIR_CONDITION_REQUIRED")
        self.add(other, "item", 1, "a", 1002)
        self.assertEqual(other.progress["conditions"], {"a": 1})

    def test_stage_location_override_and_default_inheritance(self):
        row = self.create(locations=["VILLAGE_SQUARE"], stages=[
            dict(id="first", conditions=[dict(id="gold", type="gold", target=1, locations=["IGNORED"])]),
            dict(id="second", location_code="REPAIR_YARD", conditions=[dict(id="work", type="labor", target=1)]),
            dict(id="third", conditions=[dict(id="done", type="gold", target=1)])])
        data = project_affair(row)
        self.assertEqual([s["locations"] for s in data["stages"]], [["VILLAGE_SQUARE"], ["REPAIR_YARD"], ["VILLAGE_SQUARE"]])
        self.assertEqual(data["stages"][2]["status"], "LOCKED")
        result = self.action("DONATE_REPAIR_RESOURCES", affair_id=row.pk, condition_id="gold", gold_amount=1)
        self.assertEqual(result["result_code"], "AFFAIR_LOCATION_INVALID")
        self.runtime.current_location_code = "VILLAGE_SQUARE"; self.runtime.save()
        self.assertEqual(self.action("DONATE_REPAIR_RESOURCES", affair_id=row.pk, condition_id="gold", gold_amount=1)["result_code"], "OK")
        row.refresh_from_db()
        self.assertEqual(resolve_condition(row, "work")["locations"], ["REPAIR_YARD"])

    def test_npc_general_item_contribution_obeys_quality_freshness_and_selected_batch(self):
        row = self.create([dict(id="food", type="item", item_ids=[1030], target=2, min_quality=4, requires_freshness=True, min_remaining_minutes=10)])
        stale = self.slot(1030, 2, quality=5, expires_world_minute=5)
        low = self.slot(1030, 2, quality=3, expires_world_minute=100)
        good = self.slot(1030, 2, quality=4, expires_world_minute=10)
        def donate(instance):
            return self.action("DONATE_REPAIR_RESOURCES", affair_id=row.pk, condition_id="food", item_id=1030, amount=2, food_instance_id=str(instance.instance_id))
        self.assertNotEqual(donate(stale)["result_code"], "OK")
        self.assertNotEqual(donate(low)["result_code"], "OK")
        self.assertEqual(donate(good)["result_code"], "OK")
        good.refresh_from_db(); stale.refresh_from_db(); low.refresh_from_db(); row.refresh_from_db()
        self.assertEqual((good.item_amount, stale.item_amount, low.item_amount, row.status), (0, 2, 2, "COMPLETED"))

    def test_unrestricted_npc_labor_uses_configured_real_duration(self):
        from village.models import NpcProfile
        NpcProfile.objects.filter(npc=self.npc).update(capabilities=["CONTRIBUTE_TO_AFFAIR"])
        row = self.create([dict(id="short_work", type="labor", target=1, unit_minutes=15)])
        self.runtime.current_location_code = "VILLAGE_SQUARE"; self.runtime.save()
        ok, code, command = CommandService.accept_command(envelope_id=str(uuid.uuid4()), sender=self.npc.agent_id,
            recipient="WuxiVillage", timestamp_ms=int(time.time() * 1000), content={
                "schema_version": "village.command.v1", "id": str(uuid.uuid4()), "request_id": str(uuid.uuid4()),
                "name": "CONTRIBUTE_TO_AFFAIR", "params": {"kind": "labor", "affair_id": row.pk, "condition_id": "short_work"},
                "queue_mode": "STRICT", "not_before_world_minute": 0, "expires_world_minute": 1000,
                "ttl_ms": 120000, "duration_minutes": 1})
        self.assertTrue(ok, code)
        self.assertEqual(command.duration_minutes, 15)
        self.assertTrue(CommandService.start_command(command.command_id)[0])
        TickService.advance({"request_id": str(uuid.uuid4()), "world_minute": 14, "time_revision": 15})
        self.assertFalse(ActionService.complete_action(command.command_id, command.request_id, True, "OK")[0])
        TickService.advance({"request_id": str(uuid.uuid4()), "world_minute": 15, "time_revision": 16})
        result = ActionService.complete_action(command.command_id, command.request_id, True, "OK")
        self.assertTrue(result[0], result)
        self.assertTrue(result[2].success, result[2].response_data)
        row.refresh_from_db(); self.assertEqual(row.status, "COMPLETED")

    def test_migration_keeps_completed_and_partial_progress_and_receipts(self):
        row = VillageAffair.objects.create(code="OLD", title="旧事务", status="READY", opened_world_minute=0,
            requirements={"materials": {"1002": 3}, "gold": 5, "labor": 2}, progress={"materials": {"1002": 3}, "gold": 5, "labor": 1}, locations=["VILLAGE_SQUARE"])
        receipt = VillageAffairContribution.objects.create(affair=row, request_id=uuid.uuid4(), actor_type="NPC", actor_id=self.npc.agent_id, kind="material", item_id=1002, quantity=3, world_minute=0)
        completed = VillageAffair.objects.create(code="OLD_DONE", title="旧已完成", status="COMPLETED", opened_world_minute=0, completed_world_minute=5, requirements={"gold": 10}, progress={"gold": 8})
        module = importlib.import_module("village.migrations.0049_flexible_affair_conditions")
        module.migrate_conditions(apps, SimpleNamespace(connection=connection))
        row.refresh_from_db(); receipt.refresh_from_db(); completed.refresh_from_db()
        self.assertEqual(row.progress["conditions"], {"legacy_material_1002": 3, "legacy_gold": 5, "legacy_labor": 1})
        self.assertEqual(project_affair(row)["active_stage_id"], "legacy_work")
        self.assertEqual(receipt.condition_id, "legacy_material_1002")
        self.assertEqual((completed.status, completed.completed_world_minute), ("COMPLETED", 5))

    def test_empty_duplicate_and_unknown_location_configuration_rejected(self):
        for req in [{"stages": []}, {"stages": [{"id": "s", "conditions": []}]},
            {"stages": [{"id": "s", "conditions": [dict(id="x", type="gold", target=True)]}]},
            {"stages": [{"id": "s", "conditions": [dict(id="x", type="gold", target=1), dict(id="x", type="gold", target=1)]}]},
            {"stages": [{"id": "s", "location_code": "UNKNOWN", "conditions": [dict(id="x", type="gold", target=1)]}]}]:
            with self.subTest(req=req), self.assertRaises(AffairError):
                normalize_definition(req)
