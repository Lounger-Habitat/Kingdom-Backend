import uuid

from django.test import TestCase

from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot
from items.models import Item, ItemType
from village.models import NpcActionReservation, NpcCommandState, NpcNeedsDaySummary, NpcProfile, NpcRuntimeState, VillageActionReceipt, VillageDeliveryOutbox, VillageLocationDefinition, VillageSimulationConfig
from village.phase2_services import LifeActionService, runtime_summary
from village.runtime_services import CommandService, ResultCode


class LifeActionServiceTests(TestCase):
    def setUp(self):
        VillageSimulationConfig.objects.create(config_key="default", village_needs_enabled=True)
        for code in ("BILL_HOME", "JACK_HOME", "RESTAURANT", "BILL_WORK"):
            VillageLocationDefinition.objects.create(location_code=code, display_name=code, center_anchor_code=code, location_type="home")
        self.npc, _ = NpcDefinition.objects.get_or_create(agent_id="10011", defaults={"name": "bill"})
        self.other, _ = NpcDefinition.objects.get_or_create(agent_id="10012", defaults={"name": "jack"})
        self.runtime, _ = NpcRuntimeState.objects.get_or_create(npc=self.npc, defaults={"current_location_code": "BILL_HOME"})
        self.other_runtime, _ = NpcRuntimeState.objects.get_or_create(npc=self.other, defaults={"current_location_code": "BILL_HOME"})
        self.runtime.current_location_code = self.other_runtime.current_location_code = "BILL_HOME"
        self.runtime.needs_world_minute = self.other_runtime.needs_world_minute = 0
        self.runtime.state_version = self.other_runtime.state_version = 0
        self.runtime.save(); self.other_runtime.save()
        NpcProfile.objects.update_or_create(npc=self.npc, defaults={"display_name": "bill", "occupation": "farmer", "home_location_code": "BILL_HOME", "work_location_code": "BILL_WORK", "social_location_code": "BILL_HOME"})
        NpcProfile.objects.update_or_create(npc=self.other, defaults={"display_name": "jack", "occupation": "hunter", "home_location_code": "JACK_HOME", "work_location_code": "BILL_WORK", "social_location_code": "BILL_HOME"})
        self.inventory, _ = NpcGlobalInventory.objects.get_or_create(npc=self.npc)
        Item.objects.update_or_create(item_id=29001, defaults={"item_name": "food", "item_type": ItemType.FOOD, "is_deleted": False})
        self.slot, _ = NpcGlobalInventorySlot.objects.update_or_create(bag=self.inventory, slot_index=999, defaults={"item_id": 29001, "item_amount": 1})
        time_state = GlobalGameTimeState.get_instance(); time_state.world_minute = 120; time_state.time_revision = 2; time_state.save(update_fields=["world_minute", "time_revision"])

    def payload(self, action, params, *, npc_id="10011", location="BILL_HOME", version=0):
        return {"request_id": str(uuid.uuid4()), "command_id": str(uuid.uuid4()), "npc_id": npc_id, "action": action, "start_world_minute": 0, "completion_world_minute": 30, "location_code": location, "expected_state_version": version, "params": params}

    def arm(self, payload):
        command = NpcCommandState.objects.create(
            command_id=uuid.UUID(payload["command_id"]), message_id=uuid.uuid4(), request_id=uuid.UUID(payload["request_id"]),
            npc=self.npc, schema_version="village.command.v1", sender_id="10011", recipient_id="env", kind=payload["action"],
            payload_sha256="test", status="RUNNING", started_world_minute=payload["start_world_minute"],
            duration_minutes=payload["completion_world_minute"] - payload["start_world_minute"],
        )
        self.runtime.current_action = payload["action"]
        self.runtime.action_status = "SLEEPING" if payload["action"] == "SLEEP" else "ACTING"
        self.runtime.active_command_id = command.command_id
        self.runtime.action_ends_world_minute = payload["completion_world_minute"]
        self.runtime.save()
        if payload["action"] == "SOCIALIZE":
            reservation = NpcActionReservation.objects.create(
                reservation_id=uuid.UUID(payload["params"]["reservation_id"]), action_kind="SOCIALIZE",
                location_code="BILL_HOME", active_lock_key="social:10011:10012", status="HELD",
                acquired_world_minute=0, expires_world_minute=30, command_id=command.command_id, request_id=command.request_id,
            )
            reservation.participants.set([self.npc, self.other])

    def test_personal_eat_is_atomic_and_replay_does_not_consume_twice(self):
        payload = self.payload("EAT", {"source": "PERSONAL_INVENTORY", "food_instance_id": str(self.slot.instance_id), "expected_asset_version": 0})
        self.arm(payload)
        result = LifeActionService.execute(payload)
        self.assertTrue(result["success"])
        self.slot.refresh_from_db(); self.inventory.refresh_from_db(); self.runtime.refresh_from_db()
        self.assertEqual((self.slot.item_amount, self.inventory.asset_version), (0, 1))
        self.assertEqual(self.runtime.hunger_fp, 0)
        replay = LifeActionService.execute(payload)
        self.assertEqual(replay["result_code"], ResultCode.COMMAND_REPLAYED)
        self.slot.refresh_from_db(); self.assertEqual(self.slot.item_amount, 0)

    def test_socialize_uses_both_runtimes_without_relationships(self):
        payload = self.payload("SOCIALIZE", {"target_npc_id": "10012", "reservation_id": str(uuid.uuid4())})
        self.arm(payload)
        result = LifeActionService.execute(payload)
        self.assertTrue(result["success"])
        self.assertEqual(result["effects"]["relationships"], [])
        self.other_runtime.refresh_from_db()
        self.assertEqual(self.other_runtime.needs_world_minute, 30)

    def test_socialize_diminishing_return_clamps_once_and_cannot_cross_day(self):
        self.runtime.social_fp = self.runtime.stress_fp = 10
        self.other_runtime.social_fp = self.other_runtime.stress_fp = 10
        self.runtime.save(update_fields=["social_fp", "stress_fp"])
        self.other_runtime.save(update_fields=["social_fp", "stress_fp"])
        from village.models import NpcSocialDailyCounter
        NpcSocialDailyCounter.objects.create(
            low_npc=self.npc, high_npc=self.other, day_index=0, socialize_count=1,
        )
        payload = self.payload(
            "SOCIALIZE",
            {"target_npc_id": "10012", "reservation_id": str(uuid.uuid4())},
        )
        self.arm(payload)
        result = LifeActionService.execute(payload)
        self.assertTrue(result["success"])
        self.runtime.refresh_from_db(); self.other_runtime.refresh_from_db()
        self.assertEqual((self.runtime.social_fp, self.runtime.stress_fp), (0, 0))
        self.assertEqual((self.other_runtime.social_fp, self.other_runtime.stress_fp), (0, 0))

        cross_day = self.payload(
            "SOCIALIZE",
            {"target_npc_id": "10012", "reservation_id": str(uuid.uuid4())},
        )
        cross_day["start_world_minute"] = 1410
        cross_day["completion_world_minute"] = 1470
        time_state = GlobalGameTimeState.get_instance()
        time_state.world_minute = 1470
        time_state.save(update_fields=["world_minute"])
        self.runtime.refresh_from_db(); self.other_runtime.refresh_from_db()
        self.runtime.needs_world_minute = self.other_runtime.needs_world_minute = 1410
        self.runtime.save(update_fields=["needs_world_minute"])
        self.other_runtime.save(update_fields=["needs_world_minute"])
        cross_day["expected_state_version"] = self.runtime.state_version
        self.arm(cross_day)
        self.assertEqual(
            LifeActionService.execute(cross_day)["result_code"],
            ResultCode.INVALID_ACTION_DURATION,
        )

    def test_socialize_keeps_unscaled_severe_hunger_stress(self):
        for participant in (self.runtime, self.other_runtime):
            participant.hunger_fp = 900
            participant.social_fp = 500
            participant.stress_fp = 500
            participant.save(update_fields=["hunger_fp", "social_fp", "stress_fp"])
        payload = self.payload(
            "SOCIALIZE",
            {"target_npc_id": "10012", "reservation_id": str(uuid.uuid4())},
        )
        self.arm(payload)
        result = LifeActionService.execute(payload)
        self.assertTrue(result["success"])
        self.runtime.refresh_from_db(); self.other_runtime.refresh_from_db()
        self.assertEqual((self.runtime.stress_fp, self.other_runtime.stress_fp), (490, 490))

    def test_work_has_no_assets_and_stale_asset_version_has_no_food_benefit(self):
        stale = self.payload("EAT", {"source": "PERSONAL_INVENTORY", "food_instance_id": str(self.slot.instance_id), "expected_asset_version": 1})
        self.arm(stale)
        self.assertEqual(LifeActionService.execute(stale)["result_code"], ResultCode.ASSET_VERSION_CONFLICT)
        self.runtime.refresh_from_db(); self.assertEqual(self.runtime.hunger_fp, 200)
        work = self.payload("WORK_PLACEHOLDER", {}, location="BILL_WORK", version=self.runtime.state_version)
        self.runtime.current_location_code = "BILL_WORK"; self.runtime.save(update_fields=["current_location_code"])
        self.arm(work)
        result = LifeActionService.execute(work)
        self.assertTrue(result["success"])
        self.assertEqual((result["effects"]["items"], result["effects"]["money"]), ([], []))
        self.assertEqual(result["effects"]["needs"], {"hunger": 3.0, "energy": -4.0, "social": 1.0, "stress": 2.0})

    def test_work_rejects_frozen_need_thresholds_without_advancing_needs(self):
        cases = (
            ("hunger_fp", 900, ResultCode.HUNGER_TOO_HIGH),
            ("energy_fp", 100, ResultCode.ENERGY_TOO_LOW),
            ("health_fp", 300, ResultCode.HEALTH_TOO_LOW),
        )
        for field, value, expected_code in cases:
            with self.subTest(field=field):
                self.runtime.refresh_from_db()
                setattr(self.runtime, field, value)
                self.runtime.current_location_code = "BILL_WORK"
                self.runtime.save(update_fields=[field, "current_location_code"])
                payload = self.payload(
                    "WORK_PLACEHOLDER", {}, location="BILL_WORK",
                    version=self.runtime.state_version,
                )
                self.arm(payload)
                result = LifeActionService.execute(payload)
                self.assertEqual(result["result_code"], expected_code)
                self.runtime.refresh_from_db()
                self.assertEqual(self.runtime.needs_world_minute, 0)
                self.assertEqual(self.runtime.action_status, "IDLE")
                setattr(self.runtime, field, {"hunger_fp": 200, "energy_fp": 800, "health_fp": 1000}[field])
                self.runtime.save(update_fields=[field])

    def test_phase_one_accept_start_then_backend_life_completion(self):
        time_state = GlobalGameTimeState.get_instance()
        time_state.world_minute = 0; time_state.time_revision = 0; time_state.save(update_fields=["world_minute", "time_revision"])
        self.runtime.current_location_code = "BILL_WORK"; self.runtime.save(update_fields=["current_location_code"])
        request_id, command_id = uuid.uuid4(), uuid.uuid4()
        content = {
            "schema_version": "village.command.v1", "id": str(command_id), "request_id": str(request_id),
            "name": "work_placeholder", "params": {}, "queue_mode": "STRICT",
            "expected_state_version": 0, "ttl_ms": 30_000, "duration_minutes": 30,
            "not_before_world_minute": 0, "expires_world_minute": 60,
        }
        import time
        accepted, _code, command = CommandService.accept_command(str(uuid.uuid4()), "10011", "env", content, int(time.time() * 1000))
        self.assertTrue(accepted)
        started, _code, _command = CommandService.start_command(command.command_id)
        self.assertTrue(started)
        time_state.world_minute = 30; time_state.time_revision = 1; time_state.save(update_fields=["world_minute", "time_revision"])
        payload = {"request_id": str(request_id), "command_id": str(command_id), "npc_id": "10011", "action": "WORK_PLACEHOLDER", "start_world_minute": 0, "completion_world_minute": 30, "location_code": "BILL_WORK", "expected_state_version": 0, "params": {}}
        result = LifeActionService.execute(payload)
        self.assertTrue(result["success"])
        command.refresh_from_db(); self.runtime.refresh_from_db()
        self.assertEqual(command.status, "SUCCEEDED")
        self.assertEqual((self.runtime.action_status, self.runtime.active_command_id), ("IDLE", None))

    def test_eat_effect_precedes_exact_day_boundary_health_finalization(self):
        time_state = GlobalGameTimeState.get_instance()
        time_state.world_minute = 1440
        time_state.time_revision = 3
        time_state.save(update_fields=["world_minute", "time_revision"])
        self.runtime.needs_world_minute = 1410
        self.runtime.health_fp = 900
        self.runtime.save(update_fields=["needs_world_minute", "health_fp"])
        NpcNeedsDaySummary.objects.create(npc=self.npc, day_index=0, sleep_minutes=360)
        payload = self.payload(
            "EAT",
            {
                "source": "PERSONAL_INVENTORY",
                "food_instance_id": str(self.slot.instance_id),
                "expected_asset_version": 0,
            },
        )
        payload["start_world_minute"] = 1410
        payload["completion_world_minute"] = 1440
        self.arm(payload)

        result = LifeActionService.execute(payload)

        self.assertTrue(result["success"])
        self.runtime.refresh_from_db()
        summary = NpcNeedsDaySummary.objects.get(npc=self.npc, day_index=0)
        self.assertEqual(self.runtime.health_fp, 920)
        self.assertEqual((summary.successful_meals, summary.finalized), (1, True))

    def test_exact_day_boundary_community_meal_uses_the_action_day(self):
        time_state = GlobalGameTimeState.get_instance()
        time_state.world_minute = 1440
        time_state.save(update_fields=["world_minute"])
        self.runtime.current_location_code = "RESTAURANT"
        self.runtime.needs_world_minute = 1410
        self.runtime.save(update_fields=["current_location_code", "needs_world_minute"])
        payload = self.payload("EAT", {"source": "COMMUNITY_MEAL"}, location="RESTAURANT")
        payload["start_world_minute"] = 1410
        payload["completion_world_minute"] = 1440
        self.arm(payload)
        result = LifeActionService.execute(payload)
        self.assertTrue(result["success"])
        from village.models import VillageCommunityMealDailyUsage
        usage = VillageCommunityMealDailyUsage.objects.get()
        self.assertEqual(usage.day_index, 0)

    def test_success_wire_envelopes_match_phase_one_shape_and_hide_internal_hash(self):
        payload = self.payload(
            "EAT",
            {"source": "PERSONAL_INVENTORY", "food_instance_id": str(self.slot.instance_id), "expected_asset_version": 0},
        )
        self.arm(payload)
        result = LifeActionService.execute(payload)
        self.assertTrue(result["success"])
        self.assertNotIn("request_payload_sha256", result)
        self.assertEqual(result["reason_code"], "")
        outcome = VillageDeliveryOutbox.objects.get(message_kind="OUTCOME").payload
        event = VillageDeliveryOutbox.objects.get(message_kind="EVENT").payload
        self.assertTrue({
            "schema_version", "ref_id", "original_message_id", "request_id", "npc_id",
            "success", "result_code", "world_event_id", "world_minute", "time_revision",
            "state_version", "effects",
        }.issubset(outcome))
        self.assertTrue({
            "schema_version", "event_id", "event_sequence", "event_type", "actor_npc_id",
            "participant_npc_ids", "location_code", "command_id", "request_id", "public_data",
        }.issubset(event))
        self.assertIsInstance(result["effects"]["items"], list)
        self.assertIsInstance(result["effects"]["relationships"], list)
        self.assertEqual(result["effects"]["needs"], {"hunger": -45.0, "energy": 5.0})

    def test_runtime_summary_exposes_shared_active_reservation(self):
        payload = self.payload(
            "SOCIALIZE",
            {"target_npc_id": "10012", "reservation_id": str(uuid.uuid4())},
        )
        self.arm(payload)
        actor = runtime_summary("10011")
        target = runtime_summary("10012")
        self.assertEqual(actor["active_reservation"]["reservation_id"], payload["params"]["reservation_id"])
        self.assertEqual(actor["active_reservation"], target["active_reservation"])

    def test_accept_rejects_malformed_phase2_command_before_it_can_run(self):
        import time
        base = {
            "schema_version": "village.command.v1", "id": str(uuid.uuid4()),
            "request_id": str(uuid.uuid4()), "name": "REST", "params": {},
            "queue_mode": "STRICT", "expected_state_version": 0,
            "not_before_world_minute": 0, "expires_world_minute": 60,
            "duration_minutes": 30, "ttl_ms": 30_000,
        }
        with_needs = {**base, "id": str(uuid.uuid4()), "request_id": str(uuid.uuid4()), "energy": 50}
        ok, code, _ = CommandService.accept_command(
            str(uuid.uuid4()), "10011", "env", with_needs, int(time.time() * 1000)
        )
        self.assertFalse(ok)
        self.assertEqual(code, ResultCode.NEEDS_FIELDS_NOT_ALLOWED)

        bad_duration = {**base, "id": str(uuid.uuid4()), "request_id": str(uuid.uuid4()), "duration_minutes": 25}
        ok, code, _ = CommandService.accept_command(
            str(uuid.uuid4()), "10011", "env", bad_duration, int(time.time() * 1000)
        )
        self.assertFalse(ok)
        self.assertEqual(code, ResultCode.INVALID_ACTION_DURATION)

    def test_start_rejects_stale_needs_cursor_and_need_threshold_before_running(self):
        import time
        time_state = GlobalGameTimeState.get_instance()
        time_state.world_minute = 30
        time_state.save(update_fields=["world_minute"])
        self.runtime.needs_world_minute = 0
        self.runtime.hunger_fp = 900
        self.runtime.current_location_code = "BILL_WORK"
        self.runtime.save(update_fields=["needs_world_minute", "hunger_fp", "current_location_code"])
        content = {
            "schema_version": "village.command.v1", "id": str(uuid.uuid4()),
            "request_id": str(uuid.uuid4()), "name": "WORK_PLACEHOLDER", "params": {},
            "queue_mode": "STRICT", "expected_state_version": self.runtime.state_version,
            "not_before_world_minute": 30, "expires_world_minute": 90,
            "duration_minutes": 30, "ttl_ms": 30_000,
        }
        accepted, _, command = CommandService.accept_command(
            str(uuid.uuid4()), "10011", "env", content, int(time.time() * 1000),
        )
        self.assertTrue(accepted)
        started, code, _ = CommandService.start_command(command.command_id)
        self.assertFalse(started)
        self.assertEqual(code, ResultCode.STALE_NEEDS_MINUTE)
        command.refresh_from_db(); self.runtime.refresh_from_db()
        self.assertEqual((command.status, self.runtime.action_status), ("FAILED", "IDLE"))

        # Once the cursor is current, the frozen hunger threshold is the next
        # authoritative gate and is also terminalized before RUNNING.
        self.runtime.needs_world_minute = 30
        self.runtime.save(update_fields=["needs_world_minute"])
        content["id"] = str(uuid.uuid4()); content["request_id"] = str(uuid.uuid4())
        content["expected_state_version"] = self.runtime.state_version
        accepted, _, command = CommandService.accept_command(
            str(uuid.uuid4()), "10011", "env", content, int(time.time() * 1000),
        )
        self.assertTrue(accepted)
        started, code, _ = CommandService.start_command(command.command_id)
        self.assertFalse(started)
        self.assertEqual(code, ResultCode.HUNGER_TOO_HIGH)

    def test_socialize_start_checks_the_target_cursor_and_health(self):
        import time
        time_state = GlobalGameTimeState.get_instance()
        time_state.world_minute = 0
        time_state.save(update_fields=["world_minute"])
        self.other_runtime.health_fp = 300
        self.other_runtime.save(update_fields=["health_fp"])
        content = {
            "schema_version": "village.command.v1", "id": str(uuid.uuid4()),
            "request_id": str(uuid.uuid4()), "name": "SOCIALIZE",
            "params": {"target_npc_id": "10012", "reservation_id": str(uuid.uuid4())},
            "queue_mode": "STRICT", "expected_state_version": self.runtime.state_version,
            "not_before_world_minute": 0, "expires_world_minute": 60,
            "duration_minutes": 30, "ttl_ms": 30_000,
        }
        accepted, _, command = CommandService.accept_command(
            str(uuid.uuid4()), "10011", "env", content, int(time.time() * 1000),
        )
        self.assertTrue(accepted)
        started, code, _ = CommandService.start_command(command.command_id)
        self.assertFalse(started)
        self.assertEqual(code, ResultCode.HEALTH_TOO_LOW)
        self.assertFalse(NpcActionReservation.objects.filter(status="HELD").exists())

    def test_no_food_terminalizes_once_without_needs_or_asset_side_effect(self):
        self.slot.item_amount = 0; self.slot.save(update_fields=["item_amount"])
        payload = self.payload("EAT", {"source": "PERSONAL_INVENTORY", "food_instance_id": str(self.slot.instance_id), "expected_asset_version": 0})
        self.arm(payload)
        result = LifeActionService.execute(payload)
        self.assertFalse(result["success"])
        self.assertEqual(result["result_code"], ResultCode.FOOD_NOT_AVAILABLE)
        self.runtime.refresh_from_db(); self.inventory.refresh_from_db()
        command = NpcCommandState.objects.get(command_id=payload["command_id"])
        receipt = VillageActionReceipt.objects.get(request_id=payload["request_id"])
        self.assertEqual((command.status, self.runtime.action_status, self.runtime.needs_world_minute, self.inventory.asset_version), ("FAILED", "IDLE", 0, 0))
        self.assertFalse(receipt.success)
        self.assertEqual(VillageDeliveryOutbox.objects.filter(receipt=receipt).count(), 2)
        replay = LifeActionService.execute(payload)
        self.assertFalse(replay["success"])
        self.assertEqual(replay["result_code"], ResultCode.FOOD_NOT_AVAILABLE)

    def test_asset_location_and_social_failures_terminalize_and_release(self):
        asset = self.payload("EAT", {"source": "PERSONAL_INVENTORY", "food_instance_id": str(self.slot.instance_id), "expected_asset_version": 9})
        self.arm(asset)
        self.assertEqual(LifeActionService.execute(asset)["result_code"], ResultCode.ASSET_VERSION_CONFLICT)
        self.runtime.refresh_from_db(); self.assertEqual(self.runtime.action_status, "IDLE")

        location = self.payload("WORK_PLACEHOLDER", {}, location="BILL_WORK", version=self.runtime.state_version)
        self.arm(location)
        self.assertEqual(LifeActionService.execute(location)["result_code"], ResultCode.INVALID_ACTION_LOCATION)
        self.runtime.refresh_from_db(); self.assertEqual(self.runtime.action_status, "IDLE")

        social = self.payload("SOCIALIZE", {"target_npc_id": "10012", "reservation_id": str(uuid.uuid4())}, version=self.runtime.state_version)
        self.arm(social)
        self.other_runtime.action_status = "MOVING"; self.other_runtime.save(update_fields=["action_status"])
        self.assertEqual(LifeActionService.execute(social)["result_code"], ResultCode.SOCIAL_TARGET_BUSY)
        reservation = NpcActionReservation.objects.get(reservation_id=social["params"]["reservation_id"])
        self.runtime.refresh_from_db()
        self.assertEqual((self.runtime.action_status, reservation.status, reservation.active_lock_key), ("IDLE", "RELEASED", None))
