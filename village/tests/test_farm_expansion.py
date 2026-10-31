"""Focused authority regressions; use config.settings_player_test only."""
import uuid

from django.test import TestCase

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.models import InventoryBag, InventorySlot, NpcDefinition, NpcGlobalInventory
from items.models import Item
from village.affairs import active_stage, resolve_condition, AffairError
from village.farm_expansion import (ensure_expansion_sites, project_expansion_sites,
    sync_expansion_effects, preparation_code, plot_affair_code)
from village.farm_irrigation import ensure_irrigation_affair
from village.models import VillageAffair, VillageFarmPlot, VillageSimulationConfig, VillageWorldEvent
from village.phase3_production import plant_preflight, harvest_preflight
from village.player_actions import _execute, ActionError
from village.player_affair_labor import start_labor, tick_player_labor
from village.player_farm import execute_farm_action, project_plot


class FarmExpansionTests(TestCase):
    def setUp(self):
        self.clock = GlobalGameTimeState.get_instance()
        self.clock.world_minute = 100
        self.clock.save()
        self.cfg, _ = VillageSimulationConfig.objects.update_or_create(config_key="default", defaults={
            "village_economy_enabled": True, "village_story_enabled": True, "farm_expansion_enabled": True})
        for item_id in (1002, 4001, 1051, 1030):
            Item.objects.update_or_create(item_id=item_id, defaults={"item_name": str(item_id), "item_type": "Commodity", "item_price": 5})
        self.player = GameAccount.objects.create(username="expansion_farmer")
        self.bag = InventoryBag.objects.create(player=self.player, character_id=self.player.username, money=300)
        self.wood = InventorySlot.objects.create(bag=self.bag, slot_index=0, item_id=1002, item_amount=100)
        npc, _ = NpcDefinition.objects.get_or_create(agent_id="10011", defaults={"name": "Bill"})
        NpcGlobalInventory.objects.get_or_create(npc=npc)
        self.irrigation = ensure_irrigation_affair(100)
        ensure_expansion_sites(100)

    def finish(self, affair):
        """Use real paid submissions and trusted continuous presence labor."""
        affair.refresh_from_db()
        while stage := active_stage(affair):
            for condition in stage["conditions"]:
                condition = resolve_condition(affair, condition["id"])
                location = condition["locations"][0]
                if condition["type"] == "labor":
                    nonce = uuid.uuid4()
                    labor = start_labor(self.player, affair, uuid.uuid4(), location, self.clock, nonce, condition["id"])
                    self.clock.world_minute += condition["unit_minutes"]
                    self.clock.save()
                    response = tick_player_labor(dict(player_id=str(self.player.pk), labor_id=labor["labor_id"],
                        authority_session_id=str(nonce), current_location_code=location, present=True,
                        observed_world_minute=self.clock.world_minute))
                    self.assertTrue(response["success"], response)
                    self.assertEqual(response["data"]["credited_units"], 1)
                else:
                    params = dict(affair_id=affair.pk, condition_id=condition["id"], kind=condition["type"], quantity=condition["target"])
                    if condition["type"] == "item":
                        params["item_id"] = condition["item_ids"][0]
                    _execute(self.player, self.bag, "contribute", params, uuid.uuid4(), location, self.clock)
                affair.refresh_from_db()

    def test_locked_operations_and_queries_are_read_only(self):
        plot = VillageFarmPlot.objects.get(plot_code="FARM_PLOT_E_04")
        prep = VillageAffair.objects.get(pk=preparation_code("E"))
        with self.assertRaises(AffairError) as error:
            resolve_condition(prep, "entrance_wood")
        self.assertEqual(error.exception.code, "FARM_IRRIGATION_REQUIRED")
        for action in ("lease_farm_plot", "plant_farm_plot", "harvest_farm_plot"):
            with self.assertRaises(ActionError) as error:
                execute_farm_action(self.player, self.bag, action, {"plot_code": plot.plot_code}, uuid.uuid4(), self.clock)
            self.assertEqual(error.exception.code, "FARM_PLOT_LOCKED")
        for preflight in (plant_preflight, harvest_preflight):
            failure = preflight(10011, {"plot_code": "FARM_PLOT_E_01", "crop_code": "CORN"}, 100)
            self.assertEqual(failure["data"]["failure_kind"], "FARM_PLOT_LOCKED")
        events = VillageWorldEvent.objects.count()
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as queries:
            snapshot = project_expansion_sites(100)
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        self.assertEqual([len(s["plots"]) for s in snapshot["sites"]], [9, 9])
        self.assertEqual(VillageWorldEvent.objects.count(), events)
        self.bag.refresh_from_db()
        self.assertEqual(self.bag.money, 300)

    def test_real_child_progress_opens_one_plot_once_and_keeps_public_leasing(self):
        self.finish(self.irrigation)
        prep = VillageAffair.objects.get(pk=preparation_code("E"))
        self.finish(prep)
        plot = VillageFarmPlot.objects.get(plot_code="FARM_PLOT_E_04")
        self.assertEqual(plot.reclamation_status, "AVAILABLE")
        child = VillageAffair.objects.get(pk=plot_affair_code(plot.plot_code))
        self.finish(child)
        plot.refresh_from_db()
        self.assertEqual(plot.reclamation_status, "READY")
        self.assertEqual(VillageFarmPlot.objects.filter(expansion_site_code="E", reclamation_status="READY").count(), 1)
        self.wood.refresh_from_db(); self.bag.refresh_from_db()
        self.assertEqual((self.wood.item_amount, self.bag.money), (56, 180))
        version, opened = plot.plot_version, plot.reclaimed_world_minute
        sync_expansion_effects(child, self.clock.world_minute + 999)
        ensure_expansion_sites(self.clock.world_minute + 999)
        plot.refresh_from_db()
        self.assertEqual((plot.plot_version, plot.reclaimed_world_minute), (version, opened))
        self.assertEqual(VillageWorldEvent.objects.filter(event_type="FARM_PLOT_RECLAIMED").count(), 1)
        other = GameAccount.objects.create(username="non_contributor")
        other_bag = InventoryBag.objects.create(player=other, character_id=other.username, money=30)
        result = execute_farm_action(other, other_bag, "lease_farm_plot", dict(plot_code=plot.plot_code,
            days=1, expected_plot_version=version, expected_daily_price=10), uuid.uuid4(), self.clock)
        self.assertEqual(result["spent_gold"], 10)
        with self.assertRaises(ActionError) as rejected:
            execute_farm_action(self.player, self.bag, "lease_farm_plot", dict(plot_code=plot.plot_code,
                days=1, expected_plot_version=version + 1, expected_daily_price=10), uuid.uuid4(), self.clock)
        self.assertEqual(rejected.exception.code, "PLOT_NOT_LEASABLE")
        self.bag.refresh_from_db()
        self.assertEqual(self.bag.money, 180)
        self.cfg.farm_expansion_enabled = False
        self.cfg.save()
        plot.refresh_from_db()
        self.assertTrue(project_plot(plot, self.clock.world_minute)["can_use"])

    def test_old_crop_and_lease_survive_install_and_recovery(self):
        batch = uuid.uuid4()
        old, _ = VillageFarmPlot.objects.update_or_create(plot_code="FARM_PLOT_D_01", defaults=dict(
            lessee_player=self.player, leased_world_minute=50, lease_ends_world_minute=1500,
            status="GROWING", crop_code="CORN", batch_id=batch, mature_world_minute=1000))
        ensure_expansion_sites(999)
        old.refresh_from_db()
        self.assertEqual((old.reclamation_status, old.expansion_site_code, old.batch_id, old.lease_ends_world_minute),
                         ("READY", "", batch, 1500))
        self.assertTrue(project_plot(old, 999)["can_use"])
