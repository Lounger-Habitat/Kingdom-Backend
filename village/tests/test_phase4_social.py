import uuid

from django.test import TestCase
from rest_framework.test import APIClient

from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot
from inventory.models import InventoryBag, InventorySlot
from account.models import GameAccount
from items.models import Item

from village.models import (
    EconomicLedgerEntry,
    NpcCommitment,
    NpcCommandState,
    NpcRelationship,
    NpcRelationshipChange,
    NpcRuntimeState,
    VillageLocationDefinition,
    VillageActionReceipt,
    VillageDeliveryOutbox,
    VillageSimulationConfig,
    VillageWorldEvent,
)
from village.phase3_asset_service import commit_economy_action
from village.phase3_contract import VILLAGE_ECONOMY_ACTION_V1
from village.phase4_social import (
    breach_due_commitments,
    cancel_commitment,
    complete_social_action,
    complete_delivery_commitment,
    create_commitment,
    grant_npc_fact,
    grant_player_fact,
    has_npc_story_fact,
    inspect_social_state,
    perform_commitment,
    reduce_relationship,
)


class PhaseFourSocialTest(TestCase):
    def setUp(self):
        cfg, _ = VillageSimulationConfig.objects.get_or_create(config_key="default")
        cfg.village_social_enabled = True
        cfg.village_economy_enabled = True
        cfg.village_direct_trade_enabled = True
        cfg.save(update_fields=["village_social_enabled", "village_economy_enabled", "village_direct_trade_enabled"])
        self.clock = GlobalGameTimeState.get_instance()
        self.clock.world_minute = 1000
        self.clock.time_revision = 7
        self.clock.save()
        VillageLocationDefinition.objects.get_or_create(
            location_code="MARKET_STREET",
            defaults={"display_name": "Market Street", "center_anchor_code": "MARKET_STREET", "location_type": "public"},
        )
        self.npcs = {}
        for agent_id in ("10011", "10012", "10013", "10014", "10015"):
            npc, _ = NpcDefinition.objects.get_or_create(agent_id=agent_id, defaults={"name": agent_id})
            self.npcs[agent_id] = npc
            inventory, _ = NpcGlobalInventory.objects.get_or_create(npc=npc, defaults={"money": 500})
            inventory.money = 500
            inventory.save(update_fields=["money"])
            NpcRuntimeState.objects.update_or_create(
                npc=npc,
                defaults={"current_location_code": "MARKET_STREET", "current_point_code": "MARKET_STREET", "action_status": "IDLE"},
            )
        self.relationship, _ = NpcRelationship.objects.update_or_create(
            from_npc=self.npcs["10011"], to_npc=self.npcs["10012"],
            defaults={"affinity": 0, "trust": 0, "obligation": 0, "conflict": 0},
        )

    def event(self, label, minute=None):
        return VillageWorldEvent.objects.create(
            dedupe_key=f"phase4-test:{label}:{uuid.uuid4()}",
            event_type=label,
            world_minute=minute if minute is not None else self.clock.world_minute,
            time_revision=self.clock.time_revision,
            actor_npc=self.npcs["10011"],
            participant_npc_ids=["10011", "10012"],
            visibility="PARTICIPANTS",
        )

    def commitment(self, kind, target, beneficiary="10012", due=2000):
        request_id = uuid.uuid4()
        result = create_commitment(
            "10011",
            {
                "beneficiary_npc_id": beneficiary,
                "commitment_type": kind,
                "target": target,
                "due_world_minute": due,
            },
            self.clock.world_minute,
            request_id,
        )
        self.assertEqual(result["result_code"], "OK")
        return NpcCommitment.objects.get(source_request_id=request_id)

    def test_directed_reducer_dedup_decrease_and_daily_cap(self):
        first = self.event("REL-1", 1000)
        self.assertEqual(reduce_relationship(first, "10011", "10012", "DELIVERY", {"trust": 4}), {"trust": 4})
        self.assertEqual(reduce_relationship(first, "10011", "10012", "DELIVERY", {"trust": 4}), {})
        self.assertEqual(reduce_relationship(self.event("REL-2", 1001), "10011", "10012", "DELIVERY", {"trust": 4}), {"trust": 2})
        self.assertEqual(reduce_relationship(self.event("REL-3", 1002), "10011", "10012", "DELIVERY", {"trust": 4}), {"trust": 1})
        for index in range(10):
            reduce_relationship(self.event(f"REL-CAP-{index}", 1003 + index), "10011", "10012", "DELIVERY", {"trust": 4})
        relation = NpcRelationship.objects.get(pk=self.relationship.pk)
        self.assertLessEqual(relation.trust, 10)
        self.assertEqual(NpcRelationshipChange.objects.filter(relationship=relation).count(), 13)

    def test_commitment_lifecycle_and_due_breach(self):
        repayment = self.commitment("REPAYMENT", {"gold_amount": 20})
        done = perform_commitment("10011", {"commitment_id": str(repayment.commitment_id)}, 1000, uuid.uuid4())
        self.assertEqual(done["result_code"], "OK")
        self.assertEqual(NpcCommitment.objects.get(pk=repayment.pk).status, "COMPLETED")

        donation = self.commitment("DONATION", {"asset_kind": "GOLD", "gold_amount": 10})
        self.assertEqual(perform_commitment("10011", {"commitment_id": str(donation.commitment_id)}, 1000, uuid.uuid4())["result_code"], "OK")
        donation_relation = NpcRelationship.objects.get(from_npc=self.npcs["10012"], to_npc=self.npcs["10011"])
        self.assertEqual((donation_relation.affinity, donation_relation.obligation), (2, 3))

        joint = self.commitment("JOINT_WORK", {"location_code": "MARKET_STREET", "work_code": "REPAIR"})
        started = perform_commitment("10011", {"commitment_id": str(joint.commitment_id), "phase": "START"}, 1000, uuid.uuid4())
        self.assertEqual(started["presentation_status"], "WORKING")
        self.assertEqual(perform_commitment("10011", {"commitment_id": str(joint.commitment_id)}, 1000, uuid.uuid4())["result_code"], "OK")

        meeting = self.commitment("MEETING", {"location_code": "MARKET_STREET"})
        self.assertEqual(perform_commitment("10011", {"commitment_id": str(meeting.commitment_id)}, 1000, uuid.uuid4())["presentation_status"], "SOCIALIZING")

        cancelled = self.commitment("DONATION", {"asset_kind": "GOLD", "gold_amount": 1})
        self.assertEqual(cancel_commitment("10011", {"commitment_id": str(cancelled.commitment_id)}, 1000, uuid.uuid4())["result_code"], "OK")

        breached = self.commitment("MEETING", {"location_code": "MARKET_STREET"}, due=1001)
        self.assertEqual(
            cancel_commitment("10011", {"commitment_id": str(breached.commitment_id)}, 1001, uuid.uuid4())["result_code"],
            "COMMITMENT_EXPIRED",
        )
        self.assertEqual(breach_due_commitments(1001), 1)
        self.assertEqual(NpcCommitment.objects.get(pk=breached.pk).status, "BREACHED")

    def test_active_perform_uses_start_duration_and_releases_command(self):
        meeting = self.commitment("MEETING", {"location_code": "MARKET_STREET"})
        request_id = uuid.uuid4()
        command_id = uuid.uuid4()
        command = NpcCommandState.objects.create(
            command_id=command_id,
            message_id=uuid.uuid4(),
            request_id=request_id,
            npc=self.npcs["10011"],
            sender_id="10011",
            recipient_id="WuxiVillage",
            kind="PERFORM_COMMITMENT",
            payload_sha256="test",
            status="RUNNING",
            started_world_minute=1000,
            duration_minutes=30,
        )
        runtime = NpcRuntimeState.objects.get(npc=self.npcs["10011"])
        runtime.current_action = "PERFORM_COMMITMENT"
        runtime.action_status = "ACTING"
        runtime.action_started_world_minute = 1000
        runtime.action_ends_world_minute = 1030
        runtime.active_command_id = command_id
        runtime.save()
        base = {
            "command_id": str(command_id),
            "request_id": str(request_id),
            "npc_id": "10011",
            "action": "PERFORM_COMMITMENT",
        }

        started = complete_social_action({
            **base,
            "params": {"commitment_id": str(meeting.commitment_id), "phase": "START"},
        })
        self.assertEqual(started["duration_minutes"], 30)
        command.refresh_from_db()
        runtime.refresh_from_db()
        self.assertEqual(command.duration_minutes, 30)
        self.assertEqual(runtime.action_ends_world_minute, 1030)

        early = complete_social_action({
            **base,
            "params": {"commitment_id": str(meeting.commitment_id)},
        })
        self.assertEqual(early["result_code"], "ACTION_NOT_FINISHED")
        self.clock.world_minute = 1030
        self.clock.save(update_fields=["world_minute"])
        completed = complete_social_action({
            **base,
            "params": {"commitment_id": str(meeting.commitment_id)},
        })
        self.assertEqual(completed["result_code"], "OK")
        command.refresh_from_db()
        runtime.refresh_from_db()
        self.assertEqual(command.status, "SUCCEEDED")
        self.assertEqual(runtime.action_status, "IDLE")
        self.assertIsNone(runtime.active_command_id)
        self.assertEqual(VillageActionReceipt.objects.get(request_id=request_id).command_id, command_id)

    def test_duplicate_open_commitment_is_rejected(self):
        params = {
            "beneficiary_npc_id": "10012",
            "commitment_type": "DONATION",
            "target": {"asset_kind": "GOLD", "gold_amount": 7},
            "due_world_minute": 2000,
        }
        first = create_commitment("10011", params, 1000, uuid.uuid4())
        self.assertEqual(first["result_code"], "OK")
        duplicate = create_commitment("10011", params, 1000, uuid.uuid4())
        self.assertEqual(duplicate["result_code"], "COMMITMENT_DUPLICATE_OPEN")
        self.assertEqual(NpcCommitment.objects.filter(status="OPEN").count(), 1)

    def test_social_state_is_outgoing_summary_only(self):
        for other_id in ("10012", "10013", "10014", "10015"):
            NpcRelationship.objects.update_or_create(
                from_npc=self.npcs["10011"], to_npc=self.npcs[other_id],
                defaults={"affinity": 20, "trust": 20, "obligation": 0, "conflict": 0},
            )
        incoming = create_commitment(
            "10012",
            {
                "beneficiary_npc_id": "10011",
                "commitment_type": "DONATION",
                "target": {"asset_kind": "GOLD", "gold_amount": 7},
                "due_world_minute": 2000,
            },
            1000,
            uuid.uuid4(),
        )
        self.assertEqual(incoming["result_code"], "OK")
        result = inspect_social_state("10011")
        rows = result["data"]["relationships"]
        self.assertEqual(len(rows), 4)
        self.assertEqual({row["other_npc_id"] for row in rows}, {"10012", "10013", "10014", "10015"})
        self.assertEqual(set(rows[0]), {"other_npc_id", "label", "has_open_commitment"})
        self.assertTrue(next(row for row in rows if row["other_npc_id"] == "10012")["has_open_commitment"])

        client = APIClient()
        account = GameAccount.objects.create(username="phase4-social-api", password="x")
        client.force_authenticate(user=account)
        response = client.get("/api/village/social/npcs/10011")
        self.assertEqual(response.status_code, 200)
        api_rows = response.data["data"]["relationships"]
        self.assertEqual(len(api_rows), 4)
        self.assertEqual(set(api_rows[0]), {"other_npc_id", "label", "has_open_commitment"})
        self.assertTrue(next(row for row in api_rows if row["other_npc_id"] == "10012")["has_open_commitment"])

    def test_npc_story_fact_guard(self):
        self.assertFalse(has_npc_story_fact("10011", "FOREST_OLD_ROUTE"))
        self.assertFalse(has_npc_story_fact("10011", "NOT_A_FACT"))
        grant_npc_fact("10011", "FOREST_OLD_ROUTE", 1000)
        self.assertTrue(has_npc_story_fact("10011", "FOREST_OLD_ROUTE"))

    def test_social_flag_off_fails_closed(self):
        cfg = VillageSimulationConfig.objects.get(config_key="default")
        cfg.village_social_enabled = False
        cfg.save(update_fields=["village_social_enabled"])
        try:
            result = __import__("village.phase4_social", fromlist=["complete_social_action"]).complete_social_action({
                "request_id": str(uuid.uuid4()),
                "command_id": str(uuid.uuid4()),
                "npc_id": "10011",
                "action": "INSPECT_SOCIAL_STATE",
                "params": {},
            })
            self.assertEqual(result["result_code"], "VILLAGE_SOCIAL_DISABLED")
        finally:
            cfg.village_social_enabled = True
            cfg.save(update_fields=["village_social_enabled"])

    def test_delivery_trade_is_atomic_and_completes_commitment(self):
        seller_inv = NpcGlobalInventory.objects.get(npc=self.npcs["10011"])
        buyer_inv = NpcGlobalInventory.objects.get(npc=self.npcs["10012"])
        NpcGlobalInventorySlot.objects.create(
            bag=seller_inv, slot_index=0, item_id=1030, item_amount=2,
            produced_world_minute=900, source_request_id=uuid.uuid4(),
        )
        commitment = self.commitment("DELIVERY", {"item_id": 1030, "amount": 1, "gold_amount": 10})
        offer_payload = {
            "schema_version": VILLAGE_ECONOMY_ACTION_V1,
            "command_id": str(uuid.uuid4()), "request_id": str(uuid.uuid4()), "message_id": str(uuid.uuid4()),
            "command_name": "PROPOSE_TRADE", "npc_id": 10012, "completed_world_minute": 1000,
            "time_revision": 7, "current_location_code": "MARKET_STREET",
            "params": {"target_npc_id": 10011, "item_id": 1030, "amount": 1, "gold_amount": 10,
                       "conversation_id": "c", "expected_seller_asset_version": None,
                       "expires_world_minute": 1200, "commitment_id": str(commitment.commitment_id)},
        }
        proposed = commit_economy_action(offer_payload)
        self.assertEqual(proposed["result_code"], "OK")
        accepted = commit_economy_action({
            "schema_version": VILLAGE_ECONOMY_ACTION_V1,
            "command_id": str(uuid.uuid4()), "request_id": str(uuid.uuid4()), "message_id": str(uuid.uuid4()),
            "command_name": "RESPOND_TRADE", "npc_id": 10011, "completed_world_minute": 1000,
            "time_revision": 7, "current_location_code": "MARKET_STREET",
            "params": {"offer_id": proposed["data"]["offer_id"], "decision": "ACCEPT"},
        })
        self.assertEqual(accepted["result_code"], "OK")
        self.assertEqual(NpcCommitment.objects.get(pk=commitment.pk).status, "COMPLETED")
        self.assertEqual(EconomicLedgerEntry.objects.filter(request_id=uuid.UUID(accepted["request_id"])).count(), 1)
        self.assertTrue(VillageActionReceipt.objects.filter(request_id=uuid.UUID(accepted["request_id"])).exists())
        # 交易事件通知对手方；执行者仍只收到自己的 OUTCOME 回执。
        self.assertTrue(VillageWorldEvent.objects.filter(event_id=accepted["event"]["event_id"]).exists())
        rows = VillageDeliveryOutbox.objects.filter(event__event_id=accepted["event"]["event_id"])
        self.assertEqual(
            {(row.message_kind, row.recipient) for row in rows},
            {("OUTCOME", "10011"), ("EVENT", "10012")},
        )
        self.assertEqual(sum(NpcGlobalInventorySlot.objects.filter(bag=buyer_inv, item_id=1030).values_list("item_amount", flat=True)), 1)

    def test_low_stock_low_price_delivery_keeps_trust_and_adds_emergency_gains(self):
        seller_inv = NpcGlobalInventory.objects.get(npc=self.npcs["10011"])
        NpcGlobalInventorySlot.objects.create(
            bag=seller_inv, slot_index=0, item_id=1030, item_amount=1,
            produced_world_minute=900, source_request_id=uuid.uuid4(),
        )
        commitment = self.commitment("DELIVERY", {"item_id": 1030, "amount": 1, "gold_amount": 8})
        proposed = commit_economy_action({
            "schema_version": VILLAGE_ECONOMY_ACTION_V1,
            "command_id": str(uuid.uuid4()), "request_id": str(uuid.uuid4()), "message_id": str(uuid.uuid4()),
            "command_name": "PROPOSE_TRADE", "npc_id": 10012, "completed_world_minute": 1000,
            "time_revision": 7, "current_location_code": "MARKET_STREET",
            "params": {"target_npc_id": 10011, "item_id": 1030, "amount": 1, "gold_amount": 8,
                       "conversation_id": "emergency", "expected_seller_asset_version": None,
                       "expires_world_minute": 1200, "commitment_id": str(commitment.commitment_id)},
        })
        accepted = commit_economy_action({
            "schema_version": VILLAGE_ECONOMY_ACTION_V1,
            "command_id": str(uuid.uuid4()), "request_id": str(uuid.uuid4()), "message_id": str(uuid.uuid4()),
            "command_name": "RESPOND_TRADE", "npc_id": 10011, "completed_world_minute": 1000,
            "time_revision": 7, "current_location_code": "MARKET_STREET",
            "params": {"offer_id": proposed["data"]["offer_id"], "decision": "ACCEPT"},
        })
        self.assertEqual(accepted["result_code"], "OK")
        relation = NpcRelationship.objects.get(from_npc=self.npcs["10012"], to_npc=self.npcs["10011"])
        self.assertEqual((relation.trust, relation.affinity, relation.obligation), (3, 3, 4))

    def test_player_private_fact_is_idempotent_and_private(self):
        player = "player-phase4-a"
        first = grant_player_fact(player, "FOREST_OLD_ROUTE", 1000)
        second = grant_player_fact(player, "FOREST_OLD_ROUTE", 1000)
        self.assertEqual(first.pk, second.pk)
        rows = VillageDeliveryOutbox.objects.filter(event__event_type="STORY_FACT_UNLOCKED")
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().recipient, "ENV_CLIENTS")
        self.assertNotIn("player-phase4-b", rows.first().payload.get("player_ids", []))

    def test_player_fact_api_uses_authenticated_identity(self):
        first = GameAccount.objects.create(username="phase4-facts-a", password="x")
        second = GameAccount.objects.create(username="phase4-facts-b", password="x")
        grant_player_fact(str(first.pk), "FOREST_OLD_ROUTE", 1000)
        grant_player_fact(str(second.pk), "BRIDGE_FOUNDATION_WARNING", 1000)
        client = APIClient()
        client.force_authenticate(user=first)
        response = client.get(
            "/api/village/social/player/facts",
            {"player_id": str(second.pk)},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["fact_codes"], ["FOREST_OLD_ROUTE"])

    def test_thirty_day_relationship_soak_stays_bounded(self):
        for day in range(30):
            minute = (day + 1) * 1440
            event = self.event(f"SOAK-{day}", minute)
            reduce_relationship(event, "10011", "10012", "JOINT_WORK", {"trust": 3, "affinity": 2})
        relation = NpcRelationship.objects.get(pk=self.relationship.pk)
        self.assertGreaterEqual(relation.trust, 0)
        self.assertLessEqual(relation.trust, 100)
        self.assertGreaterEqual(relation.affinity, 0)
        self.assertLessEqual(relation.affinity, 100)
        self.assertLessEqual(VillageDeliveryOutbox.objects.count(), 30)

    def test_long_action_freezes_start_and_result_witnesses_separately(self):
        NpcRuntimeState.objects.filter(npc=self.npcs["10015"]).update(current_location_code="NORA_HOME")
        commitment = self.commitment(
            "JOINT_WORK",
            {"location_code": "MARKET_STREET", "work_code": "REPAIR"},
        )
        started = perform_commitment(
            "10011", {"commitment_id": str(commitment.commitment_id), "phase": "START"}, 1000, uuid.uuid4()
        )
        start_event = VillageWorldEvent.objects.get(event_id=started["world_event_id"])
        self.assertNotIn("10015", start_event.witness_npc_ids)

        NpcRuntimeState.objects.filter(npc=self.npcs["10015"]).update(current_location_code="MARKET_STREET")
        completed = perform_commitment(
            "10011", {"commitment_id": str(commitment.commitment_id)}, 1060, uuid.uuid4()
        )
        result_event = VillageWorldEvent.objects.get(event_id=completed["world_event_id"])
        self.assertIn("10015", result_event.witness_npc_ids)

    def test_player_can_only_give_emergency_supply_to_formal_npc(self):
        player = GameAccount.objects.create(username="phase4-gift-player", password="x")
        bag = InventoryBag.objects.create(player=player, character_id=player.username)
        Item.objects.get_or_create(item_id=1030, defaults={"item_name": "phase4 gift"})
        InventorySlot.objects.create(bag=bag, slot_index=0, item_id=1030, item_amount=1)
        client = APIClient()
        client.force_authenticate(user=player)
        body = {
            "requestId": str(uuid.uuid4()),
            "requesterId": str(player.pk),
            "initiator": {"type": "player", "id": "self"},
            "other": {"type": "npc", "id": "10011"},
            "giveItem": [{"itemID": 1030, "amount": 1}],
            "getItem": [], "giveMoney": 0, "getMoney": 0,
        }
        response = client.post("/api/env/actions/character-trade", body, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(NpcGlobalInventorySlot.objects.filter(bag__npc=self.npcs["10011"], item_id=1030).count(), 1)

        reverse = dict(body)
        reverse["requestId"] = str(uuid.uuid4())
        reverse["initiator"] = {"type": "npc", "id": "10011"}
        reverse["other"] = {"type": "player", "id": "self"}
        reverse["getItem"] = [{"itemID": 1030, "amount": 1}]
        reverse["giveItem"] = []
        reverse_response = client.post("/api/env/actions/character-trade", reverse, format="json")
        self.assertEqual(reverse_response.status_code, 409)


    def test_discovered_commitment_examples_are_accepted_by_real_contract(self):
        from village.command_catalog import agent_tool
        from village.phase4_contract import validate_commitment_target
        for code in ("RESTAURANT", "RECORD_HALL"):
            VillageLocationDefinition.objects.create(location_code=code, display_name=code,
                center_anchor_code=code, location_type="public")
        schema = agent_tool("CREATE_COMMITMENT")["parameters"]
        self.assertEqual(len(schema["oneOf"]), 6)
        for example in schema["examples"]:
            with self.subTest(kind=example["commitment_type"], target=example["target"]):
                self.assertEqual(validate_commitment_target(example["commitment_type"], example["target"]), example["target"])
                result = create_commitment("10014", example, 1000, uuid.uuid4())
                self.assertEqual(result["result_code"], "OK", result)
        self.assertIn("commitment_id", agent_tool("PROPOSE_TRADE")["parameters"]["properties"])

    def test_bad_commitment_reports_specific_target_or_deadline_field(self):
        base = dict(beneficiary_npc_id="10011", commitment_type="DONATION",
            target={"asset_kind": "GOLD", "gold_amount": 2}, due_world_minute=2000)
        for patch, field in (({"target": {"asset_kind": "GOLD"}}, "target.gold_amount"),
                ({"target": {"asset_kind": "GOLD", "gold_amount": True}}, "target.gold_amount"),
                ({"target": {"asset_kind": "GOLD", "gold_amount": 2, "amount": 1}}, "target.amount"),
                ({"due_world_minute": 1000}, "due_world_minute"),
                ({"commitment_type": "MEETING", "target": {"location_code": "MISSING_PLACE"}}, "target.location_code")):
            result = create_commitment("10014", dict(base, **patch), 1000, uuid.uuid4())
            self.assertEqual(result["result_code"], "SOCIAL_INVALID_PARAMS")
            self.assertIn(field, [e["field"] for e in result["data"]["field_errors"]])
