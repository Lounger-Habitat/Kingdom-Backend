import copy
import threading
import uuid

from django.db import connection, connections
from django.test import TestCase, TransactionTestCase

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.models import InventoryBag, InventorySlot, NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot
from items.models import Item
from village.models import (NpcProfile, VillageLocationDefinition, VillagePersonalCommission,
    VillagePersonalCommissionEscrow, VillagePersonalCommissionReceipt,
    VillagePlayerAffairLabor, VillageWorldEvent)
from village.personal_commissions import (CommissionError, publish_commission, accept_commission,
    contribute_commission, complete_commission_labor, abandon_commission, withdraw_commission,
    expire_commissions, project_commission, list_commissions, validate_commission_condition)


COMMANDS = ["PUBLISH_PERSONAL_COMMISSION", "INSPECT_PERSONAL_COMMISSIONS", "ACCEPT_PERSONAL_COMMISSION",
            "ACT_PERSONAL_COMMISSION", "WITHDRAW_PERSONAL_COMMISSION", "ABANDON_PERSONAL_COMMISSION"]


class CommissionFixture:
    def setUp(self):
        super().setUp()
        self.clock = GlobalGameTimeState.get_instance()
        self.clock.world_minute = 100
        self.clock.save()
        for code in ("COMMISSION_HOME", "COMMISSION_YARD"):
            VillageLocationDefinition.objects.get_or_create(location_code=code, defaults=dict(
                display_name=code, center_anchor_code=code, location_type="PUBLIC"))
        self.npc = NpcDefinition.objects.create(agent_id="commission-publisher", name="发布者")
        self.worker = NpcDefinition.objects.create(agent_id="commission-worker", name="承接者")
        for npc in (self.npc, self.worker):
            NpcProfile.objects.create(npc=npc, display_name=npc.name, occupation="Test",
                home_location_code="COMMISSION_HOME", work_location_code="COMMISSION_HOME",
                social_location_code="COMMISSION_HOME", capabilities=COMMANDS)
        self.publisher_bag = NpcGlobalInventory.objects.create(npc=self.npc, money=1000)
        self.worker_bag = NpcGlobalInventory.objects.create(npc=self.worker, money=100)
        self.player = GameAccount.objects.create(username="commission-player")
        self.other = GameAccount.objects.create(username="commission-other")
        self.player_bag = InventoryBag.objects.create(player=self.player, character_id=self.player.username, money=100)
        self.other_bag = InventoryBag.objects.create(player=self.other, character_id=self.other.username, money=100)
        Item.objects.update_or_create(pk=1002, defaults=dict(item_name="木材", item_price=5, is_deleted=False))
        Item.objects.update_or_create(pk=1030, defaults=dict(item_name="玉米", item_price=5, is_deleted=False))

    def params(self, **overrides):
        return dict(title="准备物品", description="按阶段交付", reward_gold=80, valid_for_minutes=1000,
            stages=[dict(id="prepare", title="材料", conditions=[dict(id="wood", type="item", item_ids=[1002], target=2)]),
                    dict(id="work", title="劳动", location_code="COMMISSION_YARD", conditions=[dict(id="labor", type="labor", target=1, unit_minutes=15)])],
            **overrides)

    def publish(self, params=None, rid=None):
        data = publish_commission(self.npc, params or self.params(), rid or uuid.uuid4(), 100)
        return VillagePersonalCommission.objects.get(pk=data["commission_id"])

    def accept(self, row, actor=None, kind="PLAYER", rid=None):
        return accept_commission(row.pk, kind, str((actor or self.player).pk), rid or uuid.uuid4(), 100)

    def give(self, row, *, condition="wood", amount=2, item_id=1002, location="COMMISSION_HOME", actor=None,
             kind="PLAYER", rid=None, instance_id=None, minute=100):
        return contribute_commission(row.pk, kind, str((actor or self.player).pk), condition, amount,
            rid or uuid.uuid4(), minute, item_id=item_id, instance_id=instance_id, location=location)

    def stock(self, amount=5, bag=None, **attributes):
        bag = bag or self.player_bag
        slot_model = NpcGlobalInventorySlot if isinstance(bag, NpcGlobalInventory) else InventorySlot
        return slot_model.objects.create(bag=bag, slot_index=bag.slots.count(), item_id=1002, item_amount=amount,
            **dict(quality=4, produced_world_minute=90, expires_world_minute=500, source_request_id=uuid.uuid4(),
                   ingredient="原始材料", dish_metadata={"origin": "wild"}, remaining_uses=7, **attributes))

    def assert_error(self, code, call):
        with self.assertRaises(CommissionError) as error:
            call()
        self.assertEqual(error.exception.code, code)


class PersonalCommissionTests(CommissionFixture, TestCase):
    def test_publication_escrows_reward_and_replay_never_changes_definition(self):
        request_id = uuid.uuid4()
        params = self.params()
        # IDs are optional, generated only once by successful publication.
        for stage in params["stages"]:
            del stage["id"]
            for condition in stage["conditions"]:
                del condition["id"]
        first = publish_commission(self.npc, params, request_id, 100)
        self.assertEqual(first, publish_commission(self.npc, copy.deepcopy(params), request_id, 200))
        self.publisher_bag.refresh_from_db()
        self.assertEqual(self.publisher_bag.money, 920)
        self.assertEqual(VillagePersonalCommission.objects.count(), 1)
        self.assertEqual(len(first["stages"][0]["conditions"][0]["id"]), 42)
        changed = dict(params, reward_gold=90)
        self.assert_error("REQUEST_CONFLICT", lambda: publish_commission(self.npc, changed, request_id, 100))

    def test_sequential_completion_transfers_real_batches_and_reward_once(self):
        row = self.publish()
        self.accept(row)
        source = self.stock()
        self.assert_error("AFFAIR_PREPARATION_INCOMPLETE", lambda: complete_commission_labor(
            row.pk, "PLAYER", str(self.player.pk), "labor", uuid.uuid4(), 100, location="COMMISSION_YARD"))
        self.give(row, instance_id=str(source.instance_id))
        source.refresh_from_db(); row.refresh_from_db()
        self.assertEqual(source.item_amount, 3)
        self.assertEqual(row.status, "ACTIVE")
        self.assertEqual(self.publisher_bag.slots.count(), 0)
        self.assertEqual(row.escrow_batches.get().status, "HELD")
        self.player_bag.refresh_from_db(); self.assertEqual(self.player_bag.money, 100)
        rid = uuid.uuid4()
        result = complete_commission_labor(row.pk, "PLAYER", str(self.player.pk), "labor", rid, 115, location="COMMISSION_YARD")
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["earned_gold"], 80)
        self.assertEqual(result, complete_commission_labor(row.pk, "PLAYER", str(self.player.pk), "labor", rid, 116, location="COMMISSION_YARD"))
        self.player_bag.refresh_from_db(); self.assertEqual(self.player_bag.money, 180)
        delivered = self.publisher_bag.slots.get()
        self.assertEqual((delivered.item_amount, delivered.quality, delivered.expires_world_minute), (2, 4, 500))
        self.assertEqual((delivered.source_request_id, delivered.dish_metadata, delivered.remaining_uses),
                         (source.source_request_id, {"origin": "wild"}, 7))
        self.assertEqual(VillageWorldEvent.objects.filter(event_type="PERSONAL_COMMISSION_COMPLETED").count(), 1)
        row.refresh_from_db(); self.assertEqual(row.reserved_reward_gold, 0)

    def test_stage_location_override_and_default_inheritance(self):
        row = self.publish(); self.accept(row); self.stock()
        self.assert_error("COMMISSION_LOCATION_INVALID", lambda: self.give(row, location="COMMISSION_YARD"))
        self.give(row)
        self.assert_error("COMMISSION_LOCATION_INVALID", lambda: validate_commission_condition(
            row.pk, "PLAYER", str(self.player.pk), "labor", 100, location="COMMISSION_HOME"))
        data = project_commission(VillagePersonalCommission.objects.get(pk=row.pk))
        self.assertEqual([s["locations"] for s in data["stages"]], [["COMMISSION_HOME"], ["COMMISSION_YARD"]])

    def test_only_assignee_can_progress_and_publisher_cannot_withdraw_accepted(self):
        row = self.publish(); self.accept(row); self.stock(bag=self.other_bag)
        self.assert_error("COMMISSION_NOT_ASSIGNEE", lambda: self.give(row, actor=self.other))
        self.assert_error("COMMISSION_ALREADY_ACCEPTED", lambda: self.accept(row, actor=self.other))
        self.assert_error("COMMISSION_ALREADY_ACCEPTED", lambda: withdraw_commission(
            row.pk, "NPC", str(self.npc.pk), uuid.uuid4(), 100))
        self.assert_error("COMMISSION_NOT_ASSIGNEE", lambda: abandon_commission(
            row.pk, "PLAYER", str(self.other.pk), uuid.uuid4(), 100))

    def test_npc_cannot_accept_own_commission_or_publish_without_capability(self):
        row = self.publish()
        self.assert_error("COMMISSION_PLAYER_ONLY", lambda: self.accept(row, actor=self.npc, kind="NPC"))
        NpcProfile.objects.filter(npc=self.npc).update(capabilities=[])
        self.assert_error("COMMISSION_FORBIDDEN", lambda: publish_commission(self.npc, self.params(), uuid.uuid4(), 100))

    def test_gold_delivery_is_held_and_refunded_with_items_on_abandon(self):
        params = self.params()
        params["stages"][0]["conditions"].append(dict(id="gold", type="gold", target=20))
        row = self.publish(params); self.accept(row)
        source = self.stock()
        self.give(row)
        self.give(row, condition="gold", amount=20, item_id=None)
        self.player_bag.refresh_from_db(); self.assertEqual(self.player_bag.money, 80)
        request_id = uuid.uuid4()
        result = abandon_commission(row.pk, "PLAYER", str(self.player.pk), request_id, 120)
        self.assertEqual(result, abandon_commission(row.pk, "PLAYER", str(self.player.pk), request_id, 121))
        self.assertEqual(result["status"], "ABANDONED")
        source.refresh_from_db(); self.assertEqual(source.item_amount, 5)
        self.assertEqual(source.expires_world_minute, 500)
        self.player_bag.refresh_from_db(); self.publisher_bag.refresh_from_db()
        self.assertEqual((self.player_bag.money, self.publisher_bag.money), (100, 1000))
        self.assertEqual(row.escrow_batches.get().status, "REFUNDED")

    def test_refund_does_not_rejuvenate_expired_food_or_overwrite_reused_slot(self):
        row = self.publish(); self.accept(row); source = self.stock(amount=2)
        self.give(row)
        # The original empty slot may be reused before a refund.
        source.refresh_from_db(); source.item_id = 1030; source.item_amount = 9; source.save()
        expire_commissions(1100)
        row.refresh_from_db(); self.assertEqual(row.status, "EXPIRED")
        refunded = self.player_bag.slots.get(item_id=1002)
        self.assertEqual((refunded.item_amount, refunded.expires_world_minute, refunded.quality), (2, 500, 4))
        self.assertNotEqual(refunded.instance_id, source.instance_id)
        source.refresh_from_db(); self.assertEqual((source.item_id, source.item_amount), (1030, 9))
        self.assertEqual(expire_commissions(1200), [])

    def test_refund_changed_same_item_batch_gets_new_identity_and_original_attributes(self):
        row=self.publish(); self.accept(row); source=self.stock(amount=2)
        original_source=source.source_request_id
        self.give(row)
        source.refresh_from_db(); source.quality=1; source.dish_metadata={"changed":True}; source.save()
        abandon_commission(row.pk,"PLAYER",str(self.player.pk),uuid.uuid4(),120)
        returned=self.player_bag.slots.get(item_amount=2)
        self.assertNotEqual(returned.instance_id,source.instance_id)
        self.assertEqual((returned.quality,returned.source_request_id,returned.dish_metadata),
                         (4,original_source,{"origin":"wild"}))

    def test_remaining_freshness_requires_real_expiry_even_without_boolean_flag(self):
        params=self.params(); params["stages"][0]["conditions"][0]["min_remaining_minutes"]=30
        row=self.publish(params); self.accept(row); source=self.stock()
        source.expires_world_minute=None; source.save()
        self.assert_error("INSUFFICIENT_QUALIFIED_ITEMS",lambda:self.give(row))
        source.refresh_from_db(); self.assertEqual(source.item_amount,5)

    def test_ai_player_identity_cannot_impersonate_a_human_assignee(self):
        row=self.publish()
        self.player.is_ai=True; self.player.save()
        self.assert_error("COMMISSION_FORBIDDEN",lambda:self.accept(row))

    def test_unaccepted_withdraw_refunds_and_other_npc_cannot_withdraw(self):
        row = self.publish()
        self.assert_error("COMMISSION_NOT_PUBLISHER", lambda: withdraw_commission(row.pk, "NPC", str(self.worker.pk), uuid.uuid4(), 100))
        withdraw_commission(row.pk, "NPC", str(self.npc.pk), uuid.uuid4(), 100)
        self.publisher_bag.refresh_from_db(); self.assertEqual(self.publisher_bag.money, 1000)
        self.assert_error("COMMISSION_CLOSED", lambda: self.accept(row))

    def test_insufficient_reward_and_invalid_definition_never_deduct(self):
        params = self.params(); params["reward_gold"] = 1001
        self.assert_error("INSUFFICIENT_GOLD", lambda: self.publish(params))
        params = self.params(); params["stages"][0]["conditions"][0]["item_ids"] = [99999999]
        self.assert_error("INVALID_AFFAIR_DEFINITION", lambda: self.publish(params))
        self.publisher_bag.refresh_from_db(); self.assertEqual(self.publisher_bag.money, 1000)
        self.assertEqual(VillagePersonalCommission.objects.count(), 0)

    def test_quality_freshness_unknown_and_overdelivery_are_atomic(self):
        params = self.params()
        params["stages"][0]["conditions"][0].update(min_quality=5, requires_freshness=True, min_remaining_minutes=60)
        row = self.publish(params); self.accept(row)
        source = self.stock()
        for values in (dict(quality=4), dict(quality=5, expires_world_minute=150),
                       dict(quality=5, expires_world_minute=None), dict(expires_world_minute=200, freshness_unknown=True)):
            for name, value in values.items(): setattr(source, name, value)
            source.save()
            self.assert_error("INSUFFICIENT_QUALIFIED_ITEMS", lambda: self.give(row))
            source.refresh_from_db(); self.assertEqual(source.item_amount, 5)
        source.quality=5; source.expires_world_minute=200; source.freshness_unknown=False; source.save()
        self.assert_error("PROJECT_NEED_FILLED", lambda: self.give(row, amount=3))
        self.give(row)
        self.assertEqual(row.escrow_batches.get().quantity, 2)

    def test_same_item_two_stages_and_replay_never_credit_next_stage(self):
        params = self.params()
        params["stages"][1] = dict(id="second", conditions=[dict(id="more_wood", type="item", item_ids=[1002], target=2)])
        row = self.publish(params); self.accept(row); source=self.stock()
        rid = uuid.uuid4()
        first = self.give(row, rid=rid)
        self.assertEqual(first, self.give(row, rid=rid))
        row.refresh_from_db(); self.assertEqual(row.progress, {"conditions": {"wood": 2}})
        self.assert_error("REQUEST_CONFLICT", lambda: self.give(row, condition="more_wood", rid=rid))
        self.give(row, condition="more_wood")
        row.refresh_from_db(); self.assertEqual(row.status, "COMPLETED")
        source.refresh_from_db(); self.assertEqual(source.item_amount, 1)

    def test_item_only_and_gold_only_commissions_need_no_labor(self):
        for condition in (dict(id="wood", type="item", item_ids=[1002], target=2), dict(id="fund", type="gold", target=20)):
            params = self.params(); params["stages"] = [dict(id="only", conditions=[condition])]
            row = self.publish(params); self.accept(row)
            if condition["type"] == "item": self.stock()
            result = self.give(row, condition=condition["id"], amount=condition["target"], item_id=1002 if condition["type"] == "item" else None)
            self.assertEqual((result["status"], result["earned_gold"]), ("COMPLETED", 80))

    def test_npc_assignee_operations_are_denied_even_with_old_capabilities_or_receipts(self):
        row = self.publish()
        source = self.stock(bag=self.worker_bag)
        operations = [
            lambda rid: accept_commission(row.pk,"NPC",str(self.worker.pk),rid,100),
            lambda rid: self.give(row,actor=self.worker,kind="NPC",rid=rid),
            lambda rid: complete_commission_labor(row.pk,"NPC",str(self.worker.pk),"labor",rid,115,location="COMMISSION_YARD"),
            lambda rid: abandon_commission(row.pk,"NPC",str(self.worker.pk),rid,100),
        ]
        for action, operation in zip(["ACCEPT","CONTRIBUTE","LABOR","ABANDON"],operations):
            self.assert_error("COMMISSION_PLAYER_ONLY",lambda:operation(uuid.uuid4()))
            rid=uuid.uuid4()
            VillagePersonalCommissionReceipt.objects.create(commission=row,actor_type="NPC",actor_id=str(self.worker.pk),
                request_id=rid,action=action,payload_hash="0"*64,response={"status":"COMPLETED","earned_gold":80},world_minute=90)
            self.assert_error("COMMISSION_PLAYER_ONLY",lambda:operation(rid))
        self.assert_error("COMMISSION_PLAYER_ONLY",lambda:validate_commission_condition(
            row.pk,"NPC",str(self.worker.pk),"labor",100,location="COMMISSION_YARD"))
        source.refresh_from_db(); self.worker_bag.refresh_from_db(); row.refresh_from_db()
        self.assertEqual((source.item_amount,self.worker_bag.money,row.status),(5,100,"OPEN"))

    def test_only_player_projection_offers_accept_or_assignee_actions(self):
        row=self.publish()
        for viewer in [(None,None),("NPC",self.npc.pk),("NPC",self.worker.pk)]:
            data=project_commission(row,actor_type=viewer[0],actor_id=viewer[1])
            self.assertFalse(data["can_accept"]); self.assertFalse(data["can_act"]); self.assertFalse(data["can_abandon"])
        self.assertTrue(project_commission(row,actor_type="PLAYER",actor_id=self.player.pk)["can_accept"])
        self.assertTrue(project_commission(row,actor_type="NPC",actor_id=self.npc.pk)["can_withdraw"])
        self.accept(row); row.refresh_from_db()
        player=project_commission(row,actor_type="PLAYER",actor_id=self.player.pk)
        self.assertTrue(player["can_act"]); self.assertTrue(player["can_abandon"])

    def test_projections_do_not_change_viewer_independent_available_flags(self):
        row=self.publish(); self.accept(row); row.refresh_from_db()
        a=project_commission(row, actor_type="PLAYER", actor_id=self.player.pk)
        b=project_commission(row, actor_type="PLAYER", actor_id=self.other.pk)
        self.assertEqual(a["conditions"], b["conditions"])
        self.assertTrue(a["can_act"]); self.assertFalse(b["can_act"])
        self.assertNotIn("escrow_gold", b)
        self.assertEqual(list_commissions(100, "PLAYER", self.other.pk), [])
        self.assertEqual(len(list_commissions(100, "NPC", self.npc.pk)), 1)

    def test_busy_public_board_does_not_hide_older_accepted_work(self):
        row=self.publish(); self.accept(row)
        VillagePersonalCommission.objects.bulk_create([
            VillagePersonalCommission(publisher=self.npc,title="新委托",requirements=row.requirements,
                locations=row.locations,progress={"conditions":{}},reward_gold=1,reserved_reward_gold=1,
                opened_world_minute=200,expires_world_minute=2000,created_request_id=uuid.uuid4())
            for _ in range(101)])
        visible=list_commissions(200,"PLAYER",self.player.pk)
        self.assertIn(str(row.pk),[entry["commission_id"] for entry in visible])

class PostgreSQLPersonalCommissions(CommissionFixture, TransactionTestCase):
    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("requires isolated PostgreSQL settings")
        super().setUp()

    def race(self, *callbacks):
        barrier=threading.Barrier(len(callbacks)); answers=[]; unexpected=[]
        def run(callback):
            connections.close_all()
            try:
                barrier.wait(timeout=10)
                try: answers.append(callback())
                except CommissionError as exc: answers.append({"error":exc.code})
            except BaseException as exc: unexpected.append(exc)
            finally: connections.close_all()
        threads=[threading.Thread(target=run,args=(call,),daemon=True) for call in callbacks]
        for thread in threads: thread.start()
        for thread in threads:
            thread.join(timeout=20); self.assertFalse(thread.is_alive(), "commission contender deadlock")
        self.assertEqual(unexpected, [])
        return answers

    def test_exclusive_accept_has_one_winner(self):
        row=self.publish()
        results=self.race(lambda:self.accept(row),lambda:self.accept(row,actor=self.other))
        self.assertEqual(sum(r.get("status")=="ACTIVE" for r in results),1)
        self.assertEqual(sum(r.get("error")=="COMMISSION_ALREADY_ACCEPTED" for r in results),1)

    def test_simultaneous_last_delivery_only_charges_and_pays_once(self):
        params=self.params(); params["stages"]=params["stages"][:1]
        row=self.publish(params); self.accept(row); source=self.stock()
        results=self.race(lambda:self.give(row),lambda:self.give(row))
        self.assertEqual(sum(r.get("status")=="COMPLETED" for r in results),1)
        source.refresh_from_db(); self.player_bag.refresh_from_db()
        self.assertEqual((source.item_amount,self.player_bag.money),(3,180))
        self.assertEqual(row.escrow_batches.count(),1)

    def test_concurrent_same_publish_receipt_only_reserves_once(self):
        rid=uuid.uuid4(); params=self.params()
        results=self.race(lambda:publish_commission(self.npc,params,rid,100),lambda:publish_commission(self.npc,params,rid,100))
        self.assertEqual(results[0],results[1])
        self.publisher_bag.refresh_from_db(); self.assertEqual(self.publisher_bag.money,920)
