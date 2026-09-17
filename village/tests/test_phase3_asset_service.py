"""Tests for Phase 3 unified asset service: commit_economy_action.

Covers feature flag, actor validation, reported-location persistence,
idempotency, version conflict, replay, request conflict, and transaction
rollback.
"""

import json
import uuid
from unittest.mock import patch

from django.test import TestCase, override_settings

from game_time.models import GlobalGameTimeState
from inventory.models import (
    NpcDefinition,
    NpcGlobalInventory,
    NpcGlobalInventorySlot,
)
from items.models import Item, ItemType
from village.models import (
    EconomicLedgerEntry,
    NpcCommandState,
    NpcRuntimeState,
    VillageActionReceipt,
    VillageFarmPlot,
    VillageSimulationConfig,
    VillageWorldEvent,
)
from village.phase3_asset_service import commit_economy_action
from village.phase3_contract import (
    VILLAGE_ECONOMY_ACTION_V1,
    ResultCode,
)


def _base_payload(**overrides):
    """Build a minimal valid economy action payload."""
    data = {
        "schema_version": VILLAGE_ECONOMY_ACTION_V1,
        "command_id": str(uuid.uuid4()),
        "request_id": str(uuid.uuid4()),
        "message_id": str(uuid.uuid4()),
        "command_name": "PLANT",
        "npc_id": 10011,
        "completed_world_minute": 700,
        "time_revision": 700,
        "current_location_code": "BILL_FARM",
        "params": {
            "plot_code": "FARM_PLOT_A",
            "crop_code": "CORN",
            "expected_asset_version": 0,
        },
    }
    data.update(overrides)
    if "params" in overrides and isinstance(overrides["params"], dict):
        data["params"] = {**data.get("params", {}), **overrides["params"]}
    return data


class Phase3AssetServiceBase(TestCase):
    """Shared setUp for all asset service tests."""

    def setUp(self):
        self.config = VillageSimulationConfig.objects.create(
            config_key="default", village_economy_enabled=True, village_direct_trade_enabled=True,
        )
        self.time = GlobalGameTimeState.get_instance()
        self.time.world_minute = 700
        self.time.time_revision = 700
        self.time.save()

        self.bill, _ = NpcDefinition.objects.get_or_create(
            agent_id="10011", defaults={"name": "bill"},
        )
        self.jack, _ = NpcDefinition.objects.get_or_create(
            agent_id="10012", defaults={"name": "jack"},
        )
        self.lucy, _ = NpcDefinition.objects.get_or_create(
            agent_id="10014", defaults={"name": "lucy"},
        )

        self.bill_inv, _ = NpcGlobalInventory.objects.get_or_create(
            npc=self.bill, defaults={"money": 200},
        )

        # Production commands require the NPC runtime row that Unity seeds
        # via checkpoints; the service rejects otherwise (Unity authority).
        for npc in (self.bill, self.jack, self.lucy):
            NpcRuntimeState.objects.get_or_create(npc=npc)

        # Clear migration-seeded slots for a clean test state
        NpcGlobalInventorySlot.objects.filter(bag=self.bill_inv).delete()

        # Farm plots
        for code in ("FARM_PLOT_A", "FARM_PLOT_B", "FARM_PLOT_C"):
            VillageFarmPlot.objects.get_or_create(
                plot_code=code, defaults={"status": "EMPTY"},
            )


class FeatureDisabledTest(Phase3AssetServiceBase):
    def setUp(self):
        super().setUp()
        self.config.village_economy_enabled = False
        self.config.save(update_fields=["village_economy_enabled"])

    def test_feature_disabled_returns_p3_feature_disabled(self):
        resp = commit_economy_action(_base_payload())
        self.assertEqual(resp["result_code"], ResultCode.P3_FEATURE_DISABLED)


class ActorValidationTest(Phase3AssetServiceBase):
    def test_wrong_actor_for_plant_returns_forbidden(self):
        """PLANT only allowed for Bill (10011)."""
        resp = commit_economy_action(_base_payload(npc_id=10012))
        self.assertEqual(resp["result_code"], ResultCode.P3_FORBIDDEN_ACTOR)

    def test_wrong_actor_for_hunt_returns_forbidden(self):
        """HUNT only allowed for Jack (10012)."""
        resp = commit_economy_action(_base_payload(
            command_name="HUNT", npc_id=10011,
            current_location_code="HUNTING_GATE",
            params={"hunt_code": "HUNT_CHICKEN", "expected_asset_version": 0},
        ))
        self.assertEqual(resp["result_code"], ResultCode.P3_FORBIDDEN_ACTOR)


class LocationAuthorityTest(Phase3AssetServiceBase):
    """Unity owns location validation; Backend persists the reported location."""

    def test_mismatched_location_is_persisted_not_rejected(self):
        # PLANT's fixed requirement is BILL_FARM, but the location gate is
        # Unity's preflight (VillageHubAdapter.ValidateUnityActionLocation).
        # Give Bill seeds and a tool so the action succeeds and the reported
        # location must be recorded as-is.
        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1051, item_amount=5,
        )
        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=1, item_id=4001, item_amount=1,
            remaining_uses=20,
        )
        request_id = str(uuid.uuid4())
        resp = commit_economy_action(_base_payload(
            request_id=request_id,
            current_location_code="RESTAURANT",
        ))
        self.assertEqual(resp["result_code"], ResultCode.OK)
        receipt = VillageActionReceipt.objects.get(request_id=request_id)
        self.assertEqual(receipt.command.location_code, "RESTAURANT")


class AssetVersionConflictTest(Phase3AssetServiceBase):
    def test_version_mismatch_returns_conflict(self):
        self.bill_inv.asset_version = 5
        self.bill_inv.save(update_fields=["asset_version"])

        resp = commit_economy_action(_base_payload(
            params={"plot_code": "FARM_PLOT_A", "crop_code": "CORN",
                    "expected_asset_version": 3},
        ))
        self.assertEqual(resp["result_code"], ResultCode.P3_ASSET_VERSION_CONFLICT)


class IdempotencyTest(Phase3AssetServiceBase):
    def setUp(self):
        super().setUp()
        # Seeds and tool for a successful PLANT
        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1051, item_amount=5,
        )
        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=1, item_id=4001, item_amount=10,
            remaining_uses=20,
        )

    def test_same_request_id_same_payload_returns_replayed(self):
        payload = _base_payload()
        resp1 = commit_economy_action(payload)
        self.assertEqual(resp1["result_code"], ResultCode.OK)
        self.assertFalse(resp1["replayed"])

        record_counts = (
            VillageActionReceipt.objects.count(),
            VillageWorldEvent.objects.count(),
            EconomicLedgerEntry.objects.count(),
        )

        resp2 = commit_economy_action(payload)
        self.assertEqual(resp2["result_code"], ResultCode.OK)
        self.assertTrue(resp2["replayed"])
        self.assertTrue(resp2["event"]["event_id"])
        self.assertEqual(resp2["event"], resp1["event"])

        comparable_resp1 = dict(resp1)
        comparable_resp1["replayed"] = True
        self.assertEqual(resp2, comparable_resp1)
        self.assertEqual(
            (
                VillageActionReceipt.objects.count(),
                VillageWorldEvent.objects.count(),
                EconomicLedgerEntry.objects.count(),
            ),
            record_counts,
        )

    def test_same_request_id_different_payload_returns_conflict(self):
        payload = _base_payload()
        resp1 = commit_economy_action(payload)
        self.assertEqual(resp1["result_code"], ResultCode.OK)

        # Same request_id but different command_name
        payload2 = {**payload, "command_name": "HARVEST"}
        resp2 = commit_economy_action(payload2)
        self.assertEqual(resp2["result_code"], ResultCode.P3_REQUEST_CONFLICT)


class PersistenceTest(Phase3AssetServiceBase):
    def setUp(self):
        super().setUp()
        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1051, item_amount=5,
        )
        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=1, item_id=4001, item_amount=10,
            remaining_uses=20,
        )

    def test_success_creates_receipt_event_ledger(self):
        payload = _base_payload()
        resp = commit_economy_action(payload)
        self.assertEqual(resp["result_code"], ResultCode.OK)

        request_id = uuid.UUID(payload["request_id"])
        self.assertTrue(VillageActionReceipt.objects.filter(request_id=request_id).exists())
        self.assertTrue(EconomicLedgerEntry.objects.filter(request_id=request_id).exists())
        self.assertTrue(VillageWorldEvent.objects.filter(request_id=request_id).exists())

    @patch("village.phase3_marketplace._call_service")
    def test_marketplace_sell_action_dispatches_and_persists(self, call_service_mock):
        call_service_mock.return_value = {
            "status": "SUCCEEDED",
            "result_code": ResultCode.OK,
            "data": {
                "listing": {"listingId": "market-1", "price": 9},
                "bag": {"money": 200, "items": []},
                "asset_version": 1,
            },
            "asset_versions": {},
            "effects": {},
            "event": {},
            "external_delta": 0,
        }
        payload = _base_payload(
            command_name="SELL_ON_MARKETPLACE",
            current_location_code="MARKET_STREET",
            npc_id=10011,
        )
        payload["params"] = {
            "instance_id": str(uuid.uuid4()),
            "amount": 2,
            "price": 9,
            "expected_asset_version": 0,
        }

        resp = commit_economy_action(payload)

        self.assertEqual(resp["result_code"], ResultCode.OK)
        self.assertEqual(resp["data"]["listing"]["listingId"], "market-1")
        self.assertTrue(
            EconomicLedgerEntry.objects.filter(
                request_id=uuid.UUID(payload["request_id"]),
                entry_type="SELL_ON_MARKETPLACE",
            ).exists()
        )

    def test_marketplace_sell_action_real_service_persists_json_safe_payload(self):
        Item.objects.create(item_id=1051, item_name="玉米", item_type=ItemType.INGREDIENT)
        slot = NpcGlobalInventorySlot.objects.get(bag=self.bill_inv, slot_index=0)
        payload = _base_payload(
            command_name="SELL_ON_MARKETPLACE",
            current_location_code="MARKET_STREET",
            npc_id=10011,
        )
        payload["params"] = {
            "instance_id": str(slot.instance_id),
            "amount": 2,
            "price": 9,
            "expected_asset_version": self.bill_inv.asset_version,
        }

        resp = commit_economy_action(payload)

        self.assertEqual(resp["result_code"], ResultCode.OK)
        self.assertIn("listing", resp["data"])
        self.assertIn("bag", resp["data"])
        receipt = VillageActionReceipt.objects.get(request_id=uuid.UUID(payload["request_id"]))
        json.dumps(receipt.response_data, ensure_ascii=False)
        self.assertEqual(receipt.response_data["data"]["listing"]["price"], 9)
        self.assertEqual(receipt.response_data["data"]["asset_version"], 1)
        self.assertEqual(resp["data"]["listing_id"], resp["data"]["listing"]["listingId"])
        self.assertIsInstance(resp["data"]["listing_id"], int)
        self.assertEqual(resp["data"]["amount"], 2)
        self.assertEqual(resp["data"]["unit_price"], 9)

        # The next action uses the returned ID without a listings query.
        delist = _base_payload(command_name="DELIST_FROM_MARKETPLACE",
            current_location_code="MARKET_STREET", npc_id=10011,
            params={"listing_id": resp["data"]["listing_id"], "expected_asset_version": 1})
        removed = commit_economy_action(delist)
        self.assertEqual(removed["result_code"], ResultCode.OK)
        self.assertEqual(removed["data"]["listing_id"], resp["data"]["listing_id"])
        self.assertEqual(removed["data"]["listing_status"], "delisted")
        self.assertEqual(sum(NpcGlobalInventorySlot.objects.filter(
            bag=self.bill_inv, item_id=1051).values_list("item_amount", flat=True)), 5)

    def test_marketplace_partial_purchase_receipt_reports_actual_settlement(self):
        from marketplace.models import MarketTransaction
        Item.objects.create(item_id=1051, item_name="玉米", item_type=ItemType.INGREDIENT)
        slot = NpcGlobalInventorySlot.objects.get(bag=self.bill_inv, slot_index=0)
        sold = commit_economy_action(_base_payload(command_name="SELL_ON_MARKETPLACE",
            current_location_code="MARKET_STREET", npc_id=10011,
            params={"instance_id": str(slot.instance_id), "amount": 4, "price": 9,
                "expected_asset_version": self.bill_inv.asset_version}))
        self.assertEqual(sold["result_code"], ResultCode.OK)
        buyer, _ = NpcGlobalInventory.objects.get_or_create(npc=self.jack)
        buyer.money = 200
        buyer.save(update_fields=["money"])
        bought = commit_economy_action(_base_payload(command_name="BUY_FROM_MARKETPLACE",
            current_location_code="MARKET_STREET", npc_id=10012,
            params={"listing_id": sold["data"]["listing_id"], "amount": 2,
                "expected_buyer_asset_version": buyer.asset_version}))
        self.assertEqual(bought["result_code"], ResultCode.OK)
        data = bought["data"]
        tx = MarketTransaction.objects.get(pk=data["transaction_id"])
        self.assertEqual(data["amount"], 2)
        self.assertEqual(data["remaining_amount"], 2)
        self.assertEqual(data["unit_price"], 9)
        self.assertEqual(data["total_price"], tx.total_price)
        self.assertEqual(data["commission"], tx.commission)
        self.assertEqual(data["seller_earnings"], tx.seller_earnings)
        buyer.refresh_from_db()
        self.assertEqual(buyer.money, 200 - data["total_price"])

    @patch("village.phase3_marketplace._service")
    def test_marketplace_missing_service_fails_closed(self, service_mock):
        service_mock.return_value = None
        payload = _base_payload(
            command_name="BUY_FROM_MARKETPLACE",
            current_location_code="MARKET_STREET",
            npc_id=10012,
        )
        payload["params"] = {
            "listing_id": str(uuid.uuid4()),
            "amount": 1,
            "expected_buyer_asset_version": 0,
        }

        resp = commit_economy_action(payload)

        self.assertEqual(resp["result_code"], ResultCode.P3_FEATURE_DISABLED)

    def test_success_reuses_running_command_and_releases_runtime(self):
        payload = _base_payload()
        command_id = uuid.UUID(payload["command_id"])
        request_id = uuid.UUID(payload["request_id"])
        message_id = uuid.uuid4()
        command = NpcCommandState.objects.create(
            command_id=command_id,
            message_id=message_id,
            request_id=request_id,
            npc=self.bill,
            sender_id="10011",
            recipient_id="WuxiVillage",
            kind="PLANT",
            payload_sha256="0" * 64,
            status="RUNNING",
            started_world_minute=700,
        )
        runtime, _ = NpcRuntimeState.objects.update_or_create(
            npc=self.bill,
            defaults={
                "current_location_code": "BILL_FARM",
                "current_point_code": "BILL_FARM",
                "current_action": "PLANT",
                "action_status": "ACTING",
                "active_command_id": command_id,
                "state_version": 3,
            },
        )
        # Unity economy completion uses command_id as message_id; the durable
        # command keeps the original Hub envelope message_id.
        payload["message_id"] = payload["command_id"]

        resp = commit_economy_action(payload)

        self.assertEqual(resp["result_code"], ResultCode.OK)
        self.assertEqual(NpcCommandState.objects.filter(command_id=command_id).count(), 1)
        command.refresh_from_db()
        runtime.refresh_from_db()
        receipt = VillageActionReceipt.objects.get(request_id=request_id)
        self.assertEqual(command.message_id, message_id)
        self.assertEqual(command.status, "SUCCEEDED")
        self.assertEqual(receipt.command_id, command_id)
        self.assertEqual(receipt.state_version, 4)
        self.assertEqual(runtime.state_version, 4)
        self.assertEqual(runtime.action_status, "IDLE")
        self.assertIsNone(runtime.active_command_id)

    def test_failure_creates_stable_failure_receipt_without_success_ledger(self):
        payload = _base_payload(params={"plot_code": "FARM_PLOT_A", "crop_code": "CORN", "expected_asset_version": 999})
        resp = commit_economy_action(payload)
        self.assertEqual(resp["result_code"], ResultCode.P3_ASSET_VERSION_CONFLICT)
        receipt = VillageActionReceipt.objects.get(request_id=payload["request_id"])
        self.assertFalse(receipt.success)
        self.assertEqual(receipt.command.status, "FAILED")
        self.assertFalse(EconomicLedgerEntry.objects.filter(request_id=payload["request_id"]).exists())
        replay = commit_economy_action(payload)
        self.assertTrue(replay["replayed"])
        replay["replayed"] = False
        self.assertEqual(replay, resp)


class InvalidPayloadTest(Phase3AssetServiceBase):
    def test_wrong_schema_version_returns_invalid_params(self):
        payload = _base_payload(schema_version="wrong.version")
        resp = commit_economy_action(payload)
        self.assertEqual(resp["result_code"], ResultCode.P3_INVALID_PARAMS)

    def test_unknown_command_returns_invalid_params(self):
        payload = _base_payload(command_name="UNKNOWN_ACTION")
        resp = commit_economy_action(payload)
        self.assertEqual(resp["result_code"], ResultCode.P3_INVALID_PARAMS)

    def test_unknown_param_returns_invalid_params(self):
        payload = _base_payload()
        payload["params"]["extra_field"] = "bad"
        resp = commit_economy_action(payload)
        self.assertEqual(resp["result_code"], ResultCode.P3_INVALID_PARAMS)

    def test_wire_tunnel_param_key_is_tolerated(self):
        """Hub 线缆隧道键 __village_command_v1 不是业务参数，白名单校验应忽略。"""
        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1051, item_amount=5,
        )
        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=1, item_id=4001, item_amount=10,
            remaining_uses=20,
        )
        payload = _base_payload()
        payload["params"]["__village_command_v1"] = {"schema_version": "village.command.v1"}
        resp = commit_economy_action(payload)
        self.assertEqual(resp["result_code"], ResultCode.OK)


class TransactionRollbackTest(Phase3AssetServiceBase):
    def test_failed_dispatch_does_not_change_inventory(self):
        """Insufficient item error should leave inventory unchanged."""
        # Only 0 seeds -- will fail
        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1051, item_amount=0,
        )
        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=1, item_id=4001, item_amount=10,
            remaining_uses=20,
        )

        tool_before = NpcGlobalInventorySlot.objects.get(
            bag=self.bill_inv, item_id=4001,
        ).remaining_uses

        resp = commit_economy_action(_base_payload())
        self.assertEqual(resp["result_code"], ResultCode.P3_INSUFFICIENT_ITEM)

        tool_after = NpcGlobalInventorySlot.objects.get(
            bag=self.bill_inv, item_id=4001,
        ).remaining_uses
        self.assertEqual(tool_before, tool_after)
