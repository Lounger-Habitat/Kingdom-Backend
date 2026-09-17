import json
import uuid
from unittest import mock

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot
from items.models import Item
from village.models import (
    DailyPriceSnapshot,
    EconomicLedgerEntry,
    NpcCommandState,
    NpcCommitment,
    NpcRelationship,
    NpcRuntimeState,
    NpcStoryFactAccess,
    NpcTradeOrder,
    VillageAccount,
    VillageActionReceipt,
    VillageLocationDefinition,
    VillageSimulationConfig,
    VillageStoryArc,
    VillageStoryFact,
    VillageStoryTransitionReceipt,
    VillageStore,
    VillageStoreAccount,
    VillageStoreInventorySlot,
    VillageStoreListing,
    VillageWorldEvent,
)
from village.phase7_services import SCAN_BATCH_SIZE, SCAN_EVENT_BUDGET, public_journal


class Phase7PresentationTests(TestCase):
    def setUp(self):
        self.player = GameAccount.objects.create(username="phase7-player", password="unused")
        self.other = GameAccount.objects.create(username="phase7-other", password="unused")
        self.env = GameAccount.objects.create(username="phase7-env", password="unused")
        self.config = VillageSimulationConfig.objects.create(
            config_key="default",
            village_presentation_enabled=True,
            village_debug_overlay_enabled=True,
        )
        self.time = GlobalGameTimeState.get_instance()
        self.time.world_minute = 3 * 1440 + 600
        self.time.time_revision = 4
        self.time.save()
        self.npcs = {}
        for npc_id in ("10011", "10012", "10013", "10014", "10015"):
            npc, _ = NpcDefinition.objects.get_or_create(
                agent_id=npc_id,
                defaults={"name": f"npc-{npc_id}", "is_test_only": False},
            )
            npc.is_test_only = False
            npc.save(update_fields=["is_test_only"])
            self.npcs[npc_id] = npc
            NpcRuntimeState.objects.get_or_create(npc=npc)
            NpcGlobalInventory.objects.get_or_create(npc=npc)

    def _token(self, account, *, env=False):
        token = AccessToken.for_user(account)
        if env:
            token["scope"] = "env_server"
            token["aud"] = "kingdom-env"
        return str(token)

    def _client(self, account=None, *, env=False):
        client = APIClient()
        if account:
            client.credentials(HTTP_AUTHORIZATION=f"Bearer {self._token(account, env=env)}")
        return client

    def _event(self, *, event_type, visibility="PUBLIC", player_ids=None, actor="10011", **extra):
        return VillageWorldEvent.objects.create(
            dedupe_key=f"phase7:{uuid.uuid4()}",
            event_type=event_type,
            world_minute=extra.pop("world_minute", self.time.world_minute),
            time_revision=self.time.time_revision,
            actor_npc=self.npcs[actor] if actor else None,
            participant_npc_ids=extra.pop("participant_npc_ids", [actor] if actor else []),
            player_ids=player_ids or [],
            location_code=extra.pop("location_code", "VILLAGE_SQUARE"),
            visibility=visibility,
            public_data=extra.pop("public_data", {}),
            private_data=extra.pop("private_data", {"secret": "never"}),
            player_summary=extra.pop("player_summary", event_type),
            hub_summary="internal hub text",
            effects={"private_delta": 99},
            command_id=uuid.uuid4(),
            request_id=uuid.uuid4(),
            **extra,
        )

    def test_config_flag_and_disabled_public_routes(self):
        response = self._client(self.env, env=True).get("/api/village/config")
        self.assertTrue(response.data["data"]["featureFlags"]["villagePresentationEnabled"])
        bootstrap = self._client(self.env, env=True).get("/api/village/runtime/bootstrap")
        self.assertTrue(bootstrap.data["data"]["config"]["village_presentation_enabled"])

        self.config.village_presentation_enabled = False
        self.config.save(update_fields=["village_presentation_enabled"])
        client = self._client(self.player)
        for path in (
            "/api/village/public/journal",
            "/api/village/public/overview",
            "/api/village/public/stores/GENERAL_STORE",
        ):
            response = client.get(path)
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.data["resultCode"], "VILLAGE_PRESENTATION_DISABLED")

    @override_settings(DEBUG=True)
    def test_player_auth_matrix_and_operations_double_gate(self):
        self.assertEqual(self._client().get("/api/village/public/journal").status_code, 401)
        self.assertEqual(
            self._client(self.env, env=True).get("/api/village/public/journal").status_code,
            403,
        )
        self.assertEqual(
            self._client(self.player).get("/api/village/operations/snapshot").status_code,
            401,
        )
        env_client = self._client(self.env, env=True)
        self.assertEqual(env_client.get("/api/village/operations/snapshot").status_code, 200)

        self.config.village_debug_overlay_enabled = False
        self.config.save(update_fields=["village_debug_overlay_enabled"])
        self.assertEqual(env_client.get("/api/village/operations/snapshot").status_code, 404)
        self.config.village_debug_overlay_enabled = True
        self.config.save(update_fields=["village_debug_overlay_enabled"])
        with override_settings(DEBUG=False):
            self.assertEqual(env_client.get("/api/village/operations/snapshot").status_code, 404)

    def test_public_store_shows_shelf_history_and_sales_without_private_data(self):
        Item.objects.create(item_id=45001, item_name="测试农具", max_use_count=20)
        store = VillageStore.objects.get(store_code="GENERAL_STORE")
        store.location_code = "GENERAL_STORE"
        store.operator_npc = self.npcs["10015"]
        store.display_name = "诺拉杂货铺"
        store.status = "OPEN"
        store.save()
        VillageStoreAccount.objects.update_or_create(store=store, defaults={"balance": 12345})
        listed_request = uuid.uuid4()
        listing = VillageStoreListing.objects.create(
            store=store,
            item_id=45001,
            price_mode="CUSTOM",
            unit_price=65,
            listed_by_npc=self.npcs["10015"],
            created_request_id=listed_request,
            listed_world_minute=self.time.world_minute - 20,
        )
        VillageStoreInventorySlot.objects.create(
            store=store,
            listing=listing,
            item_id=45001,
            item_amount=2,
            remaining_uses=18,
            item_attributes={
                "dish_quality": 4,
                "ingredient": "木材、铁",
                "dish_metadata": {"private_batch": "never"},
            },
        )
        EconomicLedgerEntry.objects.create(
            request_id=listed_request,
            entry_type="LIST_STORE_ITEM",
            actor_npc=self.npcs["10015"],
            changes={
                "store_code": "GENERAL_STORE",
                "listing_id": str(listing.listing_id),
                "item_id": 45001,
                "amount": 2,
                "unit_price": 65,
                "private_delta": 999,
            },
            world_minute=self.time.world_minute - 20,
        )
        VillageActionReceipt.objects.create(
            request_id=listed_request,
            npc=self.npcs["10015"],
            action_kind="ECONOMY_LIST_STORE_ITEM",
            success=True,
            result_code="OK",
            response_data={"data": {"amount": 2, "unit_price": 65, "secret": "never"}},
            world_minute=self.time.world_minute - 20,
            time_revision=self.time.time_revision,
        )
        buy_request = uuid.uuid4()
        EconomicLedgerEntry.objects.create(
            request_id=buy_request,
            entry_type="BUY_FROM_STORE",
            actor_npc=self.npcs["10011"],
            changes={
                "listing_id": str(listing.listing_id),
                "gross_amount": 65,
                "fee_amount": 3,
                "store_net_amount": 62,
                "buyer_private_balance": 500,
            },
            world_minute=self.time.world_minute - 5,
        )
        VillageActionReceipt.objects.create(
            request_id=buy_request,
            npc=self.npcs["10011"],
            action_kind="ECONOMY_BUY_FROM_STORE",
            success=True,
            result_code="OK",
            response_data={"data": {"amount": 1, "gross_amount": 65, "fee_amount": 3,
                                     "store_net_amount": 62, "store_account_balance": 999}},
            world_minute=self.time.world_minute - 5,
            time_revision=self.time.time_revision,
        )

        client = self._client(self.player)
        response = client.get("/api/village/public/stores/GENERAL_STORE")
        self.assertEqual(response.status_code, 200)
        data = response.data["data"]
        self.assertEqual(data["display_name"], "诺拉杂货铺")
        self.assertEqual(data["coin_balance"], 12345)
        self.assertEqual(data["summary"], {
            "active_listing_count": 1,
            "available_amount": 2,
            "today_transaction_count": 1,
            "today_gross": 65,
        })
        self.assertEqual(data["listings"][0]["batches"][0]["remaining_uses"], 18)
        tooltip = data["listings"][0]["batches"][0]["batch"]
        self.assertEqual(tooltip["usage"], {"mode": "FINITE", "remaining": 18, "maximum": 20})
        self.assertEqual(tooltip["quality"], 4)
        self.assertEqual(tooltip["dish"]["ingredients"], "木材、铁")
        self.assertEqual(tooltip["dish"]["metadata"], {})
        self.assertEqual(tooltip["id"], "")
        self.assertIsNone(tooltip["origin"])
        self.assertEqual(tooltip["quantity"], 2)
        self.assertEqual(data["listing_history"][0]["action"], "LIST")
        self.assertEqual(data["transactions"][0]["amount"], 1)
        serialized = json.dumps(data, ensure_ascii=False)
        for forbidden in (
            "private_batch", "private_delta", "buyer_private_balance", "store_account_balance",
            "dish_metadata", "request_id", "decision_id",
        ):
            self.assertNotIn(forbidden, serialized)

        store.status = "CLOSED"
        store.save(update_fields=["status"])
        self.assertEqual(client.get("/api/village/public/stores/GENERAL_STORE").status_code, 200)
        self.assertEqual(client.get("/api/village/public/stores/UNKNOWN").status_code, 404)
        self.assertEqual(client.get("/api/village/public/stores/GENERAL_STORE?limit=1").status_code, 400)
        self.assertEqual(
            self._client(self.env, env=True).get("/api/village/public/stores/GENERAL_STORE").status_code,
            403,
        )

    def test_store_inventory_includes_unshelved_reserved_and_expired_stock_without_mutation(self):
        store = VillageStore.objects.get(pk="GENERAL_STORE")
        Item.objects.create(item_id=45001, item_name="库存农具", max_use_count=20)
        Item.objects.create(item_id=45002, item_name="仅在库商品")
        Item.objects.create(item_id=45003, item_name="仅在售商品")
        listing = VillageStoreListing.objects.create(store=store, item_id=45001, unit_price=65,
            price_mode="CUSTOM", listed_by_npc=self.npcs["10015"], created_request_id=uuid.uuid4(), listed_world_minute=self.time.world_minute)
        inactive = VillageStoreListing.objects.create(store=store, item_id=45001, unit_price=70,
            price_mode="CUSTOM", listed_by_npc=self.npcs["10015"], status="INACTIVE", created_request_id=uuid.uuid4(), listed_world_minute=0)
        for amount, reserved, linked, expiry, attrs in (
            (3, 1, listing, None, {}), (5, 1, None, None, {"private_note": "secret"}),
            (2, 0, None, self.time.world_minute, {}),
            (1, 0, None, None, {"freshness_unknown": True}), (4, 0, inactive, None, {}),
        ):
            VillageStoreInventorySlot.objects.create(store=store, item_id=45001, item_amount=amount,
                reserved_amount=reserved, listing=linked, expires_world_minute=expiry,
                remaining_uses=12, item_attributes=attrs)
        VillageStoreInventorySlot.objects.create(store=store, item_id=45002, item_amount=7)
        VillageStoreInventorySlot.objects.create(store=store, item_id=45002, item_amount=0)
        VillageStoreInventorySlot.objects.create(store_id="RESTAURANT_STORE", item_id=45002, item_amount=99)
        shelf_only = VillageStoreListing.objects.create(store=store, item_id=45003, unit_price=5,
            price_mode="CUSTOM", listed_by_npc=self.npcs["10015"], created_request_id=uuid.uuid4(), listed_world_minute=self.time.world_minute)
        VillageStoreInventorySlot.objects.create(store=store, item_id=45003, item_amount=6, listing=shelf_only)
        before = list(store.inventory_slots.order_by("id").values())
        client = self._client(self.player)
        data = client.get("/api/village/public/stores/GENERAL_STORE").data["data"]
        inventory = {row["item_id"]: row for row in data["inventory"]}
        row = inventory[45001]
        self.assertEqual({key: row[key] for key in ("total_amount", "available_amount", "listed_amount",
            "unlisted_amount", "reserved_amount", "unavailable_amount")}, {
            "total_amount": 15, "available_amount": 10, "listed_amount": 2,
            "unlisted_amount": 8, "reserved_amount": 2, "unavailable_amount": 3})
        self.assertEqual(len(row["batches"]), 5)
        self.assertEqual(row["batches"][0]["batch"]["usage"]["remaining"], 12)
        self.assertEqual(inventory[45002]["item_name"], "仅在库商品")
        self.assertEqual(inventory[45002]["total_amount"], 7)
        warehouse = {row["item_id"]: row for row in data["warehouse"]}
        self.assertEqual({key: warehouse[45001][key] for key in ("total_amount", "available_amount", "listed_amount",
            "unlisted_amount", "reserved_amount", "unavailable_amount")}, {
            "total_amount": 12, "available_amount": 8, "listed_amount": 0,
            "unlisted_amount": 8, "reserved_amount": 1, "unavailable_amount": 3})
        self.assertEqual(len(warehouse[45001]["batches"]), 4)
        self.assertTrue(all(not batch["is_listed"] for row in warehouse.values() for batch in row["batches"]))
        self.assertEqual(warehouse[45002]["total_amount"], 7)
        self.assertNotIn(45003, warehouse)
        self.assertEqual(next(row for row in data["listings"] if row["item_id"] == 45001)["available_amount"], 2)
        self.assertNotIn("secret", json.dumps(data))
        self.assertEqual(before, list(store.inventory_slots.order_by("id").values()))
        restaurant = client.get("/api/village/public/stores/RESTAURANT_STORE").data["data"]
        self.assertNotIn("inventory", restaurant)
        self.assertNotIn("warehouse", restaurant)

    def test_general_store_balance_refreshes_and_missing_account_stays_unknown(self):
        client = self._client(self.player)
        url = "/api/village/public/stores/GENERAL_STORE"
        account, _ = VillageStoreAccount.objects.update_or_create(
            store_id="GENERAL_STORE", defaults={"balance": 72}
        )
        self.assertEqual(client.get(url).data["data"]["coin_balance"], 72)
        account.balance = 0
        account.save(update_fields=["balance"])
        self.assertEqual(client.get(url).data["data"]["coin_balance"], 0)
        account.delete()
        self.assertIsNone(client.get(url).data["data"]["coin_balance"])
        self.assertFalse(VillageStoreAccount.objects.filter(store_id="GENERAL_STORE").exists())
        self.assertNotIn("coin_balance", client.get(
            "/api/village/public/stores/RESTAURANT_STORE"
        ).data["data"])

    def test_journal_privacy_filters_and_strict_reverse_cursor(self):
        public_old = self._event(
            event_type="HARVEST_COMPLETED",
            public_data={"category": "ECONOMY", "secret": "no"},
            world_minute=2 * 1440 + 10,
            location_code="BILL_FARM",
        )
        mine = self._event(
            event_type="PLAYER_NOTIFIED",
            visibility="PLAYER_PRIVATE",
            player_ids=[str(self.player.id)],
            actor="10012",
        )
        self._event(
            event_type="OTHER_PRIVATE",
            visibility="PLAYER_PRIVATE",
            player_ids=[str(self.other.id)],
        )
        self._event(event_type="SYSTEM_ONLY", visibility="SYSTEM", actor=None)
        newest = self._event(event_type="STORY_TRANSITION", public_data={"category": "STORY"})

        client = self._client(self.player)
        page = client.get("/api/village/public/journal", {"limit": 2})
        self.assertEqual(page.status_code, 200)
        events = page.data["data"]["events"]
        self.assertEqual([row["event_sequence"] for row in events], [newest.event_sequence, mine.event_sequence])
        self.assertEqual(
            set(events[0]),
            {
                "event_id", "event_sequence", "event_type", "world_minute",
                "actor_npc_id", "participant_npc_ids", "location_code",
                "player_summary", "category",
            },
        )
        serialized = json.dumps(page.data)
        for forbidden in ("private_data", "public_data", "hub_summary", "effects", "command_id", "request_id", "cause_event_id"):
            self.assertNotIn(forbidden, serialized)

        cursor = page.data["data"]["next_before_sequence"]
        next_page = client.get("/api/village/public/journal", {"before_sequence": cursor})
        self.assertEqual([row["event_sequence"] for row in next_page.data["data"]["events"]], [public_old.event_sequence])
        filtered = client.get("/api/village/public/journal", {
            "day_index": 2, "category": "economy", "npc_id": "10011", "location_code": "BILL_FARM",
        })
        self.assertEqual([row["event_id"] for row in filtered.data["data"]["events"]], [str(public_old.event_id)])
        for params in ({"limit": 101}, {"limit": 0}, {"before_sequence": "bad"}, {"player_id": self.other.id}):
            response = client.get("/api/village/public/journal", params)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.data["resultCode"], "INVALID_QUERY_PARAMETER")

    def test_journal_sparse_visibility_has_fixed_scan_budget_and_continuation(self):
        visible = self._event(event_type="VISIBLE_OLD")
        VillageWorldEvent.objects.bulk_create([
            VillageWorldEvent(
                dedupe_key=f"phase7:sparse:{index}",
                event_type="PRIVATE_NEWER",
                world_minute=self.time.world_minute,
                time_revision=self.time.time_revision,
                visibility="PLAYER_PRIVATE",
                player_ids=[str(self.other.id)],
            )
            for index in range(SCAN_EVENT_BUDGET + 5)
        ])

        with mock.patch.object(connection.features, "supports_json_field_contains", False):
            with CaptureQueriesContext(connection) as first_queries:
                first = public_journal(str(self.player.id), {"limit": 1})
            self.assertEqual(first["events"], [])
            self.assertIsNotNone(first["next_before_sequence"])
            self.assertLessEqual(
                len(first_queries),
                SCAN_EVENT_BUDGET // SCAN_BATCH_SIZE,
            )

            with CaptureQueriesContext(connection) as next_queries:
                continued = public_journal(str(self.player.id), {
                    "limit": 1,
                    "before_sequence": first["next_before_sequence"],
                })
        self.assertEqual(
            [row["event_sequence"] for row in continued["events"]],
            [visible.event_sequence],
        )
        self.assertLessEqual(len(next_queries), 1)

    def test_overview_is_public_and_bounded(self):
        injected = self._client(self.player).get(
            "/api/village/public/overview", {"player_id": self.other.id}
        )
        self.assertEqual(injected.status_code, 400)
        self.assertEqual(injected.data["resultCode"], "INVALID_QUERY_PARAMETER")

        bill_inventory = NpcGlobalInventory.objects.get(npc=self.npcs["10011"])
        bill_inventory.money = 999
        bill_inventory.asset_version = 88
        bill_inventory.save()
        NpcGlobalInventorySlot.objects.create(
            bag=bill_inventory, slot_index=0, item_id=1030, item_amount=25
        )
        DailyPriceSnapshot.objects.create(
            day_index=2, status="PUBLISHED", prices={"1030": 8, "1056": 12, "9999": 999}
        )
        VillageAccount.objects.create(account_code="PUBLIC_FUND", balance=500, asset_version=9)
        EconomicLedgerEntry.objects.create(
            request_id=uuid.uuid4(), entry_type="IMPORT_GOODS", changes={},
            external_delta=-20, world_minute=self.time.world_minute,
        )
        for side in ("BUY", "SELL"):
            NpcTradeOrder.objects.create(
                request_id=uuid.uuid4(), side=side, npc=self.npcs["10015"], item_id=1030,
                amount=2, unit_price=8, delivery_point_code="MARKET_STREET",
                expires_world_minute=self.time.world_minute + 60,
            )
        VillageLocationDefinition.objects.create(
            location_code="MARKET_STREET", display_name="Market", center_anchor_code="MARKET",
            location_type="market", opening_hours={"alwaysOpen": False}, is_public=True,
        )
        VillageLocationDefinition.objects.create(
            location_code="ALWAYS_OPEN", display_name="Always", center_anchor_code="ALWAYS",
            location_type="public", opening_hours={"alwaysOpen": True}, is_public=True,
        )
        VillageLocationDefinition.objects.create(
            location_code="DAY_SHOP", display_name="Day", center_anchor_code="DAY",
            location_type="shop",
            opening_hours={"alwaysOpen": False, "openMinute": 480, "closeMinute": 1020},
            is_public=True,
        )
        VillageLocationDefinition.objects.create(
            location_code="NIGHT_SHOP", display_name="Night", center_anchor_code="NIGHT",
            location_type="shop",
            opening_hours={"alwaysOpen": False, "openMinute": 1320, "closeMinute": 360},
            is_public=True,
        )
        VillageLocationDefinition.objects.create(
            location_code="SECRET_ROOM", display_name="Secret", center_anchor_code="SECRET",
            location_type="secret", is_public=False,
        )
        arc = VillageStoryArc.objects.create(
            arc_code="BROKEN_TRADE_ROUTE_V1", current_phase="EXECUTION",
            current_plan="BRIDGE_REPAIR", story_version=7,
        )
        VillageStoryFact.objects.create(
            arc=arc, bridge_condition="REPAIRING", repair_fund=10,
            bill_warning_revealed=True, mark_budget_delay_revealed=True,
        )
        self._event(
            event_type="STORY_DONATE_REPAIR_RESOURCES",
            player_ids=[str(self.player.id)],
        )

        response = self._client(self.player).get("/api/village/public/overview")
        self.assertEqual(response.status_code, 200)
        data = response.data["data"]
        self.assertEqual(len(data["economy"]["price_history"]), 1)
        self.assertEqual(len(data["economy"]["public_orders"]), 1)
        self.assertEqual(data["economy"]["today_import_total"], 20)
        self.assertEqual(data["story"]["player_contribution"]["event_count"], 1)
        locations = {row["location_code"]: row["is_open"] for row in data["locations"]}
        self.assertEqual(locations, {
            "ALWAYS_OPEN": True,
            "DAY_SHOP": True,
            "MARKET_STREET": False,
            "NIGHT_SHOP": False,
        })
        self.assertEqual(set(data["story"]), {
            "arc_code", "public_problem", "public_plan", "progress",
            "player_contribution", "recent_transition_reason", "retired_plan",
        })
        self.assertEqual(set(data["story"]["progress"]), {
            "bridge_condition", "repair_fund", "repair_wood", "repair_metal",
            "repair_labor_hours", "community_meals",
        })
        serialized = json.dumps(data).lower()
        for forbidden in (
            "asset_version", "delivery_reservation", "audit", "candidate_plans",
            "available_actions", "transition_condition", "bill_warning_revealed",
            "mark_budget_delay_revealed", "private_data",
        ):
            self.assertNotIn(forbidden, serialized)

        arc.current_plan = "FOREST_ROUTE"
        arc.save(update_fields=["current_plan"])
        fact = VillageStoryFact.objects.get(arc=arc)
        fact.forest_route_scouted = 2
        fact.forest_route_supply = 3
        fact.emergency_caravan_contacted = True
        fact.emergency_caravan_succeeded = False
        fact.save()
        self.time.world_minute = 3 * 1440 + 1380
        self.time.save(update_fields=["world_minute"])
        forest = self._client(self.player).get("/api/village/public/overview").data["data"]
        self.assertEqual(set(forest["story"]["progress"]), {
            "retired", "message", "emergency_caravan_status",
        })
        self.assertTrue(forest["story"]["progress"]["retired"])
        self.assertEqual(forest["story"]["progress"]["emergency_caravan_status"], "FAILED")
        forest_locations = {
            row["location_code"]: row["is_open"] for row in forest["locations"]
        }
        self.assertTrue(forest_locations["ALWAYS_OPEN"])
        self.assertFalse(forest_locations["DAY_SHOP"])
        self.assertTrue(forest_locations["NIGHT_SHOP"])

    def test_checkpoint_route_rejects_needs_and_uses_existing_whitelist(self):
        runtime = NpcRuntimeState.objects.get(npc=self.npcs["10011"])
        runtime.state_version = 3
        runtime.hunger_fp = 777
        runtime.current_action = "MOVE"
        runtime.save()
        client = self._client(self.env, env=True)
        forbidden = client.put("/api/village/runtime/checkpoint", {
            "world_minute": 999999,
            "time_revision": 999,
            "checkpoints": [{
                "npc_id": "10011", "expected_state_version": 3,
                "current_location_code": "TAVERN", "public_needs": {"hunger": 1},
                "state_version": 999, "current_action": "SLEEP",
            }],
        }, format="json")
        self.assertEqual(forbidden.status_code, 400)
        self.assertEqual(forbidden.data["resultCode"], "CHECKPOINT_FIELD_NOT_ALLOWED")
        runtime.refresh_from_db()
        self.assertEqual((runtime.hunger_fp, runtime.state_version, runtime.current_action), (777, 3, "MOVE"))
        self.assertEqual(GlobalGameTimeState.get_instance().world_minute, self.time.world_minute)

        allowed = client.put("/api/village/runtime/checkpoint", {
            "world_minute": 999999,
            "time_revision": 999,
            "checkpoints": [{
                "npc_id": "10011", "expected_state_version": 3,
                "current_location_code": "TAVERN",
            }],
        }, format="json")
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(set(allowed.data), {"success", "resultCode", "results"})
        runtime.refresh_from_db()
        self.assertEqual((runtime.current_location_code, runtime.state_version, runtime.hunger_fp), ("TAVERN", 4, 777))

    @override_settings(DEBUG=True)
    def test_operations_has_precise_values_with_hard_list_caps(self):
        runtime = NpcRuntimeState.objects.get(npc=self.npcs["10011"])
        runtime.hunger_fp = 333
        runtime.energy_fp = 444
        runtime.save()
        for index in range(6):
            VillageActionReceipt.objects.create(
                request_id=uuid.uuid4(), npc=self.npcs["10011"], action_kind="MOVE",
                success=True, result_code=f"OK_{index}", world_minute=index,
                time_revision=index,
            )
        NpcCommandState.objects.bulk_create([
            NpcCommandState(
                command_id=uuid.uuid4(), message_id=uuid.uuid4(), request_id=uuid.uuid4(),
                npc=self.npcs["10011"], sender_id="10011", recipient_id="env-server-001",
                kind="MOVE", payload_sha256=f"{index:064d}",
            )
            for index in range(101)
        ])
        VillageAccount.objects.bulk_create([
            VillageAccount(account_code=f"OPS_{index:03d}", balance=index)
            for index in range(101)
        ])
        NpcTradeOrder.objects.bulk_create([
            NpcTradeOrder(
                order_id=uuid.uuid4(), request_id=uuid.uuid4(), side="BUY",
                npc=self.npcs["10015"], item_id=1030, amount=1, unit_price=1,
                delivery_point_code="MARKET_STREET",
                expires_world_minute=self.time.world_minute + index,
            )
            for index in range(101)
        ])
        source_event = self._event(event_type="OPS_SOURCE")
        NpcRelationship.objects.create(
            from_npc=self.npcs["10011"], to_npc=self.npcs["10012"],
            affinity=1, trust=2, obligation=3, conflict=4,
            last_meaningful_event=source_event,
        )
        NpcCommitment.objects.bulk_create([
            NpcCommitment(
                commitment_id=uuid.uuid4(), commitment_type="JOINT_WORK",
                promisor_npc=self.npcs["10011"], beneficiary_npc=self.npcs["10012"],
                target={"work_code": "REPAIR"}, due_world_minute=100 + index,
                source_request_id=uuid.uuid4(), created_event=source_event,
                created_world_minute=100,
            )
            for index in range(101)
        ])
        NpcStoryFactAccess.objects.create(
            npc=self.npcs["10011"], fact_code="INTERNAL_FACT",
            unlocked_by_event=source_event, unlocked_world_minute=100,
        )
        arcs = [
            VillageStoryArc(
                arc_code=f"OPS_ARC_{index:03d}", current_phase="EXECUTION",
                current_plan="BRIDGE_REPAIR", story_version=index,
            )
            for index in range(101)
        ]
        VillageStoryArc.objects.bulk_create(arcs)
        story_arc = VillageStoryArc.objects.get(arc_code="OPS_ARC_000")
        VillageStoryFact.objects.create(
            arc=story_arc, bill_warning_revealed=True, repair_wood=7,
        )
        VillageStoryTransitionReceipt.objects.bulk_create([
            VillageStoryTransitionReceipt(
                request_id=uuid.uuid4(), arc=story_arc, from_phase="AFTERMATH",
                to_phase="EXECUTION", selected_plan="BRIDGE_REPAIR", source_day=index,
                world_event=source_event, response_data={"internal": index},
            )
            for index in range(101)
        ])
        response = self._client(self.env, env=True).get("/api/village/operations/snapshot")
        self.assertEqual(response.status_code, 200)
        data = response.data["data"]
        bill = next(row for row in data["npcs"] if row["npc_id"] == "10011")
        self.assertEqual(bill["needs"]["hunger"], 33.3)
        self.assertEqual(len(bill["recent_results"]), 5)
        self.assertEqual(len(data["commands"]), 100)
        self.assertEqual(len(data["economy"]["accounts"]), 100)
        self.assertEqual(len(data["economy"]["orders"]), 100)
        self.assertEqual(len(data["social"]["commitments"]), 100)
        self.assertEqual(data["social"]["relationships"][0]["trust"], 2)
        self.assertEqual(data["social"]["fact_access"][0]["fact_code"], "INTERNAL_FACT")
        self.assertEqual(len(data["story"]["arcs"]), 100)
        self.assertEqual(len(data["story"]["transition_receipts"]), 100)
        self.assertTrue(data["story"]["facts"][0]["bill_warning_revealed"])
        self.assertEqual(len(data["technical"]["commands"]), 100)
        self.assertEqual(data["technical"]["tick_latency_ms"], {
            "p50": "unavailable", "p95": "unavailable", "p99": "unavailable",
        })
        self.assertEqual(data["technical"]["hub_failure_rate"], "unavailable")
        self.assertEqual(data["technical"]["checkpoint"]["last_success_at"], "unavailable")
