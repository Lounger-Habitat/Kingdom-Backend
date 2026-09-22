import copy
import uuid
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from village.affairs import create_resource_affair
from village.models import (VillageLocationDefinition, VillagePersonalCommission,
                            VillagePlayerAffairLabor, VillagePersonalCommissionEscrow,
                            NpcProfile)
from village.personal_commissions import publish_commission
from village.player_actions import execute_player_action
from village.player_affair_labor import tick_player_labor
from village.tests.test_player_actions import PlayerFixture


class PlayerCommissionTests(PlayerFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.nonce = str(uuid.uuid4())
        for code in ("RESTAURANT", "REPAIR_YARD"):
            VillageLocationDefinition.objects.get_or_create(location_code=code, defaults=dict(
                display_name=code, center_anchor_code=code, location_type="PUBLIC"))
        NpcProfile.objects.update_or_create(npc=self.npc, defaults=dict(display_name="Lucy", occupation="厨师",
            home_location_code="RESTAURANT", work_location_code="RESTAURANT", social_location_code="RESTAURANT",
            capabilities=["PUBLISH_PERSONAL_COMMISSION"]))

    def publish(self, stages=None, reward=80, expires=1440):
        return publish_commission(self.npc, dict(title="饭店委托", reward_gold=reward,
            valid_for_minutes=expires, default_location_code="RESTAURANT", stages=stages or [
                dict(id="supplies", title="准备物资", conditions=[dict(id="wood", type="item", item_ids=[1002], target=2)]),
                dict(id="work", title="整理", location_code="REPAIR_YARD", conditions=[
                    dict(id="labor", type="labor", target=2, unit_minutes=15)]),
            ]), uuid.uuid4(), self.clock.world_minute)

    def action(self, action, commission, **params):
        player = params.pop("player", None)
        location = params.pop("location", "RESTAURANT")
        rid = params.pop("rid", None)
        payload = self.payload(action, dict(commission_id=commission["commission_id"], **params),
                               player=player, location=location, rid=rid)
        payload["authority_session_id"] = self.nonce
        return execute_player_action(payload)

    def accepted(self, **kwargs):
        commission = self.publish(**kwargs)
        result = self.action("commission_accept", commission)
        self.assertTrue(result["success"], result)
        return commission

    def ready(self):
        commission = self.accepted()
        self.slot(2, item_id=1002)
        result = self.action("commission_contribute", commission, condition_id="wood", quantity=2, item_id=1002)
        self.assertTrue(result["success"], result)
        result = self.action("commission_labor_start", commission, condition_id="labor", location="REPAIR_YARD")
        self.assertTrue(result["success"], result)
        return commission, result["data"]["result"]

    def tick(self, labor, advance=0, **overrides):
        self.clock.world_minute += advance
        self.clock.time_revision += 1
        self.clock.save()
        data = dict(player_id=str(self.a.pk), labor_id=labor["labor_id"], authority_session_id=self.nonce,
                    current_location_code="REPAIR_YARD", present=True, observed_world_minute=self.clock.world_minute)
        data.update(overrides)
        return tick_player_labor(data)

    def test_no_automatic_commission_or_retired_supply_action(self):
        for _ in range(2):
            view = self.run_action("overview")
            self.assertTrue(view["success"], view)
            self.assertEqual(view["data"]["overview"]["commissions"], [])
            self.assertNotIn("contracts", view["data"]["overview"])
            self.assertNotIn("my_claims", view["data"]["overview"])
        self.assertFalse(VillagePersonalCommission.objects.exists())
        for action, params in (("claim", dict(contract_id=str(uuid.uuid4()), quantity=1)),
                               ("deliver", dict(claim_id=str(uuid.uuid4()), quantity=1)),
                               ("cancel", dict(claim_id=str(uuid.uuid4())))):
            self.assertEqual(self.run_action(action, params)["result_code"], "INVALID_PARAMS")

    def test_only_acceptor_can_deliver_and_future_stage_is_locked(self):
        commission = self.accepted()
        self.slot(2, item_id=1002, bag=self.bag_b)
        denied = self.action("commission_contribute", commission, player=self.b, condition_id="wood", quantity=2, item_id=1002)
        self.assertFalse(denied["success"], denied)
        self.assertEqual(self.bag_b.slots.get().item_amount, 2)
        self.assertFalse(self.action("commission_labor_start", commission, condition_id="labor", location="REPAIR_YARD")["success"])
        self.assertFalse(self.action("commission_accept", commission, player=self.b)["success"])

    def test_items_stay_in_escrow_until_real_labor_completes_and_replay_pays_once(self):
        commission, labor = self.ready()
        self.assertEqual(self.lucy.slots.filter(item_id=1002).count(), 0)
        self.assertEqual(VillagePersonalCommissionEscrow.objects.get().quantity, 2)
        first = self.tick(labor, 15)
        self.assertEqual(first["data"]["credited_units"], 1)
        self.bag_a.refresh_from_db(); self.assertEqual(self.bag_a.money, 100)
        done = self.tick(labor, 15)
        self.assertEqual(done["data"]["earned_gold"], 80)
        self.assertEqual(done["data"]["labor"]["status"], "COMPLETED")
        self.assertEqual(self.tick(labor)["data"]["credited_units"], 0)
        self.bag_a.refresh_from_db(); self.lucy.refresh_from_db()
        self.assertEqual((self.bag_a.money, self.lucy.money), (180, 920))
        self.assertEqual(self.lucy.slots.get(item_id=1002).item_amount, 2)
        self.assertEqual(VillagePersonalCommission.objects.get().status, "COMPLETED")

    def test_abandon_returns_items_without_refreshing_original_batch_freshness(self):
        commission = self.accepted()
        source = self.slot(2, item_id=1002, quality=4, expires_world_minute=250,
                           dish_metadata={"origin": "original"})
        self.assertTrue(self.action("commission_contribute", commission, condition_id="wood", quantity=2, item_id=1002)["success"])
        self.assertTrue(self.action("commission_abandon", commission)["success"])
        self.lucy.refresh_from_db(); self.assertEqual(self.lucy.money, 1000)
        returned = self.bag_a.slots.filter(item_id=1002, item_amount__gt=0).get()
        self.assertEqual((returned.item_amount, returned.quality, returned.expires_world_minute), (2, 4, 250))
        self.assertEqual(returned.dish_metadata, {"origin": "original"})

    def test_wrong_location_and_missing_presence_never_credit_labor(self):
        commission = self.accepted()
        self.slot(2, item_id=1002)
        self.assertFalse(self.action("commission_contribute", commission, condition_id="wood", quantity=2,
                                    item_id=1002, location="REPAIR_YARD")["success"])
        self.assertEqual(self.bag_a.slots.get().item_amount, 2)
        self.assertTrue(self.action("commission_contribute", commission, condition_id="wood", quantity=2, item_id=1002)["success"])
        self.assertFalse(self.action("commission_labor_start", commission, condition_id="labor")["success"])
        started = self.action("commission_labor_start", commission, condition_id="labor", location="REPAIR_YARD")
        self.assertTrue(started["success"], started)
        result = self.tick(started["data"]["result"], 15, present=False)
        self.assertEqual(result["data"]["credited_units"], 0)
        self.assertEqual(result["data"]["labor"]["status"], "CANCELLED")

    def test_expiry_refunds_even_without_any_player_action(self):
        from village.runtime_services import TimeService
        commission = self.accepted(expires=30)
        self.slot(2, item_id=1002)
        self.assertTrue(self.action("commission_contribute", commission, condition_id="wood", quantity=2, item_id=1002)["success"])
        self.assertTrue(TimeService.receive_time(131, self.clock.time_revision + 1)[0])
        self.assertEqual(VillagePersonalCommission.objects.get().status, "EXPIRED")
        self.lucy.refresh_from_db(); self.assertEqual(self.lucy.money, 1000)
        self.assertEqual(sum(s.item_amount for s in self.bag_a.slots.all()), 2)

    def test_personal_and_public_labor_share_one_presence_session(self):
        commission, labor = self.ready()
        public = create_resource_affair("PUBLIC_WORK", "公共劳动", {"stages": [dict(id="work", conditions=[
            dict(id="public_work", type="labor", target=1, unit_minutes=10)])]}, ["REPAIR_YARD"], 100)
        payload = self.payload("labor_start", dict(affair_id=public.pk, condition_id="public_work"), location="REPAIR_YARD")
        payload["authority_session_id"] = self.nonce
        self.assertEqual(execute_player_action(payload)["result_code"], "LABOR_ALREADY_ACTIVE")
        self.assertEqual(VillagePlayerAffairLabor.objects.filter(status="ACTIVE").count(), 1)

    def test_observer_cannot_claim_and_original_response_is_idempotent(self):
        commission = self.publish()
        payload = self.payload("commission_accept", dict(commission_id=commission["commission_id"]))
        self.assertEqual(execute_player_action({**payload, "read_only": True})["result_code"], "MODE_READ_ONLY")
        first = execute_player_action(payload)
        self.assertTrue(first["success"], first)
        self.assertEqual(first, execute_player_action(copy.deepcopy(payload)))
        overview = self.run_action("overview")["data"]["overview"]
        self.assertEqual(overview["my_commissions"][0]["commission_id"], commission["commission_id"])

    def test_labor_stops_at_stage_boundary_and_stale_presence_cannot_accumulate(self):
        commission = self.accepted(stages=[
            dict(id="first", conditions=[dict(id="one", type="labor", target=1, unit_minutes=10)]),
            dict(id="second", conditions=[dict(id="two", type="labor", target=1, unit_minutes=10)]),
        ])
        start = self.action("commission_labor_start", commission, condition_id="one")
        self.assertTrue(start["success"], start)
        result = self.tick(start["data"]["result"], 30, current_location_code="RESTAURANT")
        self.assertEqual(result["data"]["credited_units"], 1)
        self.assertEqual(result["data"]["labor"]["status"], "COMPLETED")
        row = VillagePersonalCommission.objects.get()
        self.assertEqual(row.progress["conditions"].get("two", 0), 0)
        start = self.action("commission_labor_start", commission, condition_id="two")
        self.assertTrue(start["success"], start)
        VillagePlayerAffairLabor.objects.filter(pk=start["data"]["result"]["labor_id"]).update(
            last_seen_at=timezone.now()-timedelta(seconds=11))
        result = self.tick(start["data"]["result"], 10, current_location_code="RESTAURANT")
        self.assertEqual(result["data"]["credited_units"], 0)
        self.assertEqual(result["data"]["labor"]["stop_reason"], "PRESENCE_EXPIRED")
