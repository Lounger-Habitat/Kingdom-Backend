"""Agent-facing read queries. These never enter the command lifecycle."""

from __future__ import annotations

from math import dist

from django.db.models import Q

from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot
from items.models import Item, ItemType
from recipes.models import Recipe

from .models import (
    DailyPriceSnapshot,
    NpcProfile,
    NpcDialogueSession,
    NpcRuntimeState,
    NpcStoryFactAccess,
    NpcTradeDelivery,
    NpcTradeOrder,
    VillageAccount,
    VillageFarmPlot,
    VillageLocationDefinition,
    VillageRestaurantState,
    VillageStoryArc,
    VillageStoryFact,
    VillageTradeOffer,
    VillageWorldEvent,
)
from .phase2_services import need_bands, needs_rules, public_needs
from .phase3_asset_service import get_available_slot_amount
from .phase3_catalog import (
    BASE_PRICES,
    CARAVAN_COOLDOWN_DAYS,
    EXPORT_WHITELIST,
    IMPORT_WHITELIST,
    RECIPE_WHITELIST,
)
from .phase3_contract import CROP_CODES, DELIVERY_POINTS, FarmPlotStatus
from .phase3_marketplace import (
    browse_marketplace_query,
    marketplace_history_query,
    my_marketplace_listings_query,
)
from .phase5_contract import FORMAL_NPC_IDS, STORY_ACTION_ACTORS, STORY_FACT_CODES
from .phase5_story import ARC_CODE, STORY_ACTION_PLANS, _snapshot


QUERY_NAMES = frozenset({
    "OBSERVE",
    "INSPECT_NEARBY",
    "INSPECT_INVENTORY",
    "INSPECT_TRADE_OFFERS",
    "INSPECT_STORY_STATE",
    "INSPECT_RELEVANT_STORIES",
    "INSPECT_STORY_DETAIL",
    "INSPECT_ORDERS",
    "INSPECT_MARKET_STATE",
    "INSPECT_FARM_STATE",
    "INSPECT_RESTAURANT_STATE",
    "INSPECT_RATEABLE_DISHES",
    "INSPECT_PUBLIC_ECONOMY_STATE",
    "BROWSE_MARKETPLACE",
    "INSPECT_STORE_INVENTORY",
    "INSPECT_SOCIAL_STATE",
    "CHECK_MY_MARKETPLACE_LISTINGS",
    "CHECK_MARKETPLACE_HISTORY",
})


def _world_minute() -> int:
    return GlobalGameTimeState.objects.filter(pk=1).values_list("world_minute", flat=True).first() or 0


def _item_names(item_ids) -> dict[int, str]:
    return dict(Item.objects.filter(item_id__in=set(item_ids)).values_list("item_id", "item_name"))


def _observe(npc: NpcDefinition) -> dict:
    runtime = NpcRuntimeState.objects.get(npc=npc)
    world_minute = _world_minute()
    dialogue = NpcDialogueSession.objects.filter(
        Q(initiator=npc) | Q(target=npc), status="ACTIVE"
    ).first()
    action_end = dialogue.expires_world_minute if dialogue else runtime.action_ends_world_minute
    return {
        "world_minute": world_minute,
        "current_location": runtime.current_location_code,
        "current_action": "DIALOGUE" if dialogue else runtime.current_action or None,
        "action_status": "DIALOGUING" if dialogue else runtime.action_status,
        "action_started_world_minute": dialogue.started_world_minute if dialogue else runtime.action_started_world_minute,
        "action_ends_world_minute": action_end,
        "remaining_minutes": max(0, action_end - world_minute) if action_end is not None else None,
        "active_command_id": str(runtime.active_command_id) if runtime.active_command_id else None,
        "interruptible": runtime.action_status == "SLEEPING" and runtime.current_action == "SLEEP",
        "needs": public_needs(runtime),
        "need_bands": need_bands(runtime),
        "needs_rules": needs_rules(),
    }


def _center(location: VillageLocationDefinition):
    value = (location.bounds or {}).get("center")
    if isinstance(value, list) and len(value) == 3 and all(isinstance(x, (int, float)) for x in value):
        return value
    return None


def _nearby(npc: NpcDefinition) -> dict:
    runtime = NpcRuntimeState.objects.get(npc=npc)
    profile = NpcProfile.objects.get(npc=npc)
    current = VillageLocationDefinition.objects.filter(
        location_code=runtime.current_location_code, is_active=True,
    ).first()
    nearby_npcs = [
        {
            "npc_id": str(row.npc.agent_id),
            "name": row.npc.village_profile.display_name,
            "activity": row.action_status,
        }
        for row in NpcRuntimeState.objects.select_related("npc", "npc__village_profile")
        .filter(current_location_code=runtime.current_location_code, npc__is_test_only=False)
        .exclude(npc=npc)
        .order_by("npc__agent_id")
    ]
    npc_names = {
        str(agent_id): display_name
        for agent_id, display_name in NpcProfile.objects.filter(
            is_active=True, npc__is_test_only=False,
        )
        .values_list("npc__agent_id", "display_name")
    }
    visible_codes = {
        profile.home_location_code,
        profile.work_location_code,
        profile.social_location_code,
        runtime.current_location_code,
    }
    candidates = VillageLocationDefinition.objects.filter(is_active=True).filter(
        Q(is_public=True, required_story_states=[]) | Q(location_code__in=visible_codes)
    ).exclude(location_code=runtime.current_location_code)
    origin = _center(current) if current else None
    locations = []
    for row in candidates:
        center = _center(row)
        distance = round(dist(origin, center), 1) if origin is not None and center is not None else None
        locations.append({
            "location_code": row.location_code,
            "name": row.display_name,
            "location_type": row.location_type,
            "distance": distance,
        })
    locations.sort(key=lambda row: (row["distance"] is None, row["distance"] or 0, row["location_code"]))
    return {
        "current_location": runtime.current_location_code,
        "npc_names": npc_names,
        "nearby_npcs": nearby_npcs,
        "nearby_locations": locations[:5],
    }


def _inventory(npc: NpcDefinition) -> dict:
    inventory = NpcGlobalInventory.objects.get(npc=npc)
    slots = list(inventory.slots.filter(item_amount__gt=0).order_by("slot_index"))
    item_details = {
        row["item_id"]: row
        for row in Item.objects.filter(
            item_id__in={slot.item_id for slot in slots}, is_deleted=False
        ).values("item_id", "item_price", "item_type")
    }
    names = _item_names(row.item_id for row in slots)
    edible_item_ids = set(Item.objects.filter(
        item_type=ItemType.FOOD, is_deleted=False,
    ).values_list("item_id", flat=True))
    return {
        "money": inventory.money,
        "items": [{
            "instance_id": str(row.instance_id),
            "food_instance_id": str(row.instance_id) if row.item_id in edible_item_ids else None,
            "item_id": row.item_id,
            "name": names.get(row.item_id, ""),
            "quantity": row.item_amount,
            "available_quantity": get_available_slot_amount(row),
            "can_list_in_store": row.item_id in item_details,
            "reference_unit_price": (
                item_details[row.item_id]["item_price"]
                if row.item_id in item_details else None
            ),
            "item_type": (
                item_details[row.item_id]["item_type"]
                if row.item_id in item_details else None
            ),
            "expires_world_minute": row.expires_world_minute,
            "rated": row.rated,
        } for row in slots],
    }


def _trade_offers(npc: NpcDefinition) -> dict:
    rows = VillageTradeOffer.objects.select_related("buyer_npc", "seller_npc").filter(
        Q(buyer_npc=npc) | Q(seller_npc=npc), status="PENDING",
    ).order_by("created_world_minute", "offer_id")
    return {"offers": [{
        "offer_id": str(row.offer_id),
        "direction": "OUTGOING" if row.buyer_npc_id == npc.pk else "INCOMING",
        "buyer_npc_id": str(row.buyer_npc.agent_id),
        "seller_npc_id": str(row.seller_npc.agent_id),
        "item_id": row.item_id,
        "quantity": row.amount,
        "gold_amount": row.gold_amount,
        "location_code": row.location_code,
        "expires_world_minute": row.expires_world_minute,
    } for row in rows]}


def _story_state(npc: NpcDefinition) -> dict:
    arc = VillageStoryArc.objects.filter(arc_code=ARC_CODE).first()
    if arc is None:
        return {"active": False, "meeting": None, "available_actions": []}
    meeting = arc.meetings.filter(status="OPEN").order_by("opened_day").first()
    detail = _broken_trade_route_detail(npc)
    return {
        **(detail or {}),
        "active": not arc.is_completed,
        "arc_code": arc.arc_code,
        "phase": arc.current_phase,
        "plan_code": arc.current_plan or None,
        "outcome": arc.outcome or None,
        "meeting": None if meeting is None else {
            "meeting_id": str(meeting.meeting_id),
            "meeting_type": meeting.meeting_type,
            "candidate_plans": list(meeting.candidate_plans or []),
            "closes_day": meeting.closes_day,
        },
        "available_actions": [
            action for action, plans in STORY_ACTION_PLANS.items()
            if arc.current_plan and arc.current_plan in plans
        ],
    }


def _broken_trade_route_detail(npc: NpcDefinition) -> dict | None:
    """Return the current story projection after NPC fact/action filtering."""
    arc = VillageStoryArc.objects.filter(arc_code=ARC_CODE).first()
    if arc is None:
        return None
    fact = VillageStoryFact.objects.filter(arc=arc).first()
    if fact is None:
        return None
    meeting = arc.meetings.filter(status="OPEN").order_by("opened_day").first()
    snapshot = _snapshot(arc, fact, meeting)
    known_facts = list(NpcStoryFactAccess.objects.filter(
        npc=npc, fact_code__in=STORY_FACT_CODES,
    ).values_list("fact_code", flat=True))
    available_actions = [
        action for action, plans in STORY_ACTION_PLANS.items()
        if not arc.is_completed and arc.current_phase == "EXECUTION"
        and arc.current_plan and arc.current_plan in plans
        and str(npc.agent_id) in STORY_ACTION_ACTORS.get(action, set())
    ]
    return {
        "story_id": ARC_CODE,
        "arc_code": ARC_CODE,
        "title": "断掉的商路",
        "type": "MAIN",
        "phase": snapshot["phase"],
        "plan": snapshot["plan"],
        "status": "COMPLETED" if snapshot["is_completed"] else "ACTIVE",
        "story_version": snapshot["story_version"],
        "bridge_condition": snapshot["bridge_condition"],
        "progress": snapshot["plan_progress"],
        "available_actions": available_actions,
        "known_facts": known_facts,
    }


_STORY_PROJECTORS = {ARC_CODE: _broken_trade_route_detail}


def _story_detail(npc: NpcDefinition, story_id: str) -> dict | None:
    if str(npc.agent_id) not in FORMAL_NPC_IDS:
        return None
    projector = _STORY_PROJECTORS.get(story_id)
    return projector(npc) if projector else None


def _relevant_stories(npc: NpcDefinition) -> dict:
    # Keep this registry as the only place a future story projector is added.
    stories = []
    for story_id in _STORY_PROJECTORS:
        detail = _story_detail(npc, story_id)
        if detail is None:
            continue
        stories.append({
            "story_id": story_id,
            "arc_code": story_id,
            "title": detail["title"],
            "type": detail["type"],
            "status": detail["status"],
            "phase": detail["phase"],
            "story_version": detail["story_version"],
            "role": "PARTICIPANT",
            "latest_summary": f"桥梁状态：{detail['bridge_condition']}",
        })
    return {"stories": stories}


def _orders(npc: NpcDefinition) -> dict:
    orders = list(NpcTradeOrder.objects.filter(
        npc=npc, status__in=("OPEN", "MATCHED"),
    ).order_by("created_at"))
    deliveries = NpcTradeDelivery.objects.select_related(
        "buyer_order__npc", "seller_order__npc",
    ).filter(
        Q(buyer_order__npc=npc) | Q(seller_order__npc=npc), status="ACTIVE",
    ).order_by("created_at")
    return {
        "orders": [{
            "order_id": str(row.order_id),
            "side": row.side,
            "item_id": row.item_id,
            "quantity": row.amount,
            "unit_price": row.unit_price,
            "delivery_point_code": row.delivery_point_code,
            "expires_world_minute": row.expires_world_minute,
            "status": row.status,
        } for row in orders],
        "deliveries": [{
            "delivery_id": str(row.delivery_id),
            "buyer_npc_id": str(row.buyer_order.npc.agent_id),
            "seller_npc_id": str(row.seller_order.npc.agent_id),
            "delivery_point_code": row.delivery_point_code,
            "quantity": row.reserved_amount,
            "expires_world_minute": row.expires_world_minute,
            "role": "SELLER" if row.seller_order.npc_id == npc.pk else "BUYER",
        } for row in deliveries],
    }


def _market_state(_npc: NpcDefinition) -> dict:
    world_minute = _world_minute()
    snapshot = DailyPriceSnapshot.objects.filter(status="PUBLISHED").order_by("-day_index").first()
    prices = dict(snapshot.prices or {}) if snapshot else {str(key): value for key, value in BASE_PRICES.items()}
    item_details = {
        row["item_id"]: row
        for row in Item.objects.filter(is_deleted=False).values(
            "item_id", "item_name", "item_price", "item_type"
        )
    }
    item_ids = (
        set(item_details)
        | {int(value) for value in prices}
        | set(IMPORT_WHITELIST)
        | set(EXPORT_WHITELIST)
    )
    account = VillageAccount.objects.filter(account_code="PUBLIC_FUND").first()
    last_caravan_day = account.last_caravan_day if account else None
    current_day = world_minute // 1440
    return {
        "world_minute": world_minute,
        "price_day_index": snapshot.day_index if snapshot else None,
        "items": [{
            "item_id": item_id,
            "name": item_details.get(item_id, {}).get("item_name", ""),
            "unit_price": prices.get(
                str(item_id),
                BASE_PRICES.get(
                    item_id, item_details.get(item_id, {}).get("item_price")
                ),
            ),
            "reference_unit_price": BASE_PRICES.get(
                item_id, item_details.get(item_id, {}).get("item_price")
            ),
            "can_list_in_store": item_id in item_details,
            "can_import": item_id in IMPORT_WHITELIST,
            "can_export": item_id in EXPORT_WHITELIST,
        } for item_id in sorted(item_ids)],
        "delivery_points": list(DELIVERY_POINTS),
        "import_item_ids": sorted(IMPORT_WHITELIST),
        "export_item_ids": sorted(EXPORT_WHITELIST),
        "caravan_available": last_caravan_day is None or current_day - last_caravan_day >= CARAVAN_COOLDOWN_DAYS,
        "next_caravan_day": None if last_caravan_day is None else last_caravan_day + CARAVAN_COOLDOWN_DAYS,
    }


def _farm_state(_npc: NpcDefinition) -> dict:
    world_minute = _world_minute()
    plots = []
    for row in VillageFarmPlot.objects.order_by("plot_code"):
        status = row.status
        if status == FarmPlotStatus.GROWING and row.mature_world_minute is not None and world_minute >= row.mature_world_minute:
            status = FarmPlotStatus.MATURE
        plots.append({
            "plot_code": row.plot_code,
            "status": status,
            "crop_code": row.crop_code or None,
            "planted_world_minute": row.planted_world_minute,
            "mature_world_minute": row.mature_world_minute,
        })
    return {"world_minute": world_minute, "plots": plots, "plantable_crop_codes": list(CROP_CODES)}


def _restaurant_state(npc: NpcDefinition) -> dict:
    world_minute = _world_minute()
    state = VillageRestaurantState.objects.filter(day_index=world_minute // 1440).first()
    inventory = NpcGlobalInventory.objects.get(npc=npc)
    available = {}
    for slot in inventory.slots.filter(item_amount__gt=0):
        available[slot.item_id] = available.get(slot.item_id, 0) + get_available_slot_amount(slot)
    recipes = Recipe.objects.prefetch_related("ingredients").filter(pk__in=RECIPE_WHITELIST).order_by("pk")
    recipe_data = []
    for recipe in recipes:
        ingredients = [{
            "item_id": row.item_id,
            "name": row.item_name,
            "required_quantity": row.count,
            "available_quantity": available.get(row.item_id, 0),
        } for row in recipe.ingredients.all()]
        recipe_data.append({
            "recipe_id": recipe.pk,
            "name": recipe.recipe_name,
            "output_item_id": recipe.item_id,
            "ingredients": ingredients,
            "can_cook": bool(ingredients) and all(x["available_quantity"] >= x["required_quantity"] for x in ingredients),
        })
    return {
        "day_index": world_minute // 1440,
        "status": state.status if state else "OPEN",
        "recipes": recipe_data,
    }


def _rateable_dishes(_npc: NpcDefinition) -> dict:
    dish_ids = list(Recipe.objects.filter(pk__in=RECIPE_WHITELIST).values_list("item_id", flat=True))
    rows = NpcGlobalInventorySlot.objects.select_related("bag__npc").filter(
        item_id__in=dish_ids, item_amount__gt=0, rated=False, bag__npc__is_test_only=False,
    ).order_by("bag__npc__agent_id", "slot_index")
    return {"dishes": [{
        "owner_npc_id": str(row.bag.npc.agent_id),
        "owner_name": row.bag.npc.name,
        "food_instance_id": str(row.instance_id),
        "item_id": row.item_id,
        "quantity": row.item_amount,
        "dish_quality": row.dish_quality,
    } for row in rows]}


def _public_economy(_npc: NpcDefinition) -> dict:
    world_minute = _world_minute()
    day_index = world_minute // 1440
    account = VillageAccount.objects.filter(account_code="PUBLIC_FUND").first()
    snapshot = DailyPriceSnapshot.objects.filter(day_index=day_index).first()
    audited = VillageWorldEvent.objects.filter(
        event_type="ECONOMY_AUDIT_PUBLIC_FUND",
        world_minute__gte=day_index * 1440,
        world_minute__lt=(day_index + 1) * 1440,
    ).exists()
    return {
        "day_index": day_index,
        "public_fund_balance": account.balance if account else 0,
        "price_index": None if snapshot is None else {
            "status": snapshot.status,
            "prices": dict(snapshot.prices or {}),
            "published_world_minute": snapshot.published_world_minute,
        },
        "audited_today": audited,
    }


def _browse_marketplace(npc: NpcDefinition, query_params=None) -> dict:
    return browse_marketplace_query(npc, query_params)


def _my_marketplace_listings(npc: NpcDefinition, query_params=None) -> dict:
    return my_marketplace_listings_query(npc, query_params)


def _marketplace_history(npc: NpcDefinition, query_params=None) -> dict:
    return marketplace_history_query(npc, query_params)


def _authorized(_npc: NpcDefinition) -> dict:
    """Capability gate for parameterized legacy query endpoints."""
    return {"authorized": True}


_HANDLERS = {
    "OBSERVE": _observe,
    "INSPECT_NEARBY": _nearby,
    "INSPECT_INVENTORY": _inventory,
    "INSPECT_TRADE_OFFERS": _trade_offers,
    "INSPECT_STORY_STATE": _story_state,
    "INSPECT_RELEVANT_STORIES": _relevant_stories,
    "INSPECT_ORDERS": _orders,
    "INSPECT_MARKET_STATE": _market_state,
    "INSPECT_FARM_STATE": _farm_state,
    "INSPECT_RESTAURANT_STATE": _restaurant_state,
    "INSPECT_RATEABLE_DISHES": _rateable_dishes,
    "INSPECT_PUBLIC_ECONOMY_STATE": _public_economy,
    "BROWSE_MARKETPLACE": _browse_marketplace,
    "CHECK_MY_MARKETPLACE_LISTINGS": _my_marketplace_listings,
    "CHECK_MARKETPLACE_HISTORY": _marketplace_history,
    "INSPECT_STORE_INVENTORY": _authorized,
    "INSPECT_SOCIAL_STATE": _authorized,
}


def execute_agent_query(npc_id: str, query_name: str, query_params=None) -> tuple[bool, str, dict | None]:
    query_name = str(query_name).strip().upper()
    if query_name not in QUERY_NAMES:
        return False, "UNKNOWN_QUERY", None
    try:
        npc = NpcDefinition.objects.select_related("village_profile").get(
            agent_id=str(npc_id), is_test_only=False, village_profile__is_active=True,
        )
    except NpcDefinition.DoesNotExist:
        return False, "NPC_NOT_FOUND", None
    capabilities = {str(value).strip().upper() for value in npc.village_profile.capabilities}
    if query_name not in capabilities:
        return False, "ACTION_NOT_ALLOWED", None
    try:
        if query_name == "INSPECT_STORY_DETAIL":
            story_id = str((query_params or {}).get("story_id", "")).strip()
            data = _story_detail(npc, story_id)
            return (True, "OK", data) if data is not None else (False, "STORY_NOT_VISIBLE", None)
        if query_name in {"BROWSE_MARKETPLACE", "CHECK_MY_MARKETPLACE_LISTINGS", "CHECK_MARKETPLACE_HISTORY"}:
            return True, "OK", _HANDLERS[query_name](npc, query_params)
        return True, "OK", _HANDLERS[query_name](npc)
    except (NpcRuntimeState.DoesNotExist, NpcGlobalInventory.DoesNotExist, NpcProfile.DoesNotExist):
        return False, "NPC_NOT_READY", None
