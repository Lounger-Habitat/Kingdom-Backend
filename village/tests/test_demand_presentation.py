"""Public demand history follows submission dates and never mutates the economy."""
import json
import uuid

from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from village.demand_presentation import public_demands
from village.models import VillageActionReceipt, VillageCaravanRequest, VillageSimulationConfig
from village.phase3_asset_service import commit_economy_action
from village.tests.test_phase3_direct_trade import DirectTradeTestBase, _payload


class DemandPresentationTests(DirectTradeTestBase):
    path = "/api/village/public/caravan-demands"

    def setUp(self):
        super().setUp()
        self.config = VillageSimulationConfig.objects.get(config_key="default")
        self.config.village_presentation_enabled = True
        self.config.caravan_item_ids = [1049, 4001]
        self.config.save()

    def submit(self, minute, items):
        self.time.world_minute = minute
        self.time.time_revision = minute
        self.time.save()
        payload = _payload("CONTACT_CARAVAN", 10015, "GENERAL_STORE", {"item_ids": items})
        payload.update(completed_world_minute=minute, time_revision=minute)
        result = commit_economy_action(payload)
        self.assertEqual(result["result_code"], "OK", result)
        return payload

    def receipt(self, minute, *, items=None, target=None, registered=True, success=True,
                action="ECONOMY_CONTACT_CARAVAN", extra=None):
        data = {"item_ids": items if items is not None else [1049], "demand_registered": registered}
        if target is not None:
            data["target_day_index"] = target
        data.update(extra or {})
        return VillageActionReceipt.objects.create(
            request_id=uuid.uuid4(), npc=self.nora, action_kind=action, success=success,
            result_code="OK" if success else "P3_INVALID_PARAMS", world_minute=minute,
            time_revision=minute, response_data={"data": data, "request_payload_sha256": "private-hash"},
        )

    def api_client(self, *, env=False):
        account = GameAccount.objects.create(username="demand-env" if env else "demand-player", password="unused")
        token = AccessToken.for_user(account)
        if env:
            token["scope"] = "env_server"
            token["aud"] = "kingdom-env"
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    def test_submission_day_stays_separate_from_target_day_across_11am_and_midnight(self):
        self.submit(659, [1049])
        self.submit(660, [4001])
        self.submit(1440 + 659, [1049, 4001])
        first = public_demands(day_index=0)
        second = public_demands(day_index=1)
        self.assertEqual(first["available_days"], [1, 0])
        self.assertEqual([r["world_minute"] for r in first["records"]], [660, 659])
        self.assertEqual([r["target_day_index"] for r in first["records"]], [1, 0])
        self.assertEqual([r["arrival_world_minute"] for r in first["records"]], [2100, 660])
        self.assertEqual(second["total_count"], 1)
        self.assertEqual(second["records"][0]["world_minute"], 2099)
        self.assertEqual(second["records"][0]["target_day_index"], 1)
        self.assertEqual(public_demands()["day_index"], 1)

    def test_replacing_same_target_demand_preserves_each_successful_submission(self):
        older = self.submit(700, [1049])
        newer = self.submit(710, [4001])
        data = public_demands(day_index=0)
        self.assertEqual(VillageCaravanRequest.objects.count(), 1)
        self.assertEqual(data["total_count"], 2)
        self.assertEqual([r["registration_id"] for r in data["records"]],
                         [newer["request_id"], older["request_id"]])
        self.assertEqual([r["items"][0]["item_id"] for r in data["records"]], [4001, 1049])
        self.assertEqual({r["record_source"] for r in data["records"]}, {"SUBMISSION"})

    def test_retry_does_not_create_another_registration(self):
        payload = self.submit(700, [1049, 4001])
        result = commit_economy_action(payload)
        self.assertTrue(result["replayed"])
        data = public_demands(day_index=0)
        self.assertEqual(data["total_count"], 1)
        self.assertEqual(data["records"][0]["registration_id"], payload["request_id"])

    def test_distinct_submissions_with_identical_items_and_minute_are_not_collapsed(self):
        first = self.submit(700, [1049])
        second = self.submit(700, [1049])
        records = public_demands(day_index=0)["records"]
        self.assertEqual(len(records), 2)
        self.assertEqual({r["registration_id"] for r in records}, {first["request_id"], second["request_id"]})

    def test_legacy_snapshot_is_retained_and_matching_latest_snapshot_is_deduplicated(self):
        VillageCaravanRequest.objects.create(day_index=0, submitted_world_minute=650, item_ids=[1049])
        receipt = self.receipt(700, items=[1049, 4001], target=1)
        VillageCaravanRequest.objects.create(day_index=1, submitted_world_minute=700,
                                            item_ids=[4001, 1049, 4001])
        data = public_demands(day_index=0)
        self.assertEqual(data["total_count"], 2)
        self.assertEqual([r["registration_id"] for r in data["records"]], [str(receipt.pk), "legacy-0"])
        self.assertEqual([r["record_source"] for r in data["records"]], ["SUBMISSION", "SAVED_REQUEST"])
        self.assertEqual(data["available_days"], [0])

    def test_receipt_without_target_day_uses_submission_cutoff_and_deduplicates_snapshot(self):
        for minute, target in ((659, 0), (660, 1)):
            self.receipt(minute)
            VillageCaravanRequest.objects.create(day_index=target, submitted_world_minute=minute, item_ids=[1049])
        records = public_demands(day_index=0)["records"]
        self.assertEqual(len(records), 2)
        self.assertEqual([r["target_day_index"] for r in records], [1, 0])
        self.assertEqual({r["record_source"] for r in records}, {"SUBMISSION"})

    def test_failed_unregistered_and_unrelated_actions_are_not_public_demands(self):
        self.receipt(1440, success=False)
        self.receipt(2880, registered=False)
        self.receipt(4320, action="ECONOMY_IMPORT_GOODS")
        data = public_demands(day_index=1)
        self.assertEqual(data["records"], [])
        self.assertEqual(data["available_days"], [])
        self.assertEqual((data["total_count"], data["page"], data["page_count"]), (0, 1, 1))

    def test_pagination_is_complete_stable_and_clamps_after_last_page(self):
        for minute in range(700, 745):
            self.receipt(minute, target=1)
        VillageCaravanRequest.objects.create(day_index=1, submitted_world_minute=744, item_ids=[1049])
        pages = [public_demands(day_index=0, page=page) for page in (1, 2, 3)]
        self.assertEqual([len(p["records"]) for p in pages], [20, 20, 5])
        self.assertTrue(all((p["total_count"], p["page_count"]) == (45, 3) for p in pages))
        rows = [r for p in pages for r in p["records"]]
        self.assertEqual([r["world_minute"] for r in rows], list(range(744, 699, -1)))
        self.assertEqual(len({r["registration_id"] for r in rows}), 45)
        self.assertEqual(public_demands(day_index=0, page=99), pages[2])

    def test_read_is_only_selects_and_whitelists_public_fields(self):
        self.receipt(700, items=[1049, 999999], target=1,
                     extra={"personal_balance": 98765, "private_data": "secret", "asset_versions": {"10015": 9}})
        VillageCaravanRequest.objects.create(day_index=1, submitted_world_minute=700, item_ids=[1049, 999999])
        with CaptureQueriesContext(connection) as queries:
            data = public_demands(day_index=0)
        self.assertTrue(queries.captured_queries)
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries.captured_queries))
        row = data["records"][0]
        self.assertEqual(set(row), {"registration_id", "world_minute", "target_day_index",
                                   "arrival_world_minute", "operator_display_name", "items", "record_source"})
        self.assertEqual(row["items"], [{"item_id": 1049, "item_name": "盐"},
                                         {"item_id": 999999, "item_name": "物品#999999"}])
        serialized = json.dumps(data)
        for private in ("personal_balance", "private_data", "asset_versions", "request_payload_sha256", "secret"):
            self.assertNotIn(private, serialized)

    def test_no_clock_falls_back_to_latest_submission_without_creating_clock(self):
        self.receipt(1440 + 700, target=2)
        GlobalGameTimeState.objects.all().delete()
        data = public_demands()
        self.assertIsNone(data["world_minute"])
        self.assertEqual(data["day_index"], 1)
        self.assertEqual(data["total_count"], 1)
        self.assertFalse(GlobalGameTimeState.objects.exists())

    def test_endpoint_requires_player_auth_feature_flag_and_get(self):
        self.assertEqual(APIClient().get(self.path).status_code, 401)
        self.assertEqual(self.api_client(env=True).get(self.path).status_code, 403)
        client = self.api_client()
        self.assertEqual(client.get(self.path).status_code, 200)
        self.assertEqual(client.post(self.path, {}).status_code, 405)
        self.config.village_presentation_enabled = False
        self.config.save(update_fields=["village_presentation_enabled"])
        response = client.get(self.path)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["resultCode"], "VILLAGE_PRESENTATION_DISABLED")

    def test_endpoint_rejects_unknown_invalid_and_ambiguous_query_parameters(self):
        client = self.api_client()
        for query in ("private=true", "day_index=-1", "day_index=", "day_index=1.5", "day_index=１２",
                      "day_index=1234567890123", "page=0", "page=-1", "page=", "page=x",
                      "day_index=0&day_index=1", "page=1&page=2"):
            with self.subTest(query=query):
                response = client.get(self.path + "?" + query)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data["resultCode"], "INVALID_QUERY_PARAMETER")
        response = client.get(self.path + "?day_index=0&page=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["day_index"], 0)
