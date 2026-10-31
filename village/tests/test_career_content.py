"""Small, isolated checks for new production and frozen processing outputs."""
import uuid

from inventory.models import NpcGlobalInventorySlot
from items.models import Item
from village.career_catalog import (TOMATO, TOMATO_SEED, SOYBEAN, SOYBEAN_SEED,
    PIG_LIVER, CHICKEN_GIZZARD, ANIMAL_BONE,
    DEER_HIDE, TOMATO_LIVER_SOUP, RECIPES, install_catalog, processing_outputs)
from village.career_development import content_access
from village.career_development_models import (VillageCareerDevelopment,
    VillageCareerEvidence, VillageCareerProductionReservation)
from village.models import VillageSimulationConfig, VillageFarmPlot, NpcCommandState
from village.phase3_production import _do_plant, _do_harvest
from village.lucy_cooking import cook
from village.jack_production import start_work, finish_work
from village.tests.test_phase3_production import ProductionTestBase


class CareerContentTests(ProductionTestBase):
    def setUp(self):
        super().setUp()
        install_catalog()
        VillageSimulationConfig.objects.filter(config_key="default").update(career_development_enabled=True)

    def trial(self, code):
        return VillageCareerDevelopment.objects.create(code=code, owner_npc_id="10012",
            owner_name="test", title=code, location_code="BUTCHER_YARD", status="TRIAL",
            trial_world_minute=700)

    def test_trial_farm_and_recipe_consume_real_inputs_and_keep_production_proof(self):
        plot, _ = VillageFarmPlot.objects.update_or_create(plot_code="FARM_PLOT_A",
            defaults=dict(status="EMPTY", lease_enabled=False, reclamation_status="READY"))
        seed = NpcGlobalInventorySlot.objects.create(bag=self.bill_inv, slot_index=2,
            item_id=TOMATO_SEED, item_amount=2)
        params = dict(plot_code=plot.plot_code, crop_code="TOMATO")
        denied = _do_plant(10011, params, 700, uuid.uuid4())
        self.assertEqual(denied["result_code"], "CAREER_CONTENT_LOCKED")
        seed.refresh_from_db()
        self.assertEqual(seed.item_amount, 2)
        self.trial("BILL_1")
        planted_id = uuid.uuid4()
        planted = _do_plant(10011, params, 700, planted_id)
        self.assertEqual(planted["status"], "SUCCEEDED", planted)
        plot.refresh_from_db()
        harvest_id = uuid.uuid4()
        harvested = _do_harvest(10011, dict(plot_code=plot.plot_code), plot.mature_world_minute, harvest_id)
        self.assertEqual(harvested["status"], "SUCCEEDED", harvested)
        crop = self.bill_inv.slots.get(item_id=TOMATO, source_request_id=harvest_id)
        self.assertGreaterEqual(crop.item_amount, 4)
        self.assertEqual(VillageCareerProductionReservation.objects.get(request_id=str(planted_id)).status, "CONSUMED")
        proof = VillageCareerEvidence.objects.get(kind="PRODUCTION", actor_id="10011")
        self.assertEqual(proof.metadata["source_request_id"], str(harvest_id))
        self.assertIn(str(crop.instance_id), proof.metadata["output_instance_ids"])

        self.trial("LUCY_1")
        for index, (item_id, amount) in enumerate(RECIPES[29421]["ingredients"].items(), 10):
            NpcGlobalInventorySlot.objects.create(bag=self.lucy_inv, slot_index=index,
                item_id=item_id, item_amount=amount, quality=3)
        cooked_id = uuid.uuid4()
        cooked = cook(10014, dict(recipe_id=29421), plot.mature_world_minute, cooked_id)
        self.assertEqual(cooked["status"], "SUCCEEDED", cooked)
        dish = self.lucy_inv.slots.get(item_id=TOMATO_LIVER_SOUP)
        self.assertEqual(dish.item_amount, 3)
        self.assertEqual(dish.source_request_id, cooked_id)
        self.assertEqual(self.lucy_inv.slots.filter(item_id=PIG_LIVER, item_amount__gt=0).count(), 0)

        self.trial("BILL_2")
        NpcGlobalInventorySlot.objects.create(bag=self.bill_inv, slot_index=8,
            item_id=SOYBEAN_SEED, item_amount=1)
        planted = _do_plant(10011, dict(plot_code=plot.plot_code, crop_code="SOYBEAN"),
            2200, uuid.uuid4())
        self.assertEqual(planted["status"], "SUCCEEDED", planted)
        plot.refresh_from_db()
        harvested = _do_harvest(10011, dict(plot_code=plot.plot_code), plot.mature_world_minute, uuid.uuid4())
        self.assertEqual(harvested["status"], "SUCCEEDED", harvested)
        self.assertTrue(self.bill_inv.slots.filter(item_id=SOYBEAN, item_amount__gte=6).exists())
        self.trial("LUCY_2")
        for recipe_id in (29422, 29423):
            for item_id, amount in RECIPES[recipe_id]["ingredients"].items():
                NpcGlobalInventorySlot.objects.create(bag=self.lucy_inv,
                    slot_index=self.lucy_inv.slots.count() + 40, item_id=item_id,
                    item_amount=amount, quality=3)
            answer = cook(10014, dict(recipe_id=recipe_id), plot.mature_world_minute, uuid.uuid4())
            self.assertEqual(answer["status"], "SUCCEEDED", answer)
            self.assertEqual(answer["data"]["dish"]["amount"], 3)

    def test_deer_hide_replaces_leather_and_started_processing_survives_pause(self):
        self.trial("JACK_1")
        self.trial("JACK_2")
        outputs = processing_outputs(29106)
        self.assertEqual(outputs[DEER_HIDE], 1)
        self.assertNotIn(29105, outputs)
        pig = processing_outputs(29102)
        self.assertEqual((pig[PIG_LIVER], pig[ANIMAL_BONE]), (1, 1))
        prey = NpcGlobalInventorySlot.objects.create(bag=self.jack_inv, slot_index=3,
            item_id=29106, item_amount=1, quality=4, produced_world_minute=600)
        self.jack_rt.current_location_code = "BUTCHER_YARD"
        self.jack_rt.save()
        for item_id, expected in ((29102, PIG_LIVER), (29101, CHICKEN_GIZZARD)):
            source = NpcGlobalInventorySlot.objects.create(bag=self.jack_inv,
                slot_index=self.jack_inv.slots.count() + 20, item_id=item_id,
                item_amount=1, quality=3, produced_world_minute=600)
            work = NpcCommandState.objects.create(command_id=uuid.uuid4(), request_id=uuid.uuid4(),
                message_id=uuid.uuid4(), npc=self.jack, kind="PROCESS_GAME", status="ACCEPTED",
                sanitized_parameters=dict(prey_instance_id=str(source.instance_id)),
                duration_minutes=5, production_state=dict(rule_version=1))
            self.assertIsNone(start_work(self.jack_rt, work, 700))
            work.status, work.started_world_minute = "RUNNING", 700
            work.save()
            answer = finish_work(10012, {}, 707, work.request_id)
            self.assertEqual(answer["status"], "SUCCEEDED", answer)
            self.assertTrue(self.jack_inv.slots.filter(item_id=expected, source_request_id=work.request_id).exists())
            work.status = "SUCCEEDED"
            work.save(update_fields=["status"])
        cmd = NpcCommandState.objects.create(command_id=uuid.uuid4(), request_id=uuid.uuid4(),
            message_id=uuid.uuid4(), npc=self.jack, kind="PROCESS_GAME", status="ACCEPTED",
            sanitized_parameters=dict(prey_instance_id=str(prey.instance_id)),
            duration_minutes=5, production_state=dict(rule_version=1))
        self.assertIsNone(start_work(self.jack_rt, cmd, 700))
        self.assertEqual(cmd.duration_minutes, 7)
        cmd.status, cmd.started_world_minute = "RUNNING", 700
        cmd.save()
        VillageSimulationConfig.objects.filter(config_key="default").update(career_development_enabled=False)
        self.assertFalse(content_access(DEER_HIDE)["can_produce"])
        finished = finish_work(10012, {}, 707, cmd.request_id)
        self.assertEqual(finished["status"], "SUCCEEDED", finished)
        skin = self.jack_inv.slots.get(item_id=DEER_HIDE)
        self.assertEqual((skin.item_amount, skin.quality, skin.produced_world_minute), (1, 4, 707))
        prey.refresh_from_db()
        self.assertEqual(prey.item_amount, 0)
        self.assertFalse(self.jack_inv.slots.filter(item_id=29105, source_request_id=cmd.request_id,
            item_amount__gt=0).exists())
        original_tomato = Item.objects.get(pk=TOMATO)
        original_tomato.item_price = 17
        original_tomato.save(update_fields=["item_price"])
        install_catalog()
        self.assertEqual(Item.objects.get(pk=TOMATO).item_price, 17)
