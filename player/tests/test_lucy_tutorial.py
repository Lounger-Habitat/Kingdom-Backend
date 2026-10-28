import uuid
import time

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from account.models import GameAccount
from inventory.models import NpcDefinition
from items.models import Item
from player.lucy_tutorial import context, handle, LUCY_STEPS, template
from player.models import PlayerTutorialState
from player.tutorial_catalog import VERSION, ROUTES
from player.tutorials import TutorialError
from village.models import VillagePersonalCommission, VillageWorldEvent
from village.personal_commissions import publish_commission, CommissionError, list_commissions
from village.tests.test_player_actions import PlayerFixture


class LucyTutorialTests(PlayerFixture, TestCase):
    def setUp(self):
        super().setUp()
        for item_id, name in ((1025, "胡萝卜"), (1035, "平菇"), (1042, "土豆")):
            Item.objects.update_or_create(pk=item_id, defaults=dict(item_name=name, item_type="蔬菜", item_price=2))

    def start(self, player=None):
        player = player or self.a
        self.assertTrue(self.run_action("tutorial_start", player=player)["success"])
        return player

    def notify(self, operation, player=None, **kwargs):
        return handle(dict(operation=operation, player_id=str((player or self.a).pk), npc_id="10014", **kwargs))

    def meet(self, player=None, conversation="meeting"):
        return self.notify("meet", player, conversation_id=conversation)

    def exchange(self, player=None, conversation="meeting", player_speech="player-hello", speech="lucy-hello"):
        self.meet(player, conversation)
        return self.notify("exchange", player, conversation_id=conversation,
            tutorial_context_id=context(player or self.a)["context_id"], player_speech_id=player_speech, speech_id=speech)

    def publish(self, player=None, rid=None):
        return publish_commission(self.npc, {"tutorial_context_id": context(player or self.a)["context_id"]},
            rid or uuid.uuid4(), self.clock.world_minute)

    def test_opening_meeting_does_not_complete_first_task_or_allow_publication(self):
        self.start()
        for _ in range(2):
            self.assertEqual(self.meet()["stage"], "meet_lucy")
        state = PlayerTutorialState.objects.get(player=self.a)
        self.assertNotIn("basics_lucy_meet", state.completed)
        self.assertNotIn("basics_lucy_meet", state.evidence)
        task = self.run_action("overview")["data"]["overview"]["tutorial"]["current_task"]
        self.assertEqual(task["code"], "basics_lucy_meet")
        self.assertIn("完成一轮交谈", task["description"])
        with self.assertRaises(CommissionError) as denied:
            self.publish()
        self.assertEqual(denied.exception.code, "TUTORIAL_NOT_READY")
        self.assertEqual(self.exchange()["stage"], "offer_help")
        state.refresh_from_db()
        self.assertEqual(state.evidence["basics_lucy_meet"],
            dict(lucy=["lucy-hello"], conversation=["meeting"], player_speech=["player-hello"]))
        revision, completed = state.revision, state.completed.copy()
        self.exchange(speech="another-reply")
        state.refresh_from_db()
        self.assertEqual((state.revision, state.completed), (revision, completed))
        self.bag_a.refresh_from_db()
        self.assertEqual(self.bag_a.money, 100)

    def test_exchange_requires_both_speeches_and_current_players_conversation_and_chapter(self):
        self.start(); self.meet()
        valid = dict(conversation_id="meeting", tutorial_context_id=context(self.a)["context_id"],
            player_speech_id="player-hello", speech_id="lucy-hello")
        for changes, code in ((dict(player_speech_id=""), "INVALID_PARAMS"),
                              (dict(speech_id=""), "INVALID_PARAMS"),
                              (dict(player_speech_id="lucy-hello"), "INVALID_PARAMS"),
                              (dict(tutorial_context_id=str(uuid.uuid4())), "TUTORIAL_CONTEXT_INVALID"),
                              (dict(conversation_id="another-meeting"), "TUTORIAL_EXCHANGE_NOT_READY")):
            with self.subTest(changes=changes), self.assertRaises(TutorialError) as denied:
                self.notify("exchange", **{**valid, **changes})
            self.assertEqual(denied.exception.code, code)
        self.start(self.b); self.meet(self.b, conversation="other-player-meeting")
        with self.assertRaises(TutorialError):
            self.notify("exchange", self.b, **valid)
        self.meet(conversation="new-meeting")
        with self.assertRaises(TutorialError) as stale:
            self.notify("exchange", **valid)
        self.assertEqual(stale.exception.code, "TUTORIAL_EXCHANGE_NOT_READY")
        self.assertEqual(context(self.a)["stage"], "meet_lucy")
        self.assertEqual(context(self.b)["stage"], "meet_lucy")

    def test_first_context_uses_lucys_view_of_the_new_traveller(self):
        self.start()
        text = self.meet()["text"]
        self.assertIn("你知道昨天有位旅行者", text)
        self.assertIn("先自然介绍自己", text)
        self.assertIn("完成一轮交谈", text)
        self.assertIn("不要编造你昨晚亲自接待过对方", text)

    def accept(self, row, player=None):
        return self.run_action("commission_accept", dict(commission_id=row["commission_id"]), player=player)

    def deliver(self, row, player=None):
        return self.run_action("commission_contribute", dict(commission_id=row["commission_id"], condition_id="ingredients",
            item_id=1025, quantity=2), player=player)

    def complete(self):
        self.start(); self.exchange()
        row = self.publish()
        self.assertTrue(self.accept(row)["success"])
        self.slot(2, item_id=1025)
        result = self.deliver(row)
        self.assertTrue(result["success"], result)
        return row

    def test_full_flow_uses_real_escrow_reward_and_confirmed_thanks(self):
        self.start()
        self.assertEqual(context(self.a)["stage"], "meet_lucy")
        self.assertEqual(self.exchange()["stage"], "offer_help")
        row = self.publish()
        self.lucy.refresh_from_db(); self.assertEqual(self.lucy.money, 995)
        self.assertEqual(context(self.a)["stage"], "accept_commission")
        self.assertTrue(self.accept(row)["success"])
        self.assertEqual(context(self.a)["stage"], "help_lucy")
        self.slot(2, item_id=1025)
        result = self.deliver(row)
        self.assertTrue(result["success"], result)
        # Existing first_supply milestone pays 15 once, separately from Lucy's 5.
        self.bag_a.refresh_from_db(); self.assertEqual(self.bag_a.money, 120)
        self.assertEqual(self.lucy.slots.get(item_id=1025).item_amount, 2)
        self.assertTrue(context(self.a)["thanks_pending"])
        # A conversation established before completion does not confirm gratitude.
        with self.assertRaises(TutorialError):
            self.notify("thanks", conversation_id="meeting", speech_id="old", commission_id=row["commission_id"])
        self.meet(conversation="return")
        result = self.notify("thanks", conversation_id="return", speech_id="lucy-thanks", commission_id=row["commission_id"])
        self.assertEqual(result["stage"], "completed")
        state = PlayerTutorialState.objects.get(player=self.a)
        self.assertTrue(all(code in state.completed for code in LUCY_STEPS))
        self.assertEqual(self.run_action("overview")["data"]["overview"]["tutorial"]["current_task"]["code"], "basics_growth")

    def test_two_players_receive_distinct_reserved_commissions(self):
        for player in (self.a, self.b):
            self.start(player); self.exchange(player)
        a, b = self.publish(self.a), self.publish(self.b)
        self.assertNotEqual(a["commission_id"], b["commission_id"])
        visible = list_commissions(self.clock.world_minute, "PLAYER", str(self.b.pk))
        self.assertNotIn(a["commission_id"], [row["commission_id"] for row in visible])
        self.assertIn(b["commission_id"], [row["commission_id"] for row in visible])
        result = self.accept(a, self.b)
        self.assertEqual(result["result_code"], "COMMISSION_RESERVED")
        publish_event = VillageWorldEvent.objects.get(dedupe_key=f"commission:{a['commission_id']}:PUBLISH")
        self.assertEqual(publish_event.visibility, "PARTICIPANTS")
        self.assertEqual(publish_event.player_ids, [str(self.a.pk)])

    def test_repeated_publication_and_thanks_do_not_repeat_economic_changes(self):
        row = self.complete()
        # Completed players cannot create a second paid onboarding order.
        with self.assertRaises(CommissionError):
            self.publish()
        self.meet(conversation="return")
        self.notify("thanks", conversation_id="return", speech_id="thanks")
        self.notify("thanks", conversation_id="return", speech_id="thanks")
        self.bag_a.refresh_from_db(); self.lucy.refresh_from_db()
        self.assertEqual((self.bag_a.money, self.lucy.money), (120, 995))
        self.assertEqual(VillagePersonalCommission.objects.count(), 1)
        self.assertEqual(VillageWorldEvent.objects.filter(event_type="PERSONAL_COMMISSION_COMPLETED").count(), 1)

    def test_new_request_id_reuses_existing_open_order_and_reward_reservation(self):
        self.start(); self.exchange()
        first, second = self.publish(), self.publish()
        self.assertEqual(first["commission_id"], second["commission_id"])
        self.lucy.refresh_from_db(); self.assertEqual(self.lucy.money, 995)
        self.assertEqual(VillagePersonalCommission.objects.count(), 1)

    def test_other_npc_or_unknown_context_cannot_publish_tutorial(self):
        self.start(); self.exchange()
        other = NpcDefinition.objects.create(agent_id="10011", name="Bill")
        from village.models import NpcProfile
        from inventory.models import NpcGlobalInventory
        NpcProfile.objects.create(npc=other, capabilities=["PUBLISH_PERSONAL_COMMISSION"])
        NpcGlobalInventory.objects.create(npc=other, money=50)
        with self.assertRaises(CommissionError):
            publish_commission(other, {"tutorial_context_id": context(self.a)["context_id"]}, uuid.uuid4(), self.clock.world_minute)
        with self.assertRaises(CommissionError):
            publish_commission(self.npc, {"tutorial_context_id": str(uuid.uuid4())}, uuid.uuid4(), self.clock.world_minute)
        self.assertFalse(VillagePersonalCommission.objects.exists())

    def test_unstarted_or_uncompleted_players_cannot_forge_progress(self):
        with self.assertRaises(TutorialError):
            self.exchange()
        self.start()
        with self.assertRaises(CommissionError):
            self.publish()
        self.exchange()
        row = self.publish(); self.accept(row)
        with self.assertRaises(TutorialError):
            self.notify("thanks", conversation_id="meeting", speech_id="claimed")
        self.assertEqual(context(self.a)["stage"], "help_lucy")

    def test_abandon_and_expiry_refund_and_allow_new_order(self):
        self.start(); self.exchange()
        first = self.publish(); self.accept(first)
        result = self.run_action("commission_abandon", dict(commission_id=first["commission_id"]))
        self.assertTrue(result["success"], result)
        self.assertEqual(context(self.a)["stage"], "offer_help")
        self.assertNotIn("basics_lucy_commission", PlayerTutorialState.objects.get(player=self.a).completed)
        second = self.publish()
        self.assertNotEqual(first["commission_id"], second["commission_id"])
        self.clock.world_minute += 1441; self.clock.save()
        third = self.publish()
        self.assertNotEqual(second["commission_id"], third["commission_id"])
        self.lucy.refresh_from_db(); self.assertEqual(self.lucy.money, 995)

    def test_context_is_read_only_and_projection_only_exposes_own_progress(self):
        self.start(); self.exchange(); self.publish()
        with CaptureQueriesContext(connection) as queries:
            self.notify("context")
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        view = self.run_action("overview", player=self.b)["data"]["overview"]["tutorial"]["lucy"]
        self.assertFalse(view["active"])
        self.assertEqual(view["commission_id"], "")

    def test_env_auth_required_and_invalid_ids_are_handled(self):
        self.start()
        api = APIClient()
        url = "/api/village/runtime/tutorial/lucy"
        body = dict(operation="meet", player_id=str(self.a.pk), npc_id="10014", conversation_id="real")
        self.assertIn(api.post(url, body, format="json").status_code, (401, 403))
        token = AccessToken.for_user(self.a)
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertIn(api.post(url, body, format="json").status_code, (401, 403))
        token["scope"], token["aud"] = "env_server", "kingdom-env"
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(api.post(url, body, format="json").status_code, 200)
        self.assertEqual(api.post(url, {**body, "player_id": "bogus"}, format="json").status_code, 400)

    def test_v5_completed_basics_stays_completed_and_in_progress_goes_to_lucy(self):
        old_codes = ("basics_move", "basics_collect", "basics_bag", "basics_income", "basics_growth", "basics_route")
        PlayerTutorialState.objects.create(player=self.a, version=5, started=True,
            completed={code: "old" for code in old_codes})
        view = self.run_action("overview")["data"]["overview"]["tutorial"]
        self.assertTrue(next(route for route in view["routes"] if route["code"] == "basics")["intro_completed"])
        self.assertEqual(view["lucy"]["stage"], "completed")
        self.assertTrue(view["life_routes_unlocked"])
        self.assertEqual(len(view["routes"]), 7)
        PlayerTutorialState.objects.create(player=self.b, version=5, started=True,
            completed={"basics_move": "old", "gathering_first": "old"})
        view = self.run_action("overview", player=self.b)["data"]["overview"]["tutorial"]
        self.assertEqual(view["current_task"]["code"], "basics_lucy_meet")
        self.assertEqual([route["code"] for route in view["routes"]], ["basics"])
        self.assertIn("gathering_first", PlayerTutorialState.objects.get(player=self.b).completed)

    def test_template_only_uses_existing_level_one_plants(self):
        from village.plant_catalog import PLANTS
        allowed = {p["item_id"] for p in PLANTS.values() if p["level"] == 1}
        condition = template()["stages"][0]["conditions"][0]
        self.assertEqual(set(condition["item_ids"]), allowed)
        self.assertEqual((condition["target"], condition["min_quality"]), (2, 1))

    def test_existing_agent_command_and_query_support_minimal_tutorial_context(self):
        from village.models import NpcRuntimeState, NpcProfile, VillageActionReceipt
        from village.runtime_services import CommandService
        from village.agent_queries import execute_agent_query
        self.start(); self.exchange()
        profile = NpcProfile.objects.get(npc=self.npc)
        profile.capabilities += ["INSPECT_PERSONAL_COMMISSIONS"]
        profile.save()
        NpcRuntimeState.objects.update_or_create(npc=self.npc, defaults=dict(current_location_code="RESTAURANT",
            action_status="IDLE", health_fp=1000, energy_fp=1000, hunger_fp=0))
        ok, code, data = execute_agent_query("10014", "INSPECT_PERSONAL_COMMISSIONS", {"tutorial_player_id": str(self.a.pk)})
        self.assertTrue(ok, code)
        identifier = data["lucy_tutorial"]["context_id"]
        body = dict(envelope_id=str(uuid.uuid4()), sender="10014", recipient="WuxiVillage",
            timestamp_ms=int(time.time()*1000), content=dict(schema_version="village.command.v1", id=str(uuid.uuid4()),
                request_id=str(uuid.uuid4()), name="PUBLISH_PERSONAL_COMMISSION", params=dict(tutorial_context_id=identifier),
                queue_mode="STRICT", ttl_ms=120000, not_before_world_minute=0, expires_world_minute=10000))
        ok, code, command = CommandService.accept_command(**body)
        self.assertTrue(ok, code)
        self.assertEqual(command.status, "SUCCEEDED")
        self.assertEqual(VillageActionReceipt.objects.get(command=command).response_data["reward_gold"], 5)

    def test_insufficient_wallet_keeps_context_retryable_without_free_assets(self):
        self.start(); self.exchange()
        self.lucy.money = 0; self.lucy.save()
        with self.assertRaises(CommissionError):
            self.publish()
        self.assertEqual(context(self.a)["stage"], "offer_help")
        self.assertFalse(VillagePersonalCommission.objects.exists())
        self.bag_a.refresh_from_db(); self.assertEqual(self.bag_a.money, 100)
        self.lucy.money = 5; self.lucy.save()
        self.publish()
        self.lucy.refresh_from_db(); self.assertEqual(self.lucy.money, 0)

    def test_mix_of_beginner_ingredients_fulfils_one_shared_quantity(self):
        self.start(); self.exchange(); row = self.publish(); self.accept(row)
        for item_id in (1025, 1042):
            self.slot(1, item_id=item_id, quality=1)
            result = self.run_action("commission_contribute", dict(commission_id=row["commission_id"],
                condition_id="ingredients", item_id=item_id, quantity=1))
            self.assertTrue(result["success"], result)
        self.assertEqual(VillagePersonalCommission.objects.get(pk=row["commission_id"]).status, "COMPLETED")
        self.assertTrue(context(self.a)["thanks_pending"])

    @override_settings(LUCY_TUTORIAL_MAX_OPEN=1)
    def test_tutorial_quota_is_separate_from_ten_regular_orders_and_retryable(self):
        for player in (self.a, self.b):
            self.start(player); self.exchange(player)
        tutorial = self.publish()
        for _ in range(10):
            publish_commission(self.npc, template(), uuid.uuid4(), self.clock.world_minute)
        self.assertEqual(VillagePersonalCommission.objects.count(), 11)
        with self.assertRaises(CommissionError) as regular_error:
            publish_commission(self.npc, template(), uuid.uuid4(), self.clock.world_minute)
        self.assertEqual(regular_error.exception.code, "COMMISSION_LIMIT")
        with self.assertRaises(CommissionError) as tutorial_error:
            self.publish(self.b)
        self.assertEqual(tutorial_error.exception.code, "COMMISSION_LIMIT")
        self.assertEqual(context(self.b)["stage"], "offer_help")
        self.assertTrue(self.accept(tutorial)["success"])
        self.assertTrue(self.run_action("commission_abandon", dict(commission_id=tutorial["commission_id"]))["success"])
        second = self.publish(self.b)
        self.assertEqual(second["tutorial_player_id"], str(self.b.pk))
        self.lucy.refresh_from_db(); self.assertEqual(self.lucy.money, 945)

    @override_settings(LUCY_TUTORIAL_MAX_OPEN=0)
    def test_configured_zero_intake_does_not_reserve_reward_or_consume_context(self):
        self.start(); self.exchange()
        token = context(self.a)["context_id"]
        with self.assertRaises(CommissionError) as failure:
            self.publish()
        self.assertEqual(failure.exception.code, "COMMISSION_LIMIT")
        self.assertFalse(VillagePersonalCommission.objects.exists())
        self.lucy.refresh_from_db(); self.assertEqual(self.lucy.money, 1000)
        with override_settings(LUCY_TUTORIAL_MAX_OPEN=1):
            self.assertEqual(context(self.a)["context_id"], token)
            self.publish()

    @override_settings(LUCY_TUTORIAL_MAX_OPEN=1)
    def test_newcomer_reclaims_expired_tutorial_capacity_without_a_world_tick(self):
        for player in (self.a, self.b):
            self.start(player); self.exchange(player)
        first = self.publish()
        self.accept(first)
        self.clock.world_minute += 1441; self.clock.save()
        second = self.publish(self.b)
        self.assertNotEqual(first["commission_id"], second["commission_id"])
        self.assertEqual(context(self.a)["stage"], "offer_help")
        self.lucy.refresh_from_db(); self.assertEqual(self.lucy.money, 995)

    def test_new_player_only_sees_lucy_and_cannot_track_cached_life_routes(self):
        view = self.run_action("overview")["data"]["overview"]["tutorial"]
        self.assertFalse(view["life_routes_unlocked"])
        self.assertEqual([row["code"] for row in view["routes"]], ["basics"])
        self.assertEqual(view["routes"][0]["title"], "入村引导 · Lucy")
        self.start()
        for route in ROUTES:
            if route == "basics":
                continue
            with self.subTest(route=route):
                denied = self.run_action("tutorial_track", {"route_code": route})
                self.assertEqual(denied["result_code"], "TUTORIAL_LUCY_REQUIRED")
        self.assertEqual(PlayerTutorialState.objects.get(player=self.a).route_code, "basics")

    def test_publication_acceptance_and_delivery_do_not_unlock_routes_before_thanks(self):
        self.start(); self.exchange()
        row = self.publish()
        self.assertFalse(self.run_action("overview")["data"]["overview"]["tutorial"]["life_routes_unlocked"])
        self.accept(row)
        self.assertFalse(self.run_action("overview")["data"]["overview"]["tutorial"]["life_routes_unlocked"])
        self.slot(2, item_id=1025); self.deliver(row)
        view = self.run_action("overview")["data"]["overview"]["tutorial"]
        self.assertEqual(view["lucy"]["stage"], "return_lucy")
        self.assertFalse(view["life_routes_unlocked"])
        self.assertEqual(len(view["routes"]), 1)
        self.meet(conversation="return")
        self.notify("thanks", conversation_id="return", speech_id="genuine-thanks", commission_id=row["commission_id"])
        view = self.run_action("overview")["data"]["overview"]["tutorial"]
        self.assertTrue(view["life_routes_unlocked"])
        self.assertEqual(len(view["routes"]), 7)
        state = PlayerTutorialState.objects.get(player=self.a)
        self.assertNotIn("basics_growth", state.completed)
        self.assertNotIn("basics_route", state.completed)
        selected = self.run_action("tutorial_track", {"route_code": "cooking"})
        self.assertTrue(selected["success"], selected)
        self.assertEqual(selected["data"]["overview"]["tutorial"]["route_code"], "cooking")

    def test_earlier_life_route_selection_projects_lucy_read_only_and_preserves_work(self):
        from player.tutorials import tutorial_overview, observe_skill
        old = PlayerTutorialState.objects.create(player=self.a, version=VERSION, started=True, route_code="cooking",
            completed={"cooking_recipe": "earlier"}, evidence={"cooking_recipe": {"hint:recipe": ["read"]}})
        with CaptureQueriesContext(connection) as queries:
            view = tutorial_overview(self.a, self.bag_a, self.clock.world_minute, {"player_id": str(self.a.pk)}, read_only=True)
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        self.assertEqual((view["route_code"], view["current_task"]["code"]), ("basics", "basics_lucy_meet"))
        self.assertTrue(view["lucy"]["active"])
        old.refresh_from_db(); self.assertEqual(old.route_code, "cooking")
        observe_skill(self.a, "player_cooking", "new-free-play-cook", {})
        old.refresh_from_db(); self.assertNotIn("cooking_prepare", old.completed)
        self.start()
        old.refresh_from_db(); self.assertEqual(old.route_code, "basics")
        self.assertEqual(old.completed["cooking_recipe"], "earlier")
        row = self.complete()
        self.meet(conversation="return")
        self.notify("thanks", conversation_id="return", speech_id="thanks", commission_id=row["commission_id"])
        selected = self.run_action("tutorial_track", {"route_code": "cooking"})
        self.assertTrue(selected["success"])
        self.assertEqual(selected["data"]["overview"]["tutorial"]["current_task"]["code"], "cooking_prepare")

    def test_route_gate_does_not_block_ordinary_gameplay(self):
        row = self.commission()
        self.slot(2, item_id=1030, quality=3)
        delivered = self.run_action("commission_contribute", self.delivery_params(row, 2))
        self.assertTrue(delivered["success"], delivered)
        self.assertEqual(VillagePersonalCommission.objects.get(pk=row).status, "COMPLETED")
        self.assertFalse(self.run_action("overview")["data"]["overview"]["tutorial"]["life_routes_unlocked"])
