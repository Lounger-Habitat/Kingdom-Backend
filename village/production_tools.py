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
from player.models import EquipmentDefinition, WeaponMove
from player.equipment import stats_for, definition_for, equipment_attributes, CAPABILITIES, equipment_catalog
from player.progression import ensure_growth, growth_snapshot
from .models import (EconomicLedgerEntry, VillageStore, VillageStoreAccount,
                     VillageStoreInventorySlot, VillageStoreListing)
from .professional_catalog import PUMPKIN_SEED

AXE_MILESTONE = "starter_axe_claimed"
STOCK_ITEMS = (1051, 29301, 29302, 29303, 29304, 29305, 4001, 4106, 4110, 4112, PUMPKIN_SEED)


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
    return True


def _definition(slot):
    return definition_for(slot)


def resolve_tool(player, bag, capability, instance_id=None, minimum_tier=1, *, minute, levels=None, context=None):
    """Pure validation. The caller may already hold world/bag locks; no setup writes."""
    if bag.player_id != player.pk or bag.character_id != player.username:
        reject("TOOL_NOT_OWNED", "只能使用本人主背包中的工具。")
    if not bag.pk:
        reject("TOOL_MISSING", "背包中没有指定用途的工具。")
    if context is None:
        definitions = {d.item_id: d for d in EquipmentDefinition.objects.filter(enabled=True, item__is_deleted=False)
                       if CAPABILITIES[d.category] == capability}
        rows = InventorySlot.objects.filter(bag=bag, item_id__in=definitions, item_amount__gt=0).order_by("slot_index")
        if connection_in_transaction():
            rows = rows.select_for_update()
        if instance_id is not None:
            rows = rows.filter(instance_id=_uuid(instance_id))
        rows = list(rows)
        definitions = {s.pk: _definition(s) for s in rows}
    else:
        # Projection callers share this request's already-loaded equipment and inventory.
        if "profiles" not in context:
            context["profiles"] = {row.item_id: row for row in EquipmentDefinition.objects.select_related("item")}
        item_ids = {item_id for item_id, d in context["profiles"].items()
                    if d.enabled and not d.item.is_deleted and CAPABILITIES[d.category] == capability}
        instance = _uuid(instance_id) if instance_id is not None else None
        rows = [s for s in context["slots"] if s.bag_id == bag.pk and s.item_id in item_ids and s.item_amount > 0
                and (instance is None or s.instance_id == instance)]
        definitions = {s.pk: stats_for(s, catalog=context["catalog"]) for s in rows}
        levels = context["levels"] if levels is None else levels
    if not rows:
        reject("TOOL_MISSING", "背包中没有指定用途的工具。")
    # Both paths use the same gates; real actions retain fresh queries and locks above.
    stats = definitions if context is not None else {pk: getattr(d, "equipment_stats", None) for pk, d in definitions.items()}
    qualified = [s for s in rows if stats[s.pk] and stats[s.pk]["tier"] >= minimum_tier]
    if not qualified:
        reject("TOOL_TIER_REQUIRED", "该资源需要更高阶工具。")
    permitted = [s for s in qualified if not stats[s.pk]["required_skill"] or
                 (levels.get(stats[s.pk]["required_skill"], 1) if levels is not None
                  else _level(player, stats[s.pk]["required_skill"])) >= stats[s.pk]["required_skill_level"]]
    if not permitted:
        reject("SKILL_LEVEL_REQUIRED", "技能等级未达到工具使用要求。")
    usable = [s for s in permitted if _fresh(s, minute) and type(s.remaining_uses) is int
              and 0 < s.remaining_uses <= stats[s.pk]["max_durability"]]
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
        from inventory.storage import add_inventory_items, attributes_from_slot
        slot = add_inventory_items(slot.bag, slot.item_id, 1, attributes=attributes_from_slot(slot))[0][0]
    slot.remaining_uses -= 1
    slot.save(update_fields=["remaining_uses"])
    return dict(item_id=slot.item_id, instance_id=str(slot.instance_id), equipment_level=slot.equipment_level,
        equipment_stats=stats_for(slot), uses=1, remaining_uses=slot.remaining_uses)


def _single_in_place(slot):
    if slot.item_amount > 1:
        from inventory.storage import add_inventory_items, attributes_from_slot
        rest_amount = slot.item_amount - 1
        slot.item_amount = 1
        slot.save(update_fields=["item_amount"])
        add_inventory_items(slot.bag, slot.item_id, rest_amount, attributes=attributes_from_slot(slot))


def _material_slots(bag, item_id, minute):
    return [s for s in bag.slots.filter(item_id=item_id, item_amount__gt=0).order_by("slot_index") if _fresh(s, minute)]


def _store():
    from .phase3_direct_trade import _TRADABLE_STORE_STATUSES
    return VillageStore.objects.filter(pk="GENERAL_STORE", operator_npc__agent_id="10015",
                                        status__in=_TRADABLE_STORE_STATUSES).first()


def tool_attributes(definition):
    return equipment_attributes(getattr(definition, "equipment_stats", None))


def _quote(player, bag, slot, operation, minute, *, context=None):
    catalog = context["catalog"] if context is not None else None
    current = catalog.get((slot.item_id, slot.equipment_level)) if catalog is not None else stats_for(slot)
    if operation == "repair" and current and current["category"] in {"sword", "spear", "bow"} and not current["repair_gold"] and not current["repair_materials"]:
        from .player_hunting import weapon_repair_quote
        return weapon_repair_quote(player, bag, slot, minute, context=context)
    target_level = slot.equipment_level + (operation == "upgrade")
    target = (catalog.get((slot.item_id, target_level)) if catalog is not None else stats_for(slot, level=target_level)) if target_level <= 5 else None
    rule = target if operation == "upgrade" else current
    requirements = rule["upgrade_materials" if operation == "upgrade" else "repair_materials"] if rule else []
    gold = rule["upgrade_gold" if operation == "upgrade" else "repair_gold"] if rule else 0
    materials = []
    for row in requirements:
        item = context["items"].get(row["item_id"]) if context is not None else Item.objects.filter(pk=row["item_id"], is_deleted=False).first()
        materials.append(dict(item_id=row["item_id"], item_name=item.item_name if item else str(row["item_id"]),
            quantity=row["quantity"], available_quantity=context["materials"].get(row["item_id"], 0) if context is not None else sum(s.item_amount for s in _material_slots(bag, row["item_id"], minute)), configured=bool(item)))
    skill_level = ((context["levels"].get(current["required_skill"], 1) if context is not None else _level(player, current["required_skill"])) if current and current["required_skill"] else 1)
    account = context["account"] if context is not None else VillageStoreAccount.objects.filter(store_id="GENERAL_STORE").first()
    reason = ("TOOL_UNAVAILABLE" if not current else "NO_UPGRADE" if operation == "upgrade" and not target else
        "TOOL_DURABILITY_UNKNOWN" if type(slot.remaining_uses) is not int or not 0 <= slot.remaining_uses <= current["max_durability"] else
        "TOOL_EXPIRED" if not _fresh(slot, minute) else
        "TOOL_ALREADY_FULL" if operation == "repair" and slot.remaining_uses == current["max_durability"] else
        "SKILL_LEVEL_REQUIRED" if skill_level < rule["required_skill_level"] else
        "STORE_UNAVAILABLE" if not account else "MATERIAL_UNAVAILABLE" if any(not m["configured"] for m in materials) else
        "INSUFFICIENT_MONEY" if bag.money < gold else "INSUFFICIENT_MATERIALS" if any(m["available_quantity"] < m["quantity"] for m in materials) else "")
    payload = dict(operation=operation, instance_id=str(slot.instance_id), item_id=slot.item_id, amount=slot.item_amount,
        remaining=slot.remaining_uses, money=bag.money, current=current, target=target, materials=materials,
        skill_level=skill_level, account_id=account.pk if account else None)
    return dict(available=not reason, reason=reason,
        quote_hash=hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
        gold_cost=gold, materials=materials, material_item_id=materials[0]["item_id"] if materials else None,
        material_quantity=materials[0]["quantity"] if materials else 0, target_item_id=slot.item_id,
        current_equipment_level=slot.equipment_level, target_equipment_level=target_level if target else slot.equipment_level,
        required_level=rule["required_skill_level"] if rule else 1,
        target_item_name=current["item_name"] if current else None,
        target_maximum_uses=target["max_durability"] if target else current["max_durability"] if current else None,
        current_attributes=equipment_attributes(current), target_attributes=equipment_attributes(target if operation == "upgrade" else current),
        location_code=None, action="upgrade_tool" if operation == "upgrade" else "repair_tool")


def tools_overview(player, bag, minute, read_only=False, *, levels=None, slots=None, capability=None, include_shop=True, context=None):
    context = {} if context is None else context
    levels = context.get("levels") if levels is None else levels
    if levels is None:
        levels = {s["code"]: s["level"] for s in growth_snapshot(player, read_only=True, compact=True)["skills"]}
    slots = context.get("slots") if slots is None else slots
    slots = (list(bag.slots.filter(item_amount__gt=0).order_by("slot_index")) if bag.pk else []) if slots is None else sorted(slots, key=lambda s: s.slot_index)
    catalog = context["catalog"] if "catalog" in context else equipment_catalog()
    context.update(catalog=catalog, levels=levels, slots=slots)
    # Farm/gathering selectors need usable instances, not service quotes or store shelves.
    if capability is None:
        materials = {}
        for source in slots:
            if _fresh(source, minute):
                materials[source.item_id] = materials.get(source.item_id, 0) + source.item_amount
        context.update(materials=materials, items=Item.objects.filter(is_deleted=False).in_bulk(),
            account=VillageStoreAccount.objects.filter(store_id="GENERAL_STORE").first())
    tools = []
    moves = context["moves"] if "moves" in context else list(WeaponMove.objects.all())
    context["moves"] = moves
    for slot in slots:
        stats = catalog.get((slot.item_id, slot.equipment_level))
        if not stats or capability is not None and stats["capability"] != capability:
            continue
        service = {}
        if capability is None:
            service = dict(repair=_quote(player, bag, slot, "repair", minute, context=context),
                           upgrade=_quote(player, bag, slot, "upgrade", minute, context=context))
            if read_only:
                for quote in service.values():
                    quote.update(available=False, reason="MODE_READ_ONLY")
        attributes = equipment_attributes(stats)
        for move in moves:
            if move.category == stats["category"]:
                value = f"×{move.multiplier:g}" if move.category != "bow" else f"×{move.minimum_multiplier:g}～{move.multiplier:g}"
                attributes.append(dict(code="move:" + move.code, label=move.name, value=value))
        tools.append(dict(instance_id=str(slot.instance_id), item_id=slot.item_id, item_name=stats["item_name"],
            equipment_level=stats["level"], level=stats["level"], category=stats["category"], advanced=stats["advanced"],
            gathering_time_reduction=stats["gathering_time_reduction"], gathering_quality_bonus=stats["quality_bonus_chance"],
            cooking_batches=stats["cooking_batches"], attributes=attributes,
            capability=stats["capability"], tier=stats["tier"], remaining_uses=slot.remaining_uses,
            maximum_uses=stats["max_durability"], required_level=stats["required_skill_level"],
            usable=_fresh(slot, minute) and type(slot.remaining_uses) is int and 0 < slot.remaining_uses <= stats["max_durability"]
                and (not stats["required_skill"] or levels.get(stats["required_skill"], 1) >= stats["required_skill_level"]),
            **service))
    if capability is not None:
        return dict(tools=tools)
    result = dict(tools=tools, can_claim_starter_axe=False,
        claim_reason="MODE_READ_ONLY" if read_only else "STARTER_EQUIPMENT_AUTOMATIC")
    if include_shop:
        result["shop"] = [dict(item_id=l.item_id, item_name=l.item_name, listing_id=str(l.pk), unit_price=l.unit_price, stock=l.stock)
                          for l in _shop_rows(minute)]
    return result


def _shop_rows(minute):
    from .lucy_restaurant import store_available
    listings = list(VillageStoreListing.objects.filter(store_id="GENERAL_STORE", item_id__in=[*STOCK_ITEMS, *EquipmentDefinition.objects.filter(enabled=True).values_list("item_id", flat=True)], status="ACTIVE").prefetch_related("inventory_slots"))
    items = Item.objects.filter(pk__in=[listing.item_id for listing in listings], is_deleted=False).in_bulk()
    for listing in listings:
        item = items.get(listing.item_id)
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
        reject("STARTER_EQUIPMENT_AUTOMATIC", "初始斧头已随背包发放，无需领取。")
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
        for requirement in quote["materials"]:
            remaining = requirement["quantity"]
            for material in _material_slots(bag, requirement["item_id"], clock.world_minute):
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
        if action == "upgrade_tool":
            slot.equipment_level = quote["target_equipment_level"]
            slot.save(update_fields=["equipment_level"])
        else:
            slot.remaining_uses = stats_for(slot)["max_durability"]
            slot.save(update_fields=["remaining_uses"])
        if quote["gold_cost"]:
            bag.money -= quote["gold_cost"]
            bag._money_change_source = action
            bag.save(update_fields=["money"])
            account.balance += quote["gold_cost"]
            account.account_version += 1
            account.save(update_fields=["balance", "account_version", "updated_at"])
        result = dict(instance_id=str(slot.instance_id), item_id=slot.item_id, previous_item_id=original_item,
            equipment_level=slot.equipment_level, remaining_uses=slot.remaining_uses, spent_gold=quote["gold_cost"],
            materials_consumed=[dict(item_id=m["item_id"], quantity=m["quantity"]) for m in quote["materials"]])
        changes = {f"PLAYER:{player.pk}": {"money": -quote["gold_cost"], **{str(m["item_id"]): -m["quantity"] for m in quote["materials"]}},
            "STORE:GENERAL_STORE": {"money": quote["gold_cost"]}, "tool": result}
    EconomicLedgerEntry.objects.create(request_id=uuid.uuid5(uuid.NAMESPACE_URL, f"production-tools:{player.pk}:{rid}"),
        entry_type=action.upper(), world_minute=clock.world_minute, changes=deepcopy(changes))
    return result


def _initial_tool_listing(store, item_id, price, remaining_uses):
    """Join only the known legacy/current initial shelves, preserving stock and history."""
    legacy = {4101: "kingdom:g1:gathering-tool:initial-stock:v1",
              4001: "village-reset:general-store-listing:4001"}.get(item_id)
    if legacy is None:
        return None
    markers = [uuid.uuid5(uuid.NAMESPACE_URL, legacy),
               uuid.uuid5(uuid.NAMESPACE_URL, f"kingdom:g2:production-stock:{item_id}:v1")]
    shelves = VillageStoreListing.objects.select_for_update().filter(store=store, item_id=item_id,
        created_request_id__in=markers, status="ACTIVE", public_meal_code="", quality=3,
        price_mode="ORIGINAL", unit_price=price).prefetch_related("inventory_slots__usage")
    compatible = []
    for shelf in shelves:
        if any(slot.reserved_amount or slot.item_id != item_id or slot.quality != 3
               or slot.equipment_level != 1 or slot.remaining_uses != remaining_uses
               for slot in shelf.inventory_slots.all()):
            continue
        compatible.append(shelf)
    if not compatible:
        return None
    compatible.sort(key=lambda shelf: shelf.created_request_id != markers[0])
    target = compatible[0]
    for duplicate in compatible[1:]:
        VillageStoreInventorySlot.objects.filter(listing=duplicate).update(listing=target)
        duplicate.status = "DELISTED"
        duplicate.save(update_fields=["status", "updated_at"])
    if len(compatible) > 1:
        store.inventory_version += 1
        store.save(update_fields=["inventory_version", "updated_at"])
    return target


@transaction.atomic
def ensure_production_catalog(minute):
    """Trusted finite initial supply. The ledger marker survives listing removal."""
    GlobalGameTimeState.objects.select_for_update().get(pk=1)
    store = _store()
    if not store or not VillageStoreAccount.objects.filter(store=store).exists():
        return []
    existing = {item.pk: _initial_tool_listing(store, item.pk, max(1, item.item_price), item.max_use_count)
                for item in Item.objects.filter(pk__in=(4001, 4101), is_deleted=False)}
    installed = []
    for item_id in dict.fromkeys([*STOCK_ITEMS, *EquipmentDefinition.objects.filter(enabled=True).values_list("item_id", flat=True)]):
        marker = uuid.uuid5(uuid.NAMESPACE_URL, "kingdom:g3:cooking-stock:4106:v1" if item_id == 4106
                            else "kingdom:g6:pumpkin-stock:29307:v1" if item_id == PUMPKIN_SEED
                            else f"kingdom:g2:production-stock:{item_id}:v1")
        if EconomicLedgerEntry.objects.filter(request_id=marker).exists():
            continue
        item = Item.objects.filter(pk=item_id, is_deleted=False).first()
        if not item:
            continue
        listing = existing.get(item_id) or VillageStoreListing.objects.create(listing_id=uuid.uuid5(marker, "listing"), store=store,
            item_id=item_id, unit_price=max(1, item.item_price), price_mode="ORIGINAL", status="ACTIVE",
            listed_by_npc=store.operator_npc, created_request_id=marker, listed_world_minute=minute)
        is_tool = EquipmentDefinition.objects.filter(item_id=item_id, enabled=True).exists()
        count = 20 if is_tool else 30
        for index in range(count if is_tool else 1):
            VillageStoreInventorySlot.objects.create(instance_id=uuid.uuid5(marker, str(index)), store=store,
                listing=listing, item_id=item_id, item_amount=1 if is_tool else count,
                remaining_uses=item.max_use_count if is_tool else None, quality=3,
                produced_world_minute=minute, common_attributes={"initial_stock_code": "G3_COOKING" if item_id == 4106 else "G6_PRODUCTION" if item_id == PUMPKIN_SEED else "G2_PRODUCTION"})
        EconomicLedgerEntry.objects.create(request_id=marker, entry_type="G3_INITIAL_STOCK" if item_id == 4106 else "G6_INITIAL_STOCK" if item_id == PUMPKIN_SEED else "G2_INITIAL_STOCK", world_minute=minute,
            changes={"STORE:GENERAL_STORE": {str(item_id): count}, "source": "G3_FINITE_COOKING_STOCK" if item_id == 4106 else "G6_FINITE_PRODUCTION_STOCK" if item_id == PUMPKIN_SEED else "G2_FINITE_PRODUCTION_STOCK"})
        installed.append(str(listing.pk))
    if installed:
        store.inventory_version += 1
        store.save(update_fields=["inventory_version", "updated_at"])
    return installed
