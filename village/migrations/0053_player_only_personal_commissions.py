"""Retire NPC acceptance without stranding old promises or command queues."""
import hashlib
import uuid

from django.db import migrations, models, transaction


RETIRED = {"ACCEPT_PERSONAL_COMMISSION", "ACT_PERSONAL_COMMISSION", "ABANDON_PERSONAL_COMMISSION"}
CODE = "COMMISSION_PLAYER_ONLY"
MESSAGE = "个人委托仅限玩家承接；旧 NPC 承接已停止，托管资产和未发报酬已退还。"


def retire_npc_acceptance(apps, schema_editor):
    alias = schema_editor.connection.alias
    def model(name, app="village"):
        return apps.get_model(app, name)
    Profile = model("NpcProfile")
    Commission = model("VillagePersonalCommission")
    Escrow = model("VillagePersonalCommissionEscrow")
    Audit = model("VillagePersonalCommissionReceipt")
    Wallet = model("NpcGlobalInventory", "inventory")
    Slot = model("NpcGlobalInventorySlot", "inventory")
    Clock = model("GlobalGameTimeState", "game_time")
    Command = model("NpcCommandState")
    Runtime = model("NpcRuntimeState")
    Receipt = model("VillageActionReceipt")
    Event = model("VillageWorldEvent")
    Outbox = model("VillageDeliveryOutbox")

    with transaction.atomic(using=alias):
        clock = Clock.objects.using(alias).select_for_update().filter(pk=1).first()
        minute, revision = (clock.world_minute, clock.time_revision) if clock else (0, 0)
        for profile in Profile.objects.using(alias).all():
            capabilities = [value for value in (profile.capabilities or []) if value not in RETIRED]
            if capabilities != profile.capabilities:
                profile.capabilities = capabilities
                profile.save(using=alias, update_fields=["capabilities"])

        def credit(npc_id, amount):
            wallet = Wallet.objects.using(alias).select_for_update().get(npc_id=npc_id)
            wallet.money += amount
            wallet.asset_version += 1
            wallet.save(using=alias, update_fields=["money", "asset_version", "updated_at"])
            return wallet

        for row in Commission.objects.using(alias).filter(actor_type="NPC", status="ACTIVE").order_by("commission_id"):
            # Do not re-run settlement or return items already delivered on
            # completed commissions. Only HELD batches still belong to escrow.
            assignee = credit(row.actor_id, row.escrow_gold)
            for batch in Escrow.objects.using(alias).filter(commission_id=row.pk, status="HELD").order_by("pk"):
                snapshot = batch.batch_snapshot
                highest = Slot.objects.using(alias).filter(bag_id=assignee.pk).aggregate(value=models.Max("slot_index"))["value"]
                common = {field: snapshot[field] for field in ("quality", "produced_world_minute", "expires_world_minute",
                    "source_request_id", "freshness_unknown") if field in snapshot}
                slot = Slot.objects.using(alias).create(bag_id=assignee.pk, slot_index=0 if highest is None else highest+1,
                    instance_id=uuid.uuid4(), item_id=batch.item_id, item_amount=batch.quantity, **common)
                # Historical migration models have no BatchComponents Python
                # properties; restore the corresponding typed rows explicitly.
                for suffix, fields in (
                    ("Usage", {"remaining": "remaining_uses"}),
                    ("Dish", {"ingredients": "ingredient", "cook_time": "cook_time", "metadata": "dish_metadata"}),
                    ("Review", {"rated": "rated", "price": "rating_price", "score": "overall_score", "comment": "review_comment"}),
                ):
                    values = {target: snapshot[source] for target, source in fields.items() if source in snapshot}
                    if values:
                        model("NpcGlobalInventorySlot" + suffix, "inventory").objects.using(alias).create(owner_id=slot.pk, **values)
                batch.status = "REFUNDED"
                batch.save(using=alias, update_fields=["status"])
            credit(row.publisher_id, row.reserved_reward_gold)
            request_id = uuid.uuid5(uuid.NAMESPACE_URL, f"personal-commission:player-only:{row.pk}")
            Audit.objects.using(alias).get_or_create(actor_type="SYSTEM", actor_id="player_only_migration", request_id=request_id,
                defaults=dict(commission_id=row.pk, action="MIGRATE", payload_hash=hashlib.sha256(CODE.encode()).hexdigest(),
                    response=dict(reason=CODE, previous_status=row.status, returned_reward_gold=row.reserved_reward_gold,
                                  returned_escrow_gold=row.escrow_gold, message=MESSAGE), world_minute=minute))
            row.status, row.reserved_reward_gold, row.escrow_gold = "ABANDONED", 0, 0
            row.completed_world_minute = minute
            row.version += 1
            row.description = (row.description + "\n" + MESSAGE).strip()
            row.save(using=alias, update_fields=["status", "reserved_reward_gold", "escrow_gold", "completed_world_minute", "version", "description"])
            model("VillagePlayerAffairLabor").objects.using(alias).filter(commission_id=row.pk, status="ACTIVE").update(
                status="CANCELLED", accumulated_minutes=0, stop_reason=CODE)

        for command in Command.objects.using(alias).select_related("npc").filter(
                kind__in=RETIRED, status__in=["RECEIVED", "QUEUED", "RUNNING", "SETTLING"]).order_by("command_id"):
            runtime = Runtime.objects.using(alias).select_for_update().filter(npc_id=command.npc_id).first()
            if runtime and runtime.active_command_id == command.pk:
                runtime.active_command_id = None
                runtime.current_action, runtime.current_goal = "", "NONE"
                runtime.action_started_world_minute, runtime.action_ends_world_minute = None, None
                if runtime.action_status != "AWAY":
                    runtime.action_status = "IDLE"
                runtime.state_version += 1
                runtime.last_simulated_world_minute = max(runtime.last_simulated_world_minute, minute)
                runtime.time_revision = revision
                runtime.save(using=alias)
            command.status, command.result_code, command.completed_world_minute = "CANCELLED", CODE, minute
            if runtime:
                command.state_version = runtime.state_version
            command.save(using=alias, update_fields=["status", "result_code", "completed_world_minute", "state_version", "updated_at"])
            model("NpcActionReservation").objects.using(alias).filter(command_id=command.pk, status="HELD").update(
                status="RELEASED", active_lock_key=None, released_world_minute=minute)
            event, _ = Event.objects.using(alias).get_or_create(dedupe_key=f"commission-player-only-command:{command.pk}", defaults=dict(
                event_type="NPC_ACTION_FAILED", actor_npc_id=command.npc_id, participant_npc_ids=[command.npc.agent_id],
                command_id=command.pk, request_id=command.request_id, world_minute=minute, time_revision=revision,
                location_code=runtime.current_location_code if runtime else command.location_code,
                visibility="PARTICIPANTS", player_summary=MESSAGE, hub_summary=MESSAGE))
            receipt, _ = Receipt.objects.using(alias).get_or_create(request_id=command.request_id, defaults=dict(
                command_id=command.pk, npc_id=command.npc_id, action_kind=command.kind, success=False, result_code=CODE,
                response_data={"message": MESSAGE}, world_minute=minute, time_revision=revision,
                state_version=command.state_version, world_event_id=event.pk))
            Outbox.objects.using(alias).get_or_create(dedupe_key=f"outcome:{command.request_id}", defaults=dict(
                recipient=command.sender_id, message_kind="OUTCOME", command_id=command.pk,
                receipt_id=receipt.pk, event_id=event.pk, priority=10, status="PENDING", payload=dict(
                    schema_version="village.outcome.v1", request_id=str(command.request_id), npc_id=command.npc.agent_id,
                    ref_id=str(command.pk), original_message_id=str(command.message_id), success=receipt.success,
                    result_code=receipt.result_code, action=command.kind, phase="COMPLETED", terminal=True,
                    world_minute=minute, time_revision=revision, message=MESSAGE, next_action=None)))


class Migration(migrations.Migration):
    dependencies = [("village", "0052_migrate_legacy_supply")]
    operations = [
        migrations.RunPython(retire_npc_acceptance),
        migrations.AddConstraint(model_name="villagepersonalcommission", constraint=models.CheckConstraint(
            condition=~models.Q(status="ACTIVE") | models.Q(actor_type="PLAYER"), name="commission_active_player_only")),
    ]
