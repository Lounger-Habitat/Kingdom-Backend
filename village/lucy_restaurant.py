"""Restaurant batch prices and cash-funded public meals, without authorization."""
import copy
import uuid

from django.db import models
from inventory.models import NpcGlobalInventory
from .models import (VillageStore, VillageStoreAccount, VillageStoreListing, VillageStoreInventorySlot,
                     VillageAccount, VillageCommunityMealDailyUsage, VillageStoryFact)
from .phase3_asset_service import _ok, _fail, add_batch, BATCH_ITEM_ATTRIBUTES, get_available_slot_amount
from .lucy_catalog import PUBLIC_MEALS
from .lucy_cooking import fixed_price, _id
from .public_governance import integer
from .phase5_contract import FORMAL_NPC_IDS


def validate_meal_terms(value, minute, expiry, budget):
    # Legacy governance record validation only; public meals no longer use these terms.
    if not isinstance(value, dict) or set(value) - {"executor_npc_id", "store_code", "menu_codes", "subsidies", "beneficiaries", "starts_world_minute", "per_npc_daily_limit", "village_daily_limit"}:
        return None
    codes = value.get("menu_codes", [])
    if not isinstance(codes, list) or not codes or any(not isinstance(c, str) or c not in PUBLIC_MEALS for c in codes) or len(set(codes)) != len(codes):
        return None
    subsidies = value.get("subsidies")
    if not isinstance(subsidies, dict) or set(subsidies) != set(codes) or any(type(subsidies[c]) is not int or subsidies[c] != PUBLIC_MEALS[c]["subsidy"] for c in codes):
        return None
    beneficiaries = value.get("beneficiaries", sorted(FORMAL_NPC_IDS))
    if not isinstance(beneficiaries, list) or not beneficiaries or any(str(n) not in FORMAL_NPC_IDS for n in beneficiaries):
        return None
    start = integer(value.get("starts_world_minute", minute))
    per_npc = integer(value.get("per_npc_daily_limit", 1), 1)
    total = integer(value.get("village_daily_limit", 5), 1)
    if (start is None or start < minute or start >= expiry or not budget or
        per_npc != 1 or total is None or total > 5 or value.get("executor_npc_id", "10014") != "10014" or
        value.get("store_code", "RESTAURANT_STORE") != "RESTAURANT_STORE"):
        return None
    return dict(executor_npc_id="10014", store_code="RESTAURANT_STORE", menu_codes=codes,
        subsidies=subsidies, beneficiaries=sorted(set(map(str, beneficiaries))), starts_world_minute=start,
        per_npc_daily_limit=per_npc, village_daily_limit=total)


def public_meal_availability(npc, minute, recipes, inventory):
    """Read current resources and policy; never approve, reserve or create rows."""
    fund = VillageAccount.objects.filter(account_code="PUBLIC_FUND").first()
    balance = fund.balance if fund else None
    usage = VillageCommunityMealDailyUsage.objects.filter(day_index=minute // 1440).first()
    npc_usage = dict(usage.npc_usage) if usage else {}
    total_used = usage.total_meals if usage else 0
    # Personal food and delisted restaurant food can be listed without cooking again.
    unlisted = {spec["item_id"]: 0 for spec in PUBLIC_MEALS.values()}
    if str(npc.agent_id) == "10014":
        for slot in inventory.slots.filter(item_id__in=unlisted, item_amount__gt=0):
            metadata = slot.dish_metadata or {}
            if metadata.get("maker_npc_id") == "10014" and not metadata.get("public_meal_claim"):
                unlisted[slot.item_id] += get_available_slot_amount(slot, minute)
        for slot in VillageStoreInventorySlot.objects.filter(store_id="RESTAURANT_STORE",
                listing__isnull=True, item_id__in=unlisted, item_amount__gt=0):
            metadata = (slot.item_attributes or {}).get("dish_metadata") or {}
            if metadata.get("maker_npc_id") == "10014" and not metadata.get("public_meal_claim"):
                unlisted[slot.item_id] += store_available(slot, minute)
    menu = []
    for code, spec in PUBLIC_MEALS.items():
        options = [r for r in recipes if r["output_item_id"] == spec["item_id"]]
        menu.append({"public_meal_code": code, "item_id": spec["item_id"], "subsidy_amount": spec["subsidy"],
            "can_cook": any(r["can_cook"] for r in options), "unlisted_portions": unlisted[spec["item_id"]],
            "recipes": [{"recipe_id": r["recipe_id"], "can_cook": r["can_cook"],
                "missing_ingredients": [{**i, "missing_quantity": i["required_quantity"] - i["available_quantity"]}
                    for i in r["ingredients"] if i["available_quantity"] < i["required_quantity"]]} for r in options]})
    store = VillageStore.objects.select_related("operator_npc").filter(pk="RESTAURANT_STORE").first()
    remaining_village = max(0, PUBLIC_MEAL_DAILY_LIMIT - total_used)
    for item in menu:
        listed = 0
        for slot in VillageStoreInventorySlot.objects.filter(store_id="RESTAURANT_STORE", item_id=item["item_id"],
                listing__status="ACTIVE", listing__unit_price=0, listing__public_meal_code=item["public_meal_code"],
                item_amount__gt=0):
            metadata = (slot.item_attributes or {}).get("dish_metadata") or {}
            if metadata.get("maker_npc_id") == "10014" and not metadata.get("public_meal_claim"):
                listed += store_available(slot, minute)
        reasons = []
        if not store or store.status != "OPEN":
            reasons.append("TRADE_STORE_CLOSED")
        if not store or store.operator_npc is None or str(store.operator_npc.agent_id) != "10014":
            reasons.append("TRADE_STORE_FORBIDDEN")
        if balance is None or balance < item["subsidy_amount"]:
            reasons.append("PUBLIC_MEAL_FUND_INSUFFICIENT")
        if not remaining_village:
            reasons.append("PUBLIC_MEAL_DAILY_LIMIT_REACHED")
        if not item["can_cook"] and not item["unlisted_portions"] and not listed:
            reasons.append("PUBLIC_MEAL_INGREDIENTS_INSUFFICIENT")
        item.update(listed_portions=listed,
            can_list_existing=item["unlisted_portions"] > 0 and store is not None and store.operator_npc_id == npc.pk,
            can_prepare_and_serve=not reasons, unavailable_reasons=reasons)
    can_serve = any(m["can_prepare_and_serve"] for m in menu)
    return {"world_minute": minute, "day_index": minute // 1440, "public_fund_balance": balance,
        "shared_daily_usage": {"total_meals": total_used, "by_npc": npc_usage},
        "per_person_daily_limit": 1, "village_daily_limit": PUBLIC_MEAL_DAILY_LIMIT,
        "remaining_village_today": remaining_village,
        "remaining_by_npc_today": {n: max(0, 1 - npc_usage.get(n, 0)) for n in sorted(FORMAL_NPC_IDS)},
        "menu": menu, "can_prepare_and_serve": can_serve,
        "unavailable_reasons": [] if can_serve else sorted({r for m in menu for r in m["unavailable_reasons"]})}


def store_available(slot, minute):
    if (slot.item_attributes or {}).get("freshness_unknown") or (slot.expires_world_minute is not None and slot.expires_world_minute <= minute):
        return 0
    return max(0, slot.item_amount - slot.reserved_amount)


def list_meal(npc_id, params, minute, request_id):
    if str(npc_id) != "10014":
        return _fail("TRADE_STORE_FORBIDDEN")
    if params.get("price_mode", "ORIGINAL") != "ORIGINAL" or "custom_unit_price" in params:
        return _fail("RESTAURANT_FIXED_PRICE_REQUIRED")
    amount, item_id = params.get("amount"), params.get("item_id")
    if integer(amount, 1) is None or integer(item_id, 1) is None:
        return _fail("P3_INVALID_PARAMS")
    store = VillageStore.objects.select_for_update().filter(pk="RESTAURANT_STORE", operator_npc__agent_id=str(npc_id)).first()
    if not store:
        return _fail("TRADE_STORE_FORBIDDEN")
    inventory = NpcGlobalInventory.objects.select_for_update().get(npc__agent_id=str(npc_id))
    if params.get("expected_operator_asset_version", inventory.asset_version) != inventory.asset_version:
        return _fail("TRADE_ASSET_VERSION_CONFLICT")
    if params.get("expected_store_inventory_version", store.inventory_version) != store.inventory_version:
        return _fail("TRADE_STORE_VERSION_CONFLICT")
    source = params.get("stock_source", "PERSONAL")
    instance = _id(params.get("food_instance_id"))
    if source == "PERSONAL":
        slot = inventory.slots.select_for_update().filter(instance_id=instance, item_id=item_id).first()
        available = get_available_slot_amount(slot, minute) if slot else 0
        attrs = {k: copy.deepcopy(getattr(slot, k)) for k in BATCH_ITEM_ATTRIBUTES} if slot else {}
    elif source == "STORE":
        slot = VillageStoreInventorySlot.objects.select_for_update().filter(store=store, instance_id=instance, listing__isnull=True, item_id=item_id).first()
        available = store_available(slot, minute) if slot else 0
        attrs = copy.deepcopy(slot.item_attributes) if slot else {}
    else:
        return _fail("P3_INVALID_PARAMS")
    if available < amount:
        return _fail("TRADE_INSUFFICIENT_ITEM")
    code = params.get("public_meal_code", "")
    if not isinstance(code, str):
        return _fail("P3_INVALID_PARAMS")
    if code:
        metadata = attrs.get("dish_metadata") or {}
        if code not in PUBLIC_MEALS or PUBLIC_MEALS[code]["item_id"] != item_id or metadata.get("maker_npc_id") != "10014" or metadata.get("public_meal_claim"):
            return _fail("PUBLIC_MEAL_INVALID_BATCH")
        price = 0
    else:
        try:
            price = fixed_price(item_id, attrs)
        except ValueError as exc:
            return _fail(str(exc))
    # Never combine batches into one price/metadata source.
    listing = VillageStoreListing.objects.create(store=store, item_id=item_id, unit_price=price,
        price_mode="ORIGINAL", public_meal_code=code,
        listed_by_npc=inventory.npc, created_request_id=request_id, listed_world_minute=minute)
    slot.item_amount -= amount
    slot.save(update_fields=["item_amount"])
    VillageStoreInventorySlot.objects.create(store=store, listing=listing, item_id=item_id, item_amount=amount,
        remaining_uses=slot.remaining_uses, produced_world_minute=slot.produced_world_minute,
        expires_world_minute=slot.expires_world_minute, item_attributes=attrs,
        source_npc=inventory.npc if source == "PERSONAL" else slot.source_npc,
        source_slot_instance_id=slot.instance_id if source == "PERSONAL" else slot.source_slot_instance_id)
    if source == "PERSONAL":
        inventory.asset_version += 1
        inventory.save(update_fields=["asset_version", "updated_at"])
    store.inventory_version += 1
    store.save(update_fields=["inventory_version", "updated_at"])
    data = dict(store_code=store.pk, listing_id=str(listing.pk), item_id=item_id, amount=amount, unit_price=price,
        price_mode="ORIGINAL", public_meal_code=code,
        store_inventory_version=store.inventory_version, stock_source=source, food_instance_id=str(slot.instance_id))
    return _ok(data=data, effects=data, external_delta=0, asset_versions={str(npc_id): inventory.asset_version})


PUBLIC_MEAL_DAILY_LIMIT = 10


def public_meal_quota(minute, *, npc_id=None, player_id=None):
    """Private, read-only quota projection; player ids never alias NPC ids."""
    usage = VillageCommunityMealDailyUsage.objects.filter(day_index=minute // 1440).first()
    counts = (usage.player_usage if player_id is not None else usage.npc_usage) if usage else {}
    identity = str(player_id if player_id is not None else npc_id)
    fund = VillageAccount.objects.filter(account_code="PUBLIC_FUND").first()
    return dict(day_index=minute // 1440, per_person_daily_limit=1, village_daily_limit=PUBLIC_MEAL_DAILY_LIMIT,
        remaining_personal_today=max(0, 1 - counts.get(identity, 0)),
        remaining_village_today=max(0, PUBLIC_MEAL_DAILY_LIMIT - (usage.total_meals if usage else 0)),
        public_fund_balance=fund.balance if fund else 0)


def take_public_meal(store, listing, minute, request_id, *, npc_id=None, player_id=None):
    """Shared atomic stock/subsidy/quota settlement. Caller holds the store lock and
    an action savepoint; all recipients serialize on the same restaurant row.
    The caller transfers the returned batch and writes its own receipt/ledger.
    """
    if (store.pk != "RESTAURANT_STORE" or not store.operator_npc_id
            or str(store.operator_npc.agent_id) != "10014"):
        return None, "TRADE_STORE_FORBIDDEN"
    spec = PUBLIC_MEALS.get(listing.public_meal_code)
    if not spec or spec["item_id"] != listing.item_id or listing.unit_price != 0:
        return None, "PUBLIC_MEAL_INVALID_BATCH"
    if player_id is None and str(npc_id) not in FORMAL_NPC_IDS:
        return None, "PUBLIC_MEAL_INELIGIBLE"
    usage = VillageCommunityMealDailyUsage.objects.select_for_update().filter(day_index=minute // 1440).first()
    field = "player_usage" if player_id is not None else "npc_usage"
    identity = str(player_id if player_id is not None else npc_id)
    counts = dict(getattr(usage, field)) if usage else {}
    if usage and (counts.get(identity, 0) >= 1 or usage.total_meals >= PUBLIC_MEAL_DAILY_LIMIT):
        return None, "PUBLIC_MEAL_DAILY_LIMIT_REACHED"
    account = VillageStoreAccount.objects.select_for_update().filter(store=store).first()
    fund = VillageAccount.objects.select_for_update().filter(account_code="PUBLIC_FUND").first()
    subsidy = spec["subsidy"]
    if not account:
        return None, "STORE_UNAVAILABLE"
    if not fund or fund.balance < subsidy:
        return None, "PUBLIC_MEAL_FUND_INSUFFICIENT"
    slots = VillageStoreInventorySlot.objects.select_for_update().filter(store=store, listing=listing, item_amount__gt=0).order_by(models.F("expires_world_minute").asc(nulls_last=True), "instance_id")
    slot = next((s for s in slots if store_available(s, minute) > 0), None)
    if not slot:
        return None, "TRADE_INSUFFICIENT_ITEM"
    attrs = copy.deepcopy(slot.item_attributes)
    metadata = attrs.get("dish_metadata") or {}
    if metadata.get("public_meal_claim") or metadata.get("maker_npc_id") != "10014":
        return None, "PUBLIC_MEAL_INVALID_BATCH"
    metadata["public_meal_claim"] = {"request_id": str(request_id), "world_minute": minute,
        "actor_type": "PLAYER" if player_id is not None else "NPC", "actor_id": identity}
    attrs["dish_metadata"] = metadata
    slot.item_amount -= 1
    slot.save(update_fields=["item_amount"])
    account.balance += subsidy
    account.account_version += 1
    account.save(update_fields=["balance", "account_version", "updated_at"])
    fund.balance -= subsidy
    fund.asset_version += 1
    fund.save(update_fields=["balance", "asset_version", "updated_at"])
    if usage is None:
        usage = VillageCommunityMealDailyUsage.objects.create(day_index=minute // 1440)
    counts[identity] = counts.get(identity, 0) + 1
    setattr(usage, field, counts)
    usage.total_meals += 1
    usage.save(update_fields=[field, "total_meals", "updated_at"])
    return dict(slot=slot, attributes=attrs, subsidy_amount=subsidy,
        remaining_personal_today=0, remaining_village_today=PUBLIC_MEAL_DAILY_LIMIT - usage.total_meals), None


def buy_public_meal(npc_id, params, minute, request_id):
    if params.get("amount") != 1 or type(params.get("amount")) is not int:
        return _fail("PUBLIC_MEAL_ONE_PORTION_ONLY")
    store = VillageStore.objects.select_for_update().get(pk="RESTAURANT_STORE")
    listing = VillageStoreListing.objects.select_for_update().filter(pk=_id(params.get("listing_id")), store=store, status="ACTIVE").first()
    if not listing or not listing.public_meal_code or listing.unit_price != 0:
        return _fail("TRADE_LISTING_NOT_ACTIVE")
    if store.status != "OPEN":
        return _fail("TRADE_STORE_CLOSED")
    if params.get("expected_store_inventory_version", store.inventory_version) != store.inventory_version:
        return _fail("TRADE_STORE_VERSION_CONFLICT")
    buyer = NpcGlobalInventory.objects.select_for_update().get(npc__agent_id=str(npc_id))
    if params.get("expected_buyer_asset_version", buyer.asset_version) != buyer.asset_version:
        return _fail("TRADE_ASSET_VERSION_CONFLICT")
    claim, error = take_public_meal(store, listing, minute, request_id, npc_id=npc_id)
    if error:
        return _fail(error)
    slot, subsidy = claim["slot"], claim["subsidy_amount"]
    received = add_batch(buyer, slot.item_id, 1, slot.produced_world_minute, slot.expires_world_minute,
        request_id, item_attributes=claim["attributes"])
    buyer.asset_version += 1
    buyer.save(update_fields=["asset_version", "updated_at"])
    store.inventory_version += 1
    store.save(update_fields=["inventory_version", "updated_at"])
    if not any(store_available(s, minute) for s in VillageStoreInventorySlot.objects.filter(listing=listing)):
        listing.status = "SOLD_OUT"
        listing.save(update_fields=["status", "updated_at"])
    VillageStoryFact.objects.filter(arc_id="BROKEN_TRADE_ROUTE_V1").update(community_meals=models.F("community_meals") + 1)
    data = dict(store_code=store.pk, item_id=slot.item_id, expires_world_minute=received.expires_world_minute,
        listing_id=str(listing.pk), amount=1, gross_amount=0, subsidy_amount=subsidy, fee_amount=0,
        store_net_amount=subsidy, food_instance_id=str(received.instance_id), public_meal_code=listing.public_meal_code,
        remaining_personal_today=0, remaining_village_today=claim["remaining_village_today"])
    return _ok(data=data, effects={str(npc_id): {"money": 0, str(slot.item_id): 1},
        "PUBLIC_FUND": {"money": -subsidy}, "STORE:RESTAURANT_STORE": {"money": subsidy, str(slot.item_id): -1},
        "public_meal": data}, external_delta=0, asset_versions={str(npc_id): buyer.asset_version})


def public_menu_info(listing, minute, npc_id):
    if not listing.public_meal_code:
        return {}
    quota = public_meal_quota(minute, npc_id=npc_id)
    spec = PUBLIC_MEALS.get(listing.public_meal_code, {})
    subsidy = spec.get("subsidy", 0)
    error = None
    if spec.get("item_id") != listing.item_id or listing.unit_price != 0:
        error = "PUBLIC_MEAL_INVALID_BATCH"
    elif str(npc_id) not in FORMAL_NPC_IDS:
        error = "PUBLIC_MEAL_INELIGIBLE"
    elif not quota["remaining_personal_today"] or not quota["remaining_village_today"]:
        error = "PUBLIC_MEAL_DAILY_LIMIT_REACHED"
    elif quota["public_fund_balance"] < subsidy:
        error = "PUBLIC_MEAL_FUND_INSUFFICIENT"
    return dict(public_meal_code=listing.public_meal_code, subsidy_amount=subsidy,
        remaining_personal_today=quota["remaining_personal_today"], remaining_village_today=quota["remaining_village_today"],
        subsidy_available=error is None, unavailable_reason=error)
