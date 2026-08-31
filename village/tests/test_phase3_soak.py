"""Phase 3 30-day accelerated Backend soak test.

Simulates 30 days of production, trade, consumption, and day-close.
Validates conservation invariants across the entire run.
"""

import uuid

from django.test import TestCase

from game_time.models import GlobalGameTimeState
from inventory.models import (
    NpcDefinition,
    NpcGlobalInventory,
    NpcGlobalInventorySlot,
)
from recipes.models import Recipe, RecipeIngredient
from village.models import (
    DailyPriceSnapshot,
    EconomicLedgerEntry,
    NpcRuntimeState,
    NpcTradeDelivery,
    NpcTradeOrder,
    VillageAccount,
    VillageFarmPlot,
    VillageRestaurantState,
    VillageSimulationConfig,
)
from village.phase3_asset_service import commit_economy_action
from village.phase3_contract import (
    VILLAGE_ECONOMY_ACTION_V1,
    DaySnapshotStatus,
    DeliveryStatus,
    OrderStatus,
    ResultCode,
)
from village.phase3_day_close import handle_day_close


def _payload(command_name, npc_id, location, params, request_id=None, wm=700):
    return {
        "schema_version": VILLAGE_ECONOMY_ACTION_V1,
        "command_id": str(uuid.uuid4()),
        "request_id": str(request_id or uuid.uuid4()),
        "message_id": str(uuid.uuid4()),
        "command_name": command_name,
        "npc_id": npc_id,
        "completed_world_minute": wm,
        "time_revision": wm,
        "current_location_code": location,
        "params": params,
    }


def _day_close_request_id(day_index: int) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"phase3:day-close:{day_index}")


def _slot_amount(bag, item_id):
    """Query total amount of item_id in bag."""
    return sum(
        s.item_amount for s in
        NpcGlobalInventorySlot.objects.filter(bag=bag, item_id=item_id)
    )


def _tool_remaining(bag, tool_item_id):
    """Query remaining uses of first tool slot."""
    slot = NpcGlobalInventorySlot.objects.filter(
        bag=bag, item_id=tool_item_id, item_amount__gt=0,
    ).exclude(remaining_uses=0).first()
    return slot.remaining_uses if slot else 0


class Phase3SoakTest(TestCase):
    """30-day accelerated economy loop test."""

    def test_30_day_economy_loop(self):
        """Run 30 days of production, trade, consumption, day-close."""
        VillageSimulationConfig.objects.create(
            config_key="default", village_economy_enabled=True,
        )
        time = GlobalGameTimeState.get_instance()
        time.world_minute = 700
        time.time_revision = 700
        time.save()

        # NPCs
        bill, _ = NpcDefinition.objects.get_or_create(agent_id="10011", defaults={"name": "bill"})
        jack, _ = NpcDefinition.objects.get_or_create(agent_id="10012", defaults={"name": "jack"})
        mark, _ = NpcDefinition.objects.get_or_create(agent_id="10013", defaults={"name": "mark"})
        lucy, _ = NpcDefinition.objects.get_or_create(agent_id="10014", defaults={"name": "lucy"})
        nora, _ = NpcDefinition.objects.get_or_create(agent_id="10015", defaults={"name": "nora"})

        # Inventories
        bill_inv, _ = NpcGlobalInventory.objects.get_or_create(npc=bill, defaults={"money": 200})
        jack_inv, _ = NpcGlobalInventory.objects.get_or_create(npc=jack, defaults={"money": 240})
        mark_inv, _ = NpcGlobalInventory.objects.get_or_create(npc=mark, defaults={"money": 120})
        lucy_inv, _ = NpcGlobalInventory.objects.get_or_create(npc=lucy, defaults={"money": 300})
        nora_inv, _ = NpcGlobalInventory.objects.get_or_create(npc=nora, defaults={"money": 450})
        inventories = [bill_inv, jack_inv, mark_inv, lucy_inv, nora_inv]

        # Runtime states
        for npc, loc in [(bill, "BILL_FARM"), (jack, "HUNTING_GATE"),
                          (mark, "RECORD_HALL"), (lucy, "RESTAURANT"),
                          (nora, "TRADE_ROAD")]:
            NpcRuntimeState.objects.get_or_create(
                npc=npc, defaults={"current_location_code": loc, "health_fp": 1000},
            )

        # Farm plots
        for code in ("FARM_PLOT_A", "FARM_PLOT_B", "FARM_PLOT_C"):
            VillageFarmPlot.objects.get_or_create(plot_code=code, defaults={"status": "EMPTY"})

        # PUBLIC_FUND
        account, _ = VillageAccount.objects.get_or_create(
            account_code="PUBLIC_FUND", defaults={"balance": 500, "last_closed_day_index": -1},
        )

        # Recipes
        recipe2, _ = Recipe.objects.update_or_create(
            pk=2, defaults={"item_id": 1053, "recipe_name": "辣子鸡丁"})
        RecipeIngredient.objects.get_or_create(
            recipe=recipe2, item_id=1056, defaults={"item_name": "鸡肉", "count": 2},
        )
        recipe12, _ = Recipe.objects.update_or_create(
            pk=12, defaults={"item_id": 1067, "recipe_name": "奶酪焗玉米"})
        RecipeIngredient.objects.get_or_create(
            recipe=recipe12, item_id=1030, defaults={"item_name": "玉米", "count": 1},
        )

        # Clear existing slots
        NpcGlobalInventorySlot.objects.filter(bag__in=inventories).delete()

        # Initial inventory
        NpcGlobalInventorySlot.objects.create(
            bag=bill_inv, slot_index=0, item_id=4001, item_amount=10, remaining_uses=20,
        )
        NpcGlobalInventorySlot.objects.create(
            bag=bill_inv, slot_index=1, item_id=1051, item_amount=30,
        )
        bill_food = NpcGlobalInventorySlot.objects.create(
            bag=bill_inv, slot_index=2, item_id=1030, item_amount=20,
            produced_world_minute=600,
        )
        NpcGlobalInventorySlot.objects.create(
            bag=jack_inv, slot_index=0, item_id=4002, item_amount=10, remaining_uses=20,
        )
        NpcGlobalInventorySlot.objects.create(
            bag=lucy_inv, slot_index=0, item_id=1056, item_amount=40,
            produced_world_minute=600, expires_world_minute=600 + 100000,
        )
        NpcGlobalInventorySlot.objects.create(
            bag=nora_inv, slot_index=0, item_id=1040, item_amount=10,
        )
        NpcGlobalInventorySlot.objects.create(
            bag=nora_inv, slot_index=1, item_id=1026, item_amount=10,
        )

        def _total_money():
            return sum(inv.money for inv in inventories) + account.balance

        initial_total_money = _total_money()
        total_external_delta = 0
        harvested_batches = set()

        def _track(resp):
            """Accumulate external_delta from any economy response."""
            nonlocal total_external_delta
            if resp and resp.get("result_code") == ResultCode.OK:
                total_external_delta += resp.get("external_delta", 0)

        # ── 30-day loop ──────────────────────────────────────────────────────
        for day in range(30):
            current_wm = day * 1440 + 720
            time.world_minute = current_wm
            time.save(update_fields=["world_minute", "time_revision"])

            # ── Plant (Bill) every 3 days ────────────────────────────────────
            if day % 3 == 0:
                if _slot_amount(bill_inv, 1051) > 0 and _tool_remaining(bill_inv, 4001) > 0:
                    bill_inv.refresh_from_db()
                    _track(commit_economy_action(_payload(
                        "PLANT", 10011, "BILL_FARM",
                        {"plot_code": "FARM_PLOT_A", "crop_code": "CORN",
                         "expected_asset_version": bill_inv.asset_version},
                        wm=current_wm,
                    )))

            # ── Hunt (Jack) every 2 days ─────────────────────────────────────
            if day % 2 == 0:
                if _tool_remaining(jack_inv, 4002) > 0:
                    jack_inv.refresh_from_db()
                    _track(commit_economy_action(_payload(
                        "HUNT", 10012, "HUNTING_GATE",
                        {"hunt_code": "HUNT_CHICKEN",
                         "expected_asset_version": jack_inv.asset_version},
                        wm=current_wm,
                    )))

            # ── Cook (Lucy) every day ────────────────────────────────────────
            if _slot_amount(lucy_inv, 1056) >= 2:
                lucy_inv.refresh_from_db()
                _track(commit_economy_action(_payload(
                    "COOK", 10014, "RESTAURANT",
                    {"recipe_id": 2, "expected_asset_version": lucy_inv.asset_version},
                    wm=current_wm,
                )))

            # ── Harvest when mature ──────────────────────────────────────────
            if day > 0 and day % 3 == 0:
                plot = VillageFarmPlot.objects.filter(
                    plot_code="FARM_PLOT_A", status="GROWING",
                ).first()
                if plot and plot.batch_id and plot.mature_world_minute:
                    time.world_minute = plot.mature_world_minute + 1
                    time.save(update_fields=["world_minute", "time_revision"])

                    bill_inv.refresh_from_db()
                    resp = commit_economy_action(_payload(
                        "HARVEST", 10011, "BILL_FARM",
                        {"plot_code": "FARM_PLOT_A",
                         "expected_asset_version": bill_inv.asset_version},
                        wm=plot.mature_world_minute + 1,
                    ))
                    _track(resp)
                    if resp["result_code"] == ResultCode.OK:
                        batch_id = str(plot.batch_id)
                        self.assertNotIn(batch_id, harvested_batches)
                        harvested_batches.add(batch_id)

                    time.world_minute = current_wm
                    time.save(update_fields=["world_minute", "time_revision"])

            # ── Import/Export every 3 days ───────────────────────────────────
            if day > 0 and day % 3 == 0:
                account.refresh_from_db()
                nora_inv.refresh_from_db()
                _track(commit_economy_action(_payload(
                    "IMPORT_GOODS", 10015, "TRADE_ROAD",
                    {"item_id": 1040, "amount": 3},
                    wm=current_wm,
                )))

                nora_corn = _slot_amount(nora_inv, 1030)
                if nora_corn > 10:
                    nora_inv.refresh_from_db()
                    _track(commit_economy_action(_payload(
                        "EXPORT_GOODS", 10015, "TRADE_ROAD",
                        {"item_id": 1030, "amount": min(3, nora_corn - 10),
                         "expected_asset_version": nora_inv.asset_version},
                        wm=current_wm,
                    )))

            # ── EAT (Bill, every 3 days) ─────────────────────────────────────
            if day % 3 == 1:
                bill_inv.refresh_from_db()
                _track(commit_economy_action(_payload(
                    "EAT", 10011, "BILL_FARM",
                    {"source": "PERSONAL_INVENTORY",
                     "food_instance_id": str(bill_food.instance_id)},
                    wm=current_wm,
                )))

            # ── Mark public work (weekly) ────────────────────────────────────
            if day % 7 == 0:
                draft = DailyPriceSnapshot.objects.filter(
                    status=DaySnapshotStatus.DRAFT,
                ).order_by("-day_index").first()
                if draft:
                    _track(commit_economy_action(_payload(
                        "PUBLISH_PRICE_INDEX", 10013, "RECORD_HALL",
                        {"day_index": draft.day_index},
                        wm=current_wm,
                    )))

                _track(commit_economy_action(_payload(
                    "AUDIT_PUBLIC_FUND", 10013, "RECORD_HALL",
                    {"day_index": day},
                    wm=current_wm,
                )))

            # ── Day-close ────────────────────────────────────────────────────
            time.world_minute = (day + 1) * 1440 - 1
            time.save(update_fields=["world_minute", "time_revision"])

            resp = handle_day_close(
                {"day_index": day},
                time.world_minute,
                _day_close_request_id(day),
            )
            self.assertEqual(resp["result_code"], ResultCode.OK,
                f"Day-close failed on day {day}: {resp.get('result_code')}")
            total_external_delta += resp.get("external_delta", 0)

        # ── Final assertions ──────────────────────────────────────────────────

        # 1. All money >= 0
        for inv in inventories:
            inv.refresh_from_db()
            self.assertGreaterEqual(inv.money, 0,
                f"NPC {inv.npc.agent_id} has negative money: {inv.money}")

        # 2. All slot amounts >= 0
        for slot in NpcGlobalInventorySlot.objects.filter(bag__in=inventories):
            self.assertGreaterEqual(slot.item_amount, 0)

        # 3. All tool remaining_uses >= 0
        for slot in NpcGlobalInventorySlot.objects.filter(
            bag__in=inventories, item_id__in=[4001, 4002],
        ):
            if slot.remaining_uses is not None:
                self.assertGreaterEqual(slot.remaining_uses, 0)

        # 4. PUBLIC_FUND >= 0
        account.refresh_from_db()
        self.assertGreaterEqual(account.balance, 0)

        # 5. No permanent reservations
        for d in NpcTradeDelivery.objects.filter(status=DeliveryStatus.ACTIVE):
            self.assertGreater(d.expires_world_minute, time.world_minute)

        # 6. Total money conservation
        final_total_money = _total_money()
        expected_total = initial_total_money + total_external_delta
        self.assertEqual(final_total_money, expected_total,
            f"Money conservation violated: initial={initial_total_money}, "
            f"final={final_total_money}, expected_delta={total_external_delta}, "
            f"actual_delta={final_total_money - initial_total_money}")

        # 7. All published prices > 0
        for snapshot in DailyPriceSnapshot.objects.filter(status=DaySnapshotStatus.PUBLISHED):
            for item_id_str, price in (snapshot.prices or {}).items():
                self.assertGreater(price, 0)

        # 8. Restaurant daily sales <= 2
        for rest_state in VillageRestaurantState.objects.all():
            self.assertLessEqual(rest_state.traveler_sales_count, 2)

        # 9. No duplicate ledger entries per request_id
        request_ids = list(EconomicLedgerEntry.objects.values_list("request_id", flat=True))
        self.assertEqual(len(request_ids), len(set(request_ids)))

        # 10. last_closed_day_index == 29
        account.refresh_from_db()
        self.assertEqual(account.last_closed_day_index, 29)
