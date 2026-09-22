"""Exercise historical models in isolated databases, never the saved village."""
import importlib
from types import SimpleNamespace
import uuid

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.models import InventoryBag, InventorySlot, NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot
from items.models import Item
from village.models import (NpcProfile, VillageLocationDefinition, VillagePersonalCommission,
    VillagePersonalCommissionEscrow, VillagePersonalCommissionReceipt, VillagePlayerActionReceipt,
    VillagePlayerContribution)
from village.personal_commissions import contribute_commission, abandon_commission


class LegacySupplyMigrationTests(TransactionTestCase):
    def setUp(self):
        super().setUp()
        loader = MigrationExecutor(connection).loader
        self.old_apps = loader.project_state([("village", "0051_personal_commissions")]).apps
        self.Contract = self.old_apps.get_model("village", "VillagePlayerContract")
        self.Claim = self.old_apps.get_model("village", "VillageContractClaim")
        # 0052 is intentionally irreversible. Recreate only its retired source
        # tables in this throwaway test DB; never roll production data backwards.
        with connection.schema_editor() as editor:
            editor.create_model(self.Contract)
            editor.create_model(self.Claim)
        self.migration = importlib.import_module("village.migrations.0052_migrate_legacy_supply")
        self.npc = NpcDefinition.objects.create(agent_id="migration-lucy", name="Lucy")
        self.wallet = NpcGlobalInventory.objects.create(npc=self.npc, money=350)
        self.player = GameAccount.objects.create(username="migration-player")
        self.bag = InventoryBag.objects.create(player=self.player, character_id=self.player.username, money=100)
        for item, name in [(1002,"木材"),(1030,"玉米"),(1023,"番茄")]:
            Item.objects.update_or_create(pk=item, defaults=dict(item_name=name,item_price=5,is_deleted=False))
        VillageLocationDefinition.objects.create(location_code="MIGRATION_RESTAURANT", display_name="饭店",
            center_anchor_code="MIGRATION_RESTAURANT", location_type="STORE")
        clock=GlobalGameTimeState.get_instance(); clock.world_minute=1000; clock.save()
        self.day = 0

    def tearDown(self):
        with connection.schema_editor() as editor:
            editor.delete_model(self.Claim)
            editor.delete_model(self.Contract)
        super().tearDown()

    def contract(self, *, quantity=12, price=5, status="LEGACY_CLOSED", expiry=2000, item=1030):
        self.day += 1
        return self.Contract.objects.create(npc_id=self.npc.pk,day_index=self.day,item_id=item,total_quantity=quantity,
            unit_price=price,min_quality=4,min_remaining_minutes=90,delivery_location_code="MIGRATION_RESTAURANT",
            status=status,expires_world_minute=expiry)

    def claim(self, contract, *, quantity=5, delivered=0, reserved=25, status="ACTIVE", expiry=1800):
        return self.Claim.objects.create(contract=contract,player_id=self.player.pk,quantity=quantity,
            delivered_quantity=delivered,reserved_gold=reserved,status=status,expires_world_minute=expiry)

    def migrate(self):
        self.migration.migrate_legacy_supply(self.old_apps,SimpleNamespace(connection=connection))

    def migrated(self, kind, source):
        return VillagePersonalCommission.objects.get(pk=self.migration.migration_id(kind,source.pk))

    def money(self):
        self.wallet.refresh_from_db(); self.bag.refresh_from_db()
        return (self.wallet.money+self.bag.money+sum(self.Claim.objects.values_list("reserved_gold",flat=True))
                +sum(VillagePersonalCommission.objects.values_list("reserved_reward_gold",flat=True))
                +sum(VillagePersonalCommission.objects.values_list("escrow_gold",flat=True)))

    def test_actual_four_offers_two_claims_become_five_commissions_without_duplicate_demand(self):
        yesterday_corn=self.contract(status="EXPIRED",expiry=900)
        yesterday_tomato=self.contract(quantity=3,price=6,status="EXPIRED",expiry=900,item=1023)
        corn=self.contract()
        tomato=self.contract(quantity=3,price=6,item=1023)
        cancelled=self.claim(corn,quantity=6,reserved=0,status="CANCELLED")
        completed=self.claim(tomato,quantity=3,delivered=3,reserved=0,status="COMPLETED")
        before=self.money(); self.migrate()
        self.assertEqual(VillagePersonalCommission.objects.count(),5)
        self.assertEqual(VillagePersonalCommissionReceipt.objects.count(),6)
        self.assertEqual(self.migrated("contract",yesterday_corn).status,"EXPIRED")
        self.assertEqual(self.migrated("contract",yesterday_tomato).status,"EXPIRED")
        self.assertEqual(self.migrated("claim",cancelled).status,"ABANDONED")
        self.assertEqual(self.migrated("claim",completed).status,"COMPLETED")
        opened=self.migrated("contract",corn)
        self.assertEqual((opened.status,opened.reserved_reward_gold,opened.requirements["stages"][0]["conditions"][0]["target"]),("OPEN",60,12))
        self.assertFalse(VillagePersonalCommission.objects.filter(pk=self.migration.migration_id("contract",tomato.pk)).exists())
        self.wallet.refresh_from_db(); self.assertEqual(self.wallet.money,290)
        self.assertEqual(self.money(),before)

    def test_partial_live_claim_continues_original_progress_and_only_unpaid_reward(self):
        contract=self.contract(quantity=5)
        claim=self.claim(contract,delivered=2,reserved=15)
        # These assets were already settled before the upgrade.
        self.bag.money=110; self.bag.save()
        NpcGlobalInventorySlot.objects.create(bag=self.wallet,slot_index=0,item_id=1030,item_amount=2,quality=4)
        before=self.money(); self.migrate()
        row=self.migrated("claim",claim)
        condition=row.requirements["stages"][0]["conditions"][0]
        self.assertEqual((row.status,row.actor_type,row.actor_id),("ACTIVE","PLAYER",str(self.player.pk)))
        self.assertEqual((condition["target"],condition["min_quality"],condition["min_remaining_minutes"]),(5,4,90))
        self.assertEqual(condition["locations"],["MIGRATION_RESTAURANT"])
        self.assertTrue(condition["requires_freshness"])
        self.assertEqual(row.progress,{"conditions":{"legacy_item":2}})
        self.assertEqual((row.reward_gold,row.reserved_reward_gold,row.expires_world_minute),(15,15,1800))
        self.assertFalse(VillagePersonalCommissionEscrow.objects.exists())
        self.assertEqual(self.money(),before)
        self.assertEqual(self.wallet.slots.get().item_amount,2)
        InventorySlot.objects.create(bag=self.bag,slot_index=0,item_id=1030,item_amount=3,quality=4,
            produced_world_minute=900,expires_world_minute=2000)
        result=contribute_commission(row.pk,"PLAYER",str(self.player.pk),"legacy_item",3,uuid.uuid4(),1000,
            item_id=1030,location="MIGRATION_RESTAURANT")
        self.assertEqual((result["status"],result["earned_gold"]),("COMPLETED",15))
        self.bag.refresh_from_db(); self.assertEqual(self.bag.money,125)
        self.assertEqual(sum(self.wallet.slots.values_list("item_amount",flat=True)),5)

    def test_abandon_after_partial_migration_only_refunds_new_escrow_not_old_deliveries(self):
        contract=self.contract(quantity=5); claim=self.claim(contract,delivered=2,reserved=15)
        NpcGlobalInventorySlot.objects.create(bag=self.wallet,slot_index=0,item_id=1030,item_amount=2,quality=4)
        self.migrate(); row=self.migrated("claim",claim)
        InventorySlot.objects.create(bag=self.bag,slot_index=0,item_id=1030,item_amount=1,quality=4,
            produced_world_minute=900,expires_world_minute=2000)
        contribute_commission(row.pk,"PLAYER",str(self.player.pk),"legacy_item",1,uuid.uuid4(),1000,
            item_id=1030,location="MIGRATION_RESTAURANT")
        abandon_commission(row.pk,"PLAYER",str(self.player.pk),uuid.uuid4(),1000)
        self.assertEqual(sum(self.bag.slots.values_list("item_amount",flat=True)),1)
        self.assertEqual(self.wallet.slots.get().item_amount,2)
        self.wallet.refresh_from_db(); self.assertEqual(self.wallet.money,365)

    def test_overdue_active_and_closed_claims_refund_remaining_reserve_once(self):
        for status in ("ACTIVE","EXPIRED","CANCELLED","COMPLETED"):
            contract=self.contract(quantity=5,status="EXPIRED",expiry=900)
            self.claim(contract,delivered=5 if status=="COMPLETED" else 2,reserved=0 if status=="COMPLETED" else 15,
                       status=status,expiry=900)
        before=self.money(); self.migrate()
        self.assertEqual(self.money(),before)
        self.wallet.refresh_from_db(); self.assertEqual(self.wallet.money,395)
        self.assertEqual(VillagePersonalCommission.objects.filter(status="ACTIVE").count(),0)
        self.assertEqual(sum(VillagePersonalCommission.objects.values_list("reserved_reward_gold",flat=True)),0)
        count=VillagePersonalCommission.objects.count(); self.migrate()
        self.assertEqual(self.money(),before); self.assertEqual(VillagePersonalCommission.objects.count(),count)

    def test_unallocated_quantity_excludes_deliveries_and_live_promises(self):
        contract=self.contract(quantity=12)
        self.claim(contract,quantity=5,delivered=2,reserved=15)
        self.claim(contract,quantity=3,delivered=1,reserved=0,status="CANCELLED")
        before=self.money(); self.migrate()
        opened=self.migrated("contract",contract)
        self.assertEqual(opened.requirements["stages"][0]["conditions"][0]["target"],6)
        self.assertEqual(opened.reserved_reward_gold,30)
        self.assertEqual(self.money(),before)

    def test_insufficient_budget_closes_unguaranteed_offer_without_creating_money(self):
        self.wallet.money=3; self.wallet.save()
        contract=self.contract(quantity=5)
        before=self.money(); self.migrate()
        row=self.migrated("contract",contract)
        self.assertEqual((row.status,row.reserved_reward_gold),("WITHDRAWN",0))
        self.assertIn("金币不足",row.description)
        self.assertEqual(self.money(),before)

    def test_replay_keeps_cash_receipts_and_later_personal_progress_unchanged(self):
        contract=self.contract(quantity=5); claim=self.claim(contract,delivered=2,reserved=15)
        self.migrate(); row=self.migrated("claim",claim)
        row.progress={"conditions":{"legacy_item":3}}; row.save()
        before=self.money(); receipts=VillagePersonalCommissionReceipt.objects.count()
        self.migrate(); row.refresh_from_db()
        self.assertEqual(row.progress,{"conditions":{"legacy_item":3}})
        self.assertEqual(VillagePersonalCommissionReceipt.objects.count(),receipts)
        self.assertEqual(self.money(),before)

    def test_old_player_payment_history_is_linked_but_not_rewritten(self):
        contract=self.contract(quantity=3); claim=self.claim(contract,quantity=3,delivered=3,reserved=0,status="COMPLETED")
        rid=uuid.uuid4()
        response={"data":{"result":{"claim_id":str(claim.pk),"earned_gold":15}}}
        action=VillagePlayerActionReceipt.objects.create(player=self.player,request_id=rid,payload_hash="a"*64,
            action="deliver",response=response,world_minute=900)
        contribution=VillagePlayerContribution.objects.create(player=self.player,request_id=rid,kind="delivery",
            item_id=1030,quantity=3,earned_gold=15,world_minute=900)
        self.migrate()
        audit=VillagePersonalCommissionReceipt.objects.get(request_id=self.migration.migration_id("audit_claim",claim.pk))
        self.assertEqual(audit.response["legacy_action_receipt_ids"],[action.pk])
        self.assertEqual(audit.response["legacy_contribution_ids"],[contribution.pk])
        self.assertEqual(audit.response["paid_gold_before_migration"],15)
        action.refresh_from_db(); contribution.refresh_from_db()
        self.assertEqual(action.response,response); self.assertEqual(contribution.earned_gold,15)

    def test_previous_upgrade_permission_append_still_preserves_existing_capabilities(self):
        contract=self.contract(status="OPEN")
        claim=self.claim(contract)
        NpcProfile.objects.create(npc=self.npc,display_name="Lucy",occupation="厨师",home_location_code="MIGRATION_RESTAURANT",
            work_location_code="MIGRATION_RESTAURANT",social_location_code="MIGRATION_RESTAURANT",capabilities=["EAT"])
        previous=importlib.import_module("village.migrations.0051_personal_commissions")
        previous.retire_legacy_and_enable_commissions(self.old_apps,None)
        previous.retire_legacy_and_enable_commissions(self.old_apps,None)
        contract.refresh_from_db(); claim.refresh_from_db()
        self.assertEqual((contract.status,claim.reserved_gold),("LEGACY_CLOSED",25))
        capabilities=NpcProfile.objects.get(npc=self.npc).capabilities
        self.assertEqual(capabilities[0],"EAT"); self.assertEqual(len(capabilities),7)
