import importlib
from types import SimpleNamespace
import uuid

from django.db import connection, IntegrityError, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from inventory.components import BATCH_COPY_FIELDS
from village.models import (NpcProfile, NpcCommandState, NpcRuntimeState, NpcActionReservation,
    VillagePersonalCommission, VillagePersonalCommissionEscrow, VillagePersonalCommissionReceipt,
    VillageActionReceipt, VillageDeliveryOutbox)
from village.tests.test_personal_commissions import CommissionFixture


class PlayerOnlyCommissionMigrationTests(CommissionFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.constraint=next(c for c in VillagePersonalCommission._meta.constraints if c.name=="commission_active_player_only")
        self.old_apps=MigrationExecutor(connection).loader.project_state([("village","0052_migrate_legacy_supply")]).apps
        self.old_commission=self.old_apps.get_model("village","VillagePersonalCommission")
        # Reproduce pre-0053 rows only inside the isolated test database.
        with connection.schema_editor() as editor:
            editor.remove_constraint(self.old_commission,self.constraint)
        self.migration=importlib.import_module("village.migrations.0053_player_only_personal_commissions")

    def tearDown(self):
        VillagePersonalCommission.objects.filter(status="ACTIVE",actor_type="NPC").update(status="ABANDONED")
        with connection.schema_editor() as editor:
            editor.add_constraint(VillagePersonalCommission,self.constraint)
        super().tearDown()

    def migrate(self):
        self.migration.retire_npc_acceptance(self.old_apps,SimpleNamespace(connection=connection))

    def legacy_active(self):
        row=self.publish()
        row.status="ACTIVE";row.actor_type="NPC";row.actor_id=str(self.worker.pk)
        row.escrow_gold=20;row.progress={"conditions":{"wood":2}};row.save()
        self.worker_bag.money=80;self.worker_bag.save()
        source=self.stock(amount=3,bag=self.worker_bag)
        snapshot={name:getattr(source,name) for name in BATCH_COPY_FIELDS}
        snapshot["source_request_id"]=str(snapshot["source_request_id"])
        source.item_amount=1;source.save()
        VillagePersonalCommissionEscrow.objects.create(commission=row,condition_id="wood",item_id=1002,
            quantity=2,source_instance_id=source.instance_id,batch_snapshot=snapshot)
        return row,source

    def test_npc_active_escrow_is_refunded_once_with_batch_metadata_preserved(self):
        row,source=self.legacy_active()
        self.migrate();self.migrate()
        row.refresh_from_db();self.publisher_bag.refresh_from_db();self.worker_bag.refresh_from_db()
        self.assertEqual((row.status,row.reserved_reward_gold,row.escrow_gold),("ABANDONED",0,0))
        self.assertEqual((self.publisher_bag.money,self.worker_bag.money),(1000,100))
        self.assertEqual(sum(self.worker_bag.slots.values_list("item_amount",flat=True)),3)
        returned=self.worker_bag.slots.get(item_amount=2)
        self.assertNotEqual(returned.instance_id,source.instance_id)
        for name in BATCH_COPY_FIELDS:
            self.assertEqual(getattr(returned,name),getattr(source,name),name)
        self.assertEqual(VillagePersonalCommissionEscrow.objects.get().status,"REFUNDED")
        self.assertEqual(VillagePersonalCommissionReceipt.objects.filter(action="MIGRATE").count(),1)

    def test_completed_npc_history_and_player_active_work_are_untouched(self):
        historic=self.publish();historic.status="COMPLETED";historic.actor_type="NPC";historic.actor_id=str(self.worker.pk)
        historic.reserved_reward_gold=0;historic.completed_world_minute=90;historic.save()
        player=self.publish();self.accept(player);player.refresh_from_db()
        expected=(historic.status,historic.version,historic.reward_gold,historic.completed_world_minute)
        self.migrate();historic.refresh_from_db();player.refresh_from_db()
        self.assertEqual((historic.status,historic.version,historic.reward_gold,historic.completed_world_minute),expected)
        self.assertEqual((player.status,player.actor_type,player.reserved_reward_gold),("ACTIVE","PLAYER",80))

    def test_all_profiles_lose_only_three_retired_capabilities(self):
        profile=NpcProfile.objects.get(npc=self.worker)
        profile.is_active=False
        profile.capabilities=["EAT","ACCEPT_PERSONAL_COMMISSION","PUBLISH_PERSONAL_COMMISSION",
                              "ACT_PERSONAL_COMMISSION","INSPECT_PERSONAL_COMMISSIONS",
                              "ABANDON_PERSONAL_COMMISSION","WITHDRAW_PERSONAL_COMMISSION"]
        profile.save()
        self.migrate();profile.refresh_from_db()
        self.assertEqual(profile.capabilities,["EAT","PUBLISH_PERSONAL_COMMISSION","INSPECT_PERSONAL_COMMISSIONS","WITHDRAW_PERSONAL_COMMISSION"])

    def test_retired_running_command_cancelled_with_receipt_and_public_work_untouched(self):
        retired=NpcCommandState.objects.create(npc=self.worker,command_id=uuid.uuid4(),message_id=uuid.uuid4(),
            request_id=uuid.uuid4(),sender_id="hub",recipient_id=self.worker.agent_id,
            kind="ACT_PERSONAL_COMMISSION",status="RUNNING",payload_sha256="0"*64)
        runtime=NpcRuntimeState.objects.create(npc=self.worker,active_command_id=retired.pk,current_action=retired.kind,
            current_goal="WORK",action_status="ACTING",action_started_world_minute=90,action_ends_world_minute=115,state_version=7)
        reservation=NpcActionReservation.objects.create(action_kind=retired.kind,command_id=retired.pk,
            acquired_world_minute=90,expires_world_minute=115,active_lock_key="old-personal-labor")
        public=NpcCommandState.objects.create(npc=self.npc,command_id=uuid.uuid4(),message_id=uuid.uuid4(),
            request_id=uuid.uuid4(),sender_id="hub",recipient_id=self.npc.agent_id,
            kind="STORY_ACT",status="RUNNING",payload_sha256="1"*64)
        public_runtime=NpcRuntimeState.objects.create(npc=self.npc,active_command_id=public.pk,current_action="STORY_ACT",action_status="ACTING")
        self.migrate();self.migrate()
        retired.refresh_from_db();runtime.refresh_from_db();reservation.refresh_from_db()
        self.assertEqual((retired.status,retired.result_code),("CANCELLED","COMMISSION_PLAYER_ONLY"))
        self.assertEqual((runtime.active_command_id,runtime.current_action,runtime.action_status,runtime.state_version),(None,"","IDLE",8))
        self.assertIsNone(runtime.action_started_world_minute);self.assertIsNone(runtime.action_ends_world_minute)
        self.assertEqual((reservation.status,reservation.active_lock_key),("RELEASED",None))
        self.assertFalse(VillageActionReceipt.objects.get(request_id=retired.request_id).success)
        self.assertEqual(VillageDeliveryOutbox.objects.filter(receipt_id=retired.request_id).count(),1)
        public.refresh_from_db();public_runtime.refresh_from_db()
        self.assertEqual((public.status,public_runtime.active_command_id),("RUNNING",public.pk))

    def test_database_constraint_rejects_new_npc_active_promises(self):
        with connection.schema_editor() as editor:
            editor.add_constraint(VillagePersonalCommission,self.constraint)
        try:
            row=self.publish()
            with self.assertRaises(IntegrityError),transaction.atomic():
                VillagePersonalCommission.objects.filter(pk=row.pk).update(status="ACTIVE",actor_type="NPC",actor_id=str(self.worker.pk))
        finally:
            with connection.schema_editor() as editor:
                editor.remove_constraint(self.old_commission,self.constraint)
