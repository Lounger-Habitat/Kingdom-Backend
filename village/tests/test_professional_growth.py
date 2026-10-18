"""G6 focused in-memory checks of actual production, samples and defense facts."""
import copy
import uuid
from unittest.mock import patch

from django.db import connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from inventory.models import InventorySlot, NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot
from items.models import Item
from player.mastery import mastery_overview
from player.models import (PlayerMasteryChallenge, PlayerSkillProgress, PlayerState, PlayerTitle,
    SkillRewardReceipt, PlayerCombatReceipt, PlayerHuntingState)
from village.advanced_gathering import professional_resource_overview
from village.forestry import execute as forestry
from village.models import (VillageFarmPlot, VillageGatheringResourceState, VillageTreeNode,
    VillageWildlife, VillageStore, VillageStoreAccount, NpcRuntimeState, VillageActionDurationConfig)
from village.player_cooking import preview_cooking, cooking_overview
from village.player_hunting import execute_combat
from village.player_processing import execute_processing_action, settle_processing_jobs
from village.production_tools import tools_overview, execute_tool_action, ToolError, ensure_production_catalog
from village.professional_catalog import PROFESSIONAL_COLLECT_TYPES
from village.tests.test_player_actions import PlayerFixture
from village.wildlife import execute as wildlife


class ProfessionalGrowthTests(PlayerFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.ap = PlayerState.objects.create(player_id=str(self.a.pk), action_points=1000)
        for item_id, name, kind in [(1001, "南瓜", "蔬菜"), (1049, "盐", "调料类"), (1057, "五花肉", "肉类"), (1034, "排骨", "肉类")]:
            Item.objects.get_or_create(pk=item_id, defaults=dict(item_name=name, item_type=kind))
        for npc_id, name in [("10015", "Nora"), ("10011", "Bill"), ("10012", "Jack")]:
            npc, _ = NpcDefinition.objects.get_or_create(agent_id=npc_id, defaults=dict(name=name))
            npc.is_test_only = False; npc.save(update_fields=["is_test_only"])
            NpcGlobalInventory.objects.get_or_create(npc=npc, defaults=dict(money=500))
        store, _ = VillageStore.objects.update_or_create(store_code="GENERAL_STORE", defaults=dict(display_name="杂货店",
            location_code="GENERAL_STORE", operator_npc=NpcDefinition.objects.get(agent_id="10015"), status="OPEN"))
        self.store_account, _ = VillageStoreAccount.objects.update_or_create(store=store, defaults=dict(balance=500))

    def level(self, skill, xp=1980, player=None):
        PlayerSkillProgress.objects.update_or_create(player=player or self.a, skill_code=skill, defaults=dict(total_xp=xp))

    def tool(self, item_id, uses=100, bag=None):
        bag = bag or self.bag_a
        return InventorySlot.objects.create(bag=bag, slot_index=bag.slots.count(), item_id=item_id, item_amount=1, remaining_uses=uses)

    def mastery(self, skill="gathering"):
        return next(c for c in mastery_overview(self.a, self.bag_a, self.clock.world_minute)["challenges"] if c["skill_code"] == skill)

    def start(self, skill="gathering"):
        result = self.run_action("start_mastery", dict(skill_code=skill))
        self.assertTrue(result["success"], result)

    def collect(self, collect_type, tool, rid=None):
        return self.run_action("collect", dict(collect_type=collect_type, tool_instance_id=str(tool.instance_id)),
                               location="DEEP_FOREST", rid=rid)

    def test_professional_overview_and_virtual_resource_restore_never_write(self):
        self.level("gathering")
        self.tool(4120)
        VillageGatheringResourceState.objects.create(code="DEEP_MUSHROOM", remaining_quantity=0, next_restore_world_minute=50)
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(professional_resource_overview("采集深林菌", 100)["remaining_quantity"], 90)
            self.assertTrue(self.mastery()["can_start"])
            cooking_overview(self.a, self.bag_a, 100, read_only=True)
        self.assertFalse([q for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))])
        self.assertFalse(PlayerMasteryChallenge.objects.exists())
        self.assertEqual(VillageGatheringResourceState.objects.get(pk="DEEP_MUSHROOM").remaining_quantity, 0)

    def test_t3_bow_upgrade_and_repair_preserve_uuid_and_charge_exact_real_materials(self):
        self.level("hunting")
        self.bag_a.money = 500; self.bag_a.save(update_fields=["money"])
        bow = self.tool(4113, 8)
        for key, count in [(29306, 4), (29105, 2), (29104, 4), (29311, 1)]:
            self.slot(count, item_id=key, quality=4, expires_world_minute=None)
        def quote(kind):
            return next(t for t in tools_overview(self.a, self.bag_a, 100)["tools"] if t["instance_id"] == str(bow.instance_id))[kind]
        upgrade = quote("upgrade")
        self.assertTrue(upgrade["available"], upgrade)
        self.assertEqual([(m["item_id"], m["quantity"]) for m in upgrade["materials"]], [(29306, 4), (29105, 2), (29104, 4)])
        original = bow.instance_id
        execute_tool_action(self.a, self.bag_a, "upgrade_tool", dict(instance_id=str(original), expected_quote=upgrade["quote_hash"]), uuid.uuid4(), self.clock)
        bow.refresh_from_db(); self.bag_a.refresh_from_db(); self.store_account.refresh_from_db()
        self.assertEqual((bow.instance_id, bow.item_id, bow.remaining_uses, self.bag_a.money, self.store_account.balance), (original, 4125, 180, 300, 700))
        bow.remaining_uses = 1; bow.save(update_fields=["remaining_uses"])
        repair = quote("repair")
        self.assertEqual((repair["gold_cost"], repair["materials"][0]["item_id"]), (25, 29311))
        with self.assertRaises(RuntimeError), transaction.atomic():
            execute_tool_action(self.a, self.bag_a, "repair_tool", dict(instance_id=str(original), expected_quote=repair["quote_hash"]), uuid.uuid4(), self.clock)
            raise RuntimeError("rollback")
        self.bag_a.refresh_from_db(); bow.refresh_from_db()
        self.assertEqual((self.bag_a.money, bow.remaining_uses, self.bag_a.slots.get(item_id=29311).item_amount), (300, 1, 1))
        execute_tool_action(self.a, self.bag_a, "repair_tool", dict(instance_id=str(original), expected_quote=repair["quote_hash"]), uuid.uuid4(), self.clock)
        self.bag_a.refresh_from_db(); self.assertEqual(self.bag_a.money, 275)
        ensure_production_catalog(100); ensure_production_catalog(100)
        self.assertEqual(sum(s.item_amount for s in VillageStore.objects.get(pk="GENERAL_STORE").inventory_slots.filter(item_id=29307)), 30)

    def test_three_shared_professional_pools_replay_and_insufficient_last_stock(self):
        tool = self.tool(4120)
        self.assertEqual(self.collect("采集深林菌", tool)["result_code"], "SKILL_LEVEL_REQUIRED")
        self.level("gathering")
        rid = uuid.uuid4()
        first = self.collect("采集深林菌", tool, rid)
        self.assertTrue(first["success"], first)
        replay = self.collect("采集深林菌", tool, rid)
        self.assertEqual(replay, first)
        reward = SkillRewardReceipt.objects.get(player=self.a, source_type="professional_collect")
        self.assertEqual(reward.result["facts"]["outputs"][0]["quantity"], 2)
        self.assertEqual(reward.result["facts"]["tool_tier"], 3)
        self.assertEqual(VillageGatheringResourceState.objects.get(pk="DEEP_MUSHROOM").remaining_quantity, 88)
        VillageGatheringResourceState.objects.filter(pk="DEEP_MUSHROOM").update(remaining_quantity=1)
        tool.refresh_from_db(); self.ap.refresh_from_db()
        before = (tool.remaining_uses, self.ap.action_points)
        self.assertEqual(self.collect("采集深林菌", tool)["result_code"], "RESOURCE_DEPLETED")
        tool.refresh_from_db(); self.ap.refresh_from_db()
        self.assertEqual((tool.remaining_uses, self.ap.action_points), before)
        for collect_type in ("采集山香草", "采集林莓"):
            self.assertTrue(self.collect(collect_type, tool)["success"])
        self.assertEqual(VillageGatheringResourceState.objects.count(), 3)

    def test_pumpkin_frozen_batch_and_real_harvest_proof(self):
        self.level("farming")
        self.start("farming")
        tool = self.tool(4121)
        self.slot(1, item_id=29307, expires_world_minute=None)
        plot = VillageFarmPlot.objects.create(plot_code="G6_TEST", lease_enabled=True, status="EMPTY", lessee_player=self.a,
            leased_world_minute=100, lease_ends_world_minute=10000)
        planted = self.run_action("plant_farm_plot", dict(plot_code=plot.plot_code, crop_code="PUMPKIN", expected_plot_version=0,
            tool_instance_id=str(tool.instance_id)), location="BILL_FARM")
        self.assertTrue(planted["success"], planted)
        plot.refresh_from_db(); frozen = copy.deepcopy(plot.production_rules_snapshot)
        self.assertEqual((frozen["quality"], frozen["harvest_xp"], frozen["output_freshness_minutes"]), (4, 30, 5760))
        self.clock.world_minute = plot.mature_world_minute; self.clock.save()
        result = self.run_action("harvest_farm_plot", dict(plot_code=plot.plot_code, expected_plot_version=plot.plot_version,
            expected_batch_id=str(plot.batch_id), tool_instance_id=str(tool.instance_id)), location="BILL_FARM")
        self.assertTrue(result["success"], result)
        output = self.bag_a.slots.get(item_id=1001)
        self.assertTrue(6 <= output.item_amount <= 8)
        self.assertEqual(output.expires_world_minute, self.clock.world_minute+5760)
        proof = SkillRewardReceipt.objects.get(player=self.a, source_type="farm_harvest").result["facts"]
        self.assertEqual((proof["tool_tier"], proof["outputs"][0]["instance_id"]), (3, str(output.instance_id)))
        requirements = self.mastery("farming")["requirements"]
        self.assertTrue(requirements[0]["complete"])
        self.assertFalse(requirements[1]["complete"])

    def test_durable_wood_real_completion_and_regrowth_keep_old_nodes_unchanged(self):
        self.level("forestry")
        self.start("forestry")
        tool = self.tool(4122)
        node = uuid.uuid4()
        self.assertTrue(forestry(dict(action="register", node_id=str(node), species="DURABLE_OAK",
            position=dict(x=0,y=0,z=0), request_id=str(uuid.uuid4())))["success"])
        payload = dict(action="finish", node_id=str(node), generation=1, attempt_id=str(uuid.uuid4()), actor_type="PLAYER",
            actor_id=str(self.a.pk), tool_instance_id=str(tool.instance_id), felled_world_minute=100, request_id=str(uuid.uuid4()))
        result = forestry(payload)
        self.assertTrue(result["success"], result)
        self.assertEqual(forestry(copy.deepcopy(payload)), result)
        wood = self.bag_a.slots.get(item_id=29311)
        self.assertEqual((wood.item_amount, wood.quality, wood.expires_world_minute), (3, 4, None))
        self.assertEqual(VillageTreeNode.objects.get(pk=node).regrow_world_minute, 10180)
        facts = SkillRewardReceipt.objects.get(player=self.a, skill_code="forestry").result["facts"]
        self.assertEqual((facts["tool_tier"], facts["outputs"][0]["instance_id"]), (3, str(wood.instance_id)))
        hardwood_node = uuid.uuid4()
        forestry(dict(action="register", node_id=str(hardwood_node), species="HARDWOOD", position=dict(x=1,y=0,z=0), request_id=str(uuid.uuid4())))
        second = forestry(dict(payload, node_id=str(hardwood_node), attempt_id=str(uuid.uuid4()), request_id=str(uuid.uuid4())))
        self.assertTrue(second["success"], second)
        self.assertTrue(self.mastery("forestry")["can_contribute"])

    def test_five_professional_cooking_batches_need_t3_and_leave_trusted_exact_output(self):
        self.level("cooking")
        self.start("cooking")
        tool = self.tool(4123)
        selections = []
        for key, count in [(1001, 5), (29308, 10), (1049, 5)]:
            raw = self.slot(count, item_id=key, source_request_id=uuid.uuid4(), quality=4, expires_world_minute=2000)
            selections.append(dict(instance_id=str(raw.instance_id), quantity=count))
        params = dict(recipe_id=29251, batches=5, tool_instance_id=str(tool.instance_id), selections=selections)
        preview = preview_cooking(self.a, self.bag_a, params, 100)
        self.assertTrue(preview["available"], preview)
        params["tool_instance_id"] = str(self.tool(4107, 50).instance_id)
        self.assertEqual(preview_cooking(self.a, self.bag_a, params, 100)["reason"], "TOOL_TIER_REQUIRED")
        params["tool_instance_id"] = str(tool.instance_id)
        params["expected_quote"] = preview["quote_hash"]
        result = self.run_action("cook_dish", params, location="RESTAURANT")
        self.assertTrue(result["success"], result)
        dish = self.bag_a.slots.get(item_id=29206)
        self.assertEqual((dish.item_amount, dish.expires_world_minute), (15, 2000))
        self.assertGreaterEqual(dish.quality, 4)
        tool.refresh_from_db(); self.assertEqual(tool.remaining_uses, 95)
        facts = SkillRewardReceipt.objects.get(player=self.a, source_type="player_cooking").result["facts"]
        self.assertEqual((facts["tool_tier"], facts["outputs"][0]["instance_id"]), (3, str(dish.instance_id)))
        for recipe_id, ingredients in [(29252, [(29107,2),(29309,1),(1049,1)]), (29253, [(1030,2),(29310,1),(1049,1)])]:
            choices = []
            for item_id, amount in ingredients:
                raw = self.slot(amount, item_id=item_id, source_request_id=uuid.uuid4(), quality=4)
                choices.append(dict(instance_id=str(raw.instance_id), quantity=amount))
            params = dict(recipe_id=recipe_id, batches=1, tool_instance_id=str(tool.instance_id), selections=choices)
            preview = preview_cooking(self.a, self.bag_a, params, 100)
            cooked = self.run_action("cook_dish", dict(params, expected_quote=preview["quote_hash"]), location="RESTAURANT")
            self.assertTrue(cooked["success"], cooked.get("result_code"))
        self.assertTrue(self.mastery("cooking")["can_contribute"])

    def combat(self, action, **kwargs):
        return execute_combat(dict(action=action, player_id=str(self.a.pk), request_id=str(uuid.uuid4()),
                                   authority_session_id="g6-env", **kwargs))

    def test_late_zero_defense_damage_persists_and_replays_across_new_sessions(self):
        boar = VillageWildlife.objects.create(prey_id=uuid.uuid4(), species="BOAR", region_id="NORTH_SLOPE",
            position={}, born_world_minute=80, death_world_minute=95, status="CLAIMED_REWARD")
        payload = dict(action="damage", player_id=str(self.a.pk), request_id=str(uuid.uuid4()), authority_session_id="old-env",
            source_kind="BOAR_CHARGE", prey_id=str(boar.pk), outcome="BLOCKED", damage=0, impact_world_minute=90)
        PlayerHuntingState.objects.create(player=self.a, recovery_ready_world_minute=130)
        first = execute_combat(payload)
        self.assertTrue(first["success"], first)
        self.assertEqual(first["data"]["health"]["current"], 100)
        self.assertEqual(first["data"]["health"]["recovery_ready_world_minute"], 130)
        PlayerHuntingState.objects.filter(player=self.a).update(authority_session_id="new-env")
        self.assertTrue(execute_combat(copy.deepcopy(payload))["data"]["replayed"])
        self.assertEqual(PlayerCombatReceipt.objects.filter(player=self.a).count(), 1)
        invalid = dict(payload, request_id=str(uuid.uuid4()), impact_world_minute=96)
        self.assertEqual(execute_combat(invalid)["result_code"], "DAMAGE_SOURCE_UNAVAILABLE")
        invalid["impact_world_minute"] = 79
        self.assertEqual(execute_combat(invalid)["result_code"], "DAMAGE_SOURCE_UNAVAILABLE")
        hit = dict(payload, request_id=str(uuid.uuid4()), outcome="HIT", damage=30)
        self.assertEqual(execute_combat(hit)["data"]["health"]["current"], 70)

    def test_gathering_mastery_transfers_exact_own_samples_once_and_grants_only_title(self):
        self.level("gathering", 5780)
        self.start()
        tool = self.tool(4120)
        for collect_type in PROFESSIONAL_COLLECT_TYPES:
            self.assertTrue(self.collect(collect_type, tool)["success"])
        view = self.mastery()
        self.assertTrue(view["can_contribute"], view)
        self.assertFalse(view["can_claim"])
        selections = [{k: r[k] for k in ("requirement_code", "instance_id", "quantity")} for r in view["recommended_selections"]]
        params = dict(skill_code="gathering", selections=selections, expected_quote=view["quote_hash"])
        nora = NpcGlobalInventory.objects.get(npc__agent_id="10015")
        money = self.bag_a.money
        with patch("player.mastery.copy_batch_attributes", side_effect=RuntimeError("failed transfer")):
            with self.assertRaises(RuntimeError):
                self.run_action("contribute_mastery", params, location="GENERAL_STORE")
        self.assertFalse(PlayerMasteryChallenge.objects.get(player=self.a).contribution_snapshot)
        result = self.run_action("contribute_mastery", params, location="GENERAL_STORE")
        self.assertTrue(result["success"], result)
        for item_id in [29308, 29309, 29310]:
            sample = nora.slots.get(item_id=item_id)
            self.assertEqual((sample.item_amount, sample.expires_world_minute), (1, 2980))
            self.assertEqual(self.bag_a.slots.get(item_id=item_id).item_amount, 1)
        self.assertTrue(self.mastery()["can_claim"])
        claimed = self.run_action("claim_mastery", dict(skill_code="gathering"))
        self.assertTrue(claimed["success"], claimed)
        self.assertEqual(claimed["data"]["result"]["money_gained"], 0)
        self.assertEqual(PlayerTitle.objects.filter(title_id="master_gathering").count(), 1)
        self.assertAlmostEqual(PlayerTitle.objects.get(title_id="master_gathering").obtained_at,
                               timezone.now().timestamp(), delta=5)
        self.assertEqual(PlayerMasteryChallenge.objects.get(player=self.a).claimed_world_minute, self.clock.world_minute)
        self.assertEqual(self.run_action("claim_mastery", dict(skill_code="gathering"))["result_code"], "ALREADY_CLAIMED")
        self.bag_a.refresh_from_db(); self.assertEqual(self.bag_a.money, money)

    def test_mastery_rejects_purchased_or_old_missing_proofs(self):
        self.level("gathering", 5780)
        self.start()
        for key in (29308, 29309, 29310):
            self.slot(2, item_id=key, source_request_id=uuid.uuid4(), quality=4)
        view = self.mastery()
        self.assertFalse(view["can_contribute"])
        self.assertTrue(view["missing"])
        self.assertEqual(view["recommended_selections"], [])

    def test_hunting_mastery_requires_same_real_boar_defense_kill_and_claimed_processing(self):
        self.level("hunting")
        self.start("hunting")
        self.tool(4110, 60)
        boar = uuid.uuid4()
        registered = wildlife(dict(action="register", prey_id=str(boar), species="BOAR", region_id="NORTH_SLOPE",
            position=dict(x=0,y=0,z=0), request_id=str(uuid.uuid4())))
        self.assertTrue(registered["success"], registered)
        defended = self.combat("damage", source_kind="BOAR_CHARGE", prey_id=str(boar), outcome="DODGED", damage=0, impact_world_minute=100)
        self.assertTrue(defended["success"], defended)
        attack = uuid.uuid4()
        permit = execute_combat(dict(action="authorize_attack", player_id=str(self.a.pk), request_id=str(attack), authority_session_id="g6-env", weapon=0))
        self.assertTrue(permit["success"], permit)
        killed = wildlife(dict(action="finish", prey_id=str(boar), attempt_id=str(uuid.uuid4()), actor_type="PLAYER",
            actor_id=str(self.a.pk), attack_id=str(attack), death_world_minute=100, request_id=str(uuid.uuid4())))
        self.assertTrue(killed["success"], killed)
        self.assertFalse(self.mastery("hunting")["can_claim"])
        jack = NpcGlobalInventory.objects.get(npc__agent_id="10012")
        NpcRuntimeState.objects.update_or_create(npc=jack.npc, defaults=dict(current_location_code="BUTCHER_YARD",
            health_fp=1000, energy_fp=800, hunger_fp=200, needs_world_minute=100, active_command_id=None, action_status="IDLE"))
        jack.slots.all().delete()
        NpcGlobalInventorySlot.objects.create(bag=jack, slot_index=0, item_id=4002, item_amount=1, remaining_uses=20)
        VillageActionDurationConfig.objects.update_or_create(action_name="PROCESS_GAME", defaults=dict(duration_minutes=5))
        prey = self.bag_a.slots.get(item_id=29108)
        params = dict(prey_instance_id=str(prey.instance_id))
        preview = execute_processing_action(self.a, self.bag_a, "preview_game_processing", params, uuid.uuid4(), self.clock)["preview"]
        self.assertTrue(preview["available"], preview)
        job = execute_processing_action(self.a, self.bag_a, "start_game_processing", dict(params, expected_quote=preview["quote_hash"]), uuid.uuid4(), self.clock)["job"]
        self.clock.world_minute = 105; self.clock.save()
        settle_processing_jobs(105)
        claimed = execute_processing_action(self.a, self.bag_a, "claim_game_processing", dict(job_id=job["job_id"]), uuid.uuid4(), self.clock)
        self.assertEqual(self.bag_a.slots.get(item_id=1057, item_amount=4).expires_world_minute, 2980)
        view = self.mastery("hunting")
        self.assertEqual(view["requirements"][0]["current_quantity"], 1)
        self.assertFalse(view["can_claim"])
        self.level("hunting", 5780)
        self.assertTrue(self.mastery("hunting")["can_claim"])
        self.assertEqual(self.mastery("hunting")["recommended_selections"], [])

    def test_business_mastery_uses_real_purchased_terminal_sales_without_cooking_requirement(self):
        from marketplace.services import create_listing
        from restaurant.models import RestaurantConfig, RestaurantHourlySlot, RestaurantSaleEvent
        from restaurant.player_stall import process_stall_tick
        self.clock.apply_world_minute(600); self.clock.save()
        self.level("business", 5780)
        self.start("business")
        RestaurantConfig.objects.update_or_create(pk=1, defaults=dict(opening_hour=10, closing_hour=19,
            base_hourly_sales=3, base_menu_capacity=5, hourly_sales_per_level=1, menu_capacity_per_level=1))
        source = self.slot(3, bag=self.bag_b, item_id=29201, quality=4, source_request_id=uuid.uuid4(),
            dish_metadata=dict(maker_player_id=str(self.b.pk), production_batch_id=str(uuid.uuid4())),
            ingredient="玉米×2", cook_time=60, rated=True, rating_price=12, overall_score=82)
        listing, error = create_listing(self.b, source.instance_id, 3, 7)
        self.assertIsNone(error)
        purchased = self.run_action("buy_marketplace", dict(listing_id=str(listing.pk), quantity=3, expected_unit_price=7), location="MARKET_STREET")
        self.assertTrue(purchased["success"], purchased.get("result_code"))
        received = purchased["data"]["result"]["purchased_batches"][0]["instance_id"]
        self.assertTrue(self.run_action("open_player_stall", location="RESTAURANT")["success"])
        self.assertTrue(self.run_action("list_stall_dish", dict(instance_id=received, quantity=3), location="RESTAURANT")["success"])
        hourly = RestaurantHourlySlot.objects.create(player=self.a, game_year=1, game_month=1, game_day=1, game_hour=10, quota=3)
        RestaurantSaleEvent.objects.bulk_create([RestaurantSaleEvent(slot=hourly, scheduled_minute=m) for m in (5,6,7)])
        self.clock.apply_world_minute(607); self.clock.save()
        process_stall_tick(self.clock)
        view = self.mastery("business")
        sales = next(e for e in view["evidence"] if e["code"] == "terminal_sales")
        self.assertEqual((sales["quantity"], sales["revenue"], sales["purchase_cost"], sales["complete"]), (3,36,21,True))
        self.assertFalse(view["can_claim"])
        self.assertIn("完成本人专业合同", view["missing"])
        self.assertEqual(view["recommended_selections"], [])

    def test_professional_future_trade_keeps_frozen_visit_and_exports_actual_nora_stock(self):
        import importlib
        from types import SimpleNamespace
        from django.apps import apps
        from village import caravan
        from village.models import VillageSimulationConfig, VillageCaravanVisit, VillageCaravanRequest
        config = VillageSimulationConfig.objects.get(config_key="default")
        config.caravan_item_ids, config.caravan_export_item_ids = [1051], [29306]
        config.save()
        frozen = VillageCaravanVisit.objects.create(day_index=0, created_world_minute=660, route="NORMAL", location_code="TRADE_ROAD",
            stock={"1051": 30}, initial_stock={"1051": 30}, import_prices={"1051": 5}, export_prices={"29306": 12}, export_capacity=60)
        original = copy.deepcopy((frozen.stock, frozen.initial_stock, frozen.import_prices, frozen.export_prices))
        migration = importlib.import_module("items.migrations.0012_professional_catalog")
        migration.install(apps, SimpleNamespace(connection=connection))
        config.refresh_from_db()
        self.assertIn(29307, config.caravan_item_ids)
        self.assertTrue({1001,29308,29309,29310,29311}.issubset(config.caravan_export_item_ids))
        self.assertEqual(caravan.sync_visit(700).pk, frozen.pk)
        frozen.refresh_from_db()
        self.assertEqual((frozen.stock, frozen.initial_stock, frozen.import_prices, frozen.export_prices), original)
        self.clock.apply_world_minute(2100); self.clock.save()
        VillageCaravanRequest.objects.create(day_index=1, item_ids=[29307], submitted_world_minute=1000)
        with patch("village.caravan.current_route", return_value="NORMAL"):
            visit = caravan.sync_visit(2100)
        self.assertEqual(visit.stock, {"29307": 30})
        self.assertIn("29311", visit.export_prices)
        nora = NpcGlobalInventory.objects.get(npc__agent_id="10015")
        nora.slots.all().delete()
        stock = NpcGlobalInventorySlot.objects.create(bag=nora, slot_index=0, item_id=29311, item_amount=2,
            quality=4, produced_world_minute=2000, source_request_id=uuid.uuid4())
        before = nora.money
        with transaction.atomic():
            sold = caravan.handle_export("10015", dict(item_id=29311, amount=2), 2100, uuid.uuid4())
        self.assertEqual(sold["result_code"], "OK", sold)
        stock.refresh_from_db(); nora.refresh_from_db(); visit.refresh_from_db()
        self.assertEqual((stock.item_amount, visit.exported_amount, nora.money-before), (0, 2, 2*visit.export_prices["29311"]))
        config.caravan_item_ids, config.caravan_export_item_ids = [], []
        config.save()
        migration.install(apps, SimpleNamespace(connection=connection))
        config.refresh_from_db()
        self.assertEqual((config.caravan_item_ids, config.caravan_export_item_ids), ([], []))

    def test_t3_pack_keeps_basic_category_collection_and_rejects_low_skill_without_wear(self):
        from inventory.models import CollectionPoint, CollectionPointLevel, CollectionPointLevelItem
        point, _ = CollectionPoint.objects.update_or_create(collect_type="采集食材", defaults=dict(
            name="G6基础兼容点", item_type="食材", action_cost=15, collect_count=3, default_level=1))
        level, _ = CollectionPointLevel.objects.get_or_create(collection_point=point, level=1)
        level.items.all().delete()
        CollectionPointLevelItem.objects.create(level=level, item_id=1001, collect_min_amount=2, collect_max_amount=2)
        tool = self.tool(4120)
        self.level("gathering", 440)
        params = dict(collect_type="采集食材", category="fruiting", tool_instance_id=str(tool.instance_id))
        denied = self.run_action("collect", params, location="FOREST_EDGE")
        self.assertEqual(denied["result_code"], "SKILL_LEVEL_REQUIRED")
        tool.refresh_from_db(); self.ap.refresh_from_db()
        self.assertEqual((tool.remaining_uses, self.ap.action_points), (100, 1000))
        self.level("gathering")
        accepted = self.run_action("collect", params, location="FOREST_EDGE")
        self.assertTrue(accepted["success"], accepted.get("result_code"))
        tool.refresh_from_db(); self.ap.refresh_from_db()
        self.assertEqual((tool.remaining_uses, self.ap.action_points), (99, 985))
        self.assertEqual(self.bag_a.slots.get(item_id=1001).item_amount, 2)
