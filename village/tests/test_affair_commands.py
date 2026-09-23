import importlib
import time
import uuid
from io import StringIO
from types import SimpleNamespace

from django.apps import apps
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate
from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot
from items.models import Item
from village.agent_queries import execute_agent_query
from village.affairs import create_resource_affair
from village.command_catalog import COMMAND_CATALOG
from village.models import (NpcProfile, NpcRuntimeState, VillageSimulationConfig, VillageLocationDefinition,
    VillageAffair, VillageAffairContribution, VillageStoryArc, VillageStoryMeeting, VillageActionReceipt,
    EconomicLedgerEntry, VillageAccount, VillagePublicDecision, VillageRouteAttempt, MarkDailySalary)
from village.runtime_services import CommandService, ActionService


@override_settings(VILLAGE_FAST_COMMAND_INTEGRATION=False)
class AffairCommandTests(TestCase):
    def setUp(self):
        VillageSimulationConfig.objects.update_or_create(config_key="default", defaults={
            "village_story_enabled": False, "village_needs_enabled": True, "village_economy_enabled": True})
        self.clock = GlobalGameTimeState.get_instance()
        self.clock.world_minute = self.clock.time_revision = 600
        self.clock.save()
        self.npc, _ = NpcDefinition.objects.get_or_create(agent_id="10011", defaults={"name": "Bill"})
        self.bag, _ = NpcGlobalInventory.objects.update_or_create(npc=self.npc, defaults={"money": 1000})
        self.bag.slots.all().delete()
        self.runtime, _ = NpcRuntimeState.objects.update_or_create(npc=self.npc, defaults={
            "current_location_code": "VILLAGE_SQUARE", "action_status": "IDLE", "needs_world_minute": 600,
            "health_fp": 1000, "energy_fp": 1000, "hunger_fp": 0})
        NpcProfile.objects.update_or_create(npc=self.npc, defaults={"display_name": "Bill", "is_active": True,
            "capabilities": ["INSPECT_AFFAIRS", "CONTRIBUTE_TO_AFFAIR"]})
        VillageLocationDefinition.objects.update_or_create(location_code="VILLAGE_SQUARE", defaults={
            "display_name": "广场", "center_anchor_code": "VILLAGE_SQUARE", "is_active": True})
        Item.objects.get_or_create(item_id=1002, defaults={"item_name": "木材", "item_price": 2})

    def project(self, code="PROJECT_A", conditions=None):
        return create_resource_affair(code=code, title=code, world_minute=600, locations=["VILLAGE_SQUARE"],
            requirements={"stages": [{"id": "stage1", "conditions": conditions or [
                {"id": "gold", "type": "gold", "target": 10}]}]})

    def accept(self, name="CONTRIBUTE_TO_AFFAIR", params=None, actor="10011", content=None, envelope=None):
        rt = NpcRuntimeState.objects.get(npc__agent_id=actor)
        content = content or {"schema_version": "village.command.v1", "id": str(uuid.uuid4()),
            "request_id": str(uuid.uuid4()), "name": name, "params": params or {}, "queue_mode": "STRICT",
            "expected_state_version": rt.state_version, "ttl_ms": 30000}
        return CommandService.accept_command(envelope or str(uuid.uuid4()), actor, "env", content, int(time.time()*1000))

    def contribute(self, affair="PROJECT_A", condition="gold", kind="gold", **extra):
        return self.accept(params={"affair_id": affair, "condition_id": condition, "kind": kind, **extra})

    def test_read_only_query_works_without_story_and_does_not_create_default_bridge(self):
        self.project()
        def read_only(execute, sql, params, many, context):
            self.assertFalse(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")), sql)
            return execute(sql, params, many, context)
        with connection.execute_wrapper(read_only):
            ok, code, data = execute_agent_query("10011", "INSPECT_AFFAIRS")
        self.assertTrue(ok, code)
        self.assertEqual(data["affairs"][0]["available_actions"], ["CONTRIBUTE_TO_AFFAIR"])
        self.assertFalse(VillageStoryArc.objects.exists())
        self.assertFalse(VillageAffair.objects.filter(pk="BRIDGE_REPAIR").exists())
        self.assertEqual(execute_agent_query("10011", "INSPECT_AFFAIRS", {"affair_id":"missing"})[1], "AFFAIR_NOT_FOUND")
        self.assertEqual(execute_agent_query("10011", "INSPECT_AFFAIRS", {"story_id":"x"})[1], "INVALID_QUERY_PARAMS")

    def test_wrong_affair_reference_returns_http_diagnostic_without_writes(self):
        from village.views import npc_agent_query
        self.project()

        def read_only(execute, sql, params, many, context):
            self.assertFalse(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")), sql)
            return execute(sql, params, many, context)

        for wrong in ("VILLAGE_SQUARE", "gold", "unknown-project"):
            with self.subTest(reference=wrong):
                request = APIRequestFactory().get("/", {"affair_id": wrong})
                force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
                with connection.execute_wrapper(read_only):
                    response = npc_agent_query(request, "10011", "INSPECT_AFFAIRS")
                self.assertEqual(response.status_code, 409)
                self.assertFalse(response.data["success"])
                self.assertEqual(response.data["resultCode"], "AFFAIR_NOT_FOUND")
                error = response.data["data"]
                self.assertEqual(error["invalid_field"], "affair_id")
                self.assertEqual(error["received_value"], wrong)
                self.assertFalse(error["tool_error"]["executed"])
                self.assertFalse(error["tool_error"]["automatic_retry_safe"])
                self.assertNotIn("allowed_values", error)

    def test_reference_discovery_reads_real_projects_without_executing_contributions(self):
        self.project("CUSTOM_PROJECT")
        ok, code, error = execute_agent_query("10011", "INSPECT_AFFAIRS", {"affair_id": "VILLAGE_SQUARE"})
        self.assertFalse(ok)
        self.assertEqual(code, "AFFAIR_NOT_FOUND")
        discover = error["discover"]
        ok, code, listing = execute_agent_query("10011", discover["tool"], discover["arguments"])
        self.assertTrue(ok, code)
        selected = next(row for row in listing["affairs"] if row["affair_id"] == "CUSTOM_PROJECT")
        ok, code, detail = execute_agent_query("10011", "INSPECT_AFFAIRS", {"affair_id": selected["affair_id"]})
        self.assertTrue(ok, code)
        self.assertEqual(len(detail["affairs"]), 1)
        self.assertEqual(detail["affairs"][0]["affair_id"], "CUSTOM_PROJECT")
        self.assertFalse(VillageAffairContribution.objects.exists())
        self.bag.refresh_from_db()
        self.assertEqual(self.bag.money, 1000)

    def test_reference_diagnostic_does_not_bypass_query_capability(self):
        NpcProfile.objects.filter(npc=self.npc).update(capabilities=[])
        ok, code, data = execute_agent_query("10011", "INSPECT_AFFAIRS", {"affair_id": "missing"})
        self.assertFalse(ok)
        self.assertEqual(code, "ACTION_NOT_ALLOWED")
        self.assertIsNone(data)

    def test_gold_settlement_and_replay_are_atomic_and_independent(self):
        a, b = self.project(), self.project("PROJECT_B")
        content = {"schema_version":"village.command.v1", "id":str(uuid.uuid4()), "request_id":str(uuid.uuid4()),
            "name":"CONTRIBUTE_TO_AFFAIR", "params":{"affair_id":a.pk,"condition_id":"gold","kind":"gold","amount":10},
            "queue_mode":"STRICT", "expected_state_version":self.runtime.state_version, "ttl_ms":30000}
        envelope = str(uuid.uuid4())
        ok, code, command = self.accept(content=content, envelope=envelope)
        self.assertTrue(ok, code)
        self.assertEqual(command.status, "SUCCEEDED")
        self.accept(content=content, envelope=envelope)
        self.bag.refresh_from_db(); a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual(self.bag.money, 990)
        self.assertEqual(a.status, "COMPLETED")
        self.assertNotEqual(b.status, "COMPLETED")
        self.assertEqual(VillageAffairContribution.objects.filter(affair=a).count(), 1)
        entry = EconomicLedgerEntry.objects.get(request_id=command.request_id)
        self.assertEqual(entry.changes["10011"]["money"], -10)
        self.assertEqual(entry.changes["AFFAIR:"+a.pk]["money"], 10)
        self.assertTrue(self.contribute(affair=b.pk, amount=10)[0])
        self.assertFalse(VillageStoryArc.objects.exists())

    def test_reject_missing_id_extra_admin_fields_wrong_place_and_overfill(self):
        self.project()
        for params in ({"kind":"gold","amount":1}, {"affair_id":"PROJECT_A","condition_id":"gold","kind":"gold","amount":1,"decision_id":"fake"},
                       {"affair_id":"PROJECT_A","condition_id":"gold","kind":"gold","amount":True}):
            self.assertEqual(self.accept(params=params)[1], "INVALID_COMMAND_SCHEMA")
        self.assertEqual(self.contribute(amount=11)[1], "PROJECT_NEED_FILLED")
        self.assertEqual(self.contribute(affair="missing", amount=1)[1], "AFFAIR_NOT_FOUND")
        NpcRuntimeState.objects.filter(pk=self.runtime.pk).update(current_location_code="BILL_HOME")
        self.assertEqual(self.contribute(amount=1)[1], "AFFAIR_LOCATION_INVALID")
        self.bag.refresh_from_db()
        self.assertEqual(self.bag.money, 1000)
        self.assertEqual(VillageAffairContribution.objects.count(), 0)

    def test_item_quality_freshness_and_insufficient_resources_do_not_mutate(self):
        self.project(conditions=[{"id":"wood", "type":"item", "target":3, "item_ids":[1002], "min_quality":2,
            "requires_freshness":True}])
        slot = NpcGlobalInventorySlot.objects.create(bag=self.bag, slot_index=0, item_id=1002, item_amount=3,
            quality=1, expires_world_minute=900)
        self.assertEqual(self.contribute(condition="wood", kind="item", item_id=1002, amount=3)[1], "AFFAIR_RESOURCE_INSUFFICIENT")
        slot.refresh_from_db(); self.assertEqual(slot.item_amount, 3)
        slot.quality=3; slot.save()
        self.assertTrue(self.contribute(condition="wood", kind="item", item_id=1002, amount=3)[0])
        slot.refresh_from_db(); self.assertEqual(slot.item_amount, 0)

    def test_real_labor_duration_and_repeat_completion(self):
        row=self.project(conditions=[{"id":"work","type":"labor","target":1,"unit_minutes":3}])
        ok, code, command=self.contribute(condition="work",kind="labor")
        self.assertTrue(ok, code)
        self.assertEqual(command.duration_minutes,3)
        self.assertTrue(CommandService.start_command(command.command_id)[0])
        self.assertEqual(ActionService.complete_action(command.command_id,command.request_id,True,"OK")[1],"AFFAIR_LABOR_NOT_FINISHED")
        self.clock.world_minute=603; self.clock.time_revision=603; self.clock.save()
        ok, code, receipt=ActionService.complete_action(command.command_id,command.request_id,True,"OK")
        self.assertTrue(ok,code); self.assertTrue(receipt.success,receipt.response_data)
        self.assertEqual(receipt.response_data["amount"],1)
        ActionService.complete_action(command.command_id,command.request_id,True,"OK")
        self.assertEqual(VillageAffairContribution.objects.filter(affair=row).count(),1)

    def test_bridge_npc_uses_yard_for_supplies_and_bridge_for_labor(self):
        from village.affairs import ensure_bridge_affair
        row = ensure_bridge_affair(600)
        self.runtime.current_location_code = "OLD_BRIDGE"
        self.runtime.save(update_fields=["current_location_code"])
        self.assertEqual(self.contribute(affair=row.pk, condition="legacy_gold", amount=1)[1], "AFFAIR_LOCATION_INVALID")
        self.runtime.current_location_code = "REPAIR_YARD"
        self.runtime.save(update_fields=["current_location_code"])
        self.assertTrue(self.contribute(affair=row.pk, condition="legacy_gold", amount=1)[0])
        row.refresh_from_db()
        row.progress["conditions"].update(legacy_material_1002=100, legacy_meals=20, legacy_gold=500)
        row.save(update_fields=["progress"])
        ok, code, misplaced = self.contribute(affair=row.pk, condition="legacy_labor", kind="labor")
        self.assertTrue(ok, code)
        self.assertEqual(CommandService.start_command(misplaced.command_id)[1], "AFFAIR_LOCATION_INVALID")
        self.runtime.current_location_code = "OLD_BRIDGE"
        self.runtime.save(update_fields=["current_location_code"])
        ok, code, command = self.contribute(affair=row.pk, condition="legacy_labor", kind="labor")
        self.assertTrue(ok, code)
        self.assertEqual(command.duration_minutes, 60)
        self.assertTrue(CommandService.start_command(command.command_id)[0])
        self.clock.world_minute = 660
        self.clock.time_revision = 660
        self.clock.save()
        ok, code, receipt = ActionService.complete_action(command.command_id, command.request_id, True, "OK")
        self.assertTrue(ok, code)
        self.assertTrue(receipt.success, receipt.response_data)
        row.refresh_from_db()
        self.assertEqual(row.progress["labor"], 1)

    def test_interrupted_labor_and_health_gate(self):
        row=self.project(conditions=[{"id":"work","type":"labor","target":1,"unit_minutes":3}])
        ok, code, command=self.contribute(condition="work",kind="labor")
        self.assertTrue(CommandService.start_command(command.command_id)[0])
        self.clock.world_minute=601; self.clock.save()
        ActionService.complete_action(command.command_id,command.request_id,False,"CANCELLED")
        self.assertFalse(VillageAffairContribution.objects.filter(affair=row).exists())
        NpcRuntimeState.objects.filter(pk=self.runtime.pk).update(health_fp=300)
        self.assertEqual(self.contribute(condition="work",kind="labor")[1],"HEALTH_TOO_LOW")

    def test_retired_tools_rejected_and_migration_idempotent(self):
        retired={"STORY_ACT","STORY_VOTE","WORK_PLACEHOLDER","SOCIALIZE","CREATE_ORDER","CANCEL_ORDER","START_DELIVERY",
            "INSPECT_STORY_STATE","INSPECT_RELEVANT_STORIES","INSPECT_STORY_DETAIL","INSPECT_ORDERS"}
        self.assertFalse(retired & set(COMMAND_CATALOG))
        for name in retired-set(CommandService.READ_QUERY_ACTIONS):
            self.assertEqual(self.accept(name=name)[1],"UNKNOWN_ACTION")
        profile=NpcProfile.objects.get(npc=self.npc)
        profile.capabilities=list(retired)+["GET_RULE","MOVE"]
        profile.save()
        migrate=importlib.import_module("village.migrations.0057_independent_affair_commands").update_capabilities
        migrate(apps,SimpleNamespace(connection=connection)); migrate(apps,SimpleNamespace(connection=connection))
        profile.refresh_from_db()
        self.assertEqual(set(profile.capabilities),{"GET_RULE","MOVE","INSPECT_AFFAIRS","CONTRIBUTE_TO_AFFAIR"})
        self.assertEqual(len(profile.capabilities),4)

    def test_commitment_commands_are_removed_from_tools_and_all_public_write_paths(self):
        from village.phase4_social import complete_social_action
        from village.seed_data import NPC_SEEDS
        from village.command_catalog import RECORDED_ACTIVITY_ACTIONS
        retired = {"CREATE_COMMITMENT", "CANCEL_COMMITMENT", "PERFORM_COMMITMENT"}
        NpcProfile.objects.filter(npc=self.npc).update(capabilities=list(retired) + ["INSPECT_SOCIAL_STATE"])
        self.assertFalse(retired & set(COMMAND_CATALOG))
        self.assertTrue(retired <= RECORDED_ACTIVITY_ACTIONS)
        for seed in NPC_SEEDS:
            self.assertFalse(retired & set(seed["capabilities"]))
        before = VillageActionReceipt.objects.count()
        for name in retired:
            with self.subTest(action=name):
                self.assertEqual(self.accept(name=name)[1], "UNKNOWN_ACTION")
                self.assertEqual(complete_social_action({"action":name})["result_code"], "ACTION_RETIRED")
        self.assertEqual(VillageActionReceipt.objects.count(), before)
        self.bag.refresh_from_db()
        self.assertEqual(self.bag.money, 1000)
        self.assertTrue(execute_agent_query("10011", "INSPECT_SOCIAL_STATE")[0])

    def test_commitment_capability_migration_preserves_other_tools_and_receipts(self):
        profile=NpcProfile.objects.get(npc=self.npc)
        profile.capabilities=["GET_RULE", " create_commitment ", "CANCEL_COMMITMENT", "PERFORM_COMMITMENT", "INSPECT_SOCIAL_STATE"]
        profile.save()
        receipt=VillageActionReceipt.objects.create(request_id=uuid.uuid4(), npc=self.npc,
            action_kind="CREATE_COMMITMENT", success=True, result_code="OK", world_minute=500, time_revision=500)
        migrate=importlib.import_module("village.migrations.0058_retire_commitment_commands").update_capabilities
        migrate(apps,SimpleNamespace(connection=connection))
        migrate(apps,SimpleNamespace(connection=connection))
        profile.refresh_from_db(); receipt.refresh_from_db()
        self.assertEqual(profile.capabilities,["GET_RULE", "INSPECT_SOCIAL_STATE"])
        self.assertEqual(receipt.action_kind,"CREATE_COMMITMENT")

    def test_day_close_and_snapshot_never_bootstrap_or_finish_global_story(self):
        from village.phase5_story import process_day_close, get_story_snapshot, dispatch_story_command
        self.project()
        self.assertEqual(process_day_close(0,600)["result_code"],"OK")
        self.assertEqual(get_story_snapshot()["data"]["phase"],"AFFAIRS")
        self.assertFalse(VillageStoryArc.objects.exists())
        self.assertFalse(VillageStoryMeeting.objects.exists())
        self.assertEqual(dispatch_story_command({"command":"STORY_ACT"})["result_code"],"ACTION_RETIRED")

    def test_caravan_opens_without_story_or_meeting_and_charges_once(self):
        npc,_=NpcDefinition.objects.get_or_create(agent_id="10015",defaults={"name":"Nora"})
        bag,_=NpcGlobalInventory.objects.update_or_create(npc=npc,defaults={"money":1000})
        NpcRuntimeState.objects.update_or_create(npc=npc,defaults={"current_location_code":"GENERAL_STORE",
            "health_fp":1000,"energy_fp":1000,"hunger_fp":0,"needs_world_minute":600})
        NpcProfile.objects.update_or_create(npc=npc,defaults={"capabilities":["CONTACT_EMERGENCY_CARAVAN"]})
        for _ in range(2):
            ok,code,command=self.accept(name="CONTACT_EMERGENCY_CARAVAN",params={"route_attempt_id":str(uuid.uuid4())},actor="10015")
            self.assertTrue(ok,code)
        cost=VillageSimulationConfig.objects.get(config_key="default").caravan_contact_cost
        bag.refresh_from_db(); self.assertEqual(bag.money,1000-cost)
        self.assertEqual(VillageRouteAttempt.objects.filter(arc__isnull=True).count(),2)
        from village.caravan import route_readiness
        self.assertTrue(route_readiness()["route_open"])
        self.assertFalse(VillageStoryArc.objects.exists())

    def test_public_meal_authorization_command_is_removed(self):
        from django.core.management import get_commands
        self.assertNotIn("configure_public_meals", get_commands())
        self.assertFalse(VillagePublicDecision.objects.exists())

    def test_mark_role_migration_preserves_life_tools_and_history(self):
        mark, _ = NpcDefinition.objects.get_or_create(agent_id="10013", defaults={"name": "Mark"})
        profile, _ = NpcProfile.objects.update_or_create(npc=mark, defaults={
            "occupation": "clerk_reviewer", "personal_goal": "公开筹集修桥资金并维持村庄秩序",
            "capabilities": ["GET_RULE", "EAT", " publish_price_index ", "AUDIT_PUBLIC_FUND",
                "INSPECT_PUBLIC_ECONOMY_STATE", "CLAIM_PUBLIC_SALARY"]})
        debt = MarkDailySalary.objects.create(day_index=0, amount_due=9,
            earned_request_id=uuid.uuid4(), earned_world_minute=590)
        migrate = importlib.import_module("village.migrations.0059_mark_dish_reviewer").update_mark_role
        migrate(apps, SimpleNamespace(connection=connection))
        migrate(apps, SimpleNamespace(connection=connection))
        profile.refresh_from_db()
        debt.refresh_from_db()
        self.assertEqual(profile.occupation, "dish_reviewer")
        self.assertEqual(profile.capabilities, ["GET_RULE", "EAT", "RATE_DISH", "INSPECT_RATEABLE_DISHES"])
        self.assertIn("菜品", profile.personal_goal)
        self.assertEqual((debt.amount_due, debt.amount_paid), (9, 0))

    def test_mark_finance_commands_are_retired_and_existing_debt_is_unchanged(self):
        from village.phase3_asset_service import commit_economy_action
        from village.tests.test_phase3_production import _payload
        from village.phase3_public_economy import handle_publish_price_index, handle_audit_public_fund
        retired={"PUBLISH_PRICE_INDEX","AUDIT_PUBLIC_FUND","INSPECT_PUBLIC_ECONOMY_STATE","CLAIM_PUBLIC_SALARY"}
        self.assertFalse(retired & set(COMMAND_CATALOG))
        debt=MarkDailySalary.objects.create(day_index=0,amount_due=9,earned_request_id=uuid.uuid4(),earned_world_minute=590)
        for name in retired-{"INSPECT_PUBLIC_ECONOMY_STATE"}:
            self.assertEqual(self.accept(name=name)[1],"UNKNOWN_ACTION")
            self.assertEqual(commit_economy_action(_payload(name,10013,"RECORD_HALL",{}))["result_code"],"ACTION_RETIRED")
        self.assertEqual(execute_agent_query("10011","INSPECT_PUBLIC_ECONOMY_STATE")[1],"UNKNOWN_QUERY")
        for handler in (handle_publish_price_index,handle_audit_public_fund):
            self.assertEqual(handler(10013,{},600,uuid.uuid4())["result_code"],"ACTION_RETIRED")
        debt.refresh_from_db()
        self.assertEqual((debt.amount_due,debt.amount_paid),(9,0))
