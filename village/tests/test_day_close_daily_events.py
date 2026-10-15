"""Daily invitations observe the complete day-close transaction at current time."""
import uuid
from unittest.mock import patch

from inventory.models import NpcGlobalInventory, NpcGlobalInventorySlot
from items.models import Item
from village.daily_events import evaluate_daily_events
from village.lucy_restaurant import list_meal
from village.models import (
    EconomicLedgerEntry, MarkDailySalary, NpcRuntimeState, VillageActionReceipt,
    VillageDailyEvent, VillageDailyEventTemplate, VillageDeliveryOutbox,
    VillageSimulationConfig, VillageStore, VillageStoreAccount, VillageStoreInventorySlot,
    VillageWorldEvent,
)
from village.phase3_contract import ResultCode
from village.phase3_day_close import handle_day_close
from village.tests.test_phase3_day_close import DayCloseTestBase, _day_close_request_id


class DayCloseDailyEventTests(DayCloseTestBase):
    def setUp(self):
        super().setUp()
        VillageDailyEvent.objects.all().delete()
        VillageDailyEventTemplate.objects.all().delete()
        VillageSimulationConfig.objects.filter(config_key="default").update(
            village_story_enabled=True, mark_employed_from_minute=0, mark_daily_salary=100)
        self.mark_inv, _ = NpcGlobalInventory.objects.update_or_create(
            npc=self.mark, defaults={"money": 120})
        Item.objects.update_or_create(pk=1053, defaults={
            "item_name": "辣子鸡丁", "item_type": "食物", "item_price": 25, "sell_percentage": 1})
        self.store, _ = VillageStore.objects.get_or_create(store_code="RESTAURANT_STORE", defaults={
            "display_name": "饭店", "location_code": "RESTAURANT", "operator_npc": self.lucy,
            "status": "OPEN", "minimum_fee": 1})
        self.store_account, _ = VillageStoreAccount.objects.get_or_create(
            store=self.store, defaults={"balance": 0, "minimum_operating_balance": 0})
        dish = NpcGlobalInventorySlot.objects.create(bag=self.lucy_inv, slot_index=0,
            item_id=1053, item_amount=3, produced_world_minute=1000,
            expires_world_minute=4000, dish_quality=3)
        listed = list_meal(10014, {"store_code": "RESTAURANT_STORE", "item_id": 1053,
            "amount": 3, "price_mode": "ORIGINAL", "food_instance_id": str(dish.instance_id)},
            1439, uuid.uuid4())
        self.assertEqual(listed["result_code"], ResultCode.OK, listed)
        self.expired = NpcGlobalInventorySlot.objects.create(bag=self.jack_inv,
            slot_index=0, item_id=1056, item_amount=1, produced_world_minute=0,
            expires_world_minute=1000)
        self.away, _ = NpcRuntimeState.objects.update_or_create(npc=self.bill,
            defaults={"current_location_code": "BILL_FARM", "away_until_world_minute": 1500})
        self.template("AFTER_CLOSE", "10014", 1030)

    def template(self, code, npc_id, item_id):
        return VillageDailyEventTemplate.objects.create(code=code, title="备料机会",
            rule_type="INVENTORY_SHORTAGE", parameters={"npc_id": npc_id, "item_ids": [item_id]},
            summary="现有库存出现缺口", resolved_summary="库存条件已改变")

    def test_evaluation_sees_all_settlements_and_uses_current_authority_minute(self):
        def evaluate_after_settlement(minute):
            self.assertEqual(minute, 2000)  # Closed day 0 ended at 1439.
            self.assertFalse(NpcGlobalInventorySlot.objects.filter(pk=self.expired.pk).exists())
            self.store_account.refresh_from_db()
            self.mark_inv.refresh_from_db()
            self.account.refresh_from_db()
            self.away.refresh_from_db()
            self.assertEqual(self.store_account.balance, 48)
            self.assertEqual(self.mark_inv.money, 220)
            self.assertEqual((self.account.balance, self.account.last_closed_day_index), (402, 0))
            self.assertIsNone(self.away.away_until_world_minute)
            self.assertTrue(MarkDailySalary.objects.filter(day_index=0).exists())
            self.assertTrue(EconomicLedgerEntry.objects.filter(request_id=_day_close_request_id(0)).exists())
            self.assertTrue(VillageActionReceipt.objects.filter(request_id=_day_close_request_id(0)).exists())
            return evaluate_daily_events(minute)

        with patch("village.daily_events.evaluate_daily_events", side_effect=evaluate_after_settlement) as evaluate:
            # A caller's older minute must not backdate newly opened invitations.
            result = handle_day_close({"day_index": 0}, 1800, _day_close_request_id(0))
        self.assertEqual(result["result_code"], ResultCode.OK)
        evaluate.assert_called_once_with(self.time.world_minute)
        invitation = VillageDailyEvent.objects.get(template_id="AFTER_CLOSE")
        self.assertEqual((invitation.status, invitation.opened_world_minute), ("OPEN", 2000))
        opened = VillageWorldEvent.objects.get(event_type="STORY_DAILY_OPENED")
        self.assertEqual(opened.world_minute, 2000)

    def test_replay_and_rejected_day_do_not_evaluate_or_publish_new_invitations(self):
        self.assertEqual(self._close_day(0)["result_code"], ResultCode.OK)
        self.template("PENDING_ON_REPLAY", "10015", 1002)
        before = (VillageDailyEvent.objects.count(), VillageWorldEvent.objects.count(),
                  VillageDeliveryOutbox.objects.count())
        with patch("village.daily_events.evaluate_daily_events") as evaluate:
            replay = self._close_day(0)
            rejected = handle_day_close({"day_index": 0}, self.time.world_minute, uuid.uuid4())
        self.assertTrue(replay["replayed"])
        self.assertEqual(rejected["result_code"], ResultCode.P3_DAY_NOT_READY)
        evaluate.assert_not_called()
        self.assertEqual(before, (VillageDailyEvent.objects.count(), VillageWorldEvent.objects.count(),
                                 VillageDeliveryOutbox.objects.count()))

    def test_failed_evaluation_rolls_back_invitation_and_business_settlement(self):
        before_events = VillageWorldEvent.objects.count()
        before_deliveries = VillageDeliveryOutbox.objects.count()

        def fail_after_evaluation(minute):
            evaluate_daily_events(minute)
            raise RuntimeError("day-close projection failed")

        with patch("village.daily_events.evaluate_daily_events", side_effect=fail_after_evaluation):
            with self.assertRaisesRegex(RuntimeError, "day-close projection failed"):
                self._close_day(0)
        self.account.refresh_from_db()
        self.store_account.refresh_from_db()
        self.mark_inv.refresh_from_db()
        self.away.refresh_from_db()
        self.assertEqual((self.account.balance, self.account.last_closed_day_index), (500, -1))
        self.assertEqual((self.store_account.balance, self.mark_inv.money), (0, 120))
        self.assertEqual(self.away.away_until_world_minute, 1500)
        self.assertTrue(NpcGlobalInventorySlot.objects.filter(pk=self.expired.pk).exists())
        self.assertEqual(sum(VillageStoreInventorySlot.objects.filter(store=self.store)
                             .values_list("item_amount", flat=True)), 3)
        self.assertFalse(VillageDailyEvent.objects.exists())
        self.assertFalse(VillageActionReceipt.objects.filter(request_id=_day_close_request_id(0)).exists())
        self.assertFalse(MarkDailySalary.objects.exists())
        self.assertEqual((VillageWorldEvent.objects.count(), VillageDeliveryOutbox.objects.count()),
                         (before_events, before_deliveries))
