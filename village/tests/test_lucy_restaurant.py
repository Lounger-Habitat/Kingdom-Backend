"""Isolated cash/item conservation and concrete sample/Agent reply integration."""
import copy
import math
import uuid
from django.test import TestCase
from django.db import transaction
from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot
from items.models import Item
from recipes.models import Recipe
from village.models import (NpcProfile, NpcRuntimeState, VillageSimulationConfig, VillageAccount,
    VillageStore, VillageStoreAccount, VillageStoreInventorySlot, VillageStoreListing, VillageDishReview,
    VillageCommunityMealDailyUsage, VillageStoryArc, VillageStoryFact, VillageStoryMeeting, VillagePublicDecision,
    MarkDailySalary, NpcCookingCounter)
from village.lucy_catalog import RECIPE_DEFINITIONS, MEALS, PUBLIC_MEALS, RELIEF_PACK
from village.lucy_configuration import configure_lucy
from village.phase3_asset_service import commit_economy_action, add_batch, get_available_slot_amount
from village.tests.test_phase3_production import _payload
from village.phase3_direct_trade import query_store_inventory
from village.public_finance import reconcile


class LucyRestaurantTests(TestCase):
    def setUp(self):
        VillageSimulationConfig.objects.create(config_key="default", village_economy_enabled=True,
            village_direct_trade_enabled=True, village_story_enabled=True)
        self.clock = GlobalGameTimeState.get_instance()
        self.clock.world_minute = self.clock.time_revision = 600
        self.clock.save()
        self.npcs, self.bags = {}, {}
        for npc_id, name in (("10011", "Bill"), ("10012", "Jack"), ("10013", "Mark"), ("10014", "Lucy"), ("10015", "Nora")):
            # Historical migrations may seed the NPC catalog before this suite.
            npc, _ = NpcDefinition.objects.get_or_create(agent_id=npc_id, defaults={"name": name})
            self.npcs[npc_id] = npc
            self.bags[npc_id], _ = NpcGlobalInventory.objects.update_or_create(npc=npc, defaults={"money": 100})
            self.bags[npc_id].slots.all().delete()
            NpcRuntimeState.objects.update_or_create(npc=npc, defaults=dict(current_location_code="RECORD_HALL" if npc_id == "10013" else "RESTAURANT",
                health_fp=1000, hunger_fp=600, energy_fp=500, needs_world_minute=600))
            NpcProfile.objects.update_or_create(npc=npc, defaults=dict(display_name=name, occupation="test", home_location_code="RESTAURANT",
                work_location_code="RESTAURANT", social_location_code="VILLAGE_SQUARE", capabilities=[]))
        for item_id in {i for spec in RECIPE_DEFINITIONS.values() for i in spec["ingredients"]}:
            Item.objects.create(item_id=item_id, item_name=f"ingredient-{item_id}", item_price=8, item_type="食材")
        self.fund = VillageAccount.objects.create(account_code="PUBLIC_FUND", balance=0, audit_opening_balance=0)
        self.store, _ = VillageStore.objects.update_or_create(store_code="RESTAURANT_STORE", defaults=dict(display_name="Lucy饭店",
            location_code="RESTAURANT", operator_npc=self.npcs["10014"], status="OPEN", minimum_fee=1))
        self.store.inventory_slots.all().delete()
        self.account, _ = VillageStoreAccount.objects.update_or_create(store=self.store, defaults=dict(balance=0, minimum_operating_balance=200))
        configure_lucy()
        self.lucy = self.bags["10014"]
        for item_id in {i for spec in RECIPE_DEFINITIONS.values() for i in spec["ingredients"]}:
            add_batch(self.lucy, item_id, 100, 500, 3500, uuid.uuid4())

    def act(self, action, params, npc="10014", expect="OK"):
        result = commit_economy_action(_payload(action, int(npc), "RESTAURANT", params))
        self.assertEqual(result["result_code"], expect, result)
        return result

    def cook(self, recipe=29201):
        result = self.act("COOK", {"recipe_id": recipe})
        return NpcGlobalInventorySlot.objects.get(instance_id=result["data"]["dish"]["instance_id"])

    def listing(self, slot, quantity=1, **extra):
        params = dict(store_code="RESTAURANT_STORE", item_id=slot.item_id, amount=quantity,
            price_mode="ORIGINAL", food_instance_id=str(slot.instance_id), **extra)
        return self.act("LIST_STORE_ITEM", params)["data"]["listing_id"]

    def fund_with(self, amount):
        self.fund.balance = self.fund.audit_opening_balance = amount
        self.fund.save()

    def decision(self, budget=100, expiry=4000):
        arc, _ = VillageStoryArc.objects.get_or_create(arc_code="BROKEN_TRADE_ROUTE_V1", defaults={"current_phase":"RATION_PERIOD", "current_plan":"RATION_AND_WAIT"})
        VillageStoryFact.objects.get_or_create(arc=arc)
        meeting = VillageStoryMeeting.objects.create(arc=arc, opened_day=0, closes_day=0,
            meeting_type=str(uuid.uuid4())[:30], status="SETTLED", selected_plan="RATION_AND_WAIT", candidate_plans=["RATION_AND_WAIT"])
        return VillagePublicDecision.objects.create(meeting=meeting, plan_code="RATION_AND_WAIT",
            approved_world_minute=550, published_world_minute=560,
            terms={"budget_gold":budget, "executor_npc_id":"10015", "expires_world_minute":expiry,
                "ration_daily_limits":{"1030":1}, "meal_service":{
                    "executor_npc_id":"10014", "store_code":"RESTAURANT_STORE", "menu_codes":["C1","C2","R1"],
                    "subsidies":{"C1":9,"C2":12,"R1":9}, "beneficiaries":list(self.npcs),
                    "starts_world_minute":560, "per_npc_daily_limit":1, "village_daily_limit":5}})

    def test_all_recipes_consume_real_inputs_and_generate_batch_metadata(self):
        for recipe_id, spec in RECIPE_DEFINITIONS.items():
            with self.subTest(recipe=recipe_id):
                before = {i: sum(self.lucy.slots.filter(item_id=i).values_list("item_amount", flat=True)) for i in spec["ingredients"]}
                slot = self.cook(recipe_id)
                self.assertEqual(slot.item_amount, spec["output"])
                self.assertEqual(slot.dish_metadata["base_quality"], 3)
                self.assertEqual(slot.dish_metadata["recipe_id"], recipe_id)
                self.assertIn(slot.dish_quality, range(2,7))
                self.assertEqual(slot.expires_world_minute, 3480)
                for item_id, count in spec["ingredients"].items():
                    self.assertEqual(before[item_id] - sum(self.lucy.slots.filter(item_id=item_id).values_list("item_amount", flat=True)), count)
        self.assertEqual(Recipe.objects.filter(village_only=True).count(), 13)

    def test_personal_procurement_from_three_suppliers_and_next_round(self):
        self.lucy.slots.all().delete()
        for npc, item, amount, gold in (("10011",1030,1,8),("10012",1056,1,12),("10015",1049,1,2)):
            add_batch(self.bags[npc],item,2,500,3500,uuid.uuid4())
        for round_index in range(2):
            for npc, item, amount, gold in (("10011",1030,1,8),("10012",1056,1,12),("10015",1049,1,2)):
                offer=self.act("PROPOSE_TRADE",{"target_npc_id":npc,"item_id":item,"amount":amount,
                    "gold_amount":gold,"expires_world_minute":3000})["data"]["offer_id"]
                self.act("RESPOND_TRADE",{"offer_id":offer,"decision":"ACCEPT"},npc=npc)
            dish=self.cook(29211)
            listing=self.listing(dish,2)
            self.act("BUY_FROM_STORE",{"listing_id":listing,"amount":1},npc="10011")
            self.act("BUY_FROM_STORE",{"listing_id":listing,"amount":1},npc="10012")
            self.account.refresh_from_db()
            self.act("WITHDRAW_STORE_FUNDS",{"store_code":"RESTAURANT_STORE","amount":self.account.balance})
        self.fund.refresh_from_db();self.account.refresh_from_db()
        total=sum(NpcGlobalInventory.objects.values_list("money",flat=True))+self.fund.balance+self.account.balance
        self.assertEqual(total,500)

    def test_shared_quota_survives_new_decision_and_resets_at_midnight(self):
        self.fund_with(200); first=self.decision(budget=200)
        listings=[self.listing(self.cook(),2,public_meal_code="C1") for _ in range(3)]
        for index,npc in enumerate(self.npcs):
            self.act("BUY_FROM_STORE",{"listing_id":listings[index//2],"amount":1},npc=npc)
        self.assertEqual(VillageCommunityMealDailyUsage.objects.get(day_index=0).total_meals,5)
        second=self.decision(budget=200)
        new_listing=self.listing(self.cook(),2,public_meal_code="C1")
        self.act("BUY_FROM_STORE",{"listing_id":new_listing,"amount":1},npc="10011",expect="PUBLIC_MEAL_DAILY_LIMIT_REACHED")
        self.clock.world_minute=1440;self.clock.save()
        payload=_payload("BUY_FROM_STORE",10011,"RESTAURANT",{"listing_id":new_listing,"amount":1})
        self.assertEqual(commit_economy_action(payload)["result_code"],"OK")
        self.assertTrue(commit_economy_action(payload)["replayed"])
        self.assertEqual(VillageCommunityMealDailyUsage.objects.get(day_index=1).total_meals,1)
        self.account.refresh_from_db();self.assertEqual(self.account.balance,54)

    def test_living_options_reports_public_meal_eligibility_without_private_accounts(self):
        from village.agent_queries import execute_agent_query
        from village.models import VillageLocationDefinition
        self.fund_with(100)
        decision = self.decision()
        listing = self.listing(self.cook(), 2, public_meal_code="C1")
        NpcProfile.objects.filter(npc=self.npcs["10011"]).update(capabilities=["INSPECT_LIVING_OPTIONS", "BUY_FROM_STORE"])
        VillageLocationDefinition.objects.update_or_create(location_code="RESTAURANT",
            defaults={"display_name": "Lucy饭店", "center_anchor_code": "RESTAURANT", "is_public": True})
        ok, code, data = execute_agent_query("10011", "INSPECT_LIVING_OPTIONS")
        self.assertTrue(ok, code)
        food = data["food_sources"][0]["foods"][0]
        self.assertTrue(food["public_meal"])
        self.assertTrue(food["available"])
        self.assertEqual(food["unit_price"], 0)
        self.assertNotIn("budget", str(data))
        self.assertNotIn("account_balance", str(data))
        self.act("BUY_FROM_STORE", {"listing_id": listing, "amount": 1}, npc="10011")
        ok, code, data = execute_agent_query("10011", "INSPECT_LIVING_OPTIONS")
        food = data["food_sources"][0]["foods"][0]
        self.assertFalse(food["available"])
        self.assertIn("PUBLIC_MEAL_DAILY_LIMIT_REACHED", food["blocked_reasons"])

    def test_low_health_public_meal_ignores_old_authorization_and_keeps_daily_limit(self):
        self.fund_with(100)
        decision = self.decision()
        listing = self.listing(self.cook(), 2, public_meal_code="C1")
        NpcRuntimeState.objects.filter(npc=self.npcs["10011"]).update(health_fp=0)
        self.bags["10011"].money = 0
        self.bags["10011"].save(update_fields=["money"])
        first = self.act("BUY_FROM_STORE", {"listing_id": listing, "amount": 1}, npc="10011")
        self.assertEqual(first["data"]["gross_amount"], 0)
        self.assertIn("food_instance_id", first["data"])
        self.act("BUY_FROM_STORE", {"listing_id": listing, "amount": 1}, npc="10011",
                 expect="PUBLIC_MEAL_DAILY_LIMIT_REACHED")
        decision.revoked_world_minute = 600
        decision.save(update_fields=["revoked_world_minute"])
        NpcRuntimeState.objects.filter(npc=self.npcs["10012"]).update(health_fp=0)
        self.act("BUY_FROM_STORE", {"listing_id": listing, "amount": 1}, npc="10012")

    def test_old_expired_revoked_listing_authorization_does_not_block_or_change_history(self):
        self.fund_with(100)
        decision = self.decision(expiry=600)
        decision.revoked_world_minute = 590
        decision.spent_gold = 100
        decision.save()
        listing = self.listing(self.cook(), 2, public_meal_code="C1")
        VillageStoreListing.objects.filter(pk=listing).update(subsidy_decision=decision)
        self.act("BUY_FROM_STORE", {"listing_id": listing, "amount": 1}, npc="10011")
        self.act("BUY_FROM_STORE", {"listing_id": listing, "amount": 1}, npc="10012")
        decision.refresh_from_db(); self.fund.refresh_from_db()
        self.assertEqual((decision.spent_gold, self.fund.balance), (100, 82))
        self.assertEqual(VillageCommunityMealDailyUsage.objects.get(day_index=0).total_meals, 2)

    def test_unlisted_or_unknown_freshness_store_food_is_not_sellable_stock(self):
        from village.phase3_direct_trade import store_inventory_status
        listing=self.listing(self.cook(),2)
        slot=VillageStoreInventorySlot.objects.get(listing_id=listing)
        slot.item_attributes=dict(slot.item_attributes,freshness_unknown=True);slot.save()
        self.assertEqual(store_inventory_status(self.store,600)[1],{})
        self.act("BUY_FROM_STORE",{"listing_id":listing,"amount":1},npc="10011",expect="TRADE_INSUFFICIENT_ITEM")
        slot.item_attributes.pop("freshness_unknown");slot.listing=None;slot.save()
        self.assertEqual(store_inventory_status(self.store,600)[1],{})

    def test_formal_commands_and_authenticated_http_settlement_keep_sample_chain(self):
        import time
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import AccessToken
        from account.models import GameAccount
        from village.runtime_services import CommandService
        from village.models import VillageActionDurationConfig
        user=GameAccount.objects.create(username="lucy-isolated-env",password="unused")
        token=AccessToken.for_user(user);token["scope"]="env_server";token["aud"]="kingdom-env"
        client=APIClient();client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        for actor,actions in (("10014",["COOK","REQUEST_DISH_REVIEW","CANCEL_DISH_REVIEW"]),("10013",["RATE_DISH"])):
            NpcProfile.objects.filter(npc__agent_id=actor).update(capabilities=actions)
        for action in ("COOK","REQUEST_DISH_REVIEW","CANCEL_DISH_REVIEW","RATE_DISH"):
            VillageActionDurationConfig.objects.update_or_create(action_name=action,defaults={"duration_minutes":1})
        def formal(action,params,actor):
            runtime=NpcRuntimeState.objects.get(npc__agent_id=actor)
            content={"schema_version":"village.command.v1","id":str(uuid.uuid4()),"request_id":str(uuid.uuid4()),
                "name":action,"params":params,"queue_mode":"STRICT","expected_state_version":runtime.state_version,"ttl_ms":86400000}
            ok,code,command=CommandService.accept_command(str(uuid.uuid4()),actor,"env",content,int(time.time()*1000))
            self.assertTrue(ok,code)
            ok,code,command=CommandService.start_command(command.command_id);self.assertTrue(ok,code)
            payload=_payload(action,int(actor),"RECORD_HALL" if action!="COOK" else "RESTAURANT",command.sanitized_parameters,command.request_id)
            payload.update(command_id=str(command.command_id),message_id=str(command.message_id))
            url="/api/village/economy/actions/complete"
            early=client.post(url,payload,format="json");self.assertEqual(early.json()["result_code"],"LUCY_ACTION_NOT_FINISHED")
            self.clock.world_minute=command.started_world_minute+command.duration_minutes;self.clock.save()
            response=client.post(url,payload,format="json");self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()["result_code"],"OK",response.json())
            command.refresh_from_db();self.assertEqual(command.status,"SUCCEEDED")
            self.assertTrue(client.post(url,payload,format="json").json()["replayed"])
            return response.json()["data"]
        cooked=formal("COOK",{"recipe_id":29201},"10014")["dish"]
        requested=formal("REQUEST_DISH_REVIEW",{"food_instance_id":cooked["instance_id"]},"10014")
        scored=formal("RATE_DISH",{"review_id":requested["review_id"],"scores":{"color":8,"aroma":7,"taste":9,"presentation":8},"comment":"真实样品的四项评价"},"10013")
        self.assertGreater(scored["rating_price"],0)
        self.assertEqual(VillageDishReview.objects.get(pk=requested["review_id"]).status,"COMPLETED")

    def test_cook_shortage_rejects_before_start_until_usable_ingredients_arrive(self):
        import time
        from village.models import NpcCommandState, VillageActionDurationConfig, VillageDeliveryOutbox
        from village.runtime_services import CommandService
        NpcProfile.objects.filter(npc=self.npcs["10014"]).update(capabilities=["COOK"])
        VillageActionDurationConfig.objects.update_or_create(action_name="COOK", defaults={"duration_minutes": 5})
        # Expired salt is not usable, even though the bag's raw quantity is ample.
        self.lucy.slots.filter(item_id=1049).update(expires_world_minute=600)
        before_items = list(self.lucy.slots.order_by("pk").values())
        before_needs = NpcRuntimeState.objects.get(npc=self.npcs["10014"])

        def accept():
            runtime = NpcRuntimeState.objects.get(npc=self.npcs["10014"])
            content = {"schema_version": "village.command.v1", "id": str(uuid.uuid4()),
                "request_id": str(uuid.uuid4()), "name": "COOK", "params": {"recipe_id": 29211},
                "queue_mode": "STRICT", "expected_state_version": runtime.state_version, "ttl_ms": 86400000}
            ok, code, command = CommandService.accept_command(str(uuid.uuid4()), "10014", "env", content, int(time.time() * 1000))
            self.assertTrue(ok, code)
            return command

        for _ in range(2):
            command = accept()
            ok, code, _ = CommandService.start_command(command.pk)
            self.assertFalse(ok)
            self.assertEqual(code, "P3_INSUFFICIENT_ITEM")
            command.refresh_from_db()
            self.assertIsNone(command.started_world_minute)
            self.assertEqual(command.status, "FAILED")
            data = VillageDeliveryOutbox.objects.get(command=command, message_kind="OUTCOME").payload["data"]
            self.assertEqual(data["actual_duration_minutes"], 0)
            self.assertEqual(data["planned_duration_minutes"], 5)
            self.assertFalse(data["tool_error"]["executed"])
            self.assertEqual(data["missing_items"][0]["item_id"], 1049)
            self.assertEqual(data["missing_items"][0]["available"], 0)
            self.assertIn("补齐", data["retry_condition"])
        self.assertFalse(NpcCommandState.objects.filter(npc=self.npcs["10014"], status="RUNNING").exists())
        self.assertEqual(list(self.lucy.slots.order_by("pk").values()), before_items)
        after_needs = NpcRuntimeState.objects.get(npc=self.npcs["10014"])
        self.assertEqual((after_needs.energy_fp, after_needs.hunger_fp), (before_needs.energy_fp, before_needs.hunger_fp))
        self.assertEqual(GlobalGameTimeState.get_instance().world_minute, 600)
        add_batch(self.lucy, 1049, 10, 600, 3500, uuid.uuid4())
        command = accept()
        ok, code, started = CommandService.start_command(command.pk)
        self.assertTrue(ok, code)
        self.assertEqual(started.status, "RUNNING")

    def test_cook_replay_does_not_reroll_or_consume_twice(self):
        payload = _payload("COOK",10014,"RESTAURANT",{"recipe_id":29201})
        first = commit_economy_action(payload)
        counter = list(NpcCookingCounter.objects.values())
        second = commit_economy_action(payload)
        self.assertTrue(second["replayed"])
        self.assertEqual(first["data"], second["data"])
        self.assertEqual(counter, list(NpcCookingCounter.objects.values()))

    def test_expired_ingredient_fails_without_partial_consumption(self):
        self.lucy.slots.filter(item_id=1049).update(expires_world_minute=600)
        before = list(self.lucy.slots.order_by("pk").values_list("item_amount",flat=True))
        self.act("COOK",{"recipe_id":29211},expect="P3_INSUFFICIENT_ITEM")
        self.assertEqual(before,list(self.lucy.slots.order_by("pk").values_list("item_amount",flat=True)))

    def test_selling_then_withdrawing_funds_restarts_personal_cycle(self):
        slot = self.cook()
        listing = self.listing(slot,2)
        price = VillageStoreListing.objects.get(pk=listing).unit_price
        self.act("BUY_FROM_STORE",{"listing_id":listing,"amount":2},npc="10011")
        self.account.refresh_from_db()
        net = self.account.balance
        self.assertEqual(self.account.minimum_operating_balance,0)
        self.assertGreater(net,0)
        self.act("WITHDRAW_STORE_FUNDS",{"store_code":"RESTAURANT_STORE","amount":net})
        self.lucy.refresh_from_db(); self.account.refresh_from_db(); self.fund.refresh_from_db()
        self.assertEqual(self.lucy.money,100+net)
        self.assertEqual(self.account.balance,0)
        self.assertEqual(net+self.fund.balance,price*2)
        self.assertTrue(reconcile(self.fund)["balance_matches"])
        self.cook()

    def test_other_npc_cannot_withdraw_lucy_money(self):
        self.act("WITHDRAW_STORE_FUNDS",{"store_code":"RESTAURANT_STORE","amount":1},npc="10015",expect="TRADE_STORE_FORBIDDEN")

    def test_custom_restaurant_price_and_arbitrary_reprice_are_blocked(self):
        slot=self.cook()
        self.act("LIST_STORE_ITEM",dict(store_code="RESTAURANT_STORE",item_id=slot.item_id,amount=1,food_instance_id=str(slot.instance_id),price_mode="CUSTOM",custom_unit_price=999),expect="RESTAURANT_FIXED_PRICE_REQUIRED")
        listing=self.listing(slot)
        self.act("UPDATE_STORE_PRICE",{"listing_id":listing,"price_mode":"CUSTOM","custom_unit_price":1},expect="RESTAURANT_FIXED_PRICE_REQUIRED")

    def test_sample_is_one_portion_reserved_then_scored_by_target_agent(self):
        slot=self.cook()
        result=self.act("REQUEST_DISH_REVIEW",{"food_instance_id":str(slot.instance_id)})
        review=VillageDishReview.objects.get(pk=result["data"]["review_id"])
        from village.models import VillageDeliveryOutbox
        deliveries = VillageDeliveryOutbox.objects.filter(event__event_type="ECONOMY_REQUEST_DISH_REVIEW", message_kind="EVENT")
        self.assertEqual(list(deliveries.values_list("recipient",flat=True)), ["10013"])
        self.assertEqual(deliveries.get().payload["public_data"]["private_data"]["review_id"], str(review.pk))
        slot.refresh_from_db()
        self.assertEqual(slot.item_amount,1)
        self.assertEqual(review.slot.item_amount,1)
        self.assertEqual(get_available_slot_amount(review.slot,600),0)
        self.act("EAT",{"source":"PERSONAL_INVENTORY","food_instance_id":str(review.slot.instance_id)},expect="P3_INSUFFICIENT_ITEM")
        reply={"review_id":str(review.pk),"scores":{"color":8,"aroma":8,"taste":8,"presentation":8},"comment":"按真实玉米和火候评价。"}
        self.act("RATE_DISH",reply,npc="10014",expect="P3_FORBIDDEN_ACTOR")
        self.act("RATE_DISH",reply,npc="10013")
        review.refresh_from_db(); review.slot.refresh_from_db()
        self.assertEqual(review.status,"COMPLETED")
        self.assertGreater(review.slot.rating_price,0)
        self.assertEqual(review.slot.dish_metadata["review_id"],str(review.pk))
        listing=self.listing(review.slot)
        self.assertEqual(VillageStoreListing.objects.get(pk=listing).unit_price,review.slot.rating_price)
        self.act("BUY_FROM_STORE",{"listing_id":listing,"amount":1},npc="10011")
        received=self.bags["10011"].slots.get(item_id=slot.item_id,item_amount=1)
        self.assertEqual(received.dish_metadata["review_id"],str(review.pk))
        self.act("RATE_DISH",reply,npc="10013",expect="DISH_REVIEW_NOT_PENDING")
        self.assertEqual(MarkDailySalary.objects.count(),0)

    def test_rating_does_not_earn_or_pay_salary_even_with_funded_debt(self):
        self.fund_with(100)
        debt = MarkDailySalary.objects.create(day_index=0, amount_due=9,
            earned_request_id=uuid.uuid4(), earned_world_minute=0)
        review_id = self.act("REQUEST_DISH_REVIEW", {
            "food_instance_id": str(self.cook().instance_id)})["data"]["review_id"]
        mark = self.bags["10013"]
        mark.refresh_from_db()
        before = (mark.money, mark.asset_version)
        self.act("RATE_DISH", {"review_id": review_id,
            "scores": {"color": 8, "aroma": 8, "taste": 8, "presentation": 8},
            "comment": "玉米清甜，火候适中。"}, npc="10013")
        mark.refresh_from_db()
        self.fund.refresh_from_db()
        debt.refresh_from_db()
        self.assertEqual((mark.money, mark.asset_version), before)
        self.assertEqual(self.fund.balance, 100)
        self.assertEqual((debt.amount_due, debt.amount_paid), (9, 0))
        self.assertEqual(MarkDailySalary.objects.count(), 1)

    def test_rating_succeeds_without_public_fund_account(self):
        review_id = self.act("REQUEST_DISH_REVIEW", {
            "food_instance_id": str(self.cook().instance_id)})["data"]["review_id"]
        self.fund.delete()
        self.act("RATE_DISH", {"review_id": review_id,
            "scores": {"color": 8, "aroma": 8, "taste": 8, "presentation": 8},
            "comment": "玉米清甜，火候适中。"}, npc="10013")
        self.assertEqual(VillageDishReview.objects.get(pk=review_id).status, "COMPLETED")
        self.assertFalse(MarkDailySalary.objects.exists())

    def test_invalid_score_and_cancel_leave_sample_usable(self):
        review_id=self.act("REQUEST_DISH_REVIEW",{"food_instance_id":str(self.cook().instance_id)})["data"]["review_id"]
        for invalid in (True,float("nan"),float("inf"),0,11):
            expected = "P3_INVALID_PARAMS" if type(invalid) is float and not math.isfinite(invalid) else "DISH_REVIEW_INVALID_SCORES"
            self.act("RATE_DISH",{"review_id":review_id,"scores":{"color":invalid,"aroma":5,"taste":5,"presentation":5},"comment":"test"},npc="10013",expect=expected)
        self.act("CANCEL_DISH_REVIEW",{"review_id":review_id})
        row=VillageDishReview.objects.get(pk=review_id)
        self.assertEqual(get_available_slot_amount(row.slot,600),1)
        self.assertFalse(row.slot.rated)

    def test_expired_pending_review_unlocks_without_new_food(self):
        review_id=self.act("REQUEST_DISH_REVIEW",{"food_instance_id":str(self.cook().instance_id)})["data"]["review_id"]
        row=VillageDishReview.objects.get(pk=review_id)
        self.assertEqual(get_available_slot_amount(row.slot,781),1)
        self.assertEqual(row.slot.item_amount,1)

    def test_public_purchase_pays_store_not_customer_and_eat_does_not_pay_again(self):
        self.fund_with(12); decision=self.decision()
        listing=self.listing(self.cook(29211),public_meal_code="C2")
        result=self.act("BUY_FROM_STORE",{"listing_id":listing,"amount":1},npc="10011")
        self.account.refresh_from_db();self.fund.refresh_from_db();self.bags["10011"].refresh_from_db()
        self.assertEqual((self.account.balance,self.fund.balance,self.bags["10011"].money),(12,0,100))
        self.act("EAT",{"source":"PERSONAL_INVENTORY","food_instance_id":result["data"]["food_instance_id"]},npc="10011")
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance,12)
        self.assertEqual(VillageCommunityMealDailyUsage.objects.get(day_index=0).total_meals,1)
        self.assertEqual(VillageStoryFact.objects.get().community_meals,1)
        self.assertTrue(reconcile(self.fund)["balance_matches"])

    def test_free_quota_shared_across_menus_and_lucy_can_claim_once(self):
        self.fund_with(100); decision=self.decision()
        a=self.listing(self.cook(),2,public_meal_code="C1")
        b=self.listing(self.cook(29241),2,public_meal_code="R1")
        self.act("BUY_FROM_STORE",{"listing_id":a,"amount":1})
        self.act("BUY_FROM_STORE",{"listing_id":b,"amount":1},expect="PUBLIC_MEAL_DAILY_LIMIT_REACHED")
        self.act("BUY_FROM_STORE",{"listing_id":a,"amount":1},npc="10011")
        self.act("BUY_FROM_STORE",{"listing_id":b,"amount":1},npc="10011",expect="PUBLIC_MEAL_DAILY_LIMIT_REACHED")

    def test_missing_fund_fails_before_any_assets_or_quota_change(self):
        listing = self.listing(self.cook(), public_meal_code="C1")
        self.act("BUY_FROM_STORE", {"listing_id": listing, "amount": 1}, npc="10011", expect="PUBLIC_MEAL_FUND_INSUFFICIENT")
        self.fund.delete()
        self.act("BUY_FROM_STORE", {"listing_id": listing, "amount": 1}, npc="10011", expect="PUBLIC_MEAL_FUND_INSUFFICIENT")
        self.assertEqual(VillageStoreInventorySlot.objects.get(listing_id=listing).item_amount, 1)
        self.assertFalse(VillageCommunityMealDailyUsage.objects.exists())

    def test_no_authorization_needed_to_list_and_claim_public_meal(self):
        self.fund_with(100)
        listing = self.listing(self.cook(), public_meal_code="C1")
        self.assertIsNone(VillageStoreListing.objects.get(pk=listing).subsidy_decision_id)
        self.assertFalse(VillagePublicDecision.objects.exists())
        self.act("BUY_FROM_STORE", {"listing_id": listing, "amount": 1}, npc="10011")

    def test_claim_marker_survives_market_snapshot_and_cannot_earn_subsidy_twice(self):
        from marketplace.services import snapshot_slot, _add_snapshot_item_to_bag
        self.fund_with(100);decision=self.decision()
        listing=self.listing(self.cook(),public_meal_code="C1")
        response=self.act("BUY_FROM_STORE",{"listing_id":listing,"amount":1},npc="10011")
        received=self.bags["10011"].slots.get(instance_id=response["data"]["food_instance_id"])
        returned=_add_snapshot_item_to_bag(self.lucy,snapshot_slot(received),1)
        self.assertIn("public_meal_claim",returned.dish_metadata)
        self.act("LIST_STORE_ITEM",dict(store_code="RESTAURANT_STORE",item_id=returned.item_id,amount=1,food_instance_id=str(returned.instance_id),price_mode="ORIGINAL",public_meal_code="C1"),expect="PUBLIC_MEAL_INVALID_BATCH")

    def test_relief_food_has_lower_recovery_and_no_pity_or_rating(self):
        slot=self.cook(29241)
        self.assertFalse(NpcCookingCounter.objects.exists())
        result=self.act("EAT",{"source":"PERSONAL_INVENTORY","food_instance_id":str(slot.instance_id)})
        self.assertEqual(result["needs_delta"],{"hunger_fp":-250,"energy_fp":0})
        runtime=NpcRuntimeState.objects.get(npc=self.npcs["10014"])
        self.assertEqual((runtime.hunger_fp,runtime.energy_fp),(350,500))
        self.act("REQUEST_DISH_REVIEW",{"food_instance_id":str(slot.instance_id)},expect="RESTAURANT_DISH_REQUIRED")

    def test_travelers_consume_only_paid_store_stock(self):
        from village.phase3_day_close import _traveler_sales
        self.fund_with(100);decision=self.decision()
        personal=self.cook()
        self.listing(self.cook(),2)
        self.listing(self.cook(),2,public_meal_code="C1")
        result,external=_traveler_sales(0,700,uuid.uuid4())
        personal.refresh_from_db(); self.lucy.refresh_from_db(); self.account.refresh_from_db()
        self.assertEqual(personal.item_amount,2)
        self.assertEqual(self.lucy.money,100)
        self.assertEqual(result["sold"],2)
        self.assertEqual(external,self.account.balance+result["fee_amount"])
        self.assertEqual(VillageStoreInventorySlot.objects.get(listing__public_meal_code="C1").item_amount,2)

    def test_menu_and_queries_have_actual_price_attributes_and_fund_reason(self):
        from village.agent_queries import _restaurant_state, _rateable_dishes
        decision=self.decision()
        self.listing(self.cook(),public_meal_code="C1")
        error,data=query_store_inventory("RESTAURANT_STORE",600,"10011")
        self.assertIsNone(error)
        self.assertEqual(data["items"][0]["unit_price"],0)
        self.assertEqual(data["items"][0]["unavailable_reason"],"PUBLIC_MEAL_FUND_INSUFFICIENT")
        self.assertIn("dish_metadata",data["items"][0]["batches"][0])
        from village.agent_queries import _cooking_recipes
        self.assertNotIn("recipes", _restaurant_state(self.npcs["10014"]))
        self.assertEqual(len(_cooking_recipes()), 13)
        self.assertEqual(_rateable_dishes(self.npcs["10013"]), {
            "filter": {"review_status": "PENDING_UNEXPIRED"}, "reviews": [], "total_count": 0, "has_more": False})

    def test_old_community_eat_no_longer_pays_personal_wallet(self):
        self.act("EAT",{"source":"COMMUNITY_MEAL"},npc="10011",expect="PUBLIC_MEAL_USE_STORE")

    def test_meal_proposal_preserves_nora_executor_and_freezes_explicit_budget(self):
        from village.public_governance import register_proposal
        decision=self.decision()
        meeting=decision.meeting
        meeting.status="OPEN";meeting.save()
        terms=copy.deepcopy(decision.terms)
        terms.update(meeting_id=str(meeting.pk),plan_code="RATION_AND_WAIT")
        terms["meal_service"]["starts_world_minute"]=600
        error,data=register_proposal(meeting.arc,meeting.arc.facts,terms,600)
        self.assertIsNone(error)
        self.assertEqual(data["proposal"]["executor_npc_id"],"10015")
        self.assertEqual(data["proposal"]["meal_service"]["executor_npc_id"],"10014")
        terms["meal_service"]["subsidies"]["C1"]=999
        self.assertEqual(register_proposal(meeting.arc,meeting.arc.facts,terms,600)[0],"PUBLIC_MEAL_INVALID_TERMS")

    def test_system_authorized_meal_uses_regular_listing_without_story(self):
        from village.phase5_story import dispatch_story_command
        self.fund_with(100)
        decision=self.decision()
        decision.meeting=None
        decision.save(update_fields=["meeting"])
        VillageStoryArc.objects.all().delete()
        slot=self.cook()
        self.assertEqual(dispatch_story_command({"command":"STORY_ACT", "params":{"action_code":"PROVIDE_COMMUNITY_MEAL"}})["result_code"],"ACTION_RETIRED")
        listing=self.listing(slot, public_meal_code="C1")
        self.act("BUY_FROM_STORE",{"listing_id":listing,"amount":1},npc="10011")
        self.assertFalse(VillageStoryArc.objects.exists())
        decision.refresh_from_db()
        self.assertEqual(decision.spent_gold,0)


    def agent_query(self, name, actor="10014"):
        from village.agent_queries import execute_agent_query
        profile = NpcProfile.objects.get(npc=self.npcs[actor])
        if name not in profile.capabilities:
            profile.capabilities = [*profile.capabilities, name]
            profile.save(update_fields=["capabilities"])
        ok, code, data = execute_agent_query(actor, name)
        self.assertTrue(ok, code)
        return data

    def meal_summary(self):
        return self.agent_query("INSPECT_RESTAURANT_STATE")["public_meal_availability"]

    def test_inventory_uses_type_specific_fields_and_compact_sale_data(self):
        dish = self.cook()
        rows = self.agent_query("INSPECT_INVENTORY")["items"]
        raw = next(r for r in rows if r["item_id"] == 1025)
        food = next(r for r in rows if r.get("food_instance_id") == str(dish.instance_id))
        self.assertEqual(raw["sale"]["target"], "GENERAL_STORE")
        self.assertNotIn("food_instance_id", raw)
        self.assertEqual(food["item_subtype"], "Dish")
        self.assertIn("dish", food)
        self.assertNotIn("can_list_in_own_store", food)
        raw_slot = self.lucy.slots.filter(item_id=1025).first()
        self.act("LIST_STORE_ITEM", dict(store_code="RESTAURANT_STORE", item_id=1025, amount=1,
            price_mode="ORIGINAL", food_instance_id=str(raw_slot.instance_id)), expect="RESTAURANT_DISH_REQUIRED")
        self.listing(dish)
        VillageStore.objects.update_or_create(store_code="GENERAL_STORE", defaults=dict(display_name="Nora", location_code="GENERAL_STORE",
            operator_npc=self.npcs["10015"], status="OPEN"))
        add_batch(self.bags["10015"], 1025, 1, 500, 3500, uuid.uuid4())
        nora = self.agent_query("INSPECT_INVENTORY", "10015")["items"][0]
        self.assertEqual(nora["sale"], {"allowed": False, "reason": "TRADE_STORE_FORBIDDEN"})

    def test_inventory_listing_flags_reject_expired_unknown_and_reserved_batches(self):
        slot = self.cook()
        for changes, reason in (({"expires_world_minute": 600}, "ITEM_EXPIRED"),
                ({"expires_world_minute": 3500, "freshness_unknown": True}, "FRESHNESS_UNKNOWN")):
            for key, value in changes.items():
                setattr(slot, key, value)
            slot.save()
            row = next(r for r in self.agent_query("INSPECT_INVENTORY")["items"]
                if r.get("food_instance_id") == str(slot.instance_id))
            self.assertEqual(row["sale"], {"allowed": False, "reason": reason})
        slot.freshness_unknown = False; slot.save()
        review_id = self.act("REQUEST_DISH_REVIEW", {"food_instance_id": str(slot.instance_id)})["data"]["review_id"]
        sample = VillageDishReview.objects.get(pk=review_id).slot
        row = next(r for r in self.agent_query("INSPECT_INVENTORY")["items"]
            if r.get("food_instance_id") == str(sample.instance_id))
        self.assertEqual(row["available_quantity"], 0)
        self.assertEqual(row["sale"], {"allowed": False, "reason": "ITEM_RESERVED"})

    def test_inventory_listing_flags_reject_incomplete_rating(self):
        slot = self.cook(); slot.rated = True; slot.rating_price = 999; slot.save()
        row = next(r for r in self.agent_query("INSPECT_INVENTORY")["items"]
            if r.get("food_instance_id") == str(slot.instance_id))
        self.assertEqual(row["dish"]["rated"], True)
        self.assertNotIn("can_list_in_store", row)
        self.act("LIST_STORE_ITEM", dict(store_code="RESTAURANT_STORE", item_id=slot.item_id, amount=1,
            price_mode="ORIGINAL", food_instance_id=str(slot.instance_id)), expect="DISH_REVIEW_INCOMPLETE")

    def test_public_meal_summary_is_read_only_and_does_not_grant_finance_authority(self):
        from unittest.mock import patch
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from village.agent_queries import execute_agent_query
        self.meal_summary()  # Set only this test NPC's query capability before tracing.
        with patch("django.db.models.query.QuerySet.select_for_update", side_effect=AssertionError("read query locked rows")):
            with CaptureQueriesContext(connection) as queries:
                summary = self.meal_summary()
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries), list(queries))
        self.assertFalse(summary["can_prepare_and_serve"])
        self.assertEqual(summary["unavailable_reasons"], ["PUBLIC_MEAL_FUND_INSUFFICIENT"])
        self.assertFalse(VillageCommunityMealDailyUsage.objects.exists())
        self.assertFalse(VillagePublicDecision.objects.exists())
        self.assertEqual(execute_agent_query("10014", "INSPECT_PUBLIC_ECONOMY_STATE")[:2], (False, "UNKNOWN_QUERY"))

    def test_general_store_sale_flag_respects_existing_tool_durability_rules(self):
        from items.models import ItemType
        Item.objects.create(item_id=4002, item_name="tool", item_type=ItemType.COLLECT_TOOL,
            item_price=20, max_use_count=20)
        slot = add_batch(self.lucy, 4002, 1, 500, None, uuid.uuid4(), remaining_uses=20)
        for uses, reason in ((20, None), (10, "TOOL_NOT_FULL_DURABILITY"),
                (0, "TOOL_BROKEN"), (None, "TOOL_DURABILITY_UNKNOWN")):
            slot.remaining_uses = uses; slot.save()
            row = next(r for r in self.agent_query("INSPECT_INVENTORY")["items"] if r["item_id"] == 4002)
            expected = ({"target": "GENERAL_STORE", "unit_price": 20} if reason is None
                else {"allowed": False, "reason": reason})
            self.assertEqual(row["sale"], expected)

    def test_public_meal_summary_uses_real_fund_not_story_projection(self):
        from unittest.mock import patch
        self.fund_with(100); decision = self.decision()
        VillageStoryFact.objects.update(repair_fund=999)
        with patch("django.db.models.query.QuerySet.select_for_update", side_effect=AssertionError("read query locked rows")):
            summary = self.meal_summary()
        self.assertEqual(summary["public_fund_balance"], 100)
        self.assertTrue(summary["can_prepare_and_serve"])
        self.assertEqual(summary["unavailable_reasons"], [])
        policy = summary
        self.assertNotIn("authorizations", summary)
        self.assertEqual(policy["remaining_by_npc_today"]["10014"], 1)
        self.assertEqual(policy["remaining_village_today"], 10)
        self.assertEqual([m["public_meal_code"] for m in summary["menu"]], ["C1", "C2", "R1"])

    def test_public_meal_summary_ignores_unpublished_future_expired_and_revoked(self):
        self.fund_with(100); decision = self.decision()
        baseline = copy.deepcopy(decision.terms)
        for change, reason in (("unpublished", "PUBLIC_DECISION_NOT_PUBLISHED"),
                ("future", "PUBLIC_MEAL_NOT_STARTED"), ("expired", "PUBLIC_AUTHORIZATION_EXPIRED"),
                ("revoked", "PUBLIC_AUTHORIZATION_EXPIRED")):
            with self.subTest(state=change):
                decision.terms = copy.deepcopy(baseline)
                decision.published_world_minute = None if change == "unpublished" else 560
                decision.revoked_world_minute = 590 if change == "revoked" else None
                if change == "future": decision.terms["meal_service"]["starts_world_minute"] = 700
                if change == "expired": decision.terms["expires_world_minute"] = 600
                decision.save()
                summary = self.meal_summary()
                self.assertTrue(summary["can_prepare_and_serve"])
                self.assertEqual(summary["unavailable_reasons"], [])

    def test_system_meal_authorization_is_independent_of_historical_meeting(self):
        self.fund_with(100)
        decision = self.decision()
        decision.meeting.status = "OPEN"
        decision.meeting.save()
        self.assertTrue(self.meal_summary()["can_prepare_and_serve"])
        decision.meeting = None
        decision.save(update_fields=["meeting"])
        self.assertTrue(self.meal_summary()["can_prepare_and_serve"])

    def test_public_meal_summary_ignores_budget_but_checks_funds_and_ingredients(self):
        self.fund_with(100); decision = self.decision()
        self.fund_with(0)
        self.assertIn("PUBLIC_MEAL_FUND_INSUFFICIENT", self.meal_summary()["unavailable_reasons"])
        self.fund_with(100); decision.spent_gold = 100; decision.save()
        self.assertTrue(self.meal_summary()["can_prepare_and_serve"])
        decision.spent_gold = 0; decision.save()
        self.lucy.slots.filter(item_id=1030).update(item_amount=0)
        summary = self.meal_summary()
        self.assertIn("PUBLIC_MEAL_INGREDIENTS_INSUFFICIENT", summary["unavailable_reasons"])
        self.assertFalse(any(m["can_cook"] for m in summary["menu"]))
        corn = summary["menu"][0]["recipes"][0]["missing_ingredients"][0]
        self.assertEqual((corn["item_id"], corn["missing_quantity"]), (1030, 2))

    def test_public_meal_summary_updates_shared_quotas_and_actual_fund_after_purchase(self):
        self.fund_with(100); decision = self.decision()
        listing = self.listing(self.cook(), 2, public_meal_code="C1")
        self.act("BUY_FROM_STORE", {"listing_id": listing, "amount": 1}, npc="10011")
        summary = self.meal_summary(); policy = summary
        self.assertEqual(summary["public_fund_balance"], 91)
        self.assertEqual(policy["remaining_by_npc_today"]["10011"], 0)
        self.assertEqual(policy["remaining_by_npc_today"]["10014"], 1)
        self.assertEqual(policy["remaining_village_today"], 9)
        VillageCommunityMealDailyUsage.objects.update(total_meals=10)
        self.assertIn("PUBLIC_MEAL_DAILY_LIMIT_REACHED", self.meal_summary()["unavailable_reasons"])

    def test_public_meal_summary_can_serve_existing_food_without_new_ingredients(self):
        self.fund_with(100); decision = self.decision()
        dish = self.cook()
        self.lucy.slots.filter(item_id=1030).update(item_amount=0)
        summary = self.meal_summary()
        self.assertTrue(summary["menu"][0]["can_list_existing"])
        listing = self.listing(dish, 2, public_meal_code="C1")
        summary = self.meal_summary()
        self.assertFalse(summary["menu"][0]["can_cook"])
        self.assertEqual(summary["menu"][0]["listed_portions"], 2)
        self.assertTrue(summary["can_prepare_and_serve"])
        VillageStoreInventorySlot.objects.filter(listing_id=listing).update(expires_world_minute=600)
        self.assertFalse(self.meal_summary()["can_prepare_and_serve"])
