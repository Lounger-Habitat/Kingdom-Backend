"""Move real promises and their escrow before removing the retired tables."""
import hashlib
import json
import uuid

from django.db import migrations, models, transaction


MIGRATION_KEY = "0052_migrate_legacy_supply"
AUDIT_ACTOR = "legacy_supply"


def migration_id(kind, source_id):
    return uuid.uuid5(uuid.NAMESPACE_URL, f"kingdom:personal-commission:{MIGRATION_KEY}:{kind}:{source_id}")


def _snapshot(row):
    return {field.attname: (str(value) if isinstance(value, uuid.UUID) else value)
            for field in row._meta.concrete_fields
            for value in [getattr(row, field.attname)]}


def migrate_legacy_supply(apps, schema_editor):
    alias = schema_editor.connection.alias
    Contract = apps.get_model("village", "VillagePlayerContract")
    Claim = apps.get_model("village", "VillageContractClaim")
    Commission = apps.get_model("village", "VillagePersonalCommission")
    Receipt = apps.get_model("village", "VillagePersonalCommissionReceipt")
    OldReceipt = apps.get_model("village", "VillagePlayerActionReceipt")
    Contribution = apps.get_model("village", "VillagePlayerContribution")
    Wallet = apps.get_model("inventory", "NpcGlobalInventory")
    Delivery = apps.get_model("village", "NpcTradeDelivery")
    Clock = apps.get_model("game_time", "GlobalGameTimeState")
    Item = apps.get_model("items", "Item")

    with transaction.atomic(using=alias):
        clock = Clock.objects.using(alias).select_for_update().filter(pk=1).first()
        minute = clock.world_minute if clock else 0

        def cash(npc_id, amount):
            wallet = Wallet.objects.using(alias).select_for_update().get(npc_id=npc_id)
            if wallet.money + amount < 0:
                raise ValueError("Legacy supply migration cannot overdraw an NPC wallet")
            wallet.money += amount
            wallet.asset_version += 1
            wallet.save(using=alias, update_fields=["money", "asset_version", "updated_at"])

        def available_money(npc_id):
            wallet = Wallet.objects.using(alias).select_for_update().get(npc_id=npc_id)
            reserved = Delivery.objects.using(alias).filter(buyer_order__npc_id=npc_id, status="ACTIVE").aggregate(
                total=models.Sum("reserved_money"))["total"] or 0
            return max(0, wallet.money - reserved)

        def definition(contract, quantity):
            if quantity <= 0:
                raise ValueError("Legacy supply has no positive quantity to archive")
            places = [contract.delivery_location_code] if contract.delivery_location_code else []
            return {"stages": [{"id": "legacy_delivery", "title": "交付物品", "location_code": "", "conditions": [{
                "id": "legacy_item", "type": "item", "label": "", "target": quantity,
                "item_ids": [contract.item_id], "min_quality": contract.min_quality,
                "requires_freshness": True, "min_remaining_minutes": contract.min_remaining_minutes,
                "locations": places}]}]}

        def audit(commission, kind, source_id, data):
            request_id = migration_id("audit_" + kind, source_id)
            body = {"migration": MIGRATION_KEY, "source_kind": kind, **data}
            digest = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            Receipt.objects.using(alias).get_or_create(actor_type="SYSTEM", actor_id=AUDIT_ACTOR,
                request_id=request_id, defaults=dict(commission=commission, action="MIGRATE", payload_hash=digest,
                                                     response=body, world_minute=minute))

        def related_history(claim):
            receipt_ids, request_ids = [], []
            for receipt in OldReceipt.objects.using(alias).filter(player_id=claim.player_id, action__in=["claim", "deliver", "cancel"]):
                candidate = ((receipt.response or {}).get("data") or {}).get("result") or {}
                try:
                    matches = uuid.UUID(str(candidate.get("claim_id"))) == claim.pk
                except (ValueError, TypeError, AttributeError):
                    matches = False
                if matches:
                    receipt_ids.append(receipt.pk)
                    request_ids.append(receipt.request_id)
            return {"legacy_action_receipt_ids": receipt_ids, "legacy_contribution_ids": list(
                Contribution.objects.using(alias).filter(player_id=claim.player_id, request_id__in=request_ids)
                .values_list("pk", flat=True))}

        for contract in Contract.objects.using(alias).select_related("npc").order_by("contract_id"):
            if Receipt.objects.using(alias).filter(actor_type="SYSTEM", actor_id=AUDIT_ACTOR,
                    request_id=migration_id("audit_contract", contract.pk)).exists():
                continue
            original_contract = _snapshot(contract)
            claims = list(Claim.objects.using(alias).filter(contract=contract).order_by("claim_id"))
            item_name = Item.objects.using(alias).filter(pk=contract.item_id).values_list("item_name", flat=True).first() or str(contract.item_id)
            title = f"{contract.npc.name}的{item_name}委托"[:100]
            claim_commissions, allocated = [], 0
            for claim in claims:
                original_claim = _snapshot(claim)
                remaining = claim.quantity - claim.delivered_quantity
                live = claim.status == "ACTIVE" and claim.expires_world_minute > minute and remaining > 0
                allocated += claim.delivered_quantity + (remaining if live else 0)
                if live:
                    status, reward, held = "ACTIVE", claim.reserved_gold, claim.reserved_gold
                    if held != remaining * contract.unit_price:
                        raise ValueError("Legacy active claim escrow does not match its unpaid promise")
                    reason = "原认领继续履约；既有交付和报酬已经结清，仅接续未支付报酬。"
                else:
                    status = ("COMPLETED" if remaining == 0 or claim.status == "COMPLETED" else
                              "ABANDONED" if claim.status in {"CANCELLED", "ABANDONED"} else "EXPIRED")
                    reward, held = claim.quantity * contract.unit_price, 0
                    reason = "原认领作为已关闭历史保留；已交付物品和已支付报酬不重新结算。"
                commission, created = Commission.objects.using(alias).get_or_create(
                    pk=migration_id("claim", claim.pk), defaults=dict(publisher_id=contract.npc_id, title=title,
                        description=f"{reason}\n迁移来源：供货 {contract.pk}；认领 {claim.pk}。",
                        requirements=definition(contract, claim.quantity), locations=[contract.delivery_location_code] if contract.delivery_location_code else [],
                        progress={"conditions": {"legacy_item": claim.delivered_quantity}}, status=status,
                        actor_type="PLAYER", actor_id=str(claim.player_id), reward_gold=reward, reserved_reward_gold=held,
                        escrow_gold=0, opened_world_minute=contract.day_index * 1440,
                        expires_world_minute=claim.expires_world_minute,
                        completed_world_minute=None if live else minute,
                        created_request_id=migration_id("publish_claim", claim.pk)))
                if created and not live and claim.reserved_gold:
                    cash(contract.npc_id, claim.reserved_gold)
                audit(commission, "claim", claim.pk, dict(legacy_contract=original_contract, legacy_claim=original_claim,
                    paid_gold_before_migration=claim.delivered_quantity * contract.unit_price,
                    reserved_reward_transferred=held, reward_refunded=claim.reserved_gold if not live else 0,
                    migration_reason=reason, **related_history(claim)))
                claim.reserved_gold = 0
                claim.save(using=alias, update_fields=["reserved_gold"])
                claim_commissions.append(commission)

            remaining = max(0, contract.total_quantity - allocated)
            funded = 0
            reason = "全部需求已有认领或已交付；原供货出处附在对应委托记录中，不生成重复需求。"
            if remaining:
                reward = remaining * contract.unit_price
                live = contract.status in {"OPEN", "LEGACY_CLOSED"} and contract.expires_world_minute > minute
                status = "EXPIRED" if contract.expires_world_minute <= minute or contract.status == "EXPIRED" else "WITHDRAWN"
                reason = "原供货剩余需求已关闭，仅保留历史。"
                if live:
                    if available_money(contract.npc_id) >= reward:
                        status, funded = "OPEN", reward
                        reason = "原未认领需求转为个人委托，已从发布者可用金币中预留全部报酬。"
                    else:
                        status = "WITHDRAWN"
                        reason = "发布者可用金币不足以担保全部报酬，迁移为关闭历史，可重新发布新委托。"
                commission, created = Commission.objects.using(alias).get_or_create(
                    pk=migration_id("contract", contract.pk), defaults=dict(publisher_id=contract.npc_id, title=title,
                        description=f"{reason}\n迁移来源：供货 {contract.pk}。",
                        requirements=definition(contract, remaining), locations=[contract.delivery_location_code] if contract.delivery_location_code else [],
                        progress={"conditions": {}}, status=status, reward_gold=reward, reserved_reward_gold=funded,
                        escrow_gold=0, opened_world_minute=contract.day_index * 1440,
                        expires_world_minute=contract.expires_world_minute,
                        completed_world_minute=None if status == "OPEN" else minute,
                        created_request_id=migration_id("publish_contract", contract.pk)))
                if created and funded:
                    cash(contract.npc_id, -funded)
            else:
                if not claim_commissions:
                    raise ValueError("Legacy zero-demand supply cannot be associated with an existing claim")
                commission = claim_commissions[0]
            audit(commission, "contract", contract.pk, dict(legacy_contract=original_contract,
                migrated_claim_commission_ids=[str(row.pk) for row in claim_commissions], unallocated_quantity=remaining,
                new_reward_reserved=funded, migration_reason=reason))


class Migration(migrations.Migration):
    dependencies = [("village", "0051_personal_commissions")]
    operations = [
        migrations.RunPython(migrate_legacy_supply),
        migrations.DeleteModel(name="VillageContractClaim"),
        migrations.DeleteModel(name="VillagePlayerContract"),
    ]
