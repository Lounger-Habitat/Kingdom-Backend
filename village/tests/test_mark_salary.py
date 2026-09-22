"""Fixed daily public wages for the externally hired dish reviewer."""
import importlib
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps
from django.db import connection
from inventory.models import NpcGlobalInventory
from village.models import (EconomicLedgerEntry, MarkDailySalary, NpcProfile,
    VillageDeliveryOutbox, VillageSimulationConfig, VillageWorldEvent)
from village.public_finance import reconcile
from village.tests.test_phase3_day_close import DayCloseTestBase


class MarkSalaryTests(DayCloseTestBase):
    def setUp(self):
        super().setUp()
        self.config = VillageSimulationConfig.objects.get(config_key="default")
        self.config.mark_daily_salary = 100
        self.config.mark_employed_from_minute = 1800  # hired during day 1
        self.config.save()
        self.mark_inv, _ = NpcGlobalInventory.objects.update_or_create(npc=self.mark, defaults={"money": 120})
        self.account.audit_opening_balance = self.account.balance
        self.account.save()

    def assert_balances(self, fund, wallet):
        self.account.refresh_from_db()
        self.mark_inv.refresh_from_db()
        self.assertEqual((self.account.balance, self.mark_inv.money), (fund, wallet))
        self.assertTrue(reconcile(self.account)["balance_matches"])

    def test_daily_wage_without_ratings_and_no_retroactive_or_duplicate_payment(self):
        self.assertEqual(self._close_day(0)["result_code"], "OK")
        self.assertFalse(MarkDailySalary.objects.exists())
        result = self._close_day(1)
        self.assertEqual(result["result_code"], "OK")
        self.assertEqual(result["data"]["salary"]["paid"], 100)
        self.assert_balances(400, 220)
        self.assertTrue(self._close_day(1)["replayed"])
        from village.phase3_day_close import handle_day_close
        self.assertEqual(handle_day_close({"day_index": 1}, 2000, uuid.uuid4())["result_code"], "P3_DAY_NOT_READY")
        self.assert_balances(400, 220)
        self.assertEqual(MarkDailySalary.objects.count(), 1)
        event = VillageWorldEvent.objects.get(event_type="MARK_DAILY_SALARY")
        self.assertEqual(event.private_data["paid"], 100)
        self.assertEqual(event.visibility, "PARTICIPANTS")
        self.assertEqual(list(VillageDeliveryOutbox.objects.filter(event=event).values_list("recipient", flat=True)), ["10013"])

    def test_legacy_debt_including_hiring_day_is_never_paid(self):
        for day in (0, 1):
            MarkDailySalary.objects.create(day_index=day, amount_due=9,
                earned_request_id=uuid.uuid4(), earned_world_minute=500)
        self._close_day(0)
        result = self._close_day(1)
        self.assertEqual(result["data"]["salary"]["outstanding"], 0)
        self.assert_balances(400, 220)
        self.assertEqual(list(MarkDailySalary.objects.filter(policy="LEGACY").values_list("amount_paid", flat=True)), [0, 0])

    def test_insufficient_funds_are_debt_then_settled_oldest_first(self):
        self.account.balance = self.account.audit_opening_balance = 50
        self.account.save()
        self._close_day(0)
        result = self._close_day(1)
        self.assertEqual(result["data"]["salary"]["outstanding"], 100)
        self.assert_balances(50, 120)
        # A real funding ledger, so reconciliation must still balance.
        self.account.balance += 250
        self.account.save()
        EconomicLedgerEntry.objects.create(request_id=uuid.uuid4(), entry_type="TEST_FUNDING",
            changes={"PUBLIC_FUND": {"money": 250}}, external_delta=250, world_minute=3000)
        self.time.world_minute = 3500
        self.time.save()
        result = self._close_day(2)
        self.assertEqual(result["data"]["salary"]["paid_days"], [1, 2])
        self.assertEqual(result["data"]["salary"]["outstanding"], 0)
        self.assert_balances(100, 320)

    def test_catchup_uses_closed_day_not_current_world_day(self):
        self.time.world_minute = 5000
        self.time.save()
        for day in range(4):
            self.assertEqual(self._close_day(day)["result_code"], "OK")
        self.assertEqual(list(MarkDailySalary.objects.order_by("day_index").values_list("day_index", flat=True)), [1, 2, 3])
        self.assert_balances(200, 420)

    def test_disabled_employment_produces_no_entitlement(self):
        self.config.mark_employed_from_minute = None
        self.config.save()
        self._close_day(0)
        self._close_day(1)
        self.assertFalse(MarkDailySalary.objects.exists())
        self.assert_balances(500, 120)

    def test_failed_day_close_rolls_back_salary_and_notification(self):
        self._close_day(0)
        with patch("village.phase3_day_close.EconomicLedgerEntry.objects.create", side_effect=RuntimeError("ledger failed")):
            with self.assertRaises(RuntimeError):
                self._close_day(1)
        self.assertFalse(MarkDailySalary.objects.exists())
        self.assertFalse(VillageWorldEvent.objects.filter(event_type="MARK_DAILY_SALARY").exists())
        self.assert_balances(500, 120)
        self.assertEqual(self._close_day(1)["result_code"], "OK")
        self.assert_balances(400, 220)

    def test_hiring_migration_starts_now_and_preserves_legacy_debt(self):
        self.config.mark_employed_from_minute = None
        self.config.save()
        profile, _ = NpcProfile.objects.get_or_create(npc=self.mark)
        debt = MarkDailySalary.objects.create(day_index=0, amount_due=9,
            earned_request_id=uuid.uuid4(), earned_world_minute=500)
        migrate = importlib.import_module("village.migrations.0060_mark_external_reviewer_salary").hire_mark_now
        migrate(apps, SimpleNamespace(connection=connection))
        self.config.refresh_from_db()
        profile.refresh_from_db()
        debt.refresh_from_db()
        self.assertEqual(self.config.mark_employed_from_minute, 2000)
        self.assertEqual(self.config.mark_daily_salary, 100)
        self.assertEqual(profile.occupation, "external_dish_reviewer")
        self.assertEqual((debt.policy, debt.amount_due, debt.amount_paid), ("LEGACY", 9, 0))
        self.time.world_minute = 3000
        self.time.save()
        migrate(apps, SimpleNamespace(connection=connection))
        self.config.refresh_from_db()
        self.assertEqual(self.config.mark_employed_from_minute, 2000)
