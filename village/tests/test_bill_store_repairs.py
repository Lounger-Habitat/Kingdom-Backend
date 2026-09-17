"""Approved Bill/store regressions. Run only with Django's isolated test DB."""
import time
import uuid
from unittest.mock import patch

from django.test import TestCase, TransactionTestCase
from game_time.models import GlobalGameTimeState
from inventory.models import NpcGlobalInventory, NpcGlobalInventorySlot, InventoryBag, InventorySlot
from account.models import GameAccount
from items.models import Item, ItemType
from village.models import (NpcRuntimeState, NpcProfile, VillageActionReceipt, EconomicLedgerEntry,
    VillageFarmPlot, VillageStoreInventorySlot, VillageStoreListing, VillageTradeOffer,
    VillageAccount, NpcTradeOrder, NpcTradeDelivery, VillageSimulationConfig)
from village.phase3_asset_service import commit_economy_action, consume_fefo, consume_tool_use, get_available_slot_amount
from village.runtime_services import CommandService, ActionService
from village.phase2_services import TickService
from village.phase5_story import complete_story_action
from village.tests.test_phase3_production import ProductionTestBase, _payload
from village.tests.test_phase3_direct_trade import DirectTradeTestBase
from village.tests import test_phase5_story as story_tests
from village.tests import test_phase2_life_actions as life_tests
from marketplace import services as market


def reserve(slot, seller, buyer, amount):
    common = dict(item_id=slot.item_id, amount=amount, unit_price=1,
                  delivery_point_code="MARKET_STREET", expires_world_minute=900)
    sell = NpcTradeOrder.objects.create(npc=seller, side="SELL", request_id=uuid.uuid4(), **common)
    buy = NpcTradeOrder.objects.create(npc=buyer, side="BUY", request_id=uuid.uuid4(), **common)
    return NpcTradeDelivery.objects.create(seller_order=sell, buyer_order=buy,
        seller_slot_instance_id=slot.instance_id, reserved_amount=amount, reserved_money=amount,
        delivery_point_code="MARKET_STREET", expires_world_minute=900)


class BillAssetRegression(ProductionTestBase):
    def test_failed_plant_rolls_back_even_after_seed_was_consumed_and_replays_failure(self):
        payload = _payload("PLANT", 10011, "BILL_FARM", {"plot_code":"FARM_PLOT_A", "crop_code":"CORN"})
        with patch("village.phase3_production.consume_tool_use", side_effect=ValueError("TOOL_BROKEN")):
            result = commit_economy_action(payload)
        self.assertEqual(result["result_code"], "P3_TOOL_BROKEN")
        self.bill_seeds.refresh_from_db(); self.bill_inv.refresh_from_db()
        self.assertEqual(self.bill_seeds.item_amount, 10)
        self.assertEqual(self.bill_inv.asset_version, 0)
        self.assertEqual(VillageFarmPlot.objects.get(plot_code="FARM_PLOT_A").status, "EMPTY")
        self.assertFalse(EconomicLedgerEntry.objects.filter(request_id=payload["request_id"]).exists())
        replay = commit_economy_action(payload); replay["replayed"] = False
        self.assertEqual(replay, result)
        self.assertFalse(VillageActionReceipt.objects.get(request_id=payload["request_id"]).success)
        self.assertEqual(self._plant()["result_code"], "OK")

    def test_three_tools_have_sixty_physical_uses_and_unknown_is_preserved(self):
        self.bill_farm_tool.item_amount = 3; self.bill_farm_tool.save()
        for used in range(60):
            consume_tool_use(10011, 4001, 700)
            slots = list(self.bill_inv.slots.filter(item_id=4001))
            self.assertEqual(sum(s.item_amount for s in slots), 3)
            self.assertEqual(sum(s.item_amount * s.remaining_uses for s in slots), 59-used)
        with self.assertRaisesRegex(ValueError, "TOOL_BROKEN"):
            consume_tool_use(10011, 4001, 700)
        unknown = NpcGlobalInventorySlot.objects.create(bag=self.bill_inv, slot_index=20,
            item_id=4001, item_amount=2, remaining_uses=None)
        with self.assertRaisesRegex(ValueError, "TOOL_DURABILITY_UNKNOWN"):
            consume_tool_use(10011, 4001, 700)
        unknown.refresh_from_db(); self.assertIsNone(unknown.remaining_uses)
        self.assertEqual(unknown.item_amount, 2)

    def test_expiry_reservations_and_multi_batch_shortfall_are_atomic(self):
        self.bill_seeds.expires_world_minute = 701; self.bill_seeds.save()
        reservation = reserve(self.bill_seeds, self.bill, self.jack, 8)
        self.assertEqual(get_available_slot_amount(self.bill_seeds, 700), 2)
        with self.assertRaises(ValueError): consume_fefo(10011, (1051,), 3, 700)
        self.bill_seeds.refresh_from_db(); self.assertEqual(self.bill_seeds.item_amount, 10)
        consume_fefo(10011, (1051,), 2, 700)
        self.bill_seeds.refresh_from_db(); self.assertEqual(self.bill_seeds.item_amount, 8)
        reservation.status="CANCELLED"; reservation.save()
        self.assertEqual(get_available_slot_amount(self.bill_seeds, 700), 8)
        self.assertEqual(get_available_slot_amount(self.bill_seeds, 701), 0)
        with self.assertRaises(ValueError): consume_fefo(10011, (1051,), 1, 702)

    def test_plant_start_rejects_missing_broken_unknown_and_invalid_tools_without_consuming_seed(self):
        NpcProfile.objects.update_or_create(npc=self.bill,defaults={"display_name":"Bill","occupation":"farmer",
            "home_location_code":"BILL_HOME","work_location_code":"BILL_FARM","social_location_code":"VILLAGE_SQUARE",
            "capabilities":["PLANT"]})
        for uses in [0,None,-1]:
            self.bill_farm_tool.remaining_uses=uses; self.bill_farm_tool.save()
            ok,code,c=CommandService.accept_command(envelope_id=str(uuid.uuid4()),sender="10011",recipient="WuxiVillage",
                timestamp_ms=int(time.time()*1000),content={"schema_version":"village.command.v1","id":str(uuid.uuid4()),
                    "request_id":str(uuid.uuid4()),"name":"PLANT","params":{"plot_code":"FARM_PLOT_A","crop_code":"CORN"},
                    "queue_mode":"STRICT","not_before_world_minute":700,"ttl_ms":120000})
            self.assertTrue(ok,code); self.assertFalse(CommandService.start_command(c.command_id)[0])
            self.bill_seeds.refresh_from_db(); self.bill_farm_tool.refresh_from_db()
            self.assertEqual(self.bill_seeds.item_amount,10); self.assertEqual(self.bill_farm_tool.remaining_uses,uses)


class StoreSellerRegression(DirectTradeTestBase):
    def setUp(self):
        super().setUp()
        Item.objects.filter(item_id=1030).update(item_price=5)
        self.store_account.balance=200; self.store_account.save()

    def sell(self, npc=10011, **overrides):
        params = dict(target_npc_id="10015", side="SELL", store_code="GENERAL_STORE",
                      item_id=1030, amount=4)
        params.update(overrides)
        return commit_economy_action(_payload("PROPOSE_TRADE", npc, "GENERAL_STORE", params))

    def test_all_four_sell_immediately_at_item_value_without_nora_or_offer(self):
        public_before = VillageAccount.objects.get(account_code="PUBLIC_FUND").balance
        nora_before=self.nora_inv.money
        NpcRuntimeState.objects.filter(npc=self.nora).update(current_location_code="NORA_HOME",
            action_status="AWAY", current_action="SLEEP", away_until_world_minute=900)
        for npc in [self.bill, self.jack, self.lucy, self.mark]:
            inv, _ = NpcGlobalInventory.objects.get_or_create(npc=npc, defaults={"money":80})
            before=inv.money
            self._give_item(inv, 1030, 4, produced=0, expires=900)
            result=self.sell(int(npc.agent_id), gold_amount=999999, expires_world_minute=1)
            self.assertEqual(result["result_code"], "OK", result)
            self.assertEqual(result["data"]["gold_amount"],20)
            self.assertEqual(result["data"]["unit_price"],5)
            self.assertEqual(result["data"]["status"],"COMPLETED")
            self.assertNotIn("offer_id",result["data"])
            inv.refresh_from_db(); self.assertEqual(inv.money,before+20)
            self.assertFalse(inv.slots.filter(item_id=1030,item_amount__gt=0).exists())
        self.store_account.refresh_from_db(); self.nora_inv.refresh_from_db()
        self.assertEqual(self.store_account.balance,120)
        self.assertEqual(self.nora_inv.money,nora_before)
        self.assertEqual(VillageAccount.objects.get(account_code="PUBLIC_FUND").balance,public_before)
        self.assertFalse(VillageTradeOffer.objects.exists())
        slots=VillageStoreInventorySlot.objects.filter(store=self.store)
        self.assertEqual(sum(s.item_amount for s in slots),16)
        self.assertTrue(all(s.produced_world_minute==0 and s.expires_world_minute==900 for s in slots))
        self.assertFalse(VillageStoreListing.objects.filter(store=self.store).exists())

    def test_store_reserve_no_personal_fallback_exact_message_and_request_replay(self):
        from village.models import VillageDeliveryOutbox
        self._give_item(self.bill_inv,1030,12)
        self.store_account.balance=40; self.store_account.save()
        payload=_payload("PROPOSE_TRADE",10011,"GENERAL_STORE",dict(target_npc_id="10015",
            side="SELL",store_code="GENERAL_STORE",item_id=1030,amount=6))
        self.assertEqual(commit_economy_action(payload)["result_code"],"OK")
        self.assertTrue(commit_economy_action(payload)["replayed"])
        self.store_account.refresh_from_db(); self.assertEqual(self.store_account.balance,10)
        failed=_payload("PROPOSE_TRADE",10011,"GENERAL_STORE",dict(payload["params"]))
        result=commit_economy_action(failed)
        self.assertEqual(result["result_code"],"TRADE_INSUFFICIENT_MONEY")
        self.assertTrue(commit_economy_action(failed)["replayed"])
        self.bill_inv.refresh_from_db(); self.nora_inv.refresh_from_db()
        self.assertEqual(self.bill_inv.money,230); self.assertEqual(self.nora_inv.money,450)
        self.assertEqual(sum(s.item_amount for s in self.bill_inv.slots.filter(item_id=1030)),6)
        outcome=VillageDeliveryOutbox.objects.get(dedupe_key="outcome:"+failed["request_id"])
        self.assertIn("商店金币不足，无法收购",outcome.payload["message"])
        self.assertFalse(EconomicLedgerEntry.objects.filter(request_id=failed["request_id"]).exists())
        altered={**payload,"params":{**payload["params"],"amount":1}}
        self.assertEqual(commit_economy_action(altered)["result_code"],"P3_REQUEST_CONFLICT")

    def test_invalid_store_actor_expired_and_reserved_stock_are_rejected(self):
        self._give_item(self.bill_inv,1030,4,produced=690,expires=701)
        self.assertNotEqual(self.sell(store_code="RESTAURANT_STORE")["result_code"],"OK")
        self.assertNotEqual(self.sell(target_npc_id="10014")["result_code"],"OK")
        self.assertNotEqual(self.sell(side="BUY",gold_amount=20,expires_world_minute=800)["result_code"],"OK")
        self.assertNotEqual(self.sell(npc=10015)["result_code"],"OK")
        slot=self.bill_inv.slots.get(item_id=1030)
        reserve(slot,self.bill,self.jack,1)
        self.assertEqual(self.sell()["result_code"],"TRADE_INSUFFICIENT_ITEM")
        GlobalGameTimeState.objects.filter(pk=1).update(world_minute=701)
        self.assertEqual(self.sell(amount=1)["result_code"],"TRADE_INSUFFICIENT_ITEM")
        self.bill_inv.refresh_from_db(); self.assertEqual(self.bill_inv.money,200)

    def test_sale_checks_seller_store_status_value_version_and_rolls_back_late_failure(self):
        self._give_item(self.bill_inv,1030,4)
        NpcRuntimeState.objects.filter(npc=self.bill).update(action_status="AWAY")
        self.assertNotEqual(self.sell()["result_code"],"OK")
        NpcRuntimeState.objects.filter(npc=self.bill).update(action_status="IDLE")
        self.store.status="CLOSED"; self.store.save()
        self.assertEqual(self.sell()["result_code"],"TRADE_STORE_CLOSED")
        self.store.status="OPEN"; self.store.save()
        Item.objects.filter(item_id=1030).update(item_price=0)
        self.assertEqual(self.sell()["result_code"],"P3_INVALID_PARAMS")
        Item.objects.filter(item_id=1030).update(item_price=7)
        self.assertEqual(self.sell(expected_seller_asset_version=999)["result_code"],"TRADE_ASSET_VERSION_CONFLICT")
        with patch("village.phase3_direct_trade.VillageStoreInventorySlot.objects.create",side_effect=ValueError("TEST_LATE_FAILURE")):
            with self.assertRaisesRegex(ValueError,"TEST_LATE_FAILURE"): self.sell()
        self.bill_inv.refresh_from_db(); self.assertEqual(self.bill_inv.money,200)
        self.assertEqual(self.bill_inv.slots.get(item_id=1030).item_amount,4)
        self.assertEqual(self.sell()["data"]["gold_amount"],28)

    def test_query_exposes_own_value_but_no_pending_sale_or_private_counterparty_bag(self):
        from village.agent_queries import _inventory,_trade_offers
        from village.models import VillageWorldEvent, VillageDeliveryOutbox
        self._give_item(self.bill_inv,1030,4)
        own=_inventory(self.bill)
        self.assertEqual(own["items"][0]["sale"],{"target":"GENERAL_STORE","unit_price":5})
        result=self.sell()
        for npc in [self.bill,self.nora,self.jack]: self.assertFalse(_trade_offers(npc)["offers"])
        self.assertFalse({"inventory","slots","personal_balance","nora_balance"}.intersection(result["data"]))
        event=VillageWorldEvent.objects.get(request_id=result["request_id"])
        self.assertIn("交易已完成",event.hub_summary)
        outcome=VillageDeliveryOutbox.objects.get(dedupe_key="outcome:"+result["request_id"])
        self.assertIn("已向商店售出",outcome.payload["message"])
        self.assertNotIn("等待",outcome.payload["message"])

    def test_old_pending_store_sell_cannot_settle_arbitrary_price_but_can_cancel(self):
        self._give_item(self.bill_inv,1030,4)
        offer=VillageTradeOffer.objects.create(buying_store=self.store,buyer_npc=self.nora,
            seller_npc=self.bill,proposer_npc=self.bill,item_id=1030,amount=4,gold_amount=100000,
            location_code="GENERAL_STORE",status="PENDING",expires_world_minute=800,
            created_request_id=uuid.uuid4(),created_world_minute=700)
        result=commit_economy_action(_payload("RESPOND_TRADE",10015,"GENERAL_STORE",
            {"offer_id":str(offer.pk),"decision":"ACCEPT"}))
        self.assertEqual(result["result_code"],"TRADE_STORE_FORBIDDEN")
        result=commit_economy_action(_payload("CANCEL_TRADE_OFFER",10011,"GENERAL_STORE",
            {"offer_id":str(offer.pk)}))
        self.assertEqual(result["result_code"],"OK")
        self.bill_inv.refresh_from_db(); self.assertEqual(self.bill_inv.money,200)


class RepairWorkRegression(TestCase):
    meeting = story_tests.PhaseFiveStoryTest.meeting
    proposal_terms = story_tests.PhaseFiveStoryTest.proposal_terms
    def setUp(self):
        story_tests.PhaseFiveStoryTest.setUp(self)
        self.arc,self.fact=story_tests.PhaseFiveStoryTest.story_arc(self)
        self.config.village_needs_enabled=True; self.config.save()
        self.rt=NpcRuntimeState.objects.get(npc=self.npcs["10011"])
        self.rt.current_location_code="REPAIR_YARD"
        self.rt.energy_fp=1000; self.rt.health_fp=1000; self.rt.hunger_fp=0; self.rt.save()
        NpcProfile.objects.update_or_create(npc=self.npcs["10011"],defaults={
            "display_name":"Bill","occupation":"farmer","home_location_code":"BILL_HOME",
            "work_location_code":"BILL_FARM","social_location_code":"VILLAGE_SQUARE","capabilities":["STORY_ACT"]})

    def accept(self, **params):
        rid=str(uuid.uuid4())
        ok,code,command=CommandService.accept_command(envelope_id=str(uuid.uuid4()), sender="10011",
            recipient="WuxiVillage",timestamp_ms=int(time.time()*1000),content={
                "schema_version":"village.command.v1","id":str(uuid.uuid4()),"request_id":rid,
                "name":"STORY_ACT","params":{"action_code":"REPAIR_LABOR","decision_id":str(self.decision.pk),**params},
                "queue_mode":"STRICT","not_before_world_minute":0,"expires_world_minute":500,
                "ttl_ms":120000,"duration_minutes":1})
        self.assertTrue(ok,code); return command

    def tick(self, minute):
        result=TickService.advance({"request_id":str(uuid.uuid4()),"world_minute":minute,"time_revision":minute+1})
        self.assertTrue(result["success"],result); return result

    def test_zero_59_60_minutes_one_hour_and_no_double_needs_after_replay(self):
        c=self.accept(); self.assertEqual(c.duration_minutes,60)
        self.assertTrue(CommandService.start_command(c.command_id)[0])
        self.assertFalse(ActionService.complete_action(c.command_id,c.request_id,True,"OK")[0])
        self.tick(59)
        self.assertFalse(ActionService.complete_action(c.command_id,c.request_id,True,"OK")[0])
        self.fact.refresh_from_db(); self.assertEqual(self.fact.repair_labor_hours,0)
        self.tick(60)
        result=ActionService.complete_action(c.command_id,c.request_id,True,"OK")
        self.assertTrue(result[0],result); self.assertTrue(result[2].success,result[2].response_data)
        self.fact.refresh_from_db(); self.rt.refresh_from_db()
        self.assertEqual(self.fact.repair_labor_hours,1); self.assertEqual(self.rt.energy_fp,920)
        ActionService.complete_action(c.command_id,c.request_id,True,"OK")
        self.fact.refresh_from_db(); self.rt.refresh_from_db()
        self.assertEqual(self.fact.repair_labor_hours,1); self.assertEqual(self.rt.energy_fp,920)

    def test_parallel_work_and_forged_direct_completion_fail(self):
        c=self.accept(); self.assertTrue(CommandService.start_command(c.command_id)[0])
        c2=self.accept(); self.assertFalse(CommandService.start_command(c2.command_id)[0])
        result=complete_story_action({"request_id":str(uuid.uuid4()),"npc_id":"10011",
            "current_location_code":"REPAIR_YARD","params":{"action_code":"REPAIR_LABOR","labor_hours":99}})
        self.assertNotEqual(result["result_code"],"OK")
        self.fact.refresh_from_db(); self.assertEqual(self.fact.repair_labor_hours,0)

    def test_tick_stops_injury_and_does_not_refund_elapsed_work(self):
        c=self.accept(); self.assertTrue(CommandService.start_command(c.command_id)[0])
        self.tick(20)
        NpcRuntimeState.objects.filter(pk=self.rt.pk).update(health_fp=200)
        self.tick(21); c.refresh_from_db(); self.rt.refresh_from_db(); self.fact.refresh_from_db()
        self.assertEqual(c.status,"FAILED"); self.assertIsNone(self.rt.active_command_id)
        self.assertLess(self.rt.energy_fp,1000); self.assertEqual(self.fact.repair_labor_hours,0)
        self.assertFalse(VillageActionReceipt.objects.get(request_id=c.request_id).success)

    def test_recovery_cancellation_consumes_elapsed_time_once_and_does_not_credit_labor(self):
        c=self.accept(); self.assertTrue(CommandService.start_command(c.command_id)[0])
        self.tick(30)
        self.assertTrue(CommandService.cancel_command(c.command_id)[0])
        self.rt.refresh_from_db(); energy=self.rt.energy_fp
        self.assertTrue(CommandService.cancel_command(c.command_id)[0])
        self.rt.refresh_from_db(); self.fact.refresh_from_db()
        self.assertEqual(self.rt.energy_fp,energy); self.assertEqual(energy,960)
        self.assertEqual(self.fact.repair_labor_hours,0)

    def test_expired_decision_and_low_energy_cannot_start_work(self):
        self.decision.terms={**self.decision.terms,"expires_world_minute":0}; self.decision.save()
        c=self.accept(); self.assertFalse(CommandService.start_command(c.command_id)[0])
        self.decision.terms={**self.decision.terms,"expires_world_minute":1000}; self.decision.save()
        NpcRuntimeState.objects.filter(pk=self.rt.pk).update(energy_fp=100)
        c=self.accept(); self.assertFalse(CommandService.start_command(c.command_id)[0])
        self.fact.refresh_from_db(); self.assertEqual(self.fact.repair_labor_hours,0)

    def test_tick_between_accept_and_start_is_allowed_but_real_state_change_is_not(self):
        c=self.accept(); self.tick(1)
        self.assertTrue(CommandService.start_command(c.command_id, start_world_minute=1)[0])
        CommandService.cancel_command(c.command_id)
        c=self.accept()
        self.rt.refresh_from_db(); self.rt.state_version+=1; self.rt.save()
        self.tick(2)
        self.assertEqual(CommandService.start_command(c.command_id)[1],"STALE_STATE_VERSION")

    def test_plan_change_and_away_stop_work(self):
        for change in ["AWAY","PLAN"]:
            c=self.accept(); self.assertTrue(CommandService.start_command(c.command_id)[0])
            if change=="AWAY": NpcRuntimeState.objects.filter(pk=self.rt.pk).update(action_status="AWAY")
            else:
                self.arc.current_plan="FOREST_ROUTE"; self.arc.save()
            self.tick(1 if change=="AWAY" else 2)
            c.refresh_from_db(); self.assertEqual(c.status,"FAILED")
            NpcRuntimeState.objects.filter(pk=self.rt.pk).update(action_status="IDLE")
        self.fact.refresh_from_db(); self.assertEqual(self.fact.repair_labor_hours,0)

    def test_mixed_donation_rolls_back_and_only_wood_1002_counts(self):
        bag=NpcGlobalInventory.objects.get(npc=self.npcs["10011"])
        bag.money=5; bag.save()
        wood=NpcGlobalInventorySlot.objects.create(bag=bag,slot_index=1,item_id=1002,item_amount=5)
        seed=NpcGlobalInventorySlot.objects.create(bag=bag,slot_index=2,item_id=1003,item_amount=5)
        def donation(item,gold=0,rid=None):
            return complete_story_action({"request_id":rid or str(uuid.uuid4()),"npc_id":"10011",
                "current_location_code":"REPAIR_YARD","params":{"action_code":"DONATE_REPAIR_RESOURCES","decision_id":str(self.decision.pk),
                    "item_id":item,"amount":2,"gold_amount":gold}})
        self.assertNotEqual(donation(1002,10)["result_code"],"OK")
        self.assertNotEqual(donation(1003)["result_code"],"OK")
        wood.refresh_from_db(); seed.refresh_from_db(); bag.refresh_from_db(); self.fact.refresh_from_db()
        self.assertEqual((wood.item_amount,seed.item_amount,bag.money,self.fact.repair_wood),(5,5,5,0))
        rid=str(uuid.uuid4()); self.assertEqual(donation(1002,2,rid)["result_code"],"OK")
        donation(1002,2,rid)
        wood.refresh_from_db(); bag.refresh_from_db(); self.fact.refresh_from_db()
        self.assertEqual((wood.item_amount,bag.money,self.fact.repair_wood),(3,3,2))


class MarketplaceFreshnessRegression(DirectTradeTestBase):
    def test_worn_tool_sale_keeps_physical_uses_after_transfer(self):
        slot=NpcGlobalInventorySlot.objects.create(bag=self.bill_inv,slot_index=0,item_id=4001,item_amount=3,remaining_uses=20)
        worn=consume_tool_use(10011,4001,700)
        listing,error=market.create_npc_listing(self.bill,worn.instance_id,1,2); self.assertIsNone(error)
        _,error=market.buy_listing_for_npc(self.jack,listing.pk,1); self.assertIsNone(error)
        received=self.jack_inv.slots.get(item_id=4001)
        self.assertEqual(received.remaining_uses,19)
        consume_tool_use(10012,4001,700)
        slots=NpcGlobalInventorySlot.objects.filter(bag__in=[self.bill_inv,self.jack_inv],item_id=4001)
        self.assertEqual(sum(s.item_amount for s in slots),3)
        self.assertEqual(sum(s.item_amount*s.remaining_uses for s in slots),58)

    def test_npc_player_npc_round_trip_keeps_source_and_expiry(self):
        self._give_item(self.bill_inv,1030,4,produced=0,expires=750)
        slot=self.bill_inv.slots.get(item_id=1030); source=uuid.uuid4()
        slot.source_request_id=source; slot.save()
        player=GameAccount.objects.create(username="batch-carrier",password="!")
        bag=InventoryBag.objects.create(player=player,character_id=player.username,money=500)
        listing,error=market.create_npc_listing(self.bill,slot.instance_id,4,2); self.assertIsNone(error)
        _,error=market.buy_listing(player,listing.pk,4); self.assertIsNone(error)
        pslot=bag.slots.get(item_id=1030)
        self.assertEqual((pslot.produced_world_minute,pslot.expires_world_minute,pslot.source_request_id),(0,750,source))
        listing,error=market.create_listing(player,pslot.instance_id,4,2); self.assertIsNone(error)
        _,error=market.buy_listing_for_npc(self.jack,listing.pk,2); self.assertIsNone(error)
        received=self.jack_inv.slots.get(item_id=1030)
        self.assertEqual((received.expires_world_minute,received.source_request_id),(750,source))
        GlobalGameTimeState.objects.filter(pk=1).update(world_minute=750)
        _,error=market.buy_listing_for_npc(self.jack,listing.pk,1); self.assertIsNotNone(error)
        _,error=market.delist_listing(player,listing.pk); self.assertIsNone(error)
        returned=bag.slots.get(item_id=1030,item_amount__gt=0)
        self.assertEqual(returned.expires_world_minute,750)
        with self.assertRaises(ValueError): consume_fefo(10012,(1030,),1,750)

    def test_unknown_legacy_perishable_is_blocked_and_cancel_does_not_make_it_eternal(self):
        self._give_item(self.bill_inv,1030,2,expires=900)
        slot=self.bill_inv.slots.get(item_id=1030)
        listing,error=market.create_npc_listing(self.bill,slot.instance_id,2,2); self.assertIsNone(error)
        listing.item_snapshot={"itemID":1030,"itemAmount":2}; listing.save()
        _,error=market.buy_listing_for_npc(self.jack,listing.pk,1); self.assertIsNotNone(error)
        _,error=market.delist_npc_listing(self.bill,listing.pk); self.assertIsNone(error)
        returned=self.bill_inv.slots.get(item_id=1030,item_amount__gt=0)
        self.assertTrue(returned.freshness_unknown)
        self.assertEqual(get_available_slot_amount(returned,700),0)

    def test_reserved_stock_cannot_be_escrowed_twice(self):
        self._give_item(self.bill_inv,1030,4,expires=900)
        slot=self.bill_inv.slots.get(item_id=1030); reserve(slot,self.bill,self.jack,3)
        _,error=market.create_npc_listing(self.bill,slot.instance_id,2,2); self.assertIsNotNone(error)
        _,error=market.create_npc_listing(self.bill,slot.instance_id,1,2); self.assertIsNone(error)
        slot.refresh_from_db(); self.assertEqual(slot.item_amount,3)
        self.assertEqual(get_available_slot_amount(slot,700),0)


class PersonalEatingFreshnessRegression(TestCase):
    setUp=life_tests.LifeActionServiceTests.setUp
    payload=life_tests.LifeActionServiceTests.payload
    arm=life_tests.LifeActionServiceTests.arm

    def test_eating_checks_expiry_at_settlement_and_preserves_reserved_food(self):
        from village.phase2_services import LifeActionService
        for mode in ["expired","unknown","reserved"]:
            self.slot.expires_world_minute=120 if mode=="expired" else 121
            self.slot.freshness_unknown=mode=="unknown"; self.slot.save()
            if mode=="reserved": reserve(self.slot,self.npc,self.other,1)
            self.runtime.refresh_from_db(); before=self.runtime.hunger_fp
            payload=self.payload("EAT",{"source":"PERSONAL_INVENTORY","food_instance_id":str(self.slot.instance_id)},
                version=self.runtime.state_version)
            self.arm(payload)
            result=LifeActionService.execute(payload)
            self.assertFalse(result["success"],result)
            self.slot.refresh_from_db(); self.runtime.refresh_from_db()
            self.assertEqual(self.slot.item_amount,1); self.assertEqual(self.runtime.hunger_fp,before)


class BatchStoreSaleRegression(DirectTradeTestBase):
    def setUp(self):
        super().setUp()
        Item.objects.filter(item_id=1030).update(item_price=5)
        self.store_account.balance=200; self.store_account.save()

    def batch_payload(self, rows=None, npc=10011, **extra):
        params = {"side": "SELL", "store_code": "GENERAL_STORE",
                  "items": rows if rows is not None else [
                      {"item_id": 1030, "amount": 4}, {"item_id": 1049, "amount": 5}]}
        params.update(extra)
        return _payload("PROPOSE_TRADE", npc, "GENERAL_STORE", params)

    def test_four_sellers_batch_without_target_preserves_batches_and_balances(self):
        NpcRuntimeState.objects.filter(npc=self.nora).update(action_status="AWAY", away_until_world_minute=900)
        for npc in [self.bill, self.jack, self.lucy, self.mark]:
            inv = NpcGlobalInventory.objects.get(npc=npc)
            before_money, before_version = inv.money, inv.asset_version
            self._give_item(inv, 1030, 4, produced=690, expires=900)
            self._give_item(inv, 1049, 5, produced=680, expires=950)
            result = commit_economy_action(self.batch_payload(npc=int(npc.agent_id)))
            self.assertEqual(result["result_code"], "OK", result)
            self.assertEqual(result["data"]["gold_amount"], 30)
            self.assertEqual(result["data"]["items"], [
                {"item_id":1030,"item_name":"玉米","amount":4,"unit_price":5,"gold_amount":20},
                {"item_id":1049,"item_name":"盐","amount":5,"unit_price":2,"gold_amount":10}])
            self.assertNotIn("item_id", result["data"])
            inv.refresh_from_db()
            self.assertEqual(inv.money, before_money + 30)
            self.assertEqual(inv.asset_version, before_version + 1)
            self.assertFalse(inv.slots.filter(item_id__in=[1030,1049],item_amount__gt=0).exists())
            changes = EconomicLedgerEntry.objects.get(request_id=result["request_id"]).changes
            self.assertEqual(changes[str(npc.agent_id)], {"money":30,"1030":-4,"1049":-5})
            self.assertEqual(changes["STORE:GENERAL_STORE"], {"money":-30,"1030":4,"1049":5})
        self.store_account.refresh_from_db(); self.store.refresh_from_db(); self.nora_inv.refresh_from_db()
        self.assertEqual((self.store_account.balance,self.store_account.account_version,self.store.inventory_version), (80,4,4))
        self.assertEqual(self.nora_inv.money, 450)
        self.assertEqual(VillageAccount.objects.get(account_code="PUBLIC_FUND").balance,500)
        self.assertFalse(VillageTradeOffer.objects.exists())
        self.assertFalse(VillageStoreListing.objects.filter(store=self.store).exists())
        slots = VillageStoreInventorySlot.objects.filter(store=self.store)
        self.assertEqual(slots.count(), 8)
        for slot in slots:
            self.assertEqual((slot.produced_world_minute,slot.expires_world_minute),
                             (690,900) if slot.item_id==1030 else (680,950))

    def test_later_item_shortfall_rolls_back_earlier_item_and_store_batch(self):
        self._give_item(self.bill_inv,1030,4,expires=900)
        self._give_item(self.bill_inv,1049,5,expires=900)
        salt = self.bill_inv.slots.get(item_id=1049)
        for mode in ["missing", "expired", "reserved"]:
            with self.subTest(mode=mode):
                salt.item_amount = 0 if mode=="missing" else 5
                salt.expires_world_minute = 700 if mode=="expired" else 900
                salt.save()
                if mode=="reserved": reserve(salt,self.bill,self.jack,1)
                payload = self.batch_payload()
                result = commit_economy_action(payload)
                self.assertEqual(result["result_code"],"TRADE_INSUFFICIENT_ITEM",result)
                self.assertEqual(result["data"]["unavailable_item_id"],1049)
                self.assertTrue(commit_economy_action(payload)["replayed"])
                self.assertEqual(self.bill_inv.slots.get(item_id=1030).item_amount,4)
                self.assertFalse(VillageStoreInventorySlot.objects.filter(store=self.store).exists())
                self.assertFalse(EconomicLedgerEntry.objects.filter(request_id=payload["request_id"]).exists())
                self.bill_inv.refresh_from_db(); self.store_account.refresh_from_db(); self.store.refresh_from_db()
                self.assertEqual((self.bill_inv.money,self.bill_inv.asset_version), (200,0))
                self.assertEqual((self.store_account.balance,self.store_account.account_version,self.store.inventory_version),(200,0,0))

    def test_whole_batch_cost_not_first_item_controls_affordability(self):
        from village.models import VillageDeliveryOutbox
        self._give_item(self.bill_inv,1030,4); self._give_item(self.bill_inv,1049,5)
        self.store_account.balance=25; self.store_account.save()
        result = commit_economy_action(self.batch_payload())
        self.assertEqual(result["result_code"],"TRADE_INSUFFICIENT_MONEY")
        self.assertEqual(result["data"]["gold_amount"],30)
        self.assertEqual(sum(s.item_amount for s in self.bill_inv.slots.all()),9)
        self.assertFalse(VillageStoreInventorySlot.objects.filter(store=self.store).exists())
        self.assertFalse(EconomicLedgerEntry.objects.exists())
        self.store_account.refresh_from_db(); self.assertEqual(self.store_account.balance,25)
        outcome = VillageDeliveryOutbox.objects.get(dedupe_key="outcome:"+result["request_id"])
        self.assertIn("商店金币不足，无法收购",outcome.payload["message"])

    def test_duplicate_rows_merge_before_stock_check_and_replay_only_pays_once(self):
        self._give_item(self.bill_inv,1030,4); self._give_item(self.bill_inv,1049,5)
        rows=[{"item_id":1030,"amount":3},{"item_id":1030,"amount":2},{"item_id":1049,"amount":5}]
        self.assertEqual(commit_economy_action(self.batch_payload(rows))["result_code"],"TRADE_INSUFFICIENT_ITEM")
        rows[0]["amount"]=2
        payload=self.batch_payload(rows)
        result=commit_economy_action(payload)
        self.assertEqual(result["result_code"],"OK",result)
        self.assertEqual(len(result["data"]["items"]),2)
        self.assertEqual(result["data"]["items"][0]["amount"],4)
        self.assertTrue(commit_economy_action(payload)["replayed"])
        altered={**payload,"params":{**payload["params"],"items":[{"item_id":1030,"amount":1}]}}
        self.assertEqual(commit_economy_action(altered)["result_code"],"P3_REQUEST_CONFLICT")
        self.bill_inv.refresh_from_db(); self.assertEqual(self.bill_inv.money,230)
        self.assertEqual(EconomicLedgerEntry.objects.count(),1)

    def test_batch_shape_is_strict_and_only_valid_for_store_sell(self):
        for rows in [[],None,{},[None],[1],[{}],[{"item_id":1030}],
                     [{"item_id":True,"amount":1}],[{"item_id":1030,"amount":True}],
                     [{"item_id":1030,"amount":0}],[{"item_id":1030,"amount":-1}],
                     [{"item_id":1030,"amount":"1"}],[{"item_id":1030,"amount":1,"unit_price":999}]]:
            with self.subTest(rows=rows):
                self.assertEqual(commit_economy_action(self.batch_payload(items=rows))["result_code"],"P3_INVALID_PARAMS")
        for extra in [{"item_id":1030},{"amount":1},{"store_code":None,"target_npc_id":"10012", "item_id":1030,"amount":1,"gold_amount":5,"expires_world_minute":800},
                      {"side":"BUY","target_npc_id":"10011","item_id":1030,"amount":1,"gold_amount":5,"expires_world_minute":800}]:
            self.assertEqual(commit_economy_action(self.batch_payload(**extra))["result_code"],"P3_INVALID_PARAMS")
        self.assertFalse(EconomicLedgerEntry.objects.exists())

    def test_unknown_later_item_and_late_exception_leave_entire_batch_unchanged(self):
        self._give_item(self.bill_inv,1030,4); self._give_item(self.bill_inv,1049,5)
        result=commit_economy_action(self.batch_payload([{"item_id":1030,"amount":4},{"item_id":999999,"amount":1}]))
        self.assertEqual(result["result_code"],"P3_INVALID_PARAMS")
        create=VillageStoreInventorySlot.objects.create
        def fail_second(**kwargs):
            if kwargs["item_id"]==1049: raise ValueError("TEST_SECOND_ITEM_FAILURE")
            return create(**kwargs)
        with patch("village.phase3_direct_trade.VillageStoreInventorySlot.objects.create",side_effect=fail_second):
            with self.assertRaisesRegex(ValueError,"TEST_SECOND_ITEM_FAILURE"):
                commit_economy_action(self.batch_payload())
        self.assertEqual(sum(s.item_amount for s in self.bill_inv.slots.all()),9)
        self.assertFalse(VillageStoreInventorySlot.objects.filter(store=self.store).exists())
        self.bill_inv.refresh_from_db(); self.store_account.refresh_from_db()
        self.assertEqual((self.bill_inv.money,self.store_account.balance),(200,200))

    def test_legacy_scalar_without_target_and_batch_result_message(self):
        from village.models import VillageWorldEvent, VillageDeliveryOutbox
        self._give_item(self.bill_inv,1030,8); self._give_item(self.bill_inv,1049,5)
        payload=self.batch_payload(item_id=1030,amount=4,gold_amount=999999)
        del payload["params"]["items"]
        result=commit_economy_action(payload)
        self.assertEqual(result["result_code"],"OK",result)
        self.assertEqual((result["data"]["item_id"],result["data"]["gold_amount"]),(1030,20))
        result=commit_economy_action(self.batch_payload())
        self.assertEqual(result["result_code"],"OK",result)
        event=VillageWorldEvent.objects.get(request_id=result["request_id"])
        outcome=VillageDeliveryOutbox.objects.get(dedupe_key="outcome:"+result["request_id"])
        for text in [event.hub_summary,outcome.payload["message"]]:
            self.assertIn("玉米×4",text); self.assertIn("盐×5",text); self.assertIn("30金币",text)
            self.assertNotIn("等待",text)

    def test_agent_discovery_and_rules_explain_batch_without_new_permission(self):
        from village.command_catalog import COMMAND_CATALOG, agent_tool
        from village.agent_queries import _rules
        NpcProfile.objects.update_or_create(npc=self.bill,defaults={"display_name":"Bill","occupation":"farmer",
            "home_location_code":"BILL_HOME","work_location_code":"BILL_FARM","social_location_code":"VILLAGE_SQUARE",
            "capabilities":["PROPOSE_TRADE","GET_RULE"]})
        schema=COMMAND_CATALOG["PROPOSE_TRADE"]["parameters"]
        self.assertNotIn("target_npc_id",schema["required"])
        self.assertEqual(schema["properties"]["items"]["minItems"],1)
        self.assertEqual(schema["properties"]["items"]["items"]["required"],["item_id","amount"])
        self.assertIn("items",str(agent_tool("PROPOSE_TRADE")))
        rules=str(_rules(self.bill))
        self.assertIn('items=[',rules); self.assertIn("不需要target_npc_id",rules)
        self.assertIn("整批失败",rules)


class CompetingStorePaymentsRegression(TransactionTestCase):
    setUp=DirectTradeTestBase.setUp
    _give_item=DirectTradeTestBase._give_item

    def test_simultaneous_direct_sales_never_overdraw_store(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import connections, OperationalError
        Item.objects.filter(item_id=1030).update(item_price=5)
        self.store_account.balance=40; self.store_account.save()
        payloads=[]
        for npc,inv in [(self.bill,self.bill_inv),(self.jack,self.jack_inv),(self.lucy,self.lucy_inv)]:
            self._give_item(inv,1030,4)
            self._give_item(inv,1049,5)
            payloads.append(_payload("PROPOSE_TRADE",int(npc.agent_id),"GENERAL_STORE",
                {"side":"SELL","store_code":"GENERAL_STORE",
                 "items":[{"item_id":1030,"amount":4},{"item_id":1049,"amount":5}]}))
        barrier=Barrier(3)
        def sell(payload):
            barrier.wait()
            try:
                for attempt in range(40):
                    try: return commit_economy_action(payload)["result_code"]
                    except OperationalError as error:
                        if "locked" not in str(error).lower(): raise
                        time.sleep(0.02)
                raise AssertionError("SQLite contention did not resolve")
            finally: connections.close_all()
        with ThreadPoolExecutor(max_workers=3) as executor:
            results=list(executor.map(sell,payloads))
        self.assertEqual(results.count("OK"),1,results)
        self.assertEqual(results.count("TRADE_INSUFFICIENT_MONEY"),2,results)
        self.store_account.refresh_from_db(); self.assertEqual(self.store_account.balance,10)
        self.assertFalse(VillageTradeOffer.objects.exists())
        self.assertEqual(sum(s.item_amount for s in VillageStoreInventorySlot.objects.filter(store=self.store,item_id=1030)),4)
        self.assertEqual(sum(s.item_amount for s in VillageStoreInventorySlot.objects.filter(store=self.store,item_id=1049)),5)
