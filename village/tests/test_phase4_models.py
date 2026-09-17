import uuid

from django.db import IntegrityError, transaction
from django.test import TestCase

from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition
from village.management.commands.seed_village_v1 import Command
from village.models import (
    NpcCommitment,
    NpcRelationship,
    NpcStoryFactAccess,
    VillageActionDurationConfig,
    VillageTradeOffer,
    VillageWorldEvent,
)
from village.seed_data import FORMAL_NPC_AGENT_IDS, NPC_RELATIONSHIP_SEEDS


class PhaseFourModelSeedTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.npcs = {
            agent_id: NpcDefinition.objects.get_or_create(agent_id=agent_id, defaults={"name": agent_id})[0]
            for agent_id in FORMAL_NPC_AGENT_IDS
        }
        GlobalGameTimeState.get_instance()

    def test_seed_creates_twenty_directed_relationships_and_four_active_facts(self):
        summary = {"relationships": 0, "story_fact_access": 0, "phase4_action_configs": 0}
        command = Command()

        command._seed_phase4_social(summary)
        command._seed_phase4_social(summary)

        self.assertEqual(NpcRelationship.objects.count(), 20)
        for from_id, to_id, affinity, trust, obligation, conflict in NPC_RELATIONSHIP_SEEDS:
            edge = NpcRelationship.objects.get(
                from_npc=self.npcs[from_id], to_npc=self.npcs[to_id]
            )
            self.assertEqual(
                (edge.affinity, edge.trust, edge.obligation, edge.conflict),
                (affinity, trust, obligation, conflict),
            )
        self.assertEqual(NpcStoryFactAccess.objects.count(), 4)
        self.assertEqual(
            set(NpcStoryFactAccess.objects.values_list("fact_code", flat=True)),
            {
                "BRIDGE_FOUNDATION_WARNING",
                "DEFERRED_BRIDGE_BUDGET",
                "BORROWED_RESERVE_SEEDS",
                "EXPENSIVE_CARAVAN_OPTION",
            },
        )
        self.assertEqual(VillageActionDurationConfig.objects.get(action_name="JOINT_WORK").duration_minutes, 120)
        self.assertEqual(VillageActionDurationConfig.objects.get(action_name="MEETING").duration_minutes, 30)
        self.assertEqual(VillageActionDurationConfig.objects.get(action_name="CREATE_COMMITMENT").duration_minutes, 1)
        self.assertEqual(VillageActionDurationConfig.objects.get(action_name="CANCEL_COMMITMENT").duration_minutes, 1)
        self.assertEqual(VillageActionDurationConfig.objects.get(action_name="PERFORM_COMMITMENT").duration_minutes, 1)

    def test_trade_offer_can_reference_commitment_and_event_audience_is_snapshot(self):
        event = VillageWorldEvent.objects.create(
            dedupe_key="phase4-model-event",
            event_type="COMMITMENT_CREATED",
            world_minute=100,
            time_revision=1,
            participant_npc_ids=["10011", "10012"],
            witness_npc_ids=["10013"],
            player_ids=["player-a"],
            visibility="PARTICIPANTS",
        )
        commitment = NpcCommitment.objects.create(
            commitment_type="DELIVERY",
            promisor_npc=self.npcs["10011"],
            beneficiary_npc=self.npcs["10012"],
            target={"item_id": 1051, "amount": 1, "gold_amount": 1},
            due_world_minute=200,
            source_request_id=uuid.uuid4(),
            created_event=event,
            created_world_minute=100,
        )
        offer = VillageTradeOffer.objects.create(
            buyer_npc=self.npcs["10012"],
            seller_npc=self.npcs["10011"],
            proposer_npc=self.npcs["10012"],
            commitment=commitment,
            item_id=1051,
            amount=1,
            gold_amount=1,
            location_code="VILLAGE_SQUARE",
            expires_world_minute=200,
            created_request_id=uuid.uuid4(),
            created_world_minute=100,
        )
        self.assertEqual(offer.commitment_id, commitment.commitment_id)
        self.assertEqual(event.witness_npc_ids, ["10013"])
        self.assertEqual(event.player_ids, ["player-a"])

    def test_relationship_and_commitment_reject_self_links(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                NpcRelationship.objects.create(from_npc=self.npcs["10011"], to_npc=self.npcs["10011"])
        event = VillageWorldEvent.objects.create(
            dedupe_key="phase4-self-commitment-event",
            event_type="COMMITMENT_CREATED",
            world_minute=100,
            time_revision=1,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                NpcCommitment.objects.create(
                    commitment_type="MEETING",
                    promisor_npc=self.npcs["10011"],
                    beneficiary_npc=self.npcs["10011"],
                    target={"location_code": "VILLAGE_SQUARE"},
                    due_world_minute=200,
                    source_request_id=uuid.uuid4(),
                    created_event=event,
                    created_world_minute=100,
                )
