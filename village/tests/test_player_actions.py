import copy
import uuid

from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.models import (InventoryBag, InventorySlot, NpcDefinition, NpcGlobalInventory,
    NpcGlobalInventorySlot, CollectionPoint, CollectionPointLevel, CollectionPointLevelItem)
from items.models import Item
from marketplace.models import MarketTransaction
from marketplace.services import create_system_listing
from player.models import PlayerState
from village.models import (VillageSimulationConfig, VillagePersonalCommission, VillagePersonalCommissionReceipt,
    VillagePlayerActionReceipt, VillagePlayerContribution, VillageStoryArc, VillageStoryFact,
    VillageStoryMeeting, VillagePublicDecision, VillageAccount, VillageLocationDefinition, NpcProfile)
from village.personal_commissions import publish_commission
from village.player_actions import execute_player_action
from village.phase3_catalog import REPAIR_WOOD_ITEM_ID, REPAIR_METAL_ITEM_ID


class PlayerFixture:
    def setUp(self):
        super().setUp()
        VillageSimulationConfig.objects.update_or_create(config_key="default", defaults=dict(
            village_economy_enabled=True, village_story_enabled=True, story_repair_wood_target=10,
            story_repair_metal_target=4, story_repair_fund_target=500, story_repair_labor_target=24))
        self.clock = GlobalGameTimeState.get_instance()
        self.clock.world_minute = 100
        self.clock.save()
        self.a = GameAccount.objects.create(username="player-alpha")
        self.b = GameAccount.objects.create(username="player-beta")
        self.bag_a = InventoryBag.objects.create(player=self.a, character_id=self.a.username, money=100)
        self.bag_b = InventoryBag.objects.create(player=self.b, character_id=self.b.username, money=100)
        self.npc, _ = NpcDefinition.objects.get_or_create(agent_id="10014", defaults=dict(name="Lucy"))
        self.lucy, _ = NpcGlobalInventory.objects.update_or_create(npc=self.npc, defaults=dict(money=1000))
        self.lucy.slots.all().delete()
        NpcProfile.objects.update_or_create(npc=self.npc, defaults=dict(display_name="Lucy", occupation="厨师",
            home_location_code="RESTAURANT", work_location_code="RESTAURANT", social_location_code="RESTAURANT",
            capabilities=["PUBLISH_PERSONAL_COMMISSION"]))
        VillageLocationDefinition.objects.update_or_create(location_code="RESTAURANT", defaults=dict(
            display_name="饭店", center_anchor_code="RESTAURANT", location_type="PUBLIC", is_active=True))
        Item.objects.update_or_create(item_id=1030, defaults=dict(item_name="玉米", item_type="蔬菜", item_price=5))
        for item_id, name, kind in [(REPAIR_WOOD_ITEM_ID, "修桥木材", "Commodity"),
                                   (REPAIR_METAL_ITEM_ID, "修桥金属", "Commodity"), (29201, "清蒸玉米", "食物")]:
            Item.objects.update_or_create(item_id=item_id, defaults=dict(item_name=name, item_type=kind, item_price=10))

    def payload(self, action, params=None, player=None, rid=None, location=None):
        defaults = dict(commission_accept="", commission_abandon="", commission_contribute="RESTAURANT",
                        contribute="REPAIR_YARD", buy_marketplace="MARKET_STREET", collect="FOREST_EDGE")
        return dict(player_id=str((player or self.a).pk), request_id=str(rid or uuid.uuid4()), action=action,
                    params=params or {}, current_location_code=location if location is not None else defaults.get(action, ""))

    def run_action(self, action, params=None, **kwargs):
        return execute_player_action(self.payload(action, params, **kwargs))

    def commission(self, quantity=2, player=None, *, accept=True):
        data = publish_commission(self.npc, dict(title="饭店个人委托", reward_gold=quantity * 5,
            valid_for_minutes=720, default_location_code="RESTAURANT", stages=[dict(id="supplies", conditions=[
                dict(id="corn", type="item", item_ids=[1030], target=quantity, min_quality=3,
                     requires_freshness=True, min_remaining_minutes=60)])]), uuid.uuid4(), self.clock.world_minute)
        commission_id = data["commission_id"]
        if accept:
            result = self.run_action("commission_accept", dict(commission_id=commission_id), player=player)
            self.assertTrue(result["success"], result)
        return commission_id

    def delivery_params(self, commission_id, quantity, **extra):
        return dict(commission_id=commission_id, condition_id="corn", item_id=1030, quantity=quantity, **extra)

    def slot(self, quantity=5, item_id=1030, bag=None, **kwargs):
        bag = bag or self.bag_a
        return InventorySlot.objects.create(bag=bag, slot_index=bag.slots.count(), item_id=item_id,
            item_amount=quantity, produced_world_minute=90, expires_world_minute=kwargs.pop("expires_world_minute", 2000), **kwargs)

    def project(self):
        arc = VillageStoryArc.objects.create(arc_code="BROKEN_TRADE_ROUTE_V1", current_phase="EXECUTION", current_plan="BRIDGE_REPAIR")
        fact = VillageStoryFact.objects.create(arc=arc)
        meeting = VillageStoryMeeting.objects.create(arc=arc, opened_day=0, closes_day=0,
            status="SETTLED", selected_plan="BRIDGE_REPAIR")
        VillagePublicDecision.objects.create(meeting=meeting, plan_code="BRIDGE_REPAIR", approved_world_minute=0,
            published_world_minute=0, terms=dict(expires_world_minute=4000))
        VillageAccount.objects.update_or_create(account_code="PUBLIC_FUND", defaults=dict(balance=0))
        return fact


class PlayerActionTests(PlayerFixture, TestCase):
    def test_main_bag_identity_not_character_id_or_other_player(self):
        decoy = InventoryBag.objects.create(player=self.b, character_id=str(self.a.pk), money=999)
        self.slot(bag=decoy)
        commission = self.commission()
        result = self.run_action("commission_contribute", self.delivery_params(commission, 2))
        self.assertEqual(result["result_code"], "INSUFFICIENT_QUALIFIED_ITEMS")
        self.assertEqual(decoy.slots.get().item_amount, 5)
        self.assertEqual(self.run_action("overview")["data"]["overview"]["money"], 100)
        self.assertEqual(self.run_action("commission_abandon", dict(commission_id=commission), player=self.b)["result_code"], "COMMISSION_NOT_ASSIGNEE")

    def test_independent_commissions_escrow_delivery_and_replay_conserve_assets(self):
        ca = self.commission(5)
        cb = self.commission(6, self.b)
        self.lucy.refresh_from_db()
        self.assertEqual(self.lucy.money, 945)
        source = self.slot(5, quality=4, dish_metadata={"origin": "wild"})
        self.slot(6, bag=self.bag_b)
        payload = self.payload("commission_contribute", self.delivery_params(ca, 5))
        first = execute_player_action(payload)
        self.assertTrue(first["success"], first)
        self.assertEqual(first, execute_player_action(copy.deepcopy(payload)))
        second = self.run_action("commission_contribute", self.delivery_params(cb, 6), player=self.b)
        self.assertTrue(second["success"], second)
        self.bag_a.refresh_from_db(); self.bag_b.refresh_from_db(); self.lucy.refresh_from_db()
        self.assertEqual(self.bag_a.money+self.bag_b.money+self.lucy.money, 1200)
        batches = list(self.lucy.slots.order_by("slot_index"))
        self.assertEqual(sum(s.item_amount for s in batches), 11)
        self.assertEqual(batches[0].quality, 4)
        self.assertEqual(batches[0].expires_world_minute, source.expires_world_minute)
        self.assertEqual(batches[0].dish_metadata, {"origin": "wild"})
        self.assertEqual(VillagePersonalCommissionReceipt.objects.filter(action="CONTRIBUTE").count(), 2)
        self.assertEqual(VillagePersonalCommission.objects.filter(status="COMPLETED").count(), 2)

    def test_partial_delivery_abandon_returns_all_held_items_and_reward(self):
        commission = self.commission(5)
        self.slot(5)
        self.assertTrue(self.run_action("commission_contribute", self.delivery_params(commission, 2))["success"])
        self.assertFalse(self.lucy.slots.exists())
        self.assertTrue(self.run_action("commission_abandon", dict(commission_id=commission))["success"])
        self.lucy.refresh_from_db(); self.bag_a.refresh_from_db()
        self.assertEqual((self.lucy.money, self.bag_a.money), (1000, 100))
        self.assertEqual(sum(s.item_amount for s in self.bag_a.slots.all()), 5)
        self.assertEqual(VillagePersonalCommission.objects.get(pk=commission).reserved_reward_gold, 0)

    def test_expiry_uses_server_clock_and_releases_budget(self):
        commission = self.commission(3)
        self.clock.world_minute = 821
        self.clock.save()
        result = self.run_action("commission_contribute", self.delivery_params(commission, 3))
        self.assertEqual(result["result_code"], "COMMISSION_CLOSED")
        self.lucy.refresh_from_db()
        self.assertEqual(self.lucy.money, 1000)
        self.assertEqual(VillagePersonalCommission.objects.get(pk=commission).status, "EXPIRED")
        forged = self.payload("overview"); forged["world_minute"] = 0
        self.assertEqual(execute_player_action(forged)["result_code"], "INVALID_PARAMS")

    def test_world_tick_releases_commission_without_any_player_online(self):
        from village.runtime_services import TimeService
        commission = self.commission(3)
        result = TimeService.receive_time(821, self.clock.time_revision+1)
        self.assertTrue(result[0])
        self.lucy.refresh_from_db()
        self.assertEqual(self.lucy.money, 1000)
        self.assertEqual(VillagePersonalCommission.objects.get(pk=commission).status, "EXPIRED")

    def test_exact_minimum_freshness_is_accepted_but_unknown_expiry_is_not(self):
        commission = self.commission(2)
        self.slot(1, expires_world_minute=160)
        self.slot(1, expires_world_minute=None)
        self.assertTrue(self.run_action("commission_contribute", self.delivery_params(commission, 1))["success"])
        self.assertEqual(self.run_action("commission_contribute", self.delivery_params(commission, 1))["result_code"], "INSUFFICIENT_QUALIFIED_ITEMS")

    def test_npc_cash_spend_cannot_spend_commission_reward_escrow(self):
        commission = self.commission(3)
        self.lucy.refresh_from_db()
        self.lucy.money = 0
        self.lucy.save()
        self.slot(3)
        result = self.run_action("commission_contribute", self.delivery_params(commission, 3))
        self.assertTrue(result["success"], result)
        self.bag_a.refresh_from_db()
        self.assertEqual(self.bag_a.money, 115)

    def test_overview_never_generates_procurement_and_has_no_old_contract_fields(self):
        for money in (0, 10, 1000):
            self.lucy.money = money; self.lucy.save()
            overview = self.run_action("overview")["data"]["overview"]
            self.assertEqual(overview["commissions"], [])
            self.assertNotIn("contracts", overview)
            self.assertNotIn("my_claims", overview)
        NpcGlobalInventorySlot.objects.create(bag=self.lucy, slot_index=0, item_id=1030, item_amount=12,
                                            produced_world_minute=90, expires_world_minute=2000)
        self.assertEqual(self.run_action("overview")["data"]["overview"]["commissions"], [])
        self.assertFalse(VillagePersonalCommission.objects.exists())

    def test_quality_expiry_and_instance_isolation_are_atomic(self):
        commission = self.commission(4)
        self.slot(2, quality=4)
        self.slot(2, quality=2)
        self.slot(2, expires_world_minute=100)
        self.slot(2, freshness_unknown=True)
        foreign = self.slot(4, bag=self.bag_b)
        result = self.run_action("commission_contribute", self.delivery_params(commission, 4))
        self.assertFalse(result["success"])
        self.assertEqual(sum(s.item_amount for s in self.bag_a.slots.all()), 8)
        result = self.run_action("commission_contribute", self.delivery_params(commission, 1, instance_id=str(foreign.instance_id)))
        self.assertFalse(result["success"])
        self.assertEqual(self.lucy.slots.count(), 0)

    def test_same_request_different_arguments_conflict_and_player_namespace(self):
        first = self.commission(accept=False)
        other = self.commission(accept=False)
        payload = self.payload("commission_accept", dict(commission_id=first))
        self.assertTrue(execute_player_action(payload)["success"])
        payload["params"]["commission_id"] = other
        self.assertEqual(execute_player_action(payload)["result_code"], "REQUEST_CONFLICT")
        payload["player_id"] = str(self.b.pk)
        self.assertTrue(execute_player_action(payload)["success"])

    def test_remote_receipt_only_never_executes_and_can_replay(self):
        commission = self.commission()
        self.slot(2)
        payload = self.payload("commission_contribute", self.delivery_params(commission, 2))
        remote = {**payload, "replay_only": True, "current_location_code": ""}
        self.assertEqual(execute_player_action(remote)["result_code"], "LOCATION_REQUIRED")
        self.assertFalse(VillagePlayerActionReceipt.objects.filter(request_id=payload["request_id"]).exists())
        first = execute_player_action(payload)
        self.assertTrue(first["success"], first)
        self.assertEqual(execute_player_action(remote), first)
        remote["params"] = {**remote["params"], "quantity": 1}
        self.assertEqual(execute_player_action(remote)["result_code"], "REQUEST_CONFLICT")

    def test_failed_receipt_replays_even_after_inventory_changes(self):
        commission = self.commission()
        payload = self.payload("commission_contribute", self.delivery_params(commission, 2))
        first = execute_player_action(payload)
        self.slot()
        self.assertEqual(execute_player_action(payload), first)
        self.assertEqual(self.bag_a.slots.get().item_amount, 5)

    def test_material_meal_need_caps_and_split_donations_do_not_farm_trust(self):
        fact = self.project()
        self.slot(120, item_id=REPAIR_WOOD_ITEM_ID)
        self.slot(30, item_id=29201)
        payload = self.payload("contribute", dict(kind="material", item_id=REPAIR_WOOD_ITEM_ID, quantity=1))
        first = execute_player_action(payload)
        self.assertTrue(first["success"], first)
        self.assertEqual(first, execute_player_action(payload))
        self.assertTrue(self.run_action("contribute", dict(kind="material", item_id=REPAIR_WOOD_ITEM_ID, quantity=99))["success"])
        self.assertEqual(self.run_action("contribute", dict(kind="material", item_id=REPAIR_WOOD_ITEM_ID, quantity=1))["result_code"], "PROJECT_NEED_FILLED")
        self.assertTrue(self.run_action("contribute", dict(kind="meal", item_id=29201, quantity=20))["success"])
        self.assertEqual(self.run_action("contribute", dict(kind="meal", item_id=29201, quantity=1))["result_code"], "PROJECT_NEED_FILLED")
        fact.refresh_from_db()
        self.assertEqual((fact.repair_wood, fact.community_meals, fact.public_trust), (100, 20, 50))
        totals = self.run_action("overview")["data"]["overview"]["contributions"]
        self.assertEqual((totals["material_quantity"], totals["meal_quantity"], totals["event_count"]), (100, 20, 3))

    def test_project_requires_correct_food_but_no_public_authorization(self):
        self.slot(item_id=REPAIR_WOOD_ITEM_ID)
        self.assertTrue(self.run_action("contribute", dict(kind="material", item_id=REPAIR_WOOD_ITEM_ID, quantity=1))["success"])
        self.project()
        self.assertEqual(self.run_action("contribute", dict(kind="meal", item_id=REPAIR_WOOD_ITEM_ID, quantity=1))["result_code"], "INVALID_MATERIAL")
        self.assertEqual(self.bag_a.slots.get().item_amount, 4)

    def test_market_purchase_exactly_once_price_conflict_and_insufficient_money(self):
        listing, _ = create_system_listing(1030, 2, 10, snapshot_overrides=dict(
            producedWorldMinute=90, expiresWorldMinute=2000, batchMetadataVersion=1))
        payload = self.payload("buy_marketplace", dict(listing_id=str(listing.pk), quantity=1, expected_unit_price=10))
        first = execute_player_action(payload)
        self.assertTrue(first["success"], first)
        self.assertEqual(execute_player_action(payload), first)
        self.assertEqual(MarketTransaction.objects.filter(listing=listing).count(), 1)
        self.bag_a.refresh_from_db(); self.assertEqual(self.bag_a.money, 90)
        bad = self.run_action("buy_marketplace", dict(listing_id=str(listing.pk), quantity=1, expected_unit_price=11))
        self.assertEqual(bad["result_code"], "PRICE_CHANGED")
        self.bag_b.money=0; self.bag_b.save()
        bad = self.run_action("buy_marketplace", dict(listing_id=str(listing.pk), quantity=1), player=self.b)
        self.assertEqual(bad["result_code"], "MARKET_PURCHASE_FAILED")
        listing.refresh_from_db(); self.assertEqual(listing.remaining_amount, 1)

    def test_collect_cost_provenance_and_new_player_first_income(self):
        point, _ = CollectionPoint.objects.update_or_create(collect_type="采集食材", defaults=dict(
            name="玩家野外食材", item_type="蔬菜", action_cost=15, collect_count=1))
        level, _ = CollectionPointLevel.objects.get_or_create(collection_point=point, level=1)
        level.items.all().delete()
        CollectionPointLevelItem.objects.create(level=level, item_id=1030, collect_min_amount=2, collect_max_amount=2)
        payload = self.payload("collect", dict(collect_type="采集食材"))
        first = execute_player_action(payload)
        self.assertTrue(first["success"], first)
        self.assertEqual(first, execute_player_action(payload))
        self.assertEqual(PlayerState.objects.get(player_id=str(self.a.pk)).action_points, 185)
        slot = self.bag_a.slots.get()
        self.assertEqual((slot.item_amount, slot.produced_world_minute, slot.expires_world_minute), (2, 100, 2980))
        self.assertEqual(str(slot.source_request_id), payload["request_id"])
        commission = self.commission()
        self.assertTrue(self.run_action("commission_contribute", self.delivery_params(commission, 2))["success"])
        self.bag_a.refresh_from_db(); self.assertEqual(self.bag_a.money, 110)
        PlayerState.objects.filter(player_id=str(self.a.pk)).update(action_points=0)
        self.assertEqual(self.run_action("collect", dict(collect_type="采集食材"))["result_code"], "INSUFFICIENT_ACTION_POINTS")

    def test_env_only_api_and_player_account_isolation(self):
        client = APIClient()
        token = AccessToken.for_user(self.a)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        payload = self.payload("overview")
        self.assertEqual(client.post("/api/village/player/action/", payload, format="json").status_code, 401)
        token["scope"] = "env_server"; token["aud"] = "kingdom-env"
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        result = client.post("/api/village/player/action/", self.payload("overview", player=self.b), format="json")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data["data"]["overview"]["player_id"], str(self.b.pk))
        self.b.is_ai = True; self.b.save()
        self.assertEqual(self.run_action("overview", player=self.b)["result_code"], "PLAYER_NOT_FOUND")

    def test_field_allowlist_and_locations(self):
        commission = self.commission()
        for q in [True, 0, -1, 1.5, "1"]:
            self.assertEqual(self.run_action("commission_contribute", self.delivery_params(commission, q))["result_code"], "INVALID_PARAMS")
        self.assertEqual(self.run_action("commission_contribute", self.delivery_params(commission, 1), location="OLD_BRIDGE")["result_code"], "COMMISSION_LOCATION_INVALID")
        self.assertEqual(self.run_action("commission_contribute", self.delivery_params(commission, 1, reward=999))["result_code"], "INVALID_PARAMS")
        self.assertEqual(self.run_action("collect", dict(collect_type=[]))["result_code"], "INVALID_PARAMS")
        self.assertEqual(self.run_action("contribute", dict(kind=[], quantity=1, item_id=1030))["result_code"], "INVALID_PARAMS")
        self.assertEqual(self.run_action("buy_marketplace", dict(listing_id="9"*50, quantity=1))["result_code"], "INVALID_PARAMS")
