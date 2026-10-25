import copy
import uuid
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from inventory.models import InventoryBag, InventorySlot, NpcDefinition
from items.models import Item
from player.models import (PlayerCombatReceipt, PlayerGrowthState, PlayerHuntingState,
                           PlayerSkillProgress, PlayerState, SkillRewardReceipt)
from village.models import (VillageHuntAttempt, VillageStore, VillageStoreAccount,
                            VillageWildlife)
from village.player_hunting import (HuntingError, execute_combat, execute_hunting_action,
    health_snapshot, hunting_overview, weapon_repair_quote)
from village.production_tools import execute_tool_action, tools_overview
from village.tests.test_player_actions import PlayerFixture
from village.wildlife import execute as wildlife


class PlayerHuntingTests(PlayerFixture, TestCase):
    def test_new_sword_and_axe_last_use_kills_earn_real_hunting_experience(self):
        for item_id, weapon in ((4130, 1), (4104, 3)):
            with self.subTest(item_id=item_id):
                tool = self.tool(item_id, 1)
                attack = uuid.uuid4()
                permit = self.call(weapon=weapon, rid=attack, instance_id=str(tool.instance_id))
                self.assertTrue(permit["success"], permit)
                prey = uuid.uuid4()
                self.assertTrue(wildlife(dict(action="register", request_id=str(uuid.uuid4()), prey_id=str(prey),
                    species="DEER", region_id="DEEP_FOREST", position=dict(x=-145, y=6.35, z=125)))["success"])
                result = wildlife(dict(action="finish", request_id=str(uuid.uuid4()), prey_id=str(prey), attempt_id=str(uuid.uuid4()),
                    actor_type="PLAYER", actor_id=str(self.a.pk), death_world_minute=100, attack_id=str(attack)))
                self.assertTrue(result["success"], result)
                self.assertTrue(result["data"]["growth_rewards"], result)

    def setUp(self):
        super().setUp()
        self.session = "test-env-session-a"
        self.ap = PlayerState.objects.create(player_id=str(self.a.pk), action_points=0)

    def tool(self, item_id=4110, uses=60):
        return InventorySlot.objects.create(bag=self.bag_a, slot_index=self.bag_a.slots.count(),
                                            item_id=item_id, item_amount=1, remaining_uses=uses)

    def call(self, action="authorize_attack", weapon=0, rid=None, session=None, **params):
        payload = dict(action=action, player_id=str(self.a.pk), request_id=str(rid or uuid.uuid4()),
                       authority_session_id=session or self.session)
        if action in {"resolve_weapon", "authorize_attack"}:
            payload["weapon"] = weapon
        return execute_combat(dict(payload, **params))

    def action(self, action, params=None):
        return execute_hunting_action(self.a, self.bag_a, action, params or {}, uuid.uuid4(), self.clock)

    def boar(self):
        # A trusted fixture exercises the reserved G6 event; G5 cannot register BOAR.
        return VillageWildlife.objects.create(prey_id=uuid.uuid4(), species="BOAR", region_id="TEST_ONLY",
            position={}, born_world_minute=100)

    def test_overview_health_resolve_and_inspect_are_pure_with_or_without_main_bag(self):
        self.tool()
        empty = InventoryBag(player=self.b, character_id=self.b.username)
        with CaptureQueriesContext(connection) as queries:
            health_snapshot(self.a)
            overview = hunting_overview(self.a, self.bag_a, 100, read_only=True)
            hunting_overview(self.b, empty, 100, read_only=True)
            self.assertTrue(self.call("resolve_weapon")["success"])
            self.assertTrue(self.call("inspect_state")["success"])
        self.assertFalse([q for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))])
        self.assertEqual(overview["health"]["current"], 100)
        self.assertFalse(PlayerHuntingState.objects.filter(player=self.a).exists())
        Item.objects.filter(pk=4110).delete()
        with CaptureQueriesContext(connection) as queries:
            missing = hunting_overview(self.a, self.bag_a, 100, read_only=True)["weapons"][0]
        self.assertFalse(missing["usable"])
        self.assertEqual(missing["max_uses"], 0)
        self.assertFalse([q for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))])

    def test_starter_kit_is_real_once_and_t1_repair_is_free_without_ap_or_xp(self):
        from inventory.services import ensure_initial_equipment
        ensure_initial_equipment(self.a, self.bag_a)
        ensure_initial_equipment(self.a, self.bag_a)
        self.assertEqual(set(self.bag_a.slots.filter(item_amount__gt=0).values_list("item_id", flat=True)), {4104, 4114})
        self.tool(4110)
        with self.assertRaises(HuntingError):
            self.action("claim_starter_hunting_kit")
        jack, _ = NpcDefinition.objects.get_or_create(agent_id="10012", defaults=dict(name="Jack"))
        jack.is_test_only = False; jack.save(update_fields=["is_test_only"])
        spear = self.bag_a.slots.get(item_id=4110)
        spear.remaining_uses = 0; spear.save(update_fields=["remaining_uses"])
        quote = weapon_repair_quote(self.a, self.bag_a, spear, 100)
        self.assertTrue(quote["available"], quote)
        self.action("repair_hunting_weapon", dict(instance_id=str(spear.instance_id), expected_quote=quote["quote_hash"]))
        spear.refresh_from_db(); self.bag_a.refresh_from_db(); self.ap.refresh_from_db()
        self.assertEqual((spear.remaining_uses, self.bag_a.money, self.ap.action_points), (60, 100, 0))
        self.assertFalse(SkillRewardReceipt.objects.filter(player=self.a).exists())

    def test_authorization_wears_once_rechecks_ownership_and_advances_private_revision(self):
        spear = self.tool()
        rid = uuid.uuid4()
        first = self.call(rid=rid)
        self.assertTrue(first["success"], first)
        second = self.call(rid=rid)
        self.assertTrue(second["data"]["replayed"])
        spear.refresh_from_db()
        self.assertEqual(spear.remaining_uses, 59)
        self.assertGreater(first["data"]["health"]["revision"], 0)
        self.assertGreater(PlayerGrowthState.objects.get(player=self.a).revision, 0)
        spear.item_amount = 0; spear.save(update_fields=["item_amount"])
        self.assertEqual(self.call()["result_code"], "TOOL_MISSING")
        self.assertEqual(self.call(rid=rid)["data"]["weapon"]["remaining_uses"], 59)
        self.ap.refresh_from_db(); self.assertEqual(self.ap.action_points, 0)

    def test_permission_conflicts_and_old_authority_sessions_never_replay_an_attack(self):
        self.tool(); self.tool(4112)
        rid = uuid.uuid4()
        self.assertTrue(self.call(rid=rid)["success"])
        self.assertEqual(self.call(weapon=2, rid=rid)["result_code"], "REQUEST_CONFLICT")
        self.assertTrue(self.call(session="new-env-session")["success"])
        self.assertEqual(self.call(rid=rid)["result_code"], "STALE_AUTHORITY_SESSION")
        self.assertEqual(self.call(rid=rid, session="new-env-session")["result_code"], "STALE_AUTHORITY_SESSION")

    def test_t2_requires_level_four_and_last_use_kill_retains_reward_after_weapon_leaves_bag(self):
        bow = self.tool(4113, 1)
        bow.equipment_level = 3; bow.save(update_fields=["equipment_level"])
        self.assertEqual(self.call(weapon=2)["result_code"], "SKILL_LEVEL_REQUIRED")
        PlayerSkillProgress.objects.create(player=self.a, skill_code="hunting", total_xp=440)
        attack = uuid.uuid4()
        permit = self.call(weapon=2, rid=attack)
        self.assertTrue(permit["success"], permit)
        self.assertEqual(permit["data"]["weapon"]["moves"]["bow_shot"]["multiplier"], 1.8)
        self.assertAlmostEqual(permit["data"]["weapon"]["draw_duration"], 1.14)
        self.assertEqual(permit["data"]["weapon"]["remaining_uses"], 0)
        bow.item_amount = 0; bow.save(update_fields=["item_amount"])
        prey = uuid.uuid4()
        registered = wildlife(dict(action="register", request_id=str(uuid.uuid4()), prey_id=str(prey),
            species="DEER", region_id="DEEP_FOREST", position=dict(x=-145, y=6.35, z=125)))
        self.assertTrue(registered["success"], registered)
        payload = dict(action="finish", request_id=str(uuid.uuid4()), prey_id=str(prey), attempt_id=str(uuid.uuid4()),
            actor_type="PLAYER", actor_id=str(self.a.pk), death_world_minute=100, attack_id=str(attack))
        result = wildlife(payload)
        self.assertTrue(result["success"], result)
        self.assertEqual(result, wildlife(copy.deepcopy(payload)))
        self.assertEqual(result["data"]["growth_rewards"][0]["xp_gained"], 32)
        self.assertEqual(self.bag_a.slots.get(item_id=29106).item_amount, 1)
        self.assertEqual(PlayerSkillProgress.objects.get(player=self.a, skill_code="hunting").total_xp, 472)
        self.assertEqual(SkillRewardReceipt.objects.filter(player=self.a, source_type="wildlife_kill").count(), 1)

    def test_damage_event_downed_cancel_and_world_minute_rest_are_durable_without_ap_recovery(self):
        self.tool(); boar = self.boar()
        pig = VillageWildlife.objects.create(prey_id=uuid.uuid4(), species="PIG", region_id="TEST_ONLY",
            position={}, born_world_minute=100, status="CLAIMED")
        attempt = VillageHuntAttempt.objects.create(attempt_id=uuid.uuid4(), prey=pig, actor_type="PLAYER",
            actor_id=str(self.a.pk), claim_version=1, status="ACTIVE", started_world_minute=100)
        pig.active_attempt_id = attempt.pk; pig.save(update_fields=["active_attempt_id"])
        rid = uuid.uuid4()
        first = self.call("damage", rid=rid, damage=100, source_kind="BOAR_CHARGE", prey_id=str(boar.pk))
        self.assertTrue(first["data"]["health"]["downed"])
        self.assertTrue(self.call("damage", rid=rid, damage=100, source_kind="BOAR_CHARGE", prey_id=str(boar.pk))["data"]["replayed"])
        attempt.refresh_from_db(); self.assertEqual(attempt.status, "CANCELLED")
        self.assertEqual(self.call()["result_code"], "PLAYER_DOWNED")
        rest = self.action("start_safe_rest")
        self.assertEqual(rest["health"]["recovery_ready_world_minute"], 130)
        with self.assertRaises(HuntingError):
            self.action("finish_safe_rest")
        self.clock.world_minute = 130; self.clock.save(update_fields=["world_minute"])
        self.action("finish_safe_rest")
        self.ap.refresh_from_db()
        self.assertEqual((health_snapshot(self.a)["current"], self.ap.action_points), (100, 0))
        self.assertTrue(self.call()["success"])

    def test_resting_injured_player_cannot_attack_and_invalid_damage_source_cannot_write(self):
        self.tool()
        bad = self.call("damage", source_kind="BOAR_CHARGE", damage=40, prey_id=str(uuid.uuid4()))
        self.assertEqual(bad["result_code"], "DAMAGE_SOURCE_UNAVAILABLE")
        self.assertFalse(PlayerHuntingState.objects.filter(player=self.a).exists())
        boar = self.boar()
        self.assertTrue(self.call("damage", source_kind="BOAR_CHARGE", damage=40, prey_id=str(boar.pk))["success"])
        self.action("start_safe_rest")
        self.assertEqual(self.call()["result_code"], "PLAYER_RESTING")
        self.assertEqual(self.call("resolve_weapon")["result_code"], "PLAYER_RESTING")

    def test_weapon_upgrade_charges_all_real_materials_once_and_preserves_instance(self):
        PlayerSkillProgress.objects.create(player=self.a, skill_code="hunting", total_xp=440)
        self.bag_a.money = 500; self.bag_a.save(update_fields=["money"])
        nora, _ = NpcDefinition.objects.get_or_create(agent_id="10015", defaults=dict(name="Nora"))
        store, _ = VillageStore.objects.update_or_create(store_code="GENERAL_STORE", defaults=dict(
            display_name="杂货店", location_code="GENERAL_STORE", operator_npc=nora, status="OPEN"))
        account, _ = VillageStoreAccount.objects.get_or_create(store=store)
        before = account.balance
        bow = self.tool(4112)
        for item_id, quantity in ((1002, 1), (29105, 2), (29104, 1)):
            self.slot(quantity, item_id=item_id, expires_world_minute=None)
        def quote():
            return next(row["upgrade"] for row in tools_overview(self.a, self.bag_a, 100)["tools"] if row["item_id"] == 4112)
        missing = quote()
        self.assertEqual(missing["reason"], "INSUFFICIENT_MATERIALS")
        self.assertEqual({r["item_id"]: r["quantity"] for r in missing["materials"]}, {1002: 2})
        feathers = self.bag_a.slots.get(item_id=1002); feathers.item_amount = 2
        feathers.save(update_fields=["item_amount"])
        original = bow.instance_id
        execute_tool_action(self.a, self.bag_a, "upgrade_tool", dict(instance_id=str(original),
            expected_quote=quote()["quote_hash"]), uuid.uuid4(), self.clock)
        bow.refresh_from_db(); account.refresh_from_db(); self.bag_a.refresh_from_db()
        self.assertEqual((bow.instance_id, bow.item_id, bow.remaining_uses, self.bag_a.money, account.balance-before), (original, 4112, 60, 480, 20))
        self.assertEqual(sum(self.bag_a.slots.filter(item_id__in=[1002, 29104, 29105]).values_list("item_amount", flat=True)), 3)

    def test_combat_receipt_failure_rolls_back_wear_health_and_personal_revision(self):
        spear = self.tool()
        with patch.object(PlayerCombatReceipt.objects, "create", side_effect=RuntimeError("receipt failed")):
            with self.assertRaises(RuntimeError):
                self.call()
        spear.refresh_from_db()
        self.assertEqual(spear.remaining_uses, 60)
        self.assertFalse(PlayerHuntingState.objects.filter(player=self.a).exists())
        self.assertFalse(PlayerGrowthState.objects.filter(player=self.a).exists())
