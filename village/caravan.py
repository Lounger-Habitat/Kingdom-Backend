"""Daily 11:00–15:00 caravans and Nora's store finance.

Called under the economy/clock transaction. A visit is frozen once per day;
queries never generate stock and exports never consume incoming capacity.
"""
import random

from django.db import transaction
from django.db.models import Q

from game_time.models import GlobalGameTimeState
from inventory.models import NpcGlobalInventory, NpcGlobalInventorySlot
from items.models import Item
from .models import (
    VillageCaravanRequest, VillageCaravanVisit, VillageSimulationConfig,
    VillageStoryFact, VillageStore, VillageStoreAccount, VillageStoreInventorySlot,
)
from .phase3_catalog import IMPORT_WHITELIST, EXPORT_WHITELIST, SAFE_STOCKS, TOOL_ITEM_IDS

ARRIVAL = 660
DEPARTURE = 900
EMERGENCY_LOCATION = "EMERGENCY_TRADE_POST"


def config():
    return VillageSimulationConfig.objects.filter(config_key="default").first()


def item_pool(cfg):
    return sorted(set(cfg.caravan_item_ids)) if cfg else sorted(IMPORT_WHITELIST)


def import_capacity(cfg, route=None):
    route = route or route_readiness()["route"]
    if not cfg:
        return 60 if route == "NORMAL" else 20
    return cfg.caravan_normal_capacity if route == "NORMAL" else cfg.caravan_emergency_capacity


def validate_demand(items, cfg):
    if not isinstance(items, list) or not items or any(type(i) is not int for i in items):
        return "P3_INVALID_PARAMS"
    if not set(items).issubset(item_pool(cfg)) or Item.objects.filter(item_id__in=set(items), is_deleted=False).count() != len(set(items)):
        return "CARAVAN_ITEM_UNAVAILABLE"
    if len(set(items)) > import_capacity(cfg):
        return "CARAVAN_CAPACITY_EXCEEDED"
    return None


def save_demand(items, minute):
    day, clock = divmod(minute, 1440)
    day += clock >= ARRIVAL
    VillageCaravanRequest.objects.update_or_create(
        day_index=day, defaults={"item_ids": sorted(set(items)), "submitted_world_minute": minute},
    )
    return day


@transaction.atomic
def sync_visit(minute):
    # Same global lock used by command starts and economy settlement.
    GlobalGameTimeState.objects.select_for_update().get(pk=1)
    day, clock = divmod(minute, 1440)
    existing = VillageCaravanVisit.objects.filter(pk=day).first()
    if existing or not ARRIVAL <= clock < DEPARTURE:
        return existing
    cfg = config()
    if not cfg or not cfg.village_economy_enabled:
        return None
    fact = VillageStoryFact.objects.filter(arc_id="BROKEN_TRADE_ROUTE_V1").first()
    route = current_route()
    location = "TRADE_ROAD" if route == "NORMAL" else EMERGENCY_LOCATION if route == "EMERGENCY" else ""
    request = VillageCaravanRequest.objects.filter(pk=day).first()
    requested = request.item_ids if request else []
    capacity = import_capacity(cfg, route)
    rng = random.Random(f"{cfg.story_fixed_seed}:caravan:{day}")
    pool = item_pool(cfg)
    valid_pool = list(Item.objects.filter(item_id__in=pool, is_deleted=False).order_by("item_id").values_list("item_id", flat=True))
    # Configuration may have changed since registration. Use the current valid
    # requested kinds, bounded by capacity, and still fill the configured load.
    requested = sorted(set(requested) & set(valid_pool))[:capacity]
    error = None
    stock = {}
    if route != "NONE" and not error and capacity > 0 and valid_pool:
        selected = requested or rng.sample(valid_pool, rng.randint(1, min(len(valid_pool), capacity)))
        amounts = [1] * len(selected)
        for _ in range(capacity - len(selected)):
            amounts[rng.randrange(len(amounts))] += 1
        stock = {str(i): n for i, n in zip(selected, amounts)}
    elif route != "NONE" and not error:
        error = "CARAVAN_SUPPLY_UNAVAILABLE"
    from .phase3_public_economy import _get_published_price
    def price(item_id, side):
        percent = rng.randint(getattr(cfg, f"caravan_{side}_price_min_percent"),
                              getattr(cfg, f"caravan_{side}_price_max_percent"))
        return max(1, (_get_published_price(item_id) * percent + 50) // 100)
    imports = {str(i): price(i, "import") for i in valid_pool}
    export_pool = Item.objects.filter(item_id__in=cfg.caravan_export_item_ids, is_deleted=False).order_by("item_id")
    exports = {str(i): price(i, "export") for i in export_pool.values_list("item_id", flat=True)}
    return VillageCaravanVisit.objects.create(
        day_index=day, route=route, location_code=location, stock=stock,
        initial_stock=dict(stock), import_prices=imports, export_prices=exports,
        export_capacity=cfg.caravan_export_capacity,
        requested_items=requested, error_code=error or "",
    )


def projection(minute):
    day, clock = divmod(minute, 1440)
    visit = VillageCaravanVisit.objects.filter(pk=day).first()
    next_day = day if clock < ARRIVAL else day + 1
    demand = VillageCaravanRequest.objects.filter(pk=next_day).first()
    present = bool(visit and visit.route != "NONE" and ARRIVAL <= clock < DEPARTURE)
    fact = VillageStoryFact.objects.filter(arc_id="BROKEN_TRADE_ROUTE_V1").first()
    route = visit.route if visit else current_route()
    location = visit.location_code if visit else ("TRADE_ROAD" if route == "NORMAL" else
        EMERGENCY_LOCATION if route == "EMERGENCY" else "")
    return {
        "day_index": day, "route": route, "route_frozen": bool(visit),
        "location_code": location,
        "arrival_world_minute": day * 1440 + ARRIVAL,
        "departure_world_minute": day * 1440 + DEPARTURE,
        "next_arrival_world_minute": next_day * 1440 + ARRIVAL,
        "can_import": present and bool(visit.stock) and any(visit.stock.values()),
        "present": present,
        "can_export": present and visit.exported_amount < visit.export_capacity and bool(visit.export_prices),
        "import_total_amount": sum(visit.initial_stock.values()) if present else 0,
        "import_remaining_amount": sum(visit.stock.values()) if present else 0,
        "export_total_amount": visit.export_capacity if present else 0,
        "export_remaining_amount": max(0, visit.export_capacity - visit.exported_amount) if present else 0,
        "reason": (visit.error_code if visit else "") or ("" if present else "CARAVAN_ROUTE_NOT_OPEN" if route == "NONE" else "CARAVAN_CLOSED"),
        "stock": dict(visit.stock) if present else {},
        "initial_stock": dict(visit.initial_stock) if present else {},
        "import_prices": dict(visit.import_prices) if present else {},
        "export_prices": dict(visit.export_prices) if present else {},
        "next_requested_items": demand.item_ids if demand else [],
        "route_readiness": route_readiness(),
    }


def agent_projection(minute):
    """Read-only caravan facts; prices always come from the frozen visit."""
    raw = projection(minute)
    cfg = config()
    present = raw["present"]
    readiness = raw["route_readiness"]
    caravan = {"status": "PRESENT" if present else "CLOSED" if readiness["route_open"] else "ROUTE_NOT_OPEN"}
    if present:
        caravan.update(location_code=raw["location_code"], departure_world_minute=raw["departure_world_minute"])
        for field in ("import_total_amount", "import_remaining_amount", "export_total_amount", "export_remaining_amount"):
            caravan[field] = raw[field]
    else:
        caravan["next_arrival_world_minute"] = raw["next_arrival_world_minute"]
        if readiness["route_open"]:
            caravan["location_code"] = "TRADE_ROAD" if readiness["route"] == "NORMAL" else EMERGENCY_LOCATION
        else:
            caravan["arrival_conditional"] = True
            caravan["opening_cost"] = cfg.caravan_contact_cost if cfg else 100
            caravan["message"] = "需先用STORY_ACT的CONTACT_EMERGENCY_CARAVAN行动个人付费开通路线；所示时间仅为计划窗口。"
    if raw["reason"] not in ("", "CARAVAN_CLOSED", "CARAVAN_ROUTE_NOT_OPEN"):
        caravan["reason"] = raw["reason"]
    imports = {int(key): amount for key, amount in raw["stock"].items()
               if present and amount > 0 and key in raw["import_prices"]}
    exports = {int(key): price for key, price in raw["export_prices"].items()
               if present}
    details = dict(Item.objects.filter(item_id__in=set(imports) | set(exports),
                                      is_deleted=False).values_list("item_id", "item_name"))
    items = []
    for item_id in sorted((set(imports) | set(exports)) & details.keys()):
        row = {"item_id": item_id, "item_name": details[item_id]}
        if item_id in imports:
            row.update(import_available_amount=imports[item_id], import_unit_price=raw["import_prices"][str(item_id)])
        if item_id in exports:
            row["export_unit_price"] = exports[item_id]
        items.append(row)
    return {"caravan": caravan, "items": items}


def request_projection(minute):
    cfg = config()
    day, clock = divmod(minute, 1440)
    target_day = day + (clock >= ARRIVAL)
    demand = VillageCaravanRequest.objects.filter(pk=target_day).first()
    requested = set(demand.item_ids if demand else [])
    rows = Item.objects.filter(item_id__in=item_pool(cfg), is_deleted=False).order_by("item_id")
    status = route_readiness()
    return {"arrival_world_minute": target_day * 1440 + ARRIVAL,
            "import_total_amount": import_capacity(cfg, status["route"]),
            "route_open": status["route_open"], "arrival_conditional": not status["route_open"],
            "requestable_items": [{"item_id": item.item_id, "item_name": item.item_name,
                **({"requested": True} if item.item_id in requested else {})} for item in rows]}


def trade_window(minute, duration=0, completing=False):
    day, clock = divmod(minute, 1440)
    visit = VillageCaravanVisit.objects.filter(pk=day).first()
    if not visit or visit.route == "NONE" or clock < ARRIVAL or clock > DEPARTURE or (clock == DEPARTURE and not completing):
        return "CARAVAN_CLOSED"
    if clock + duration > DEPARTURE:
        return "CARAVAN_TOO_LATE"
    return None


def store_finances(npc_id, store_code=None):
    accounts = VillageStoreAccount.objects.select_related("store").filter(store__operator_npc__agent_id=str(npc_id))
    if store_code is not None:
        accounts = accounts.filter(store_id=store_code)
    account = accounts.first()
    if not account:
        return None
    minute = GlobalGameTimeState.objects.get(pk=1).world_minute
    stock = []
    for slot in VillageStoreInventorySlot.objects.filter(store=account.store, listing__isnull=True, item_amount__gt=0):
        expired = slot.expires_world_minute is not None and slot.expires_world_minute <= minute
        stock.append({"instance_id": str(slot.instance_id), "item_id": slot.item_id,
            "item_amount": slot.item_amount, "reserved_amount": slot.reserved_amount,
            "withdrawable_amount": 0 if expired else max(0, slot.item_amount - slot.reserved_amount),
            "expired": expired, "remaining_uses": slot.remaining_uses,
            "expires_world_minute": slot.expires_world_minute, "item_attributes": slot.item_attributes})
    return {
        "store_code": account.store_id, "balance": account.balance,
        "minimum_operating_balance": account.minimum_operating_balance,
        "withdrawable": max(0, account.balance - account.minimum_operating_balance),
        "account_version": account.account_version,
        "daily_limits": account.daily_limits,
        "limits_until_world_minute": account.limits_until_world_minute,
        "unlisted_stock": stock,
    }


def owned_store(npc_id, code):
    return VillageStore.objects.select_for_update().filter(
        pk=code, operator_npc__agent_id=str(npc_id),
    ).first()


def handle_contact(npc_id, params, minute, request_id):
    from .phase3_asset_service import _fail, _ok
    items = params.get("item_ids")
    error = validate_demand(items, config())
    if error:
        return _fail(error)
    day = save_demand(items, minute)
    status = route_readiness(npc_id)
    names = dict(Item.objects.filter(item_id__in=items).values_list("item_id", "item_name"))
    return _ok(data={"item_ids": sorted(set(items)), "requested_items": [{"item_id": item_id, "item_name": names[item_id]} for item_id in sorted(set(items))],
        "import_total_amount": import_capacity(config(), status["route"]),
        "arrival_world_minute": day * 1440 + ARRIVAL,
        "target_day_index": day, "demand_registered": True, **status,
        "arrival_conditional": not status["route_open"], "route_frozen": False,
        "message": "需求已登记；路线已开通，到访后请查询库存和报价再购买。" if status["route_open"] else
            "需求已登记，但路线未开通；目标11:00仅为计划窗口，登记需求不会让商队自动到达。"})


def current_route():
    from .models import VillageAffair, VillageRouteAttempt
    fact = VillageStoryFact.objects.filter(arc_id="BROKEN_TRADE_ROUTE_V1").first()
    if VillageAffair.objects.filter(pk="BRIDGE_REPAIR", status="COMPLETED").exists() or (fact and fact.bridge_condition == "REPAIRED"):
        return "NORMAL"
    if VillageRouteAttempt.objects.filter(result_code="SUCCESS").exists() or (fact and fact.emergency_caravan_succeeded):
        return "EMERGENCY"
    return "NONE"


def route_readiness(npc_id=None):
    from .models import VillageStoryArc
    fact = VillageStoryFact.objects.filter(arc_id="BROKEN_TRADE_ROUTE_V1").first()
    route = current_route()
    missing = []
    if route == "NONE":
        if str(npc_id) == "10015":
            cfg = config()
            money = NpcGlobalInventory.objects.filter(npc__agent_id="10015").values_list("money", flat=True).first() or 0
            if money < (cfg.caravan_contact_cost if cfg else 100): missing.append("CONTACT_FUNDS_REQUIRED")
        missing.append("FIRST_EMERGENCY_CONTACT_REQUIRED")
    return {"route": route, "route_open": route != "NONE", "missing_prerequisites": missing}


def handle_deposit(npc_id, params, minute, request_id):
    from .phase3_asset_service import _fail, _ok
    from .phase3_direct_trade import _get_buyer_available_money
    amount = params.get("amount")
    if type(amount) is not int or amount <= 0:
        return _fail("P3_INVALID_PARAMS")
    store = owned_store(npc_id, params.get("store_code"))
    if not store:
        return _fail("TRADE_STORE_FORBIDDEN")
    account = VillageStoreAccount.objects.select_for_update().get(store=store)
    inventory = NpcGlobalInventory.objects.select_for_update().get(npc__agent_id=str(npc_id))
    if amount > _get_buyer_available_money(inventory):
        return _fail("P3_INSUFFICIENT_MONEY", data={"required_amount": amount, "available_amount": _get_buyer_available_money(inventory), "payer": str(npc_id)})
    inventory.money -= amount
    inventory.asset_version += 1
    inventory.save(update_fields=["money", "asset_version", "updated_at"])
    account.balance += amount
    account.account_version += 1
    account.save(update_fields=["balance", "account_version", "updated_at"])
    return _ok(asset_versions={str(npc_id): inventory.asset_version}, external_delta=0,
        effects={str(npc_id): {"money": -amount}, "STORE:" + store.pk: {"money": amount}},
        data={"store_code": store.pk, "deposited": amount, "personal_balance": inventory.money,
              "personal_available_money": _get_buyer_available_money(inventory),
              "store_balance": account.balance, "minimum_operating_balance": account.minimum_operating_balance,
              "withdrawable": max(0, account.balance - account.minimum_operating_balance)})


def handle_withdraw_item(npc_id, params, minute, request_id):
    from .phase3_asset_service import _fail, _ok, add_batch, get_available_slot_amount
    item, amount = params.get("item_id"), params.get("amount")
    if type(item) is not int or type(amount) is not int or amount <= 0 or not Item.objects.filter(item_id=item, is_deleted=False).exists():
        return _fail("P3_INVALID_PARAMS")
    store = owned_store(npc_id, params.get("store_code"))
    if not store:
        return _fail("TRADE_STORE_FORBIDDEN")
    inventory = NpcGlobalInventory.objects.select_for_update().get(npc__agent_id=str(npc_id))
    try:
        consumed = consume_unlisted(store, item, amount, minute)
    except ValueError:
        available = sum(row["withdrawable_amount"] for row in store_finances(npc_id)["unlisted_stock"]
                        if row["item_id"] == item)
        return _fail("TRADE_INSUFFICIENT_ITEM", data={"store_code": store.pk, "item_id": item,
                     "required_amount": amount, "available_amount": available})
    batches = []
    for slot, taken in consumed:
        received = add_batch(inventory, item, taken, slot.produced_world_minute,
            slot.expires_world_minute, request_id, remaining_uses=slot.remaining_uses,
            item_attributes=slot.item_attributes)
        batches.append({"source_store_instance_id": str(slot.instance_id),
            "source_slot_instance_id": str(slot.source_slot_instance_id) if slot.source_slot_instance_id else None,
            "source_npc_id": str(slot.source_npc.agent_id) if slot.source_npc_id else None,
            "personal_instance_id": str(received.instance_id), "amount": taken,
            "produced_world_minute": slot.produced_world_minute, "expires_world_minute": slot.expires_world_minute,
            "remaining_uses": slot.remaining_uses})
    store.inventory_version += 1
    store.save(update_fields=["inventory_version", "updated_at"])
    inventory.asset_version += 1
    inventory.save(update_fields=["asset_version", "updated_at"])
    available = sum(row["withdrawable_amount"] for row in store_finances(npc_id)["unlisted_stock"] if row["item_id"] == item)
    personal_available = sum(get_available_slot_amount(slot) for slot in inventory.slots.filter(item_id=item)
        if slot.expires_world_minute is None or slot.expires_world_minute > minute)
    return _ok(asset_versions={str(npc_id): inventory.asset_version}, external_delta=0,
        effects={str(npc_id): {str(item): amount}, "STORE:" + store.pk: {str(item): -amount}},
        data={"store_code": store.pk, "item_id": item, "amount": amount, "batches": batches,
              "remaining_withdrawable": available, "personal_available_quantity": personal_available,
              "store_inventory_version": store.inventory_version})


def handle_withdraw(npc_id, params, minute, request_id):
    from .phase3_asset_service import _fail, _ok
    amount = params.get("amount")
    if type(amount) is not int or amount <= 0:
        return _fail("P3_INVALID_PARAMS")
    store = owned_store(npc_id, params.get("store_code"))
    if not store:
        return _fail("TRADE_STORE_FORBIDDEN")
    account = VillageStoreAccount.objects.select_for_update().get(store=store)
    if params.get("expected_account_version", account.account_version) != account.account_version:
        return _fail("TRADE_STORE_VERSION_CONFLICT")
    available = max(0, account.balance - account.minimum_operating_balance)
    if amount > available:
        return _fail("STORE_RESERVE_REQUIRED", data={"withdrawable": available})
    inventory = NpcGlobalInventory.objects.select_for_update().get(npc__agent_id=str(npc_id))
    account.balance -= amount
    account.account_version += 1
    account.save()
    inventory.money += amount
    inventory.asset_version += 1
    inventory.save(update_fields=["money", "asset_version", "updated_at"])
    return _ok(asset_versions={str(npc_id): inventory.asset_version}, external_delta=0,
               effects={str(npc_id): {"money": amount}, "STORE:" + store.pk: {"money": -amount}},
               data={"store_code": store.pk, "withdrawn": amount, "store_balance": account.balance, "personal_balance": inventory.money,
                     "minimum_operating_balance": account.minimum_operating_balance,
                     "withdrawable": max(0, account.balance-account.minimum_operating_balance)})


def handle_limits(npc_id, params, minute, request_id):
    from .phase3_asset_service import _fail, _ok
    item, limit, until = params.get("item_id"), params.get("daily_limit"), params.get("expires_world_minute")
    if type(item) is not int or type(limit) is not int or limit < 0 or type(until) is not int or until <= minute or not Item.objects.filter(item_id=item, is_deleted=False).exists():
        return _fail("P3_INVALID_PARAMS")
    store = owned_store(npc_id, params.get("store_code"))
    if not store:
        return _fail("TRADE_STORE_FORBIDDEN")
    account = VillageStoreAccount.objects.select_for_update().get(store=store)
    limits = dict(account.daily_limits) if account.limits_until_world_minute and account.limits_until_world_minute > minute else {}
    if limit:
        limits[str(item)] = limit
    else:
        limits.pop(str(item), None)
    account.daily_limits = limits
    account.limits_until_world_minute = until
    account.account_version += 1
    account.save()
    return _ok(data={"store_code": store.pk, "item_id": item, "daily_limit": limit, "rule_removed": limit == 0,
        "daily_limits": limits, "expires_world_minute": until, "expiry_scope": "STORE_RULE_SET",
        "public_limits_unchanged": True})


def available_village_stock(item_id, minute):
    from .phase3_asset_service import get_available_slot_amount
    live = Q(expires_world_minute__isnull=True) | Q(expires_world_minute__gt=minute)
    personal = NpcGlobalInventorySlot.objects.filter(live, item_id=item_id, item_amount__gt=0,
        bag__npc__is_test_only=False, bag__npc__agent_id__in=[10011, 10012, 10013, 10014, 10015])
    stored = VillageStoreInventorySlot.objects.filter(live, item_id=item_id, item_amount__gt=0)
    return sum(get_available_slot_amount(s) for s in personal) + sum(max(0, s.item_amount - s.reserved_amount) for s in stored)


def handle_import(npc_id, params, minute, request_id):
    from .phase3_asset_service import _fail, _ok, add_batch
    from .phase3_direct_trade import _get_buyer_available_money
    # Reject stale store-funded requests instead of silently charging the NPC.
    if "store_code" in params:
        return _fail("P3_INVALID_PARAMS", data={"invalid_field": "store_code"})
    error = trade_window(minute, completing=True)
    if error:
        return _fail(error)
    visit = VillageCaravanVisit.objects.select_for_update().get(pk=minute // 1440)
    rows = params.get("items", [{"item_id": params.get("item_id"), "amount": params.get("amount")}])
    if not isinstance(rows, list) or not rows:
        return _fail("P3_INVALID_PARAMS")
    requested = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"item_id", "amount"} or type(row["item_id"]) is not int or type(row["amount"]) is not int or row["amount"] <= 0:
            return _fail("P3_INVALID_PARAMS")
        key = str(row["item_id"])
        requested[key] = requested.get(key, 0) + row["amount"]
    if any(i not in visit.import_prices for i in requested):
        return _fail("P3_INVALID_PARAMS")
    if any(n > visit.stock.get(i, 0) for i, n in requested.items()):
        return _fail("CARAVAN_INSUFFICIENT_STOCK", data={"remaining_stock": visit.stock})
    items = {str(i.item_id): i for i in Item.objects.filter(item_id__in=requested, is_deleted=False)}
    if set(items) != set(requested):
        return _fail("CARAVAN_ITEM_UNAVAILABLE")
    total = sum(visit.import_prices[i] * n for i, n in requested.items())
    inventory = NpcGlobalInventory.objects.select_for_update().get(npc__agent_id=str(npc_id))
    money = _get_buyer_available_money(inventory)
    if money < total:
        return _fail("P3_INSUFFICIENT_MONEY", data={"required_amount": total, "available_amount": money,
            "payer": str(npc_id)})
    for key, n in requested.items():
        item = items[key]
        uses = item.max_use_count if item.item_id in TOOL_ITEM_IDS else None
        add_batch(inventory, item.item_id, n, minute, None, request_id, remaining_uses=uses)
        visit.stock[key] -= n
    visit.save(update_fields=["stock"])
    inventory.money -= total
    inventory.asset_version += 1
    inventory.save()
    versions = {str(npc_id): inventory.asset_version}
    imports = [{"item_id": int(i), "amount": n, "unit_price": visit.import_prices[i], "total_cost": n * visit.import_prices[i]} for i, n in requested.items()]
    effects = {"nora_balance": inventory.money, "imports": imports, "payer": str(npc_id), "total_cost": total}
    effects[effects["payer"]] = {"money": -total, **{i: n for i, n in requested.items()}}
    if len(imports) == 1:
        effects["import"] = imports[0]
    return _ok(asset_versions=versions, effects=effects, data={**effects, "stock_destination": "PERSONAL_INVENTORY",
        "remaining_stock": {i: visit.stock[i] for i in requested}}, external_delta=-total)


def handle_export(npc_id, params, minute, request_id):
    from .phase3_asset_service import _fail, _ok
    from .phase3_direct_trade import _consume_personal_fefo_available
    error = trade_window(minute, completing=True)
    if error:
        return _fail(error)
    item, amount = params.get("item_id"), params.get("amount")
    if type(item) is not int or type(amount) is not int or amount <= 0:
        return _fail("P3_INVALID_PARAMS")
    visit = VillageCaravanVisit.objects.select_for_update().get(pk=minute // 1440)
    if str(item) not in visit.export_prices or not Item.objects.filter(item_id=item, is_deleted=False).exists():
        return _fail("CARAVAN_ITEM_UNAVAILABLE")
    remaining = max(0, visit.export_capacity - visit.exported_amount)
    if amount > remaining:
        return _fail("CARAVAN_EXPORT_CAPACITY_EXCEEDED", data={"export_remaining_amount": remaining})
    inventory = NpcGlobalInventory.objects.select_for_update().get(npc__agent_id=str(npc_id))
    if params.get("expected_asset_version", inventory.asset_version) != inventory.asset_version:
        return _fail("P3_ASSET_VERSION_CONFLICT")
    surplus = max(0, available_village_stock(item, minute) - SAFE_STOCKS.get(item, 0))
    if amount > surplus:
        return _fail("EXPORT_SAFETY_STOCK_REQUIRED", data={"maximum_amount": surplus})
    try:
        _consume_personal_fefo_available(inventory, item, amount, minute)
    except ValueError:
        return _fail("P3_INSUFFICIENT_ITEM")
    price = visit.export_prices[str(item)]
    income = price * amount
    inventory.money += income
    inventory.asset_version += 1
    inventory.save()
    visit.exported_amount += amount
    visit.save(update_fields=["exported_amount"])
    data = {"export_remaining_amount": remaining - amount, "income_account": str(npc_id), str(npc_id): {"money": income, str(item): -amount}, "export": {"item_id": item, "amount": amount, "unit_price": price, "total_income": income}, "nora_balance": inventory.money}
    return _ok(asset_versions={str(npc_id): inventory.asset_version}, effects=data, data=data, external_delta=income)


def consume_unlisted(store, item_id, amount, minute):
    rows = list(VillageStoreInventorySlot.objects.select_for_update().filter(
        Q(expires_world_minute__isnull=True) | Q(expires_world_minute__gt=minute),
        store=store, listing__isnull=True, item_id=item_id, item_amount__gt=0,
    ).order_by("expires_world_minute", "produced_world_minute", "instance_id"))
    rows.sort(key=lambda s: (s.expires_world_minute is None, s.expires_world_minute or 0))
    if sum(max(0, s.item_amount - s.reserved_amount) for s in rows) < amount:
        raise ValueError("insufficient unlisted store stock")
    consumed = []
    for slot in rows:
        take = min(amount, max(0, slot.item_amount - slot.reserved_amount))
        if take:
            slot.item_amount -= take
            slot.save(update_fields=["item_amount"])
            consumed.append((slot, take))
            amount -= take
        if not amount:
            break
    return consumed
