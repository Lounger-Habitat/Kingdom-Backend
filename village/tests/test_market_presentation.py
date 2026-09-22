from datetime import timedelta
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken
from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from items.models import Item
from marketplace.models import MarketListing
from village.models import VillageSimulationConfig
from village.market_presentation import public_market


class MarketPresentationTests(TestCase):
    def setUp(self):
        self.config, _ = VillageSimulationConfig.objects.update_or_create(config_key="default", defaults={"village_presentation_enabled": True})
        self.clock = GlobalGameTimeState.get_instance()
        self.clock.world_minute = 1000
        self.clock.save()
        Item.objects.update_or_create(item_id=45001, defaults={"item_name": "测试农具", "item_type": "Usable", "max_use_count": 20})
        self.user = GameAccount.objects.create(username="MarketReader", password="unused")
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + str(AccessToken.for_user(self.user)))

    def listing(self, **kwargs):
        data = dict(source_type="system", item_id=45001, item_name_snapshot="测试农具", item_type_snapshot="Usable", price=65, total_amount=2, remaining_amount=2, item_snapshot={"batchMetadataVersion": 1, "private_note": "never"}, remaining_uses=12)
        data.update(kwargs)
        return MarketListing.objects.create(**data)

    def test_only_current_listings_are_returned_without_writing_expiry_or_private_snapshots(self):
        live = self.listing()
        expired = self.listing(expires_at=timezone.now() - timedelta(seconds=1))
        self.listing(item_snapshot={"expiresWorldMinute": 999})
        self.listing(remaining_amount=0)
        self.listing(status="delisted")
        result = public_market({})
        self.assertEqual([r["listing_id"] for r in result["listings"]], [live.pk])
        self.assertEqual(result["listings"][0]["batch"]["usage"], {"mode": "FINITE", "remaining": 12, "maximum": 20})
        self.assertNotIn("private_note", str(result))
        expired.refresh_from_db()
        self.assertEqual(expired.status, "active")

    def test_search_category_sort_and_pagination_cover_all_matches(self):
        for price in range(25):
            self.listing(price=price)
        self.listing(item_id=45002, item_type_snapshot="食材", item_name_snapshot="玉米", price=30)
        first = public_market({"sort": "price_asc"})
        second = public_market({"sort": "price_asc", "page": "2"})
        self.assertEqual(first["total_count"], 26)
        self.assertEqual(len(first["listings"]), 20)
        self.assertEqual(len(second["listings"]), 6)
        self.assertEqual(first["item_count"], 2)
        self.assertEqual(public_market({"category": "food"})["total_count"], 1)
        self.assertEqual(public_market({"category": "tool", "sort": "price_desc"})["listings"][0]["unit_price"], 24)
        self.assertEqual(public_market({"keyword": "玉米"})["total_count"], 1)
        self.assertEqual(public_market({"keyword": "不存在"})["total_count"], 0)

    def test_endpoint_auth_flag_and_invalid_queries(self):
        path = "/api/village/public/market"
        self.assertEqual(APIClient().get(path).status_code, 401)
        self.assertEqual(self.client.get(path).status_code, 200)
        for query in ("page=0", "page=abc", "sort=unknown", "category=bad", "pageSize=500"):
            self.assertEqual(self.client.get(path + "?" + query).status_code, 400)
        self.config.village_presentation_enabled = False
        self.config.save(update_fields=["village_presentation_enabled"])
        self.assertEqual(self.client.get(path).status_code, 404)

    def test_authenticated_owner_flags_and_freshness_are_read_only(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        other = GameAccount.objects.create(username="OtherMarketSeller")
        own = self.listing(source_type="player", seller=self.user,
            item_snapshot={"batchMetadataVersion": 1, "expiresWorldMinute": 1600})
        theirs = self.listing(source_type="player", seller=other,
            item_snapshot={"batchMetadataVersion": 1, "freshnessUnknown": True})
        system = self.listing()
        expired = self.listing(source_type="player", seller=self.user,
            expires_at=timezone.now() - timedelta(seconds=1))
        with CaptureQueriesContext(connection) as queries:
            answer = self.client.get("/api/village/public/market")
        self.assertEqual(answer.status_code, 200)
        rows = {r["listing_id"]: r for r in answer.data["data"]["listings"]}
        self.assertEqual((rows[own.pk]["is_own_listing"], rows[own.pk]["source_type"]), (True, "player"))
        self.assertFalse(rows[theirs.pk]["is_own_listing"])
        self.assertEqual((rows[system.pk]["is_own_listing"], rows[system.pk]["source_type"]), (False, "system"))
        self.assertEqual(rows[own.pk]["batch"]["freshness"], {"state": "FRESH", "expiresWorldMinute": 1600})
        self.assertEqual(rows[theirs.pk]["batch"]["freshness"]["state"], "UNKNOWN")
        self.assertNotIn(expired.pk, rows)
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        expired.refresh_from_db()
        self.assertEqual(expired.status, "active")
        self.assertEqual(self.client.get("/api/village/public/market", {"player_id": other.pk}).status_code, 400)
