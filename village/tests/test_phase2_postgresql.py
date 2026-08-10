"""Real PostgreSQL contention acceptance checks.

Run only with ``config.settings_postgres_test``.  Each contender enters from
its own thread-local connection and waits at a barrier immediately before the
transactional service call.
"""

import threading
import uuid

from django.db import connection, connections
from django.test import TransactionTestCase

from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot
from items.models import Item, ItemType
from village.models import (
    NpcActionReservation,
    NpcCommandState,
    NpcNeedsDaySummary,
    NpcProfile,
    NpcRuntimeState,
    NpcSocialDailyCounter,
    VillageActionReceipt,
    VillageLocationDefinition,
    VillageSimulationConfig,
)
from village.phase2_services import LifeActionService, NeedsService
from village.runtime_services import CommandService, ResultCode


class PostgreSQLPhaseTwoConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("requires config.settings_postgres_test")
        VillageSimulationConfig.objects.create(config_key="default", village_needs_enabled=True)
        for code in ("HOME", "SOCIAL"):
            VillageLocationDefinition.objects.create(location_code=code, display_name=code, center_anchor_code=code, location_type="home")
        self.bill = NpcDefinition.objects.create(agent_id="10011", name="bill")
        self.jack = NpcDefinition.objects.create(agent_id="10012", name="jack")
        self.bill_rt = NpcRuntimeState.objects.create(npc=self.bill, current_location_code="HOME")
        self.jack_rt = NpcRuntimeState.objects.create(npc=self.jack, current_location_code="HOME")
        for npc, home in ((self.bill, "HOME"), (self.jack, "HOME")):
            NpcProfile.objects.create(npc=npc, display_name=npc.name, occupation="test", home_location_code=home, work_location_code=home, social_location_code="SOCIAL")
        time = GlobalGameTimeState.get_instance()
        time.world_minute = 30
        time.time_revision = 1
        time.save(update_fields=["world_minute", "time_revision"])

    def _run_contenders(self, calls):
        barrier = threading.Barrier(len(calls))
        results, failures = [], []
        result_lock = threading.Lock()

        def runner(label, call):
            connections.close_all()
            try:
                barrier.wait(timeout=10)
                outcome = call()
                with result_lock:
                    results.append((label, outcome))
            except BaseException as exc:  # surface a deadlock/timeout as test failure
                with result_lock:
                    failures.append((label, exc))
            finally:
                connections.close_all()

        threads = [threading.Thread(target=runner, args=(label, call), daemon=True) for label, call in calls]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertFalse([thread.name for thread in threads if thread.is_alive()], "PostgreSQL lock deadlock")
        self.assertFalse(failures, failures)
        return dict(results)

    def _arm_eat(self):
        bag = NpcGlobalInventory.objects.create(npc=self.bill)
        Item.objects.create(item_id=29001, item_name="food", item_type=ItemType.FOOD)
        slot = NpcGlobalInventorySlot.objects.create(bag=bag, slot_index=0, item_id=29001, item_amount=1)
        request_id, command_id = uuid.uuid4(), uuid.uuid4()
        NpcCommandState.objects.create(
            command_id=command_id, message_id=uuid.uuid4(), request_id=request_id,
            npc=self.bill, schema_version="village.command.v1", sender_id="10011", recipient_id="env",
            kind="EAT", payload_sha256="test", status="RUNNING", started_world_minute=0, duration_minutes=30,
        )
        self.bill_rt.active_command_id = command_id
        self.bill_rt.current_action = "EAT"
        self.bill_rt.action_status = "ACTING"
        self.bill_rt.action_ends_world_minute = 30
        self.bill_rt.save()
        return request_id, command_id, slot

    def test_tick_and_personal_eat_contention_is_single_authority_write(self):
        request_id, command_id, slot = self._arm_eat()
        tick_payload = {
            "npc_id": "10011", "advance_to_world_minute": 30, "expected_state_version": 0,
            "tick_id": str(uuid.uuid4()),
            "activity_intervals": [{"interval_id": str(uuid.uuid4()), "start_world_minute": 0, "end_world_minute": 30, "activity": "IDLE"}],
        }
        eat_payload = {
            "request_id": str(request_id), "command_id": str(command_id), "npc_id": "10011", "action": "EAT",
            "start_world_minute": 0, "completion_world_minute": 30, "location_code": "HOME", "expected_state_version": 0,
            "params": {"source": "PERSONAL_INVENTORY", "food_instance_id": str(slot.instance_id), "expected_asset_version": 0},
        }
        outcomes = self._run_contenders([
            ("tick", lambda: NeedsService.advance(tick_payload)),
            ("eat", lambda: LifeActionService.execute(eat_payload)),
        ])
        successful = [label for label, result in outcomes.items() if result["success"]]
        self.assertEqual(len(successful), 1, outcomes)
        self.assertIn(outcomes["tick"]["result_code"], {ResultCode.LIFE_ACTION_COMPLETED, ResultCode.STALE_STATE_VERSION})
        self.assertIn(outcomes["eat"]["result_code"], {ResultCode.LIFE_ACTION_COMPLETED, ResultCode.STALE_STATE_VERSION})
        slot.refresh_from_db(); self.bill_rt.refresh_from_db()
        self.assertIn(slot.item_amount, {0, 1})
        self.assertEqual(self.bill_rt.needs_world_minute, 30)
        self.assertEqual(VillageActionReceipt.objects.filter(npc=self.bill).count(), 1)
        self.assertLessEqual(NpcNeedsDaySummary.objects.filter(npc=self.bill).count(), 1)

    def test_same_tick_id_concurrently_replays_one_receipt(self):
        tick_payload = {
            "npc_id": "10011", "advance_to_world_minute": 30, "expected_state_version": 0,
            "tick_id": str(uuid.uuid4()),
            "activity_intervals": [{
                "interval_id": str(uuid.uuid4()), "start_world_minute": 0,
                "end_world_minute": 30, "activity": "IDLE",
            }],
        }
        outcomes = self._run_contenders([
            ("first", lambda: NeedsService.advance(tick_payload)),
            ("second", lambda: NeedsService.advance(tick_payload)),
        ])
        self.assertTrue(all(result["success"] for result in outcomes.values()), outcomes)
        self.assertEqual(
            {result["result_code"] for result in outcomes.values()},
            {ResultCode.LIFE_ACTION_COMPLETED, ResultCode.COMMAND_REPLAYED},
        )
        self.assertEqual(VillageActionReceipt.objects.filter(request_id=tick_payload["tick_id"]).count(), 1)

    def test_same_eat_request_concurrently_consumes_once_and_replays(self):
        request_id, command_id, slot = self._arm_eat()
        payload = {
            "request_id": str(request_id), "command_id": str(command_id), "npc_id": "10011",
            "action": "EAT", "start_world_minute": 0, "completion_world_minute": 30,
            "location_code": "HOME", "expected_state_version": 0,
            "params": {"source": "PERSONAL_INVENTORY", "food_instance_id": str(slot.instance_id), "expected_asset_version": 0},
        }
        outcomes = self._run_contenders([
            ("first", lambda: LifeActionService.execute(payload)),
            ("second", lambda: LifeActionService.execute(payload)),
        ])
        self.assertTrue(all(result["success"] for result in outcomes.values()), outcomes)
        self.assertEqual(
            {result["result_code"] for result in outcomes.values()},
            {ResultCode.LIFE_ACTION_COMPLETED, ResultCode.COMMAND_REPLAYED},
        )
        slot.refresh_from_db()
        self.assertEqual(slot.item_amount, 0)
        self.assertEqual(VillageActionReceipt.objects.filter(request_id=request_id).count(), 1)

    def _social_command(self, actor, target, reservation_id):
        request_id, command_id = uuid.uuid4(), uuid.uuid4()
        command = NpcCommandState.objects.create(
            command_id=command_id, message_id=uuid.uuid4(), request_id=request_id, npc=actor,
            schema_version="village.command.v1", sender_id=str(actor.agent_id), recipient_id="env", kind="SOCIALIZE",
            location_code="HOME", sanitized_parameters={"target_npc_id": str(target.agent_id), "reservation_id": str(reservation_id)},
            payload_sha256="test", status="RECEIVED", duration_minutes=30,
        )
        return command

    def test_reverse_socialize_start_order_does_not_deadlock_or_double_reserve(self):
        time = GlobalGameTimeState.get_instance(); time.world_minute = 0; time.time_revision = 0; time.save(update_fields=["world_minute", "time_revision"])
        first = self._social_command(self.bill, self.jack, uuid.uuid4())
        second = self._social_command(self.jack, self.bill, uuid.uuid4())
        outcomes = self._run_contenders([
            ("bill", lambda: CommandService.start_command(first.command_id)[:2]),
            ("jack", lambda: CommandService.start_command(second.command_id)[:2]),
        ])
        success_count = sum(1 for success, code in outcomes.values() if success and code == ResultCode.COMMAND_ACCEPTED)
        self.assertEqual(success_count, 1, outcomes)
        self.assertEqual(NpcActionReservation.objects.filter(action_kind="SOCIALIZE", status="HELD").count(), 1)
        winner = first if outcomes["bill"][0] else second
        winner.refresh_from_db()
        time = GlobalGameTimeState.get_instance(); time.world_minute = 30; time.time_revision = 2; time.save(update_fields=["world_minute", "time_revision"])
        reservation = NpcActionReservation.objects.get(command_id=winner.command_id)
        completion = {
            "request_id": str(winner.request_id), "command_id": str(winner.command_id), "npc_id": str(winner.npc.agent_id),
            "action": "SOCIALIZE", "start_world_minute": 0, "completion_world_minute": 30, "location_code": "HOME",
            "expected_state_version": 0,
            "params": {"target_npc_id": "10012" if winner.npc_id == self.bill.id else "10011", "reservation_id": str(reservation.reservation_id)},
        }
        completed = LifeActionService.execute(completion)
        self.assertTrue(completed["success"], completed)
        reservation.refresh_from_db()
        self.assertEqual((reservation.status, reservation.active_lock_key), ("COMMITTED", None))
        self.assertEqual(NpcSocialDailyCounter.objects.get(day_index=0).socialize_count, 1)
