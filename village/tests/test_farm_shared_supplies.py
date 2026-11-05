"""Focused checks for the shared farm stock; no full-game test run required."""
import uuid

from django.test import TestCase

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.models import InventoryBag, InventorySlot
from items.models import Item
from village.affairs import AffairError, complete_labor, condition_progress, project_affair, resolve_condition
from village.farm_expansion import ensure_expansion_sites
from village.farm_irrigation import ensure_irrigation_affair
from village.farm_supplies import SUPPLY_CODE, DEPOT_LOCATION, stock_state
from village.models import VillageAffair, VillageFarmPlot, VillageSimulationConfig
from village.player_actions import _execute
from village.player_affair_labor import start_labor, cancel_labor, tick_player_labor


class SharedFarmSupplyTests(TestCase):
    def setUp(self):
        self.clock = GlobalGameTimeState.get_instance()
        self.clock.world_minute = 100
        self.clock.save()
        VillageSimulationConfig.objects.update_or_create(config_key="default", defaults=dict(
            village_economy_enabled=True, village_story_enabled=True, farm_expansion_enabled=True))
        Item.objects.update_or_create(item_id=1002, defaults=dict(item_name="木头", item_type="Commodity", item_price=5))
        irrigation = ensure_irrigation_affair(100)
        irrigation.status = "COMPLETED"
        irrigation.progress = {"conditions": {c["id"]:c["target"] for s in irrigation.requirements["stages"] for c in s["conditions"]}}
        irrigation.save()
        ensure_expansion_sites(100)
        self.player = GameAccount.objects.create(username="shared_supply_farmer")
        self.bag = InventoryBag.objects.create(player=self.player, character_id=self.player.username, money=40)
        self.wood = InventorySlot.objects.create(bag=self.bag, slot_index=0, item_id=1002, item_amount=60)

    def row(self, suffix):
        return VillageAffair.objects.get(pk="FARM_EXPANSION_"+suffix)

    def ready_plots(self):
        for site in ("E", "F"):
            prep = self.row(site+"_PREP")
            prep.progress = {"conditions":dict(entrance_labor=1, water_labor=1)}
            prep.status = "COMPLETED"
            prep.save()
        pool = self.row("SUPPLY")
        pool.progress.update(conditions=dict(shared_wood=24, shared_gold=40))
        pool.save()
        for suffix in ("E_01", "E_02"):
            row = self.row(suffix)
            row.progress = {"conditions":dict(clearing_labor=1)}
            row.save()

    def test_deposit_reservation_cancel_and_completion_spend_only_shared_stock(self):
        self.ready_plots()
        _execute(self.player, self.bag, "contribute", dict(affair_id=SUPPLY_CODE,
            condition_id="shared_wood", kind="item", item_id=1002, quantity=2), uuid.uuid4(), DEPOT_LOCATION, self.clock)
        first, second = self.row("E_01"), self.row("E_02")
        nonce = uuid.uuid4()
        start_labor(self.player, first, uuid.uuid4(), "FARM_WORK_E_01", self.clock, nonce, "leveling_labor")
        self.assertEqual(stock_state()["available"]["wood"], 0)
        with self.assertRaises(AffairError) as rejected:
            resolve_condition(second, "leveling_labor", "labor")
        self.assertEqual(rejected.exception.code, "FARM_SHARED_STOCK_INSUFFICIENT")
        cancel_labor(self.player)
        self.assertEqual(stock_state()["available"]["wood"], 2)
        labor = start_labor(self.player, second, uuid.uuid4(), "FARM_WORK_E_02", self.clock, nonce, "leveling_labor")
        self.clock.world_minute += 20
        self.clock.save()
        result = tick_player_labor(dict(player_id=str(self.player.pk), labor_id=labor["labor_id"],
            authority_session_id=str(nonce), current_location_code="FARM_WORK_E_02", present=True,
            observed_world_minute=self.clock.world_minute))
        self.assertTrue(result["success"], result)
        self.assertEqual(result["data"]["credited_units"], 1)
        self.assertEqual(stock_state()["stock"]["wood"], 0)
        self.assertEqual(result["data"]["affair"]["resource_pool"]["stages"][0]["conditions"][0]["stock_quantity"], 0)
        self.wood.refresh_from_db(); self.bag.refresh_from_db()
        self.assertEqual((self.wood.item_amount, self.bag.money), (58,40))

    def test_empty_stock_does_not_block_field_clearing_before_public_preparation(self):
        row = self.row("E_01")
        self.assertEqual(resolve_condition(row, "clearing_labor")["type"], "labor")
        complete_labor(row, condition_id="clearing_labor", world_minute=100, request_id=uuid.uuid4(), player_id=str(self.player.pk))
        self.assertEqual(VillageFarmPlot.objects.get(plot_code="FARM_PLOT_E_01").reclamation_status, "CLEARED")
        self.assertEqual(stock_state()["used"], dict(wood=0,gold=0))
        with self.assertRaises(AffairError):
            resolve_condition(self.row("E_PREP"), "entrance_labor")

    def test_legacy_paid_materials_import_once_without_erasing_work(self):
        pool = self.row("SUPPLY")
        pool.progress = {"conditions":{}}
        pool.save()
        prep = self.row("E_PREP")
        prep.progress = {"conditions":dict(entrance_wood=6,entrance_labor=1)}
        prep.save()
        plot = self.row("E_01")
        plot.progress = {"conditions":dict(leveling_wood=2,clearing_labor=1)}
        plot.save()
        ensure_expansion_sites(100)
        ensure_expansion_sites(100)
        state = stock_state()
        self.assertEqual((state["submitted"]["wood"], state["used"]["wood"], state["stock"]["wood"]), (8,6,2))
        self.assertEqual(condition_progress(self.row("E_01"))["clearing_labor"], 1)
        self.assertEqual(project_affair(self.row("E_01"))["resource_pool"]["affair_id"], SUPPLY_CODE)
