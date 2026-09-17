"""Agent-facing read queries. These never enter the command lifecycle."""

from __future__ import annotations

import json
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
    NpcNeedsDaySummary,
    NpcRuntimeState,
    NpcStoryFactAccess,
    NpcTradeDelivery,
    NpcTradeOrder,
    VillageAccount,
    VillageFarmPlot,
    VillageLocationDefinition,
    VillageRestaurantState,
    VillageStore,
    VillageStoryArc,
    VillageStoryFact,
    VillageTradeOffer,
    VillageWorldEvent,
)
from .phase2_services import need_bands, public_needs
from .phase3_asset_service import get_available_slot_amount
from .phase3_catalog import (
    BASE_PRICES,
    EXPORT_WHITELIST,
    IMPORT_WHITELIST,
    RECIPE_WHITELIST,
    farm_crop_catalog,
)
from .phase3_contract import CROP_CODES, DELIVERY_POINTS, PLOT_CODES, FarmPlotStatus
from .phase3_marketplace import (
    AgentQueryError,
    browse_marketplace_query,
    marketplace_history_query,
    my_marketplace_listings_query,
)
from .phase5_contract import FORMAL_NPC_IDS, STORY_ACTION_ACTORS, STORY_FACT_CODES
from .phase5_story import ARC_CODE, STORY_ACTION_PLANS, _snapshot
from .public_finance import public_finance_query
from .public_governance import ADMIN_ACTIONS, governance_query


QUERY_NAMES = frozenset({
    "GET_RULE",
    "OBSERVE",
    "INSPECT_NEARBY",
    "INSPECT_INVENTORY",
    "INSPECT_TRADE_OFFERS",
    "INSPECT_STORY_STATE",
    "INSPECT_RELEVANT_STORIES",
    "INSPECT_STORY_DETAIL",
    "INSPECT_ORDERS",
    "INSPECT_MARKET_STATE",
    "INSPECT_CARAVAN_REQUESTS",
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


def _farm_catalog_with_output_names() -> dict:
    catalog = farm_crop_catalog()
    names = _item_names(rule["output_item_id"] for rule in catalog.values())
    for rule in catalog.values():
        rule["output_item_name"] = names.get(rule["output_item_id"], "")
    return catalog


_TOOL_ITEM_TYPES = frozenset({
    ItemType.HOE_TOOL,
    ItemType.CHOP_TOOL,
    ItemType.BREAK_TOOL,
    ItemType.REAP_TOOL,
    ItemType.WATER_TOOL,
    ItemType.COLLECT_TOOL,
})

_PERISHABLE_ITEM_TYPES = frozenset({
    ItemType.FOOD,
    ItemType.INGREDIENT,
    ItemType.MEAT,
    ItemType.VEGETABLE,
    ItemType.FRUIT,
    ItemType.SEASONING,
})


def _seed_planting_fields(item_id, item_type, crop_catalog) -> dict:
    if item_type != ItemType.SEED:
        return {}

    rule = next(
        (value for value in crop_catalog.values() if value["seed_item_id"] == item_id),
        None,
    )
    if rule is None:
        return {"planting": {"supported": False}}

    return {
        "planting": {
            "crop_code": rule["crop_code"],
            "harvest_item_id": rule["output_item_id"],
            "harvest_item_name": rule["output_item_name"],
            "harvest_quantity_min": rule["yield_min"],
            "harvest_quantity_max": rule["yield_max"],
            "growth_minutes": rule["growth_minutes"],
        }
    }


def _inventory_item_projection(
    npc, row, item, name, available, minute, crop_catalog, dish_item_ids,
) -> dict:
    item_type = item["item_type"] if item else None
    projected = {
        "item_id": row.item_id,
        "instance_id": str(row.instance_id),
        "name": name,
        "item_type": item_type,
        "quantity": row.item_amount,
        "available_quantity": available,
    }

    sale_fields = _store_listing_fields(
        npc, None, item, set(), slot=row, minute=minute, available=available,
    )
    sale_reason = sale_fields["general_store_sale_unavailable_reason"]
    projected["sale"] = (
        {"target": "GENERAL_STORE", "unit_price": item["item_price"]}
        if sale_reason is None
        else {"allowed": False, "reason": sale_reason}
    )

    projected.update(_seed_planting_fields(row.item_id, item_type, crop_catalog))

    if item_type in _TOOL_ITEM_TYPES:
        durability = {}
        if row.remaining_uses is not None:
            durability["remaining_uses"] = row.remaining_uses
        if item and item["max_use_count"] >= 0:
            durability["max_uses"] = item["max_use_count"]
        if durability:
            projected["durability"] = durability

    if item_type in _PERISHABLE_ITEM_TYPES:
        if row.expires_world_minute is not None:
            projected["expires_world_minute"] = row.expires_world_minute
        if row.freshness_unknown:
            projected["freshness_unknown"] = True

    if item_type == ItemType.FOOD:
        projected["food_instance_id"] = str(row.instance_id)
        if row.item_id in dish_item_ids:
            projected["item_subtype"] = "Dish"
            dish = {"rated": row.rated}
            if row.rated:
                dish["overall_score"] = row.overall_score
            if row.cook_time:
                dish["cook_time_seconds"] = row.cook_time
            if row.ingredient:
                dish["ingredient"] = row.ingredient
            if row.dish_metadata:
                dish["metadata"] = row.dish_metadata
            projected["dish"] = dish

    if item_type == ItemType.USABLE and row.remaining_uses is not None:
        usage = {"remaining_uses": row.remaining_uses}
        if item and item["max_use_count"] >= 0:
            usage["max_uses"] = item["max_use_count"]
        projected["usage"] = usage

    return projected


def _observe(npc: NpcDefinition) -> dict:
    runtime = NpcRuntimeState.objects.get(npc=npc)
    world_minute = _world_minute()
    dialogue = NpcDialogueSession.objects.filter(
        Q(initiator=npc) | Q(target=npc), status="ACTIVE"
    ).first()
    action_end = dialogue.expires_world_minute if dialogue else runtime.action_ends_world_minute
    profile = npc.village_profile
    day_summary = NpcNeedsDaySummary.objects.filter(
        npc=npc, day_index=world_minute // 1440,
    ).first()
    today_sleep_minutes = day_summary.sleep_minutes if day_summary else 0
    today_longest_sleep_minutes = (
        day_summary.longest_continuous_sleep_minutes if day_summary else 0
    )
    previous_core_night_sleep_minutes = (
        day_summary.core_night_sleep_minutes if day_summary else 0
    )
    previous_core_night_penalized = bool(
        day_summary.core_night_penalized if day_summary else False
    )
    known_locations = {}
    if profile is not None:
        for key, code in (
            ("home", profile.home_location_code),
            ("work", profile.work_location_code),
            ("social", profile.social_location_code),
        ):
            if code:
                known_locations[key] = code
    return {
        "self_npc_id": str(npc.agent_id),
        "self_name": profile.display_name if profile else npc.name,
        "world_minute": world_minute,
        "current_location": runtime.current_location_code,
        "known_locations": known_locations,
        "current_action": "DIALOGUE" if dialogue else runtime.current_action or None,
        "action_status": "DIALOGUING" if dialogue else runtime.action_status,
        "action_started_world_minute": dialogue.started_world_minute if dialogue else runtime.action_started_world_minute,
        "action_ends_world_minute": action_end,
        "remaining_minutes": max(0, action_end - world_minute) if action_end is not None else None,
        "active_command_id": str(runtime.active_command_id) if runtime.active_command_id else None,
        "interruptible": runtime.action_status == "SLEEPING" and runtime.current_action == "SLEEP",
        "needs": public_needs(runtime),
        "need_bands": need_bands(runtime),
        "today_sleep_minutes": today_sleep_minutes,
        "continuous_sleep_minutes": runtime.continuous_sleep_minutes,
        "today_longest_continuous_sleep_minutes": today_longest_sleep_minutes,
        "previous_core_night_sleep_minutes": previous_core_night_sleep_minutes,
        "previous_core_night_penalized": previous_core_night_penalized,
        "sleep": {
            "today_sleep_minutes": today_sleep_minutes,
            "continuous_sleep_minutes": runtime.continuous_sleep_minutes,
            "today_longest_continuous_sleep_minutes": today_longest_sleep_minutes,
            "previous_core_night_sleep_minutes": previous_core_night_sleep_minutes,
            "previous_core_night_penalized": previous_core_night_penalized,
        },
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


def _store_listing_fields(npc, store_code, item, restaurant_items, *, slot=None, minute=None, available=None):
    """Item eligibility in market queries; actual batch eligibility in inventory."""
    from .lucy_cooking import fixed_price
    from .phase3_direct_trade import general_store_tool_sale_reason

    reason = None
    if not store_code:
        reason = "NO_OPERATED_STORE"
    elif not item:
        reason = "TRADE_ITEM_NOT_FOR_SALE"
    elif store_code == "RESTAURANT_STORE":
        if item["item_id"] not in restaurant_items:
            reason = "RESTAURANT_DISH_REQUIRED"
        elif slot is not None:
            try:
                fixed_price(slot.item_id, {"rated": slot.rated, "rating_price": slot.rating_price,
                    "dish_quality": slot.dish_quality, "dish_metadata": slot.dish_metadata})
            except ValueError as exc:
                reason = str(exc)
    batch_reason = None
    if slot is not None:
        if slot.freshness_unknown:
            batch_reason = "FRESHNESS_UNKNOWN"
        elif slot.expires_world_minute is not None and slot.expires_world_minute <= minute:
            batch_reason = "ITEM_EXPIRED"
        elif not available:
            batch_reason = "ITEM_RESERVED" if slot.item_amount > 0 else "TRADE_INSUFFICIENT_ITEM"
    reason = reason or batch_reason
    sell_reason = batch_reason
    if str(npc.agent_id) not in FORMAL_NPC_IDS - {"10015"}:
        sell_reason = "TRADE_STORE_FORBIDDEN"
    elif not item or item["item_price"] <= 0:
        sell_reason = "ITEM_VALUE_UNAVAILABLE"
    elif slot is not None:
        sell_reason = sell_reason or general_store_tool_sale_reason(
            item["item_type"], item["max_use_count"], slot.remaining_uses,
        )
    return {
        "own_store_code": store_code,
        "can_list_in_store": reason is None,
        "can_list_in_own_store": reason is None,
        "own_store_listing_unavailable_reason": reason,
        "can_sell_to_general_store": sell_reason is None,
        "general_store_sale_unavailable_reason": sell_reason,
        "listing_eligibility_scope": "BATCH" if slot is not None else "ITEM_TYPE_ONLY",
    }


def _restaurant_item_ids():
    from .lucy_catalog import RELIEF_PACK
    return set(Recipe.objects.filter(pk__in=RECIPE_WHITELIST).exclude(item_id=RELIEF_PACK)
               .values_list("item_id", flat=True))


def _inventory(npc: NpcDefinition, query_params=None) -> dict:
    from .caravan import store_finances
    from .jack_production import production_info
    from .phase3_direct_trade import _get_buyer_available_money
    inventory = NpcGlobalInventory.objects.get(npc=npc)
    minute = GlobalGameTimeState.objects.get(pk=1).world_minute
    slots = list(inventory.slots.filter(item_amount__gt=0).order_by("slot_index"))
    item_ids = (query_params or {}).get("item_ids")
    if item_ids is not None:
        if isinstance(item_ids, str):
            try:
                item_ids = json.loads(item_ids)
            except (ValueError, TypeError):
                raise ValueError("item_ids must be an array of item IDs")
        if not isinstance(item_ids, list) or not item_ids or len(item_ids) > 100 or any(
            type(item_id) is not int or item_id <= 0 for item_id in item_ids
        ):
            raise ValueError("item_ids must be a nonempty array of positive integers")
        slots = [slot for slot in slots if slot.item_id in item_ids]
    item_details = {
        row["item_id"]: row
        for row in Item.objects.filter(
            item_id__in={slot.item_id for slot in slots}, is_deleted=False
        ).values("item_id", "item_price", "item_type", "max_use_count")
    }
    names = _item_names(row.item_id for row in slots)
    dish_item_ids = _restaurant_item_ids()
    available = {row.instance_id: get_available_slot_amount(row, minute) for row in slots}
    crop_catalog = _farm_catalog_with_output_names()
    result = {
        "money": inventory.money,
        "available_money": _get_buyer_available_money(inventory),
        "items": [
            _inventory_item_projection(
                npc, row, item_details.get(row.item_id), names.get(row.item_id, ""),
                available[row.instance_id], minute, crop_catalog, dish_item_ids,
            )
            for row in slots
        ],
    }
    result.update(total_count=len(slots), has_more=False)
    if item_ids is not None:
        result["filter"] = {"item_ids": sorted(set(item_ids))}
    tools = {}
    for slot in slots:
        if slot.remaining_uses is None:
            continue
        summary = tools.setdefault(slot.item_id, dict(item_id=slot.item_id, item_name=names.get(slot.item_id, ""),
            quantity=0, usable_count=0, total_remaining_uses=0))
        summary["quantity"] += slot.item_amount
        usable = available[slot.instance_id] if slot.remaining_uses > 0 else 0
        summary["usable_count"] += usable
        summary["total_remaining_uses"] += usable * max(0, slot.remaining_uses)
    if tools:
        result["tool_summaries"] = list(tools.values())
    if item_ids is not None:
        # A filtered item lookup need not repeat wallets, store finances or production state.
        result.pop("money", None)
        result.pop("available_money", None)
        return result
    finances = store_finances(npc.agent_id)
    if finances is not None:
        result["store_finances"] = finances
    if str(npc.agent_id) == "10012":
        result["production"] = production_info(npc, minute)
    return result


def _trade_offers(npc: NpcDefinition) -> dict:
    rows = list(VillageTradeOffer.objects.select_related(
        "buyer_npc", "seller_npc", "proposer_npc"
    ).filter(
        Q(buyer_npc=npc) | Q(seller_npc=npc), status="PENDING",
        expires_world_minute__gt=GlobalGameTimeState.objects.get(pk=1).world_minute,
    ).order_by("created_world_minute", "offer_id"))
    minute = GlobalGameTimeState.objects.get(pk=1).world_minute
    recent = VillageTradeOffer.objects.select_related("buyer_npc", "seller_npc").filter(
        Q(buyer_npc=npc) | Q(seller_npc=npc)).exclude(status="PENDING", expires_world_minute__gt=minute)
    recent_count = recent.count()
    recent = list(recent.order_by("-updated_at")[:20])
    names = _item_names(row.item_id for row in rows + recent)
    return {"offers_scope": "PENDING_UNEXPIRED", "recent_offers_scope": "LATEST_20_RESOLVED_OR_EXPIRED",
        "has_more_recent_offers": recent_count > len(recent),
        "recent_offers": [{"offer_id": str(row.offer_id), "status": "EXPIRED" if row.status == "PENDING" else row.status,
            "item_id": row.item_id, "item_name": names.get(row.item_id), "amount": row.amount, "gold_amount": row.gold_amount,
            "buyer_npc_id": str(row.buyer_npc.agent_id), "seller_npc_id": str(row.seller_npc.agent_id),
            "resolved_world_minute": row.resolved_world_minute, "expires_world_minute": row.expires_world_minute} for row in recent],
        "offers": [{
        "offer_id": str(row.offer_id),
        "direction": "OUTGOING" if row.proposer_npc_id == npc.pk else "INCOMING",
        "side": "BUY" if row.proposer_npc_id == row.buyer_npc_id else "SELL",
        "proposer_npc_id": str(row.proposer_npc.agent_id),
        "buyer_npc_id": str(row.buyer_npc.agent_id),
        "seller_npc_id": str(row.seller_npc.agent_id),
        "item_id": row.item_id,
        "item_name": names.get(row.item_id),
        "quantity": row.amount,
        "gold_amount": row.gold_amount,
        "store_code": row.buying_store_id,
        "location_code": row.location_code,
        "expires_world_minute": row.expires_world_minute,
    } for row in rows]}


def _story_state(npc: NpcDefinition) -> dict:
    from .story_state_projection import project_story_state

    arc = VillageStoryArc.objects.filter(arc_code=ARC_CODE).first()
    if arc is None:
        return {
            "status": "INACTIVE",
            "available_actions": [],
            "available_actions_explanation": "当前没有可公开的剧情行动。",
        }
    detail = _broken_trade_route_detail(npc)
    if detail is None:
        return {
            "story_id": arc.arc_code,
            "status": "UNAVAILABLE",
            "available_actions": [],
            "available_actions_explanation": "当前剧情状态尚未准备好，暂时没有可公开行动。",
        }
    return project_story_state(
        detail, npc.agent_id, npc.village_profile.capabilities or [],
        _world_minute(), arc.outcome,
    )


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
    if str(npc.agent_id) == "10013":
        available_actions = sorted(set(available_actions) | ADMIN_ACTIONS)
        if arc.current_phase == "AFTERMATH" and not arc.is_completed:
            available_actions.remove("CALL_MEETING")
    if str(npc.agent_id) == "10015":
        available_actions = sorted(set(available_actions) | {"CONTACT_EMERGENCY_CARAVAN"})
    governance = governance_query(arc, npc.agent_id, _world_minute())
    if str(npc.agent_id) == "10015" and any(
        d["plan_code"] == "RATION_AND_WAIT" and d["published_world_minute"] is not None
        and d["revoked_world_minute"] is None and d["terms"].get("expires_world_minute", 0) > _world_minute()
        for d in governance["public_decisions"]
    ):
        available_actions = sorted(set(available_actions) | {"ENABLE_STORE_LIMITS"})
    runtime = NpcRuntimeState.objects.filter(npc=npc).first()
    unavailable = runtime is not None and (
        runtime.health_fp <= 300 or runtime.action_status in {"AWAY", "SLEEPING"}
        or (runtime.away_until_world_minute is not None and runtime.away_until_world_minute > _world_minute())
    )
    if unavailable:
        available_actions = []
        waiting_reason = "当前失能、睡眠或缺席，恢复后重新查询可履行的公共事务。"
    elif available_actions:
        waiting_reason = "以下为角色当前阶段的公共能力；执行仍须满足地点、会议提案、有效投票或既有授权等条件。"
    elif snapshot["is_completed"]:
        waiting_reason = "主线剧情已完成；已公布的公共授权仍按各自期限执行。"
    elif meeting is not None and "STORY_VOTE" in {
        str(value).strip().upper() for value in (npc.village_profile.capabilities or [])
    }:
        waiting_reason = f"公开会议进行中，可参加会议；会议在第{meeting.closes_day}日结束。"
    elif snapshot["phase"] == "EXECUTION":
        waiting_reason = "当前阶段没有分配给你的可执行公开行动。"
    elif snapshot["phase"] == "AFTERMATH":
        # 二跑复盘（R3）：写明推进条件，避免 agent 反复查询同一空窗结论。
        waiting_reason = (
            "当前阶段（AFTERMATH）不开放执行行动；日结评估满足任一条件后自动进入下一阶段："
            "距起始日满 1 天、粮食覆盖不足 4 天、或工具覆盖不足 7 天。"
            "此前可处理日常生计（种植/狩猎/备货/就餐），也可以等待。"
        )
    else:
        waiting_reason = "当前阶段不开放执行行动，等待后续日结评估推进，届时重新查看剧情。"
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
        "available_actions_explanation": waiting_reason,
        "known_facts": known_facts,
        "retired_plan": arc.current_plan == "FOREST_ROUTE",
        "emergency_route_rule": "Nora个人支付一次开通费，无森林侦察、补给、会议或剧情阶段前置；11:00后当前到访路线已冻结，次日生效。",
        **governance,
    }


_STORY_PROJECTORS = {ARC_CODE: _broken_trade_route_detail}


def _story_detail(npc: NpcDefinition, story_id: str) -> dict | None:
    if str(npc.agent_id) not in FORMAL_NPC_IDS:
        return None
    projector = _STORY_PROJECTORS.get(story_id)
    detail = projector(npc) if projector else None
    if detail is None:
        return None
    from .story_state_projection import project_story_state
    arc = VillageStoryArc.objects.filter(arc_code=story_id).first()
    projected = project_story_state(detail, npc.agent_id, npc.village_profile.capabilities or [],
                                    _world_minute(), arc.outcome if arc else None)
    projected["type"] = detail.get("type", "MAIN")
    return projected


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
            "item_id": row.seller_order.item_id,
            "buyer_order_id": str(row.buyer_order.order_id),
            "seller_order_id": str(row.seller_order.order_id),
            "buyer_npc_id": str(row.buyer_order.npc.agent_id),
            "seller_npc_id": str(row.seller_order.npc.agent_id),
            "delivery_point_code": row.delivery_point_code,
            "quantity": row.reserved_amount,
            "expires_world_minute": row.expires_world_minute,
            "role": "SELLER" if row.seller_order.npc_id == npc.pk else "BUYER",
        } for row in deliveries],
    }


def _strict_query_int(query_params, name: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    value = (query_params or {}).get(name, default)
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer string")
    if isinstance(value, str):
        if not value.isdigit():
            raise ValueError(f"{name} must be an integer string")
        value = int(value)
    elif isinstance(value, int):
        value = int(value)
    else:
        raise ValueError(f"{name} must be an integer string")
    if value < minimum or maximum is not None and value > maximum:
        raise ValueError(f"{name} is out of range")
    return value


def _market_state(_npc: NpcDefinition, query_params=None) -> dict:
    item_id = None
    if (query_params or {}).get("item_id") is not None:
        item_id = _strict_query_int(query_params, "item_id", 0)
    page = _strict_query_int(query_params, "page", 1, minimum=1)
    page_size = _strict_query_int(query_params, "page_size", 20, minimum=1, maximum=50)
    world_minute = _world_minute()
    from .caravan import agent_projection
    result = agent_projection(world_minute)
    items = result["items"]
    if item_id is not None:
        items = [row for row in items if row["item_id"] == item_id]
        result["item_id"] = item_id
        result["item_found"] = bool(items)
    total = len(items)
    start = (page - 1) * page_size
    result.update(world_minute=world_minute, items=items[start:start + page_size],
                  total=total, page=page, page_size=page_size, has_more=start + page_size < total)
    if result["has_more"]:
        result["next_page"] = page + 1
    return result


def _caravan_requests(_npc: NpcDefinition) -> dict:
    from .caravan import request_projection
    minute = _world_minute()
    return {"world_minute": minute, **request_projection(minute)}


def _farm_state(_npc: NpcDefinition, query_params=None) -> dict:
    plot_code = None
    if query_params is not None and "plot_code" in query_params:
        value = query_params.get("plot_code")
        if not isinstance(value, str) or not (plot_code := value.strip().upper()) or plot_code not in PLOT_CODES:
            raise ValueError("plot_code must be one of the allowed plot codes")
    world_minute = _world_minute()
    crop_catalog = farm_crop_catalog()
    plots = []
    plot_filter = {"plot_code": plot_code} if plot_code is not None else {"plot_code__in": PLOT_CODES}
    for row in VillageFarmPlot.objects.filter(**plot_filter).order_by("plot_code"):
        status = row.status
        if status == FarmPlotStatus.GROWING and row.mature_world_minute is not None and world_minute >= row.mature_world_minute:
            status = FarmPlotStatus.MATURE
        plot = {
            "plot_code": row.plot_code,
            "status": status,
        }
        if status != FarmPlotStatus.EMPTY:
            crop_rule = crop_catalog.get(row.crop_code, {})
            plot["crop_code"] = row.crop_code
            plot["crop_name"] = crop_rule.get("display_name", row.crop_code)
            if status == FarmPlotStatus.MATURE:
                plot["harvestable"] = True
            else:
                plot["remaining_minutes"] = max(
                    0, (row.mature_world_minute or world_minute) - world_minute
                )
        plots.append(plot)
    return {"plots": plots}


def _cooking_recipes() -> list:
    return [{"recipe_id": recipe.pk, "name": recipe.recipe_name,
             "output_item_id": recipe.item_id, "output_quantity": recipe.output_quantity,
             "ingredients": [{"item_id": row.item_id, "name": row.item_name,
                              "required_quantity": row.count} for row in recipe.ingredients.all()]}
            for recipe in Recipe.objects.prefetch_related("ingredients").filter(
                pk__in=RECIPE_WHITELIST).order_by("pk")]


def _restaurant_state(npc: NpcDefinition) -> dict:
    from .caravan import store_finances
    from .lucy_cooking import pending_reviews
    from .lucy_restaurant import public_meal_availability
    from .phase3_direct_trade import query_store_inventory, store_inventory_status
    world_minute = _world_minute()
    store = VillageStore.objects.filter(pk="RESTAURANT_STORE").first()
    if store is None:
        return {"store_code": "RESTAURANT_STORE", "status": "UNAVAILABLE", "menu": None}
    menu = query_store_inventory("RESTAURANT_STORE", world_minute, npc.agent_id)[1]
    data = {"store_code": store.pk, "location_code": store.location_code,
            "status": store.status, "inventory_status": store_inventory_status(store, world_minute)[0],
            "menu": menu}
    # Public visitors never receive another business's account or private samples.
    if store.operator_npc_id != npc.pk:
        return data
    inventory = NpcGlobalInventory.objects.get(npc=npc)
    available = {}
    for slot in inventory.slots.filter(item_amount__gt=0):
        available[slot.item_id] = available.get(slot.item_id, 0) + get_available_slot_amount(slot, world_minute)
    recipe_data = _cooking_recipes()
    for recipe in recipe_data:
        for ingredient in recipe["ingredients"]:
            ingredient["available_quantity"] = available.get(ingredient["item_id"], 0)
        recipe["can_cook"] = bool(recipe["ingredients"]) and all(
            i["available_quantity"] >= i["required_quantity"] for i in recipe["ingredients"])
    finances = store_finances(npc.agent_id, store_code=store.pk)
    if finances and finances["store_code"] == store.pk:
        data["store_finances"] = finances
    data["pending_reviews"] = [dict(review, reserved_amount=1)
                               for review in pending_reviews(npc, world_minute)["reviews"]]
    data["public_meal_availability"] = public_meal_availability(npc, world_minute, recipe_data, inventory)
    return data


def _rateable_dishes(_npc: NpcDefinition, query_params=None) -> dict:
    from .lucy_cooking import pending_reviews
    return pending_reviews(_npc, _world_minute(), (query_params or {}).get("review_id"))



def _public_economy(_npc: NpcDefinition) -> dict:
    world_minute = _world_minute()
    day_index = world_minute // 1440
    account = VillageAccount.objects.filter(account_code="PUBLIC_FUND").first()
    snapshot = DailyPriceSnapshot.objects.filter(day_index=day_index).first()
    price_index_status = snapshot.status if snapshot else "MISSING"
    audited = VillageWorldEvent.objects.filter(
        event_type="ECONOMY_AUDIT_PUBLIC_FUND",
        world_minute__gte=day_index * 1440,
        world_minute__lt=(day_index + 1) * 1440,
    ).exists()
    return {
        "day_index": day_index,
        "public_fund_balance": account.balance if account else None,
        "public_fund_available": account is not None,
        "price_index": None if snapshot is None else {
            "status": snapshot.status,
            "prices": dict(snapshot.prices or {}),
            "price_items": [{"item_id": int(item_id), "unit_price": price}
                            for item_id, price in (snapshot.prices or {}).items()],
            "published_world_minute": snapshot.published_world_minute,
        },
        "price_index_status": price_index_status,
        "draft_exists": snapshot is not None and price_index_status == "DRAFT",
        "published": price_index_status == "PUBLISHED",
        "audited_today": audited,
        **public_finance_query(world_minute),
    }


def _browse_marketplace(npc: NpcDefinition, query_params=None) -> dict:
    return browse_marketplace_query(npc, query_params)


def _my_marketplace_listings(npc: NpcDefinition, query_params=None) -> dict:
    return my_marketplace_listings_query(npc, query_params)


def _marketplace_history(npc: NpcDefinition, query_params=None) -> dict:
    return marketplace_history_query(npc, query_params)


def _rules(npc: NpcDefinition) -> dict:
    """The NPC's current environment rules, identical to the discover projection.

    Different NPCs get different rules because the text is generated from each
    NPC's enabled instruction set.
    """
    from .agent_context import build_agent_context
    from .runtime_services import CommandService

    actions = CommandService.allowed_actions_for(npc)
    if "SLEEP" in actions:
        actions.add("WAKE_UP")
    data = build_agent_context(actions, profile=npc.village_profile)
    if "COOK" in actions:
        data["cooking_recipes"] = _cooking_recipes()
    return data


def _authorized(_npc: NpcDefinition) -> dict:
    """Capability gate for parameterized legacy query endpoints."""
    return {"authorized": True}


_HANDLERS = {
    "GET_RULE": _rules,
    "OBSERVE": _observe,
    "INSPECT_NEARBY": _nearby,
    "INSPECT_INVENTORY": _inventory,
    "INSPECT_TRADE_OFFERS": _trade_offers,
    "INSPECT_STORY_STATE": _story_state,
    "INSPECT_RELEVANT_STORIES": _relevant_stories,
    "INSPECT_ORDERS": _orders,
    "INSPECT_MARKET_STATE": _market_state,
    "INSPECT_CARAVAN_REQUESTS": _caravan_requests,
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
    from .agent_feedback import add_item_names
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
            return (True, "OK", add_item_names(data)) if data is not None else (False, "STORY_NOT_VISIBLE", None)
        if query_name in {"BROWSE_MARKETPLACE", "CHECK_MY_MARKETPLACE_LISTINGS", "CHECK_MARKETPLACE_HISTORY"}:
            return True, "OK", add_item_names(_HANDLERS[query_name](npc, query_params))
        handler = _HANDLERS[query_name]
        if query_name in {"INSPECT_MARKET_STATE", "INSPECT_FARM_STATE", "INSPECT_INVENTORY", "INSPECT_RATEABLE_DISHES"}:
            try:
                return True, "OK", add_item_names(handler(npc, query_params))
            except ValueError:
                return False, "INVALID_QUERY_PARAMS", None
        return True, "OK", add_item_names(handler(npc))
    except AgentQueryError as error:
        return False, error.code, error.data
    except (NpcRuntimeState.DoesNotExist, NpcGlobalInventory.DoesNotExist, NpcProfile.DoesNotExist):
        return False, "NPC_NOT_READY", None
