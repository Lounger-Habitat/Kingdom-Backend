import uuid
from copy import deepcopy
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.components import copy_batch_attributes
from inventory.models import InventoryBag, InventorySlot, NpcDefinition
from items.models import Item
from marketplace.models import MarketListing
from marketplace.services import snapshot_slot, _listing_batch
from player.equipment import stats_for
from player.models import EquipmentDefinition, EquipmentLevel, WeaponMove, PlayerSkillProgress
from village.models import VillageStore, VillageStoreAccount, VillageStoreInventorySlot, VillageSimulationConfig
from village.production_tools import _quote, execute_tool_action, resolve_tool, ToolError
from village.player_hunting import execute_combat


class EquipmentTests(TestCase):
    def setUp(self):
        self.player = GameAccount.objects.create(username="equipment-five-levels")
        self.bag = InventoryBag.objects.create(player=self.player, character_id=self.player.username, money=10000)
        self.clock = GlobalGameTimeState.get_instance()
        self.clock.world_minute = 100
        self.clock.save()
        nora, _ = NpcDefinition.objects.get_or_create(agent_id="10015", defaults={"name": "Nora"})
        self.store, _ = VillageStore.objects.update_or_create(store_code="GENERAL_STORE", defaults=dict(
            display_name="杂货店", location_code="GENERAL_STORE", operator_npc=nora, status="OPEN"))
        VillageStoreAccount.objects.update_or_create(store=self.store, defaults={"balance": 0})
        for item_id in (1002, 4003, 29306):
            Item.objects.get_or_create(pk=item_id, defaults=dict(item_name=f"材料{item_id}", item_price=10))
            self.slot(item_id, amount=100)

    def slot(self, item_id, level=1, uses=10, amount=1):
        return InventorySlot.objects.create(bag=self.bag, slot_index=self.bag.slots.count(), item_id=item_id,
            item_amount=amount, remaining_uses=uses, equipment_level=level)

    def skill(self, code, xp=5780):
        PlayerSkillProgress.objects.update_or_create(player=self.player, skill_code=code, defaults={"total_xp": xp})

    def test_seed_has_sixteen_species_eighty_levels_and_seven_moves(self):
        self.assertEqual(EquipmentDefinition.objects.count(), 16)
        self.assertEqual(EquipmentLevel.objects.count(), 80)
        self.assertEqual(WeaponMove.objects.count(), 7)
        self.assertEqual(list(EquipmentLevel.objects.filter(equipment__item_id=4114).values_list("attack_min", "attack_max")),
                         [(15, 18), (18, 21), (21, 24), (24, 27), (27, 30)])

    def test_upgrade_keeps_species_identity_and_remaining_durability(self):
        self.skill("hunting")
        sword = self.slot(4114, uses=7)
        quote = _quote(self.player, self.bag, sword, "upgrade", 100)
        self.assertTrue(quote["available"], quote)
        result = execute_tool_action(self.player, self.bag, "upgrade_tool", dict(
            instance_id=str(sword.instance_id), expected_quote=quote["quote_hash"]), uuid.uuid4(), self.clock)
        sword.refresh_from_db()
        self.assertEqual((sword.item_id, sword.equipment_level, sword.remaining_uses), (4114, 2, 7))
        self.assertEqual(result["instance_id"], str(sword.instance_id))
        self.assertEqual(stats_for(sword)["attack_min"], 18)

    def test_admin_configuration_change_invalidates_quote_without_spending(self):
        self.skill("hunting")
        sword = self.slot(4114)
        quote = _quote(self.player, self.bag, sword, "upgrade", 100)
        EquipmentLevel.objects.filter(equipment__item_id=4114, level=2).update(attack_min=99, attack_max=100)
        with self.assertRaises(ToolError) as raised:
            execute_tool_action(self.player, self.bag, "upgrade_tool", dict(instance_id=str(sword.instance_id),
                expected_quote=quote["quote_hash"]), uuid.uuid4(), self.clock)
        self.assertEqual(raised.exception.code, "QUOTE_CHANGED")
        self.bag.refresh_from_db()
        sword.refresh_from_db()
        self.assertEqual(self.bag.money, 10000)
        self.assertEqual(sword.equipment_level, 1)

    def test_level_five_has_no_upgrade(self):
        self.skill("hunting")
        sword = self.slot(4114, level=5)
        self.assertEqual(_quote(self.player, self.bag, sword, "upgrade", 100)["reason"], "NO_UPGRADE")

    def test_content_gate_uses_instance_level_for_both_species(self):
        self.skill("forestry")
        basic = self.slot(4104, level=3)
        steel = self.slot(4105, level=1)
        self.assertEqual(resolve_tool(self.player, self.bag, "forestry", basic.instance_id, minimum_tier=2, minute=100).pk, basic.pk)
        with self.assertRaises(ToolError):
            resolve_tool(self.player, self.bag, "forestry", steel.instance_id, minimum_tier=2, minute=100)

    def test_attack_roll_is_in_base_range_and_retries_reuse_it(self):
        self.skill("hunting")
        VillageSimulationConfig.objects.update_or_create(config_key="default", defaults={"village_economy_enabled": True})
        sword = self.slot(4114, uses=20)
        payload = dict(action="authorize_attack", player_id=str(self.player.pk), request_id=str(uuid.uuid4()),
                       authority_session_id="equipment-test", weapon=1, instance_id=str(sword.instance_id))
        with patch("village.player_hunting.secrets.randbelow", side_effect=[3, 0]):
            result = execute_combat(payload)
        self.assertTrue(result["success"], result)
        weapon = result["data"]["weapon"]
        self.assertIn(weapon["attack_base"], range(15, 19))
        self.assertTrue(weapon["critical"])
        self.assertEqual(weapon["critical_chance"], 0.1)
        self.assertEqual(weapon["critical_multiplier"], 1.5)
        self.assertEqual(weapon["moves"]["sword_light_0"]["multiplier"], 1.2)
        self.assertEqual(weapon["moves"]["sword_heavy"]["multiplier"], 2)
        self.assertNotIn("damage_multiplier", weapon)
        with patch("village.player_hunting.secrets.randbelow", side_effect=AssertionError("A retry must not roll damage or critical again")):
            self.assertEqual(execute_combat(payload)["data"]["weapon"], weapon)
        sword.refresh_from_db()
        self.assertEqual(sword.remaining_uses, 19)

    def test_changed_move_is_used_by_next_authorization(self):
        self.skill("hunting")
        VillageSimulationConfig.objects.update_or_create(config_key="default", defaults={"village_economy_enabled": True})
        sword = self.slot(4130)
        WeaponMove.objects.filter(code="sword_heavy").update(multiplier=2.5)
        result = execute_combat(dict(action="resolve_weapon", player_id=str(self.player.pk), request_id=str(uuid.uuid4()),
            authority_session_id="equipment-test", weapon=1, instance_id=str(sword.instance_id)))
        self.assertTrue(result["success"], result)
        self.assertEqual(result["data"]["weapon"]["moves"]["sword_heavy"]["multiplier"], 2.5)

    def test_store_and_market_transfers_preserve_level(self):
        sword = self.slot(4130, level=4, uses=55)
        store_slot = VillageStoreInventorySlot(store=self.store, item_id=sword.item_id, item_amount=1)
        copy_batch_attributes(sword, store_slot)
        store_slot.save()
        store_slot.refresh_from_db()
        self.assertEqual((store_slot.equipment_level, store_slot.remaining_uses), (4, 55))
        listing = MarketListing.objects.create(seller=self.player, item_id=sword.item_id,
            total_amount=1, remaining_amount=1, price=100, item_snapshot=snapshot_slot(sword))
        listing.refresh_from_db()
        self.assertEqual(_listing_batch(listing)["equipmentLevel"], 4)
        self.assertEqual(listing.remaining_uses, 55)

    def test_reads_do_not_repair_or_initialize(self):
        sword = self.slot(4114, level=3, uses=0)
        with CaptureQueriesContext(connection) as queries:
            stats = stats_for(sword)
        self.assertEqual(stats["attack_min"], 21)
        self.assertFalse([q for q in queries if q["sql"].lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE"))])
        sword.refresh_from_db()
        self.assertEqual(sword.remaining_uses, 0)

    def test_admin_rejects_invalid_ranges_and_probabilities(self):
        row = EquipmentLevel.objects.get(equipment__item_id=4114, level=1)
        row.attack_min = row.attack_max+1
        with self.assertRaises(ValidationError):
            row.full_clean()
        row.attack_min = 15
        row.quality_bonus_chance = 1.1
        with self.assertRaises(ValidationError):
            row.full_clean()

    def test_admin_pages_are_registered_and_render(self):
        admin = get_user_model().objects.create_superuser(username="equipment-admin", password="test-only")
        self.client.force_login(admin)
        for path in ("/admin/player/equipmentdefinition/", "/admin/player/equipmentlevel/", "/admin/player/weaponmove/"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
        profile = EquipmentDefinition.objects.get(item_id=4114)
        response = self.client.get(f"/admin/player/equipmentdefinition/{profile.pk}/change/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "基础攻击下限")

    def test_admin_post_changes_the_actual_runtime_attack_range(self):
        import json
        admin = get_user_model().objects.create_superuser(username="equipment-editor", password="test-only")
        self.client.force_login(admin)
        row = EquipmentLevel.objects.get(equipment__item_id=4114, level=1)
        payload = {}
        for field in row._meta.fields:
            if field.name != "id":
                value = getattr(row, field.attname)
                payload[field.name] = json.dumps(value) if field.name.endswith("materials") else value
        payload.update(attack_min=16, attack_max=19)
        response = self.client.post(f"/admin/player/equipmentlevel/{row.pk}/change/", payload)
        self.assertEqual(response.status_code, 302)
        stats = stats_for(self.slot(4114))
        self.assertEqual((stats["attack_min"], stats["attack_max"]), (16, 19))

    def test_invalid_attack_configuration_cannot_spend_durability(self):
        VillageSimulationConfig.objects.update_or_create(config_key="default", defaults={"village_economy_enabled": True})
        sword = self.slot(4114)
        EquipmentLevel.objects.filter(equipment__item_id=4114, level=1).update(attack_min=0, attack_max=0)
        result = execute_combat(dict(action="authorize_attack", player_id=str(self.player.pk), request_id=str(uuid.uuid4()),
            authority_session_id="equipment-test", weapon=1, instance_id=str(sword.instance_id)))
        self.assertFalse(result["success"])
        self.assertEqual(result["result_code"], "WEAPON_CONFIGURATION_UNAVAILABLE")
        sword.refresh_from_db()
        self.assertEqual(sword.remaining_uses, 10)

    def test_buyback_uses_this_instances_level_maximum(self):
        from village.player_store import store_sale_quote
        axe = self.slot(4104, level=3, uses=40)
        item = Item.objects.get(pk=4104)
        self.assertEqual(item.max_use_count, 20)
        self.assertTrue(store_sale_quote(axe, item, 100)["allowed"])
        axe.remaining_uses = 39
        self.assertFalse(store_sale_quote(axe, item, 100)["allowed"])
