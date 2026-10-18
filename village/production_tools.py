"""Real personal production tools, deterministic service quotes and finite stock."""
from copy import deepcopy
import hashlib
import json
import uuid

from django.db import models, transaction
from game_time.models import GlobalGameTimeState
from inventory.components import copy_batch_attributes
from inventory.models import InventorySlot
from items.models import Item
from player.models import ItemGameplayDefinition, PlayerMilestoneProgress
from player.progression import ensure_growth, growth_snapshot
from .models import (EconomicLedgerEntry, VillageStore, VillageStoreAccount,
                     VillageStoreInventorySlot, VillageStoreListing)

UPGRADES = {4101: (4102, 6), 4001: (4103, 8), 4104: (4105, 10), 4106: (4107, 8)}
AXE_MILESTONE = "starter_axe_claimed"
STOCK_ITEMS = (1051, 29301, 29302, 29303, 29304, 29305, 4001, 4106)


class ToolError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def reject(code, message):
    raise ToolError(code, message)


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        reject("INVALID_PARAMS", "工具实例标识无效。")


def _level(player, code):
    return next(s["level"] for s in growth_snapshot(player, read_only=True)["skills"] if s["code"] == code)


def _fresh(slot, minute):
    return not slot.freshness_unknown and (slot.expires_world_minute is None or slot.expires_world_minute > minute)


def _definition(slot):
    return ItemGameplayDefinition.objects.select_related("item").filter(item_id=slot.item_id, item__is_deleted=False).first()


def resolve_tool(player, bag, capability, instance_id=None, minimum_tier=1, *, minute):
    """Pure validation. The caller may already hold world/bag locks; no setup writes."""
    if bag.player_id != player.pk or bag.character_id != player.username:
        reject("TOOL_NOT_OWNED", "只能使用本人主背包中的工具。")
    definitions = {d.item_id: d for d in ItemGameplayDefinition.objects.select_related("item").filter(
        capability_code=capability, item__is_deleted=False)}
    rows = InventorySlot.objects.filter(bag=bag, item_id__in=definitions, item_amount__gt=0).order_by("slot_index")
    if connection_in_transaction():
        rows = rows.select_for_update()
    if instance_id is not None:
        rows = rows.filter(instance_id=_uuid(instance_id))
    rows = list(rows)
    if not rows:
        reject("TOOL_MISSING", "背包中没有指定用途的工具。")
    qualified = [s for s in rows if definitions[s.item_id].tier >= minimum_tier]
    if not qualified:
        reject("TOOL_TIER_REQUIRED", "该资源需要更高阶工具。")
    permitted = [s for s in qualified if not definitions[s.item_id].required_skill or
                 _level(player, definitions[s.item_id].required_skill) >= definitions[s.item_id].required_level]
    if not permitted:
        reject("SKILL_LEVEL_REQUIRED", "技能等级未达到工具使用要求。")
    usable = [s for s in permitted if _fresh(s, minute) and type(s.remaining_uses) is int
              and 0 < s.remaining_uses <= definitions[s.item_id].item.max_use_count]
    if not usable:
        reject("TOOL_DURABILITY_UNKNOWN" if any(s.remaining_uses is None for s in permitted) else "TOOL_BROKEN",
               "工具已损坏、过期或耐久信息无效。")
    return min(usable, key=lambda s: (s.remaining_uses, s.slot_index))


def connection_in_transaction():
    from django.db import connection
    return connection.in_atomic_block


def _index(bag):
    value = bag.slots.aggregate(n=models.Max("slot_index"))["n"]
    return 0 if value is None else value + 1


@transaction.atomic
def wear_tool(slot):
    """One successful use; leave the remaining stack completely unchanged."""
    if slot.item_amount <= 0 or type(slot.remaining_uses) is not int or slot.remaining_uses <= 0:
        reject("TOOL_BROKEN", "工具没有可用耐久。")
    if slot.item_amount > 1:
        slot.item_amount -= 1
        slot.save(update_fields=["item_amount"])
        used = InventorySlot(bag=slot.bag, slot_index=_index(slot.bag), item_id=slot.item_id,
                             item_amount=1, item_bag_name=slot.item_bag_name)
        copy_batch_attributes(slot, used)
        used.save()
        slot = used
    slot.remaining_uses -= 1
    slot.save(update_fields=["remaining_uses"])
    return dict(item_id=slot.item_id, instance_id=str(slot.instance_id), uses=1, remaining_uses=slot.remaining_uses)


def _single_in_place(slot):
    if slot.item_amount > 1:
        rest = InventorySlot(bag=slot.bag, slot_index=_index(slot.bag), item_id=slot.item_id,
            item_amount=slot.item_amount - 1, item_bag_name=slot.item_bag_name)
        copy_batch_attributes(slot, rest)
        rest.save()
        slot.item_amount = 1
        slot.save(update_fields=["item_amount"])


def _material_slots(bag, item_id, minute):
    return [s for s in bag.slots.filter(item_id=item_id, item_amount__gt=0).order_by("slot_index") if _fresh(s, minute)]


def _store():
    from .phase3_direct_trade import _TRADABLE_STORE_STATUSES
    return VillageStore.objects.filter(pk="GENERAL_STORE", operator_npc__agent_id="10015",
                                        status__in=_TRADABLE_STORE_STATUSES).first()


def _quote(player, bag, slot, operation, minute):
    definition = _definition(slot)
    target_id, material_quantity = UPGRADES.get(slot.item_id, (None, 0))
    tier = definition.tier if definition else 0
    gold = 100 if operation == "upgrade" else 5 if tier == 1 else 15
    material = 1002 if operation == "upgrade" or tier == 1 else 29306
    material_quantity = material_quantity if operation == "upgrade" else 1
    target = Item.objects.filter(pk=target_id, is_deleted=False).first() if target_id else None
    required = 4 if operation == "upgrade" else definition.required_level if definition else 1
    skill = definition.required_skill if definition else ""
    level = _level(player, skill) if skill else 1
    available_material = sum(s.item_amount for s in _material_slots(bag, material, minute))
    store = _store()
    account = VillageStoreAccount.objects.filter(store=store).first() if store else None
    reason = ("TOOL_UNAVAILABLE" if not definition or definition.capability_code not in {"farming", "forestry", "targeted_gathering", "cooking"}
        or definition.tier not in (1, 2) else "NO_UPGRADE" if operation == "upgrade" and not target else
        "TOOL_DURABILITY_UNKNOWN" if type(slot.remaining_uses) is not int or not 0 <= slot.remaining_uses <= definition.item.max_use_count else
        "TOOL_EXPIRED" if not _fresh(slot, minute) else "TOOL_ALREADY_FULL" if operation == "repair" and slot.remaining_uses == definition.item.max_use_count else
        "SKILL_LEVEL_REQUIRED" if level < required else "STORE_UNAVAILABLE" if not account else
        "INSUFFICIENT_MONEY" if bag.money < gold else "INSUFFICIENT_MATERIALS" if available_material < material_quantity else "")
    payload = dict(operation=operation, instance_id=str(slot.instance_id), item_id=slot.item_id,
        amount=slot.item_amount, remaining=slot.remaining_uses, money=bag.money, material_available=available_material,
        gold=gold, material=material, material_quantity=material_quantity, target_id=target_id,
        level=level, account_id=account.pk if account else None, definition_version=definition.rules_version if definition else 0,
        maximum_uses=definition.item.max_use_count if definition else None,
        target_maximum_uses=target.max_use_count if target else None)
    return dict(available=not reason, reason=reason, quote_hash=hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
        gold_cost=gold, material_item_id=material, material_quantity=material_quantity,
        target_item_id=target_id if operation == "upgrade" else slot.item_id, required_level=required)


def tools_overview(player, bag, minute, read_only=False):
    definitions = {d.item_id: d for d in ItemGameplayDefinition.objects.select_related("item").filter(
        capability_code__in=["farming", "forestry", "targeted_gathering", "cooking"], item__is_deleted=False)}
    tools = []
    for slot in bag.slots.filter(item_id__in=definitions, item_amount__gt=0).order_by("slot_index") if bag.pk else []:
        definition = definitions[slot.item_id]
        repair, upgrade = _quote(player, bag, slot, "repair", minute), _quote(player, bag, slot, "upgrade", minute)
        if read_only:
            for quote in (repair, upgrade):
                quote.update(available=False, reason="MODE_READ_ONLY")
        tools.append(dict(instance_id=str(slot.instance_id), item_id=slot.item_id, item_name=definition.item.item_name,
            capability=definition.capability_code, tier=definition.tier, remaining_uses=slot.remaining_uses,
            maximum_uses=definition.item.max_use_count, required_level=definition.required_level,
            usable=_fresh(slot, minute) and type(slot.remaining_uses) is int
                and 0 < slot.remaining_uses <= definition.item.max_use_count
                and (not definition.required_skill or _level(player, definition.required_skill) >= definition.required_level),
            repair=repair, upgrade=upgrade))
    claimed = PlayerMilestoneProgress.objects.filter(player=player, code=AXE_MILESTONE).exists()
    axe = Item.objects.filter(pk=4104, is_deleted=False).exists()
    return dict(tools=tools, can_claim_starter_axe=axe and not claimed and not read_only,
        claim_reason="MODE_READ_ONLY" if read_only else "ALREADY_CLAIMED" if claimed else "AXE_UNAVAILABLE" if not axe else "",
        shop=[dict(item_id=l.item_id, item_name=l.item_name, listing_id=str(l.pk), unit_price=l.unit_price, stock=l.stock)
              for l in _shop_rows(minute)])


def _shop_rows(minute):
    from .lucy_restaurant import store_available
    for listing in VillageStoreListing.objects.filter(store_id="GENERAL_STORE", item_id__in=STOCK_ITEMS, status="ACTIVE"):
        item = Item.objects.filter(pk=listing.item_id, is_deleted=False).first()
        if not item:
            continue
        listing.item_name = item.item_name
        listing.stock = sum(store_available(s, minute) for s in listing.inventory_slots.all())
        yield listing


@transaction.atomic
def execute_tool_action(player, bag, action, params, rid, clock):
    GlobalGameTimeState.objects.select_for_update().get(pk=1)
    if bag.player_id != player.pk or bag.character_id != player.username:
        reject("TOOL_NOT_OWNED", "只能整备本人主背包中的工具。")
    if action == "claim_starter_axe":
        if params:
            reject("INVALID_PARAMS", "领取基础斧不需要参数。")
        state = ensure_growth(player)
        if PlayerMilestoneProgress.objects.filter(player=player, code=AXE_MILESTONE).exists():
            reject("ALREADY_CLAIMED", "每人只能免费领取一次基础斧，可维修已有工具。")
        item = Item.objects.filter(pk=4104, is_deleted=False).first()
        if not item:
            reject("AXE_UNAVAILABLE", "基础斧目录尚未就绪。")
        slot = InventorySlot.objects.create(bag=bag, slot_index=_index(bag), item_id=4104, item_amount=1,
            remaining_uses=item.max_use_count, quality=3, produced_world_minute=clock.world_minute, source_request_id=rid)
        PlayerMilestoneProgress.objects.create(player=player, code=AXE_MILESTONE, title="领取基础斧",
            source_type="claim_starter_axe", source_id=str(rid))
        state.revision += 1
        state.save(update_fields=["revision"])
        result = dict(item_id=4104, instance_id=str(slot.instance_id), quantity=1, remaining_uses=slot.remaining_uses)
        changes = {f"PLAYER:{player.pk}": {"4104": 1}, "source": "G2_ONCE_PER_PLAYER"}
    else:
        if action not in ("repair_tool", "upgrade_tool") or set(params) != {"instance_id", "expected_quote"}:
            reject("INVALID_PARAMS", "工具操作参数无效。")
        slot = InventorySlot.objects.select_for_update().filter(bag=bag, instance_id=_uuid(params["instance_id"]), item_amount__gt=0).first()
        if not slot:
            reject("TOOL_MISSING", "本人背包中没有该工具实例。")
        quote = _quote(player, bag, slot, "repair" if action == "repair_tool" else "upgrade", clock.world_minute)
        if not isinstance(params["expected_quote"], str) or params["expected_quote"] != quote["quote_hash"]:
            reject("QUOTE_CHANGED", "工具或材料、资金条件已变化，请刷新报价。")
        if not quote["available"]:
            reject(quote["reason"], "当前条件不满足工具维修或升级。")
        account = VillageStoreAccount.objects.select_for_update().get(store_id="GENERAL_STORE")
        remaining = quote["material_quantity"]
        for material in _material_slots(bag, quote["material_item_id"], clock.world_minute):
            count = min(material.item_amount, remaining)
            material.item_amount -= count
            material.save(update_fields=["item_amount"])
            remaining -= count
            if not remaining:
                break
        if remaining:
            reject("INSUFFICIENT_MATERIALS", "维修材料已变化。")
        original_item = slot.item_id
        _single_in_place(slot)
        slot.item_id = quote["target_item_id"]
        slot.remaining_uses = Item.objects.get(pk=slot.item_id).max_use_count
        slot.save(update_fields=["item_id", "remaining_uses"])
        bag.money -= quote["gold_cost"]
        bag._money_change_source = action
        bag.save(update_fields=["money"])
        account.balance += quote["gold_cost"]
        account.account_version += 1
        account.save(update_fields=["balance", "account_version", "updated_at"])
        result = dict(instance_id=str(slot.instance_id), item_id=slot.item_id, previous_item_id=original_item,
            remaining_uses=slot.remaining_uses, spent_gold=quote["gold_cost"],
            materials_consumed=[dict(item_id=quote["material_item_id"], quantity=quote["material_quantity"])])
        changes = {f"PLAYER:{player.pk}": {"money": -quote["gold_cost"], str(quote["material_item_id"]): -quote["material_quantity"]},
            "STORE:GENERAL_STORE": {"money": quote["gold_cost"]}, "tool": result}
    EconomicLedgerEntry.objects.create(request_id=uuid.uuid5(uuid.NAMESPACE_URL, f"production-tools:{player.pk}:{rid}"),
        entry_type=action.upper(), world_minute=clock.world_minute, changes=deepcopy(changes))
    return result


@transaction.atomic
def ensure_production_catalog(minute):
    """Trusted finite initial supply. The ledger marker survives listing removal."""
    GlobalGameTimeState.objects.select_for_update().get(pk=1)
    store = _store()
    if not store or not VillageStoreAccount.objects.filter(store=store).exists():
        return []
    installed = []
    for item_id in STOCK_ITEMS:
        marker = uuid.uuid5(uuid.NAMESPACE_URL, "kingdom:g3:cooking-stock:4106:v1" if item_id == 4106
                            else f"kingdom:g2:production-stock:{item_id}:v1")
        if EconomicLedgerEntry.objects.filter(request_id=marker).exists():
            continue
        item = Item.objects.filter(pk=item_id, is_deleted=False).first()
        if not item:
            continue
        listing = VillageStoreListing.objects.create(listing_id=uuid.uuid5(marker, "listing"), store=store,
            item_id=item_id, unit_price=max(1, item.item_price), price_mode="ORIGINAL", status="ACTIVE",
            listed_by_npc=store.operator_npc, created_request_id=marker, listed_world_minute=minute)
        is_tool = item_id in (4001, 4106)
        count = 20 if is_tool else 30
        for index in range(count if is_tool else 1):
            VillageStoreInventorySlot.objects.create(instance_id=uuid.uuid5(marker, str(index)), store=store,
                listing=listing, item_id=item_id, item_amount=1 if is_tool else count,
                remaining_uses=item.max_use_count if is_tool else None, quality=3,
                produced_world_minute=minute, common_attributes={"initial_stock_code": "G3_COOKING" if item_id == 4106 else "G2_PRODUCTION"})
        EconomicLedgerEntry.objects.create(request_id=marker, entry_type="G3_INITIAL_STOCK" if item_id == 4106 else "G2_INITIAL_STOCK", world_minute=minute,
            changes={"STORE:GENERAL_STORE": {str(item_id): count}, "source": "G3_FINITE_COOKING_STOCK" if item_id == 4106 else "G2_FINITE_PRODUCTION_STOCK"})
        installed.append(str(listing.pk))
    if installed:
        store.inventory_version += 1
        store.save(update_fields=["inventory_version", "updated_at"])
    return installed
