"""Finite, fully funded introductory procurement and actual public reserve stock.

Every mutation uses the same world lock as village economics. Views never create
orders, money or inventory. Money and item transfers use the existing ledger.
"""
from copy import deepcopy
import hashlib
import json
import uuid

from django.db import models, transaction

from game_time.models import GlobalGameTimeState
from inventory.components import copy_batch_attributes, same_batch_attributes
from inventory.models import (CollectionPointLevelItem, InventoryBag, InventorySlot,
                              NpcGlobalInventory, NpcGlobalInventorySlot)
from items.models import Item
from player.progression_rules import BUSINESS_ORDER_XP
from marketplace.services import get_player_main_bag
from .models import (EconomicLedgerEntry, StarterReserveTransfer, StarterSupplyBatch,
                     StarterSupplyDelivery, StarterSupplyOrder, StarterSupplyReceipt, VillageAccount)

BATCH_CODE = "G1_STARTER_SUPPLY_V1"
ORDER_COUNT, ORDER_REWARD, REQUIRED_QUANTITY = 5, 54, 6
LIFETIME = 3 * 1440
LOCATION = "GENERAL_STORE"


class SupplyError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def _fail(code, message):
    raise SupplyError(code, message)


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        _fail("INVALID_PARAMS", "订单或请求标识无效。")


def _clock(minute):
    clock = GlobalGameTimeState.objects.select_for_update().filter(pk=1).first()
    if not clock or type(minute) is not int or minute != clock.world_minute:
        _fail("WORLD_TIME_CHANGED", "世界时间已经变化，请刷新后重试。")
    return clock


def _digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _ledger(key, kind, changes, minute):
    EconomicLedgerEntry.objects.create(request_id=uuid.uuid5(uuid.NAMESPACE_URL, "starter:" + key),
        entry_type=kind, changes=changes, external_delta=0, world_minute=minute)


def _next_index(bag):
    return (bag.slots.aggregate(n=models.Max("slot_index"))["n"] or 0) + 1


def _copy_quantity(source, target, quantity):
    cls = NpcGlobalInventorySlot if isinstance(target, NpcGlobalInventory) else InventorySlot
    row = cls(bag=target, slot_index=_next_index(target), item_id=source.item_id,
              item_amount=quantity, item_bag_name=source.item_bag_name)
    copy_batch_attributes(source, row)
    row.save()
    source.item_amount -= quantity
    source.save(update_fields=["item_amount"])
    return row


def _batch_line(slot, quantity):
    return dict(instance_id=str(slot.instance_id), item_id=slot.item_id, quantity=quantity,
        quality=slot.quality, produced_world_minute=slot.produced_world_minute,
        expires_world_minute=slot.expires_world_minute,
        source_request_id=str(slot.source_request_id) if slot.source_request_id else None,
        freshness_unknown=slot.freshness_unknown)


@transaction.atomic
def ensure_supply_batch(minute):
    """Called only after a successful first collection, never from overview."""
    _clock(minute)
    existing = StarterSupplyBatch.objects.select_for_update().filter(pk=BATCH_CODE).first()
    if existing:
        return existing
    fund = VillageAccount.objects.select_for_update().filter(account_code="PUBLIC_FUND").first()
    ids = list(CollectionPointLevelItem.objects.filter(level__collection_point__collect_type="采集食材",
        level__level=1, item__is_deleted=False, collect_weight__gt=0, collect_min_amount__gt=0,
        collect_max_amount__gte=models.F("collect_min_amount")).order_by("item_id")
        .values_list("item_id", flat=True).distinct())
    total = ORDER_COUNT * ORDER_REWARD
    reason = "NO_ACCEPTED_ITEMS" if not ids else "PUBLIC_BUDGET_UNAVAILABLE" if not fund or fund.balance < total else ""
    batch = StarterSupplyBatch.objects.create(code=BATCH_CODE, fund=fund, status="UNAVAILABLE" if reason else "OPEN",
        unavailable_reason=reason, opened_world_minute=minute, expires_world_minute=minute + LIFETIME)
    if reason:
        return batch
    fund.balance -= total
    fund.asset_version += 1
    fund.save(update_fields=["balance", "asset_version", "updated_at"])
    batch.reserve_bag = InventoryBag.objects.create(character_id="PUBLIC_RESERVE:" + BATCH_CODE)
    batch.reserved_gold = batch.escrow_gold = total
    batch.save(update_fields=["reserve_bag", "reserved_gold", "escrow_gold"])
    for i in range(ORDER_COUNT):
        escrow = InventoryBag.objects.create(character_id=f"STARTER_ESCROW:{BATCH_CODE}:{i}")
        StarterSupplyOrder.objects.create(batch=batch, escrow_bag=escrow, accepted_item_ids=list(ids))
    _ledger("publish:" + BATCH_CODE, "STARTER_RESERVE", {"PUBLIC_FUND": {"money": -total},
        "STARTER_ESCROW": {"money": total}}, minute)
    return batch


def _return_deliveries(order):
    bag, _ = get_player_main_bag(order.actor_player)
    bag = InventoryBag.objects.select_for_update().get(pk=bag.pk)
    returned = []
    for delivery in order.deliveries.select_for_update().filter(status="HELD").select_related("slot").order_by("pk"):
        source = InventorySlot.objects.select_for_update().get(pk=delivery.slot_id)
        original = InventorySlot.objects.select_for_update().filter(bag=bag, instance_id=delivery.source_instance_id).first()
        if original and original.item_id == source.item_id and same_batch_attributes(original, source):
            original.item_amount += delivery.quantity
            original.save(update_fields=["item_amount"])
            source.item_amount -= delivery.quantity
            source.save(update_fields=["item_amount"])
            target = original
        else:
            target = _copy_quantity(source, bag, delivery.quantity)
            # If the original row was removed, restore its identity as well as
            # its attributes. A changed original batch remains independent.
            if original is None:
                target.instance_id = delivery.source_instance_id
                target.save(update_fields=["instance_id"])
        returned.append(_batch_line(target, delivery.quantity))
        delivery.status = "RETURNED"
        delivery.save(update_fields=["status"])
    return returned


@transaction.atomic
def expire_supply(minute):
    _clock(minute)
    batch = StarterSupplyBatch.objects.select_for_update().filter(pk=BATCH_CODE, status="OPEN",
                                                                   expires_world_minute__lte=minute).first()
    if not batch:
        return 0
    returned = []
    for order in batch.orders.select_for_update().filter(status__in=["OPEN", "ACTIVE"]).order_by("order_id"):
        if order.actor_player_id:
            returned.extend(dict(line, player_id=str(order.actor_player_id)) for line in _return_deliveries(order))
        order.status = "EXPIRED"
        order.save(update_fields=["status"])
    refund = batch.escrow_gold
    if refund:
        fund = VillageAccount.objects.select_for_update().get(pk=batch.fund_id)
        fund.balance += refund
        fund.asset_version += 1
        fund.save(update_fields=["balance", "asset_version", "updated_at"])
    batch.status, batch.escrow_gold = "EXPIRED", 0
    batch.refunded_gold += refund
    batch.revision += 1
    batch.save(update_fields=["status", "escrow_gold", "refunded_gold", "revision"])
    _ledger("expire:" + BATCH_CODE, "STARTER_REFUND", {"PUBLIC_FUND": {"money": refund},
        "STARTER_ESCROW": {"money": -refund}, "returned_batches": returned}, minute)
    return refund


def _eligibility(slot, order, minute, *, items=None):
    if slot.item_id not in order.accepted_item_ids:
        return "ITEM_NOT_ACCEPTED"
    if not 1 <= slot.quality <= 6:
        return "QUALITY_REQUIRED"
    if slot.produced_world_minute is not None and slot.produced_world_minute > minute:
        return "PROVENANCE_REQUIRED"
    available = (Item.objects.filter(pk=slot.item_id, is_deleted=False).exists() if items is None
                 else slot.item_id in items and not items[slot.item_id].is_deleted)
    if not available:
        return "ITEM_UNAVAILABLE"
    return ""


def _order_data(order, player, slots, items, minute, *, read_only=False, completed_personal=False):
    expired = order.batch.expires_world_minute <= minute and order.status != "COMPLETED"
    status = "EXPIRED" if expired else order.status
    mine = player is not None and order.actor_player_id == player.pk
    reason = ("MODE_READ_ONLY" if read_only else "ORDER_EXPIRED" if expired else "PERSONAL_ORDER_COMPLETED"
              if completed_personal and status != "COMPLETED" else "ORDER_NOT_OWNED" if status == "ACTIVE" and not mine else "")
    rows = []
    for slot in slots:
        if slot.item_id not in order.accepted_item_ids:
            continue
        unsuitable = _eligibility(slot, order, minute, items=items)
        rows.append(dict(instance_id=str(slot.instance_id), item_id=slot.item_id,
            item_name=items[slot.item_id].item_name if slot.item_id in items else str(slot.item_id), quantity=slot.item_amount,
            eligible_quantity=min(slot.item_amount, order.required_quantity - order.delivered_quantity)
                if not unsuitable and status == "ACTIVE" and mine and not read_only else 0,
            reason=unsuitable or reason))
    return dict(order_id=str(order.pk), title="公共储备入门供货", status=status,
        actor_player_id=str(order.actor_player_id) if order.actor_player_id else None,
        required_quantity=order.required_quantity, delivered_quantity=order.delivered_quantity,
        reward_gold=order.reward_gold, reward_business_xp=BUSINESS_ORDER_XP, location_code=LOCATION,
        expires_world_minute=order.batch.expires_world_minute, accepted_item_ids=list(order.accepted_item_ids),
        delivery_batches=rows, unavailable_reason=reason,
        can_accept=status == "OPEN" and not read_only and not completed_personal,
        can_deliver=status == "ACTIVE" and mine and not read_only,
        can_abandon=status == "ACTIVE" and mine and not read_only)


def supply_overview(player, bag, minute, *, read_only=False):
    batch = StarterSupplyBatch.objects.filter(pk=BATCH_CODE).first()
    if batch is None:
        return dict(available=False, reason="FIRST_COLLECTION_REQUIRED", orders=[], completed_personal=False, version=0)
    order_rows = list(batch.orders.select_related("batch").order_by("order_id"))
    player_id = player.pk if player is not None else None
    completed = any(order.actor_player_id == player_id and order.status == "COMPLETED" for order in order_rows)
    active = batch.expires_world_minute > minute and any(
        order.actor_player_id == player_id and order.status == "ACTIVE" for order in order_rows)
    accepted_ids = {item_id for order in order_rows for item_id in order.accepted_item_ids}
    items = Item.objects.filter(pk__in=accepted_ids).only("item_name", "is_deleted").in_bulk() if bag and bag.pk else {}
    slots = list(bag.slots.filter(item_amount__gt=0, item_id__in=accepted_ids).order_by("slot_index")) if bag and bag.pk else []
    orders = [_order_data(order, player, slots, items, minute, read_only=read_only, completed_personal=completed)
              for order in order_rows]
    for order in orders:
        if active:
            order["can_accept"] = False
            if order["status"] == "OPEN":
                order["unavailable_reason"] = "ANOTHER_ORDER_ACTIVE"
    available = not completed and (active or any(o["status"] == "OPEN" for o in orders))
    reason = batch.unavailable_reason
    if not reason:
        reason = "PERSONAL_ORDER_COMPLETED" if completed else "BATCH_EXPIRED" if batch.expires_world_minute <= minute else "" if available else "NO_OPEN_ORDERS"
    return dict(available=available, reason=reason, orders=orders, completed_personal=completed,
        version=batch.revision, reserved_gold=batch.reserved_gold, escrow_gold=batch.escrow_gold,
        paid_gold=batch.paid_gold, refunded_gold=batch.refunded_gold)


@transaction.atomic
def execute_supply(player, bag, action, params, rid, minute):
    _clock(minute)
    rid = _uuid(rid)
    if action not in ("supply_accept", "supply_deliver", "supply_abandon"):
        _fail("INVALID_PARAMS", "公共采购操作无效。")
    expected = {"order_id", "instance_id", "quantity"} if action == "supply_deliver" else {"order_id"}
    if not isinstance(params, dict) or set(params) != expected or bag.player_id != player.pk:
        _fail("INVALID_PARAMS", "公共采购参数无效。")
    digest = _digest(dict(action=action, params=params))
    old = StarterSupplyReceipt.objects.filter(player=player, request_id=rid).first()
    if old:
        if old.payload_hash != digest:
            _fail("REQUEST_CONFLICT", "同一请求编号不能用于不同操作。")
        return deepcopy(old.result)
    batch = StarterSupplyBatch.objects.select_for_update().filter(pk=BATCH_CODE, status="OPEN").first()
    if not batch or batch.expires_world_minute <= minute:
        _fail("ORDER_EXPIRED", "本批公共采购已结束或未开放。")
    order = StarterSupplyOrder.objects.select_for_update().filter(pk=_uuid(params["order_id"]), batch=batch).first()
    if not order:
        _fail("ORDER_NOT_FOUND", "采购订单不存在。")
    bag = InventoryBag.objects.select_for_update().get(pk=bag.pk)
    if action == "supply_accept":
        if batch.orders.filter(actor_player=player, status="COMPLETED").exists():
            _fail("PERSONAL_ORDER_COMPLETED", "每人本批最多完成一份公共采购。")
        if batch.orders.filter(actor_player=player, status="ACTIVE").exists():
            _fail("ANOTHER_ORDER_ACTIVE", "请先完成或放弃当前认领的采购。")
        if order.status != "OPEN":
            _fail("ORDER_UNAVAILABLE", "该订单已被认领。")
        order.status, order.actor_player = "ACTIVE", player
        order.save(update_fields=["status", "actor_player"])
        result = dict(order_id=str(order.pk), status="ACTIVE")
    else:
        if order.status != "ACTIVE" or order.actor_player_id != player.pk:
            _fail("ORDER_NOT_OWNED", "该订单未由你认领或已经结束。")
        if action == "supply_abandon":
            returned = _return_deliveries(order)
            order.status, order.actor_player, order.delivered_quantity = "OPEN", None, 0
            order.save(update_fields=["status", "actor_player", "delivered_quantity"])
            result = dict(order_id=str(order.pk), status="OPEN", returned_batches=returned)
            _ledger(f"action:{player.pk}:{rid}", "STARTER_ABANDON", {"returned_batches": returned,
                "player_id": str(player.pk), "order_id": str(order.pk)}, minute)
        else:
            quantity = params["quantity"]
            if type(quantity) is not int or not 0 < quantity <= order.required_quantity - order.delivered_quantity:
                _fail("INVALID_PARAMS", "交付数量超过订单剩余需求。")
            source = InventorySlot.objects.select_for_update().filter(bag=bag, instance_id=_uuid(params["instance_id"])).first()
            if source is None or source.item_amount < quantity:
                _fail("INSUFFICIENT_ITEMS", "指定批次的物品不足。")
            error = _eligibility(source, order, minute)
            if error:
                _fail(error, "该批次不满足物品、品质、来源或剩余鲜期要求。")
            target = _copy_quantity(source, order.escrow_bag, quantity)
            StarterSupplyDelivery.objects.create(order=order, source_instance_id=source.instance_id,
                                                 slot=target, quantity=quantity)
            order.delivered_quantity += quantity
            complete = order.delivered_quantity == order.required_quantity
            if complete:
                for delivery in order.deliveries.select_for_update().filter(status="HELD").select_related("slot"):
                    slot = delivery.slot
                    if _eligibility(slot, order, minute):
                        _fail("HELD_BATCH_NO_LONGER_ELIGIBLE", "较早交付的批次已不满足鲜期或目录要求，请弃单取回。")
                    slot.bag, slot.slot_index = batch.reserve_bag, _next_index(batch.reserve_bag)
                    slot.save(update_fields=["bag", "slot_index"])
                    delivery.status = "RESERVED"
                    delivery.save(update_fields=["status"])
                if batch.escrow_gold < order.reward_gold:
                    _fail("ESCROW_INCONSISTENT", "采购报酬托管异常。")
                batch.escrow_gold -= order.reward_gold
                batch.paid_gold += order.reward_gold
                bag.money += order.reward_gold
                bag._money_change_source = "starter_supply"
                bag.save(update_fields=["money"])
                order.status = "COMPLETED"
            order.save(update_fields=["delivered_quantity", "status"])
            result = dict(order_id=str(order.pk), status=order.status, delivered_quantity=order.delivered_quantity,
                          earned_gold=order.reward_gold if complete else 0)
            _ledger(f"action:{player.pk}:{rid}", "STARTER_DELIVERY", {
                f"PLAYER:{player.pk}": {"money": result["earned_gold"], str(source.item_id): -quantity},
                "STARTER_ESCROW": {"money": -result["earned_gold"]},
                "delivered_batch": _batch_line(target, quantity), "order_id": str(order.pk),
                "destination": "PUBLIC_RESERVE" if complete else "ORDER_ESCROW"}, minute)
    batch.revision += 1
    batch.save(update_fields=["revision", "escrow_gold", "paid_gold"])
    result["supply_version"] = batch.revision
    StarterSupplyReceipt.objects.create(player=player, request_id=rid, action=action, payload_hash=digest,
                                       result=deepcopy(result))
    return result


@transaction.atomic
def spend_reserve(batch_id, beneficiary_bag, allocations, request_id, minute, purpose):
    """Trusted explicit transfer; no player API and no automatic NPC consumption."""
    _clock(minute)
    rid = _uuid(request_id)
    if not isinstance(purpose, str) or not 0 < len(purpose.strip()) <= 200 or not isinstance(allocations, list) or not allocations:
        _fail("INVALID_PARAMS", "支用需要明确用途和真实批次。")
    if isinstance(beneficiary_bag, InventoryBag) and beneficiary_bag.pk and beneficiary_bag.player_id:
        target = InventoryBag.objects.select_for_update().get(pk=beneficiary_bag.pk)
        beneficiary = f"PLAYER:{target.player_id}"
    elif isinstance(beneficiary_bag, NpcGlobalInventory) and beneficiary_bag.pk:
        target = NpcGlobalInventory.objects.select_for_update().get(pk=beneficiary_bag.pk)
        beneficiary = f"NPC:{target.npc_id}"
    else:
        _fail("INVALID_BENEFICIARY", "只能支用至实际存在的玩家或 NPC 库存。")
    normalized = []
    for row in allocations:
        if not isinstance(row, dict) or set(row) != {"instance_id", "quantity"} or type(row["quantity"]) is not int or row["quantity"] <= 0:
            _fail("INVALID_PARAMS", "支用批次数量无效。")
        normalized.append(dict(instance_id=str(_uuid(row["instance_id"])), quantity=row["quantity"]))
    if len({row["instance_id"] for row in normalized}) != len(normalized):
        _fail("INVALID_PARAMS", "同一批次不能重复支用。")
    digest = _digest(dict(batch=str(batch_id), beneficiary=beneficiary, bag_id=target.pk,
                          allocations=sorted(normalized, key=lambda r: r["instance_id"]), purpose=purpose))
    old = StarterReserveTransfer.objects.filter(pk=rid).first()
    if old:
        if old.payload_hash != digest:
            _fail("REQUEST_CONFLICT", "该支用编号已用于其他用途。")
        return deepcopy(old.result)
    batch = StarterSupplyBatch.objects.select_for_update().filter(pk=batch_id).first()
    if not batch or not batch.reserve_bag_id:
        _fail("RESERVE_UNAVAILABLE", "公共储备不存在。")
    moved = []
    for row in normalized:
        source = InventorySlot.objects.select_for_update().filter(bag=batch.reserve_bag,
                                                                  instance_id=row["instance_id"]).first()
        if not source or source.item_amount < row["quantity"]:
            _fail("INSUFFICIENT_RESERVE", "公共储备的该批次物资不足。")
        moved.append(_batch_line(_copy_quantity(source, target, row["quantity"]), row["quantity"]))
    if isinstance(target, NpcGlobalInventory):
        target.asset_version += 1
        target.save(update_fields=["asset_version"])
    result = dict(batch_id=str(batch.pk), beneficiary=beneficiary, purpose=purpose, batches=moved)
    StarterReserveTransfer.objects.create(request_id=rid, batch=batch, purpose=purpose, beneficiary=beneficiary,
        world_minute=minute, payload_hash=digest, result=deepcopy(result))
    _ledger("spend:" + str(rid), "STARTER_USE", {"beneficiary": beneficiary, "batches": moved,
        "purpose": purpose, "source": "PUBLIC_RESERVE"}, minute)
    batch.revision += 1
    batch.save(update_fields=["revision"])
    return result
