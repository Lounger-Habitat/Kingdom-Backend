"""Restaurant batch prices and authorized, cash-funded public meals."""
import copy
import uuid

from django.db import transaction, models
from inventory.models import NpcGlobalInventory
from .models import (VillageStore, VillageStoreAccount, VillageStoreListing, VillageStoreInventorySlot,
                     VillageAccount, VillageCommunityMealDailyUsage, VillageStoryFact, VillagePublicDecision)
from .phase3_asset_service import _ok, _fail, add_batch, BATCH_ITEM_ATTRIBUTES, get_available_slot_amount
from .lucy_catalog import PUBLIC_MEALS
from .lucy_cooking import fixed_price, _id
from .public_governance import authorized_decision, integer
from .phase5_contract import FORMAL_NPC_IDS


def validate_meal_terms(value, minute, expiry, budget):
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


def meal_authorization(decision_id, code, minute, actor=None, *, lock=True):
    decision, error = authorized_decision({"decision_id": decision_id}, "RATION_AND_WAIT", minute, lock=lock)
    if error:
        return None, None, error
    terms = decision.terms.get("meal_service") or {}
    if (terms.get("executor_npc_id") != "10014" or terms.get("store_code") != "RESTAURANT_STORE"
        or code not in terms.get("menu_codes", []) or terms.get("subsidies", {}).get(code) != PUBLIC_MEALS.get(code, {}).get("subsidy")):
        return None, None, "PUBLIC_MEAL_NOT_AUTHORIZED"
    if minute < terms.get("starts_world_minute", 0):
        return None, None, "PUBLIC_MEAL_NOT_STARTED"
    if actor is not None and str(actor) not in terms.get("beneficiaries", []):
        return None, None, "PUBLIC_MEAL_INELIGIBLE"
    return decision, terms, None


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
    authorizations = []
    for decision in VillagePublicDecision.objects.filter(plan_code="RATION_AND_WAIT").order_by("-approved_world_minute", "pk"):
        terms = decision.terms.get("meal_service") or {}
        if not terms:
            continue
        remaining_budget = max(0, decision.terms.get("budget_gold", 0) - decision.spent_gold)
        remaining_village = max(0, terms.get("village_daily_limit", 5) - total_used)
        remaining_by_npc = {str(n): max(0, terms.get("per_npc_daily_limit", 1) - npc_usage.get(str(n), 0))
            for n in terms.get("beneficiaries", [])}
        policy_menu = []
        for item in menu:
            code = item["public_meal_code"]
            _, _, error = meal_authorization(decision.pk, code, minute, lock=False)
            reasons = [error] if error else []
            listed = 0
            for slot in VillageStoreInventorySlot.objects.filter(store_id="RESTAURANT_STORE", item_id=item["item_id"],
                    listing__status="ACTIVE", listing__unit_price=0, listing__public_meal_code=code,
                    listing__subsidy_decision=decision, item_amount__gt=0):
                metadata = (slot.item_attributes or {}).get("dish_metadata") or {}
                if metadata.get("maker_npc_id") == "10014" and not metadata.get("public_meal_claim"):
                    listed += store_available(slot, minute)
            if not store or store.status != "OPEN":
                reasons.append("TRADE_STORE_CLOSED")
            if not store or store.operator_npc is None or str(store.operator_npc.agent_id) != "10014":
                reasons.append("TRADE_STORE_FORBIDDEN")
            if remaining_budget < item["subsidy_amount"]:
                reasons.append("PUBLIC_MEAL_BUDGET_EXHAUSTED")
            if balance is None or balance < item["subsidy_amount"]:
                reasons.append("PUBLIC_MEAL_FUND_INSUFFICIENT")
            if not remaining_village or not any(remaining_by_npc.values()):
                reasons.append("PUBLIC_MEAL_DAILY_LIMIT_REACHED")
            if not item["can_cook"] and not item["unlisted_portions"] and not listed:
                reasons.append("PUBLIC_MEAL_INGREDIENTS_INSUFFICIENT")
            policy_menu.append({"public_meal_code": code, "subsidy_amount": item["subsidy_amount"],
                "authorization_valid": error is None, "listed_portions": listed,
                "can_list_existing": error is None and item["unlisted_portions"] > 0
                    and store is not None and store.operator_npc_id == npc.pk,
                "can_prepare_and_serve": not reasons, "unavailable_reasons": reasons})
        authorizations.append({"decision_id": str(decision.pk),
            "published_world_minute": decision.published_world_minute, "revoked_world_minute": decision.revoked_world_minute,
            "starts_world_minute": terms.get("starts_world_minute"),
            "expires_world_minute": decision.terms.get("expires_world_minute"),
            "approved_menu_codes": terms.get("menu_codes", []), "beneficiaries": terms.get("beneficiaries", []),
            "budget_gold": decision.terms.get("budget_gold", 0), "spent_gold": decision.spent_gold,
            "remaining_budget": remaining_budget, "per_npc_daily_limit": terms.get("per_npc_daily_limit", 1),
            "village_daily_limit": terms.get("village_daily_limit", 5), "remaining_village_today": remaining_village,
            "remaining_by_npc_today": remaining_by_npc, "menu": policy_menu})
    can_serve = any(m["can_prepare_and_serve"] for a in authorizations for m in a["menu"])
    return {"world_minute": minute, "day_index": minute // 1440, "public_fund_balance": balance,
        "shared_daily_usage": {"total_meals": total_used, "by_npc": npc_usage},
        "menu": menu, "authorizations": authorizations,
        "can_prepare_and_serve": can_serve,
        "unavailable_reasons": [] if can_serve else (
            sorted({reason for a in authorizations for m in a["menu"] for reason in m["unavailable_reasons"]})
            if authorizations else ["PUBLIC_AUTHORIZATION_REQUIRED"])}


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
    decision = None
    if code:
        metadata = attrs.get("dish_metadata") or {}
        if code not in PUBLIC_MEALS or PUBLIC_MEALS[code]["item_id"] != item_id or metadata.get("maker_npc_id") != "10014" or metadata.get("public_meal_claim"):
            return _fail("PUBLIC_MEAL_INVALID_BATCH")
        decision, _, error = meal_authorization(params.get("decision_id"), code, minute)
        if error:
            return _fail(error)
        price = 0
    else:
        if params.get("decision_id"):
            return _fail("PUBLIC_MEAL_NOT_AUTHORIZED")
        try:
            price = fixed_price(item_id, attrs)
        except ValueError as exc:
            return _fail(str(exc))
    # Never combine batches into one price/metadata source.
    listing = VillageStoreListing.objects.create(store=store, item_id=item_id, unit_price=price,
        price_mode="ORIGINAL", public_meal_code=code, subsidy_decision=decision,
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
        price_mode="ORIGINAL", public_meal_code=code, decision_id=str(decision.pk) if decision else None,
        store_inventory_version=store.inventory_version, stock_source=source, food_instance_id=str(slot.instance_id))
    return _ok(data=data, effects=data, external_delta=0, asset_versions={str(npc_id): inventory.asset_version})


def buy_public_meal(npc_id, params, minute, request_id):
    if params.get("amount") != 1 or type(params.get("amount")) is not int:
        return _fail("PUBLIC_MEAL_ONE_PORTION_ONLY")
    store = VillageStore.objects.select_for_update().get(pk="RESTAURANT_STORE")
    listing = VillageStoreListing.objects.select_for_update().filter(pk=_id(params.get("listing_id")), store=store, status="ACTIVE").first()
    if not listing or not listing.public_meal_code or listing.unit_price != 0:
        return _fail("TRADE_LISTING_NOT_ACTIVE")
    if PUBLIC_MEALS.get(listing.public_meal_code, {}).get("item_id") != listing.item_id:
        return _fail("PUBLIC_MEAL_INVALID_BATCH")
    if store.status != "OPEN":
        return _fail("TRADE_STORE_CLOSED")
    if params.get("expected_store_inventory_version", store.inventory_version) != store.inventory_version:
        return _fail("TRADE_STORE_VERSION_CONFLICT")
    decision, terms, error = meal_authorization(listing.subsidy_decision_id, listing.public_meal_code, minute, npc_id)
    if error:
        return _fail(error)
    buyer = NpcGlobalInventory.objects.select_for_update().get(npc__agent_id=str(npc_id))
    if params.get("expected_buyer_asset_version", buyer.asset_version) != buyer.asset_version:
        return _fail("TRADE_ASSET_VERSION_CONFLICT")
    usage = VillageCommunityMealDailyUsage.objects.select_for_update().filter(day_index=minute // 1440).first()
    if usage and (usage.npc_usage.get(str(npc_id), 0) >= terms["per_npc_daily_limit"] or usage.total_meals >= terms["village_daily_limit"]):
        return _fail("PUBLIC_MEAL_DAILY_LIMIT_REACHED")
    account = VillageStoreAccount.objects.select_for_update().get(store=store)
    fund = VillageAccount.objects.select_for_update().get(account_code="PUBLIC_FUND")
    subsidy = terms["subsidies"][listing.public_meal_code]
    if decision.spent_gold + subsidy > decision.terms["budget_gold"]:
        return _fail("PUBLIC_MEAL_BUDGET_EXHAUSTED")
    if fund.balance < subsidy:
        return _fail("PUBLIC_MEAL_FUND_INSUFFICIENT")
    slots = VillageStoreInventorySlot.objects.select_for_update().filter(store=store, listing=listing, item_amount__gt=0).order_by(models.F("expires_world_minute").asc(nulls_last=True), "instance_id")
    slot = next((s for s in slots if store_available(s, minute) > 0), None)
    if not slot:
        return _fail("TRADE_INSUFFICIENT_ITEM")
    attrs = copy.deepcopy(slot.item_attributes)
    metadata = attrs.get("dish_metadata") or {}
    if metadata.get("public_meal_claim") or metadata.get("maker_npc_id") != "10014":
        return _fail("PUBLIC_MEAL_INVALID_BATCH")
    metadata["public_meal_claim"] = {"request_id": str(request_id), "decision_id": str(decision.pk), "world_minute": minute, "npc_id": str(npc_id)}
    attrs["dish_metadata"] = metadata
    slot.item_amount -= 1
    slot.save(update_fields=["item_amount"])
    received = add_batch(buyer, slot.item_id, 1, slot.produced_world_minute, slot.expires_world_minute, request_id, item_attributes=attrs)
    buyer.asset_version += 1
    buyer.save(update_fields=["asset_version", "updated_at"])
    account.balance += subsidy
    account.account_version += 1
    account.save(update_fields=["balance", "account_version", "updated_at"])
    fund.balance -= subsidy
    fund.asset_version += 1
    fund.save(update_fields=["balance", "asset_version", "updated_at"])
    decision.spent_gold += subsidy
    decision.save(update_fields=["spent_gold"])
    if usage is None:
        usage = VillageCommunityMealDailyUsage.objects.create(day_index=minute // 1440)
    usage.npc_usage = dict(usage.npc_usage, **{str(npc_id): usage.npc_usage.get(str(npc_id), 0) + 1})
    usage.total_meals += 1
    usage.save(update_fields=["npc_usage", "total_meals", "updated_at"])
    store.inventory_version += 1
    store.save(update_fields=["inventory_version", "updated_at"])
    if not any(store_available(s, minute) for s in VillageStoreInventorySlot.objects.filter(listing=listing)):
        listing.status = "SOLD_OUT"
        listing.save(update_fields=["status", "updated_at"])
    VillageStoryFact.objects.filter(arc_id="BROKEN_TRADE_ROUTE_V1").update(community_meals=models.F("community_meals") + 1)
    data = dict(store_code=store.pk, item_id=slot.item_id, expires_world_minute=received.expires_world_minute,
        listing_id=str(listing.pk), amount=1, gross_amount=0, subsidy_amount=subsidy, fee_amount=0,
        store_net_amount=subsidy, food_instance_id=str(received.instance_id), decision_id=str(decision.pk),
        remaining_personal_today=0, remaining_village_today=max(0, terms["village_daily_limit"] - usage.total_meals))
    return _ok(data=data, effects={str(npc_id): {"money": 0, str(slot.item_id): 1},
        "PUBLIC_FUND": {"money": -subsidy}, "STORE:RESTAURANT_STORE": {"money": subsidy, str(slot.item_id): -1},
        "public_meal": data}, external_delta=0, asset_versions={str(npc_id): buyer.asset_version})


def public_menu_info(listing, minute, npc_id):
    if not listing.public_meal_code:
        return {}
    # Queries must not take write locks or create quota rows.
    decision = VillagePublicDecision.objects.select_related("meeting").filter(pk=listing.subsidy_decision_id).first()
    terms = (decision.terms.get("meal_service") or {}) if decision else {}
    usage = VillageCommunityMealDailyUsage.objects.filter(day_index=minute // 1440).first()
    fund = VillageAccount.objects.filter(account_code="PUBLIC_FUND").first()
    subsidy = terms.get("subsidies", {}).get(listing.public_meal_code, 0)
    own = max(0, terms.get("per_npc_daily_limit", 1) - (usage.npc_usage.get(str(npc_id), 0) if usage else 0))
    total = max(0, terms.get("village_daily_limit", 5) - (usage.total_meals if usage else 0))
    error = None
    if not decision or decision.published_world_minute is None or decision.revoked_world_minute is not None or decision.terms.get("expires_world_minute", 0) <= minute:
        error = "PUBLIC_AUTHORIZATION_EXPIRED"
    elif minute < terms.get("starts_world_minute", 0):
        error = "PUBLIC_MEAL_NOT_STARTED"
    elif str(npc_id) not in terms.get("beneficiaries", []):
        error = "PUBLIC_MEAL_INELIGIBLE"
    elif not own or not total:
        error = "PUBLIC_MEAL_DAILY_LIMIT_REACHED"
    elif decision.spent_gold + subsidy > decision.terms.get("budget_gold", 0):
        error = "PUBLIC_MEAL_BUDGET_EXHAUSTED"
    elif not fund or fund.balance < subsidy:
        error = "PUBLIC_MEAL_FUND_INSUFFICIENT"
    return dict(public_meal_code=listing.public_meal_code, subsidy_amount=subsidy,
        decision_id=str(listing.subsidy_decision_id), expires_world_minute=decision.terms.get("expires_world_minute") if decision else None,
        remaining_personal_today=own, remaining_village_today=total, subsidy_available=error is None, unavailable_reason=error)
