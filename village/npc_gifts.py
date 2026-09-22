"""One-way NPC gifts, settled by the existing atomic economy command service."""
import uuid

from django.db.models import Max

from account.models import GameAccount
from inventory.components import copy_batch_attributes
from inventory.models import InventoryBag, InventorySlot, NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot
from items.models import Item
from .phase3_asset_service import _fail, _ok, get_available_money, get_available_slot_amount


FORMAL_NPC_IDS = frozenset({"10011", "10012", "10013", "10014", "10015"})
MAX_AMOUNT = 2147483647


def validate_gift(params, sender_id):
    required = {"exchange_id", "target_type", "target_id", "items"}
    if not isinstance(params, dict) or not required <= set(params) or set(params) - required - {"gold_amount"}:
        return "GIFT_INVALID_PARAMS"
    try:
        if str(uuid.UUID(params["exchange_id"])) != params["exchange_id"]:
            return "GIFT_INVALID_PARAMS"
    except (ValueError, TypeError, AttributeError):
        return "GIFT_INVALID_PARAMS"
    target_type, target_id = params["target_type"], params["target_id"]
    if target_type not in ("PLAYER", "NPC") or not isinstance(target_id, str) or not target_id or len(target_id) > 100:
        return "GIFT_INVALID_PARAMS"
    if target_type == "NPC" and (target_id not in FORMAL_NPC_IDS or target_id == str(sender_id)):
        return "GIFT_TARGET_INVALID"
    gold_amount = params.get("gold_amount", 0)
    if type(gold_amount) is not int or not 0 <= gold_amount <= MAX_AMOUNT:
        return "GIFT_INVALID_PARAMS"
    rows = params["items"]
    if not isinstance(rows, list) or len(rows) > 20 or (not rows and gold_amount == 0):
        return "GIFT_INVALID_PARAMS"
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"instance_id", "amount"}:
            return "GIFT_INVALID_PARAMS"
        try:
            instance_id = uuid.UUID(row["instance_id"])
        except (ValueError, TypeError, AttributeError):
            return "GIFT_INVALID_PARAMS"
        if instance_id in seen or type(row["amount"]) is not int or not 1 <= row["amount"] <= MAX_AMOUNT:
            return "GIFT_INVALID_PARAMS"
        seen.add(instance_id)
    return None


def exchange_key(npc_id, params):
    return f"npc-gift:{npc_id}:{params['exchange_id']}"


def same_gift(left, right):
    """Batch ordering and UUID spelling do not create a different delivery."""
    def canonical(params):
        return (params["target_type"], params["target_id"], params.get("gold_amount", 0), sorted(
            (str(uuid.UUID(row["instance_id"])), row["amount"]) for row in params["items"]))
    return canonical(left) == canonical(right)


def give_items(npc_id, params, world_minute, request_id):
    error = validate_gift(params, npc_id)
    if error:
        return _fail(error)
    if str(npc_id) not in FORMAL_NPC_IDS:
        return _fail("P3_FORBIDDEN_ACTOR")
    target_type, target_id = params["target_type"], params["target_id"]
    sender = NpcDefinition.objects.filter(agent_id=str(npc_id), is_test_only=False).first()
    if sender is None:
        return _fail("P3_FORBIDDEN_ACTOR")

    # The economy service holds the world lock first. Player bags precede NPC
    # bags, matching the player settlement path; NPC bags are locked in ID order.
    target_bag = None
    recipient = None
    if target_type == "PLAYER":
        if not target_id.isascii() or not target_id.isdecimal() or not 0 < int(target_id) <= 9223372036854775807:
            return _fail("GIFT_TARGET_INVALID")
        recipient = GameAccount.objects.filter(pk=int(target_id), is_active=True).first()
        if recipient is not None:
            target_bag = InventoryBag.objects.select_for_update().filter(player=recipient, character_id=recipient.username).first()
    else:
        recipient = NpcDefinition.objects.filter(agent_id=target_id, is_test_only=False).first()
    if recipient is None or (target_type == "PLAYER" and target_bag is None):
        return _fail("GIFT_TARGET_INVALID")

    owners = [sender.pk] + ([recipient.pk] if target_type == "NPC" else [])
    bags = {bag.npc_id: bag for bag in NpcGlobalInventory.objects.select_for_update().filter(npc_id__in=owners).order_by("npc_id")}
    source_bag = bags.get(sender.pk)
    if target_type == "NPC":
        target_bag = bags.get(recipient.pk)
    if source_bag is None or target_bag is None:
        return _fail("GIFT_TARGET_INVALID")

    gold_amount = params.get("gold_amount", 0)
    available_money = get_available_money(source_bag)
    if gold_amount > available_money:
        return _fail("GIFT_INSUFFICIENT_MONEY", data={"required_amount": gold_amount,
            "available_amount": available_money})
    if gold_amount > 0 and target_bag.money > MAX_AMOUNT - gold_amount:
        return _fail("GIFT_TARGET_MONEY_LIMIT")

    rows = sorted(params["items"], key=lambda row: str(uuid.UUID(row["instance_id"])))
    selected = []
    for row in rows:
        slot = source_bag.slots.select_for_update().filter(instance_id=row["instance_id"], item_amount__gt=0).first()
        if slot is None:
            return _fail("GIFT_ITEM_NOT_OWNED", data={"instance_id": row["instance_id"]})
        item = Item.objects.filter(pk=slot.item_id, is_deleted=False).first()
        if item is None:
            return _fail("GIFT_ITEM_INVALID", data={"instance_id": row["instance_id"]})
        available = get_available_slot_amount(slot, world_minute)
        if available < row["amount"]:
            return _fail("GIFT_INSUFFICIENT_ITEM", data={"instance_id": row["instance_id"],
                "item_id": slot.item_id, "required_amount": row["amount"], "available_amount": available})
        selected.append((slot, item, row["amount"]))

    slot_model = NpcGlobalInventorySlot if target_type == "NPC" else InventorySlot
    last_index = target_bag.slots.aggregate(value=Max("slot_index"))["value"]
    next_index = 0 if last_index is None else last_index + 1
    delivered = []
    deltas = {}
    for source, item, amount in selected:
        target = slot_model(bag=target_bag, slot_index=next_index, item_id=source.item_id,
            item_amount=amount, item_bag_name=source.item_bag_name, hide_in_shop=target_type == "NPC")
        copy_batch_attributes(source, target)
        target.save()
        source.item_amount -= amount
        source.save(update_fields=["item_amount"])
        next_index += 1
        delivered.append({"item_id": source.item_id, "item_name": item.item_name, "amount": amount,
            "source_instance_id": str(source.instance_id), "received_instance_id": str(target.instance_id)})
        key = str(source.item_id)
        deltas[key] = deltas.get(key, 0) + amount

    if gold_amount > 0:
        source_bag.money -= gold_amount
        target_bag.money += gold_amount
        deltas["money"] = gold_amount
        if target_type == "PLAYER":
            target_bag._money_change_source = "npc_gift"
            target_bag.save(update_fields=["money"])

    versions = {}
    for bag in bags.values():
        bag.asset_version += 1
        bag.save(update_fields=["money", "asset_version", "updated_at"])
        versions[str(npc_id) if bag.npc_id == sender.pk else target_id] = bag.asset_version
    target_name = recipient.name if target_type == "NPC" else recipient.username
    receiver_key = target_id if target_type == "NPC" else "PLAYER:" + str(recipient.pk)
    return _ok(asset_versions=versions,
        effects={str(npc_id): {key: -value for key, value in deltas.items()}, receiver_key: deltas},
        data={"status": "DELIVERED", "exchange_id": params["exchange_id"],
              "sender_npc_id": str(npc_id), "sender_name": sender.name,
              "target_type": target_type, "target_id": str(recipient.pk) if target_type == "PLAYER" else target_id,
              "target_name": target_name, "items": delivered, "gold_amount": gold_amount},
        event_visibility="PARTICIPANTS",
        participant_npc_ids=[str(npc_id)] + ([target_id] if target_type == "NPC" else []),
        player_ids=[str(recipient.pk)] if target_type == "PLAYER" else [])
