"""Trusted Env player actions, serialized with the existing world/economy lock.

All financial promises live in commission escrow. No client supplies time, NPC identity,
price, reward, or stock. A failed action rolls back before its receipt is saved.
"""
import copy
import hashlib
import json
import logging
import random
import uuid
from contextlib import nullcontext
from time import perf_counter

from django.db import connection, models, transaction
from django.utils import timezone

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from items.quality import production_quality, player_skill_level, quality_rng
from .phase3_asset_service import deterministic_rng
from inventory.models import InventoryBag, InventorySlot
from items.models import Item
from marketplace.models import MarketListing, MarketListingStatus
from marketplace.services import buy_listing, create_listing, delist_listing, get_player_main_bag, _listing_batch
from marketplace.serializers import serialize_listing, serialize_transaction
from inventory.serializers import serialize_bag
from .models import (VillagePlayerActionReceipt,
                     VillagePlayerContribution, VillageSimulationConfig, VillageWorldEvent, VillageAffair)
from .lucy_catalog import MEALS


LOCATIONS = {
    "claim_starter_hunting_kit": {"HUNTING_GATE"},
    "start_safe_rest": {"VILLAGE_SQUARE"},
    "finish_safe_rest": {"VILLAGE_SQUARE"},
    "start_game_processing": {"BUTCHER_YARD"},
    "claim_game_processing": {"BUTCHER_YARD"},
    "buy_marketplace": {"MARKET_STREET"},
    "list_marketplace": {"MARKET_STREET"},
    "delist_marketplace": {"MARKET_STREET"},
    "buy_store": {"GENERAL_STORE", "RESTAURANT"},
    "sell_store": {"GENERAL_STORE"},
    "supply_deliver": {"GENERAL_STORE"},
    "cook_dish": {"RESTAURANT"},
    "start_cooking": {"RESTAURANT"},
    "set_cooking_heat": {"RESTAURANT"},
    "prepare_business_orders": {"RESTAURANT"},
    "request_dish_review": {"RECORD_HALL"},
    **{action: {"PLAYER_STALL"} for action in ("open_player_stall", "list_stall_dish",
        "withdraw_stall_dish", "upgrade_player_stall")},
}
PARAMS = {
    "deliver_caravan_order": {"order_id", "item_id", "instance_id", "quantity", "expected_unit_price"},
    "start_mastery": {"skill_code"},
    "contribute_mastery": {"skill_code", "selections", "expected_quote"},
    "claim_mastery": {"skill_code"},
    "prepare_professional_contracts": set(),
    "claim_starter_hunting_kit": set(),
    "repair_hunting_weapon": {"instance_id", "expected_quote"},
    "start_safe_rest": set(),
    "finish_safe_rest": set(),
    "preview_game_processing": {"prey_instance_id", "quantity"},
    "start_game_processing": {"prey_instance_id", "quantity", "expected_quote"},
    "cancel_game_processing": {"job_id"},
    "claim_game_processing": {"job_id"},
    "overview": {"section"},
    "plant_state": set(),
    "contribute": {"affair_id", "condition_id", "kind", "item_id", "quantity", "instance_id"},
    "labor_start": {"affair_id", "condition_id"}, "labor_cancel": {"labor_id"},
    "commission_accept": {"commission_id"},
    "commission_contribute": {"commission_id", "condition_id", "quantity", "item_id", "instance_id"},
    "commission_labor_start": {"commission_id", "condition_id"},
    "commission_abandon": {"commission_id"},
    "buy_marketplace": {"listing_id", "quantity", "expected_unit_price"},
    "list_marketplace": {"instance_id", "quantity", "unit_price"},
    "delist_marketplace": {"listing_id"},
    "buy_store": {"listing_id", "quantity", "expected_unit_price", "expected_quality"},
    "sell_store": {"instance_id", "quantity", "expected_unit_price"},
    "collect": {"node_id", "attempt_id", "collect_type", "category", "tool_instance_id"},
    "supply_accept": {"order_id"},
    "supply_deliver": {"order_id", "instance_id", "quantity"},
    "supply_abandon": {"order_id"},
    "track_goal": {"goal_code"},
    "claim_goal_rewards": set(),
    "repair_tool": {"instance_id", "expected_quote"},
    "upgrade_tool": {"instance_id", "expected_quote"},
    "claim_starter_axe": set(),
    "preview_cooking": {"recipe_id", "batches", "selections", "tool_instance_id", "cut_style"},
    "cook_dish": {"recipe_id", "batches", "selections", "tool_instance_id", "expected_quote", "cut_style", "session_id"},
    "start_cooking": {"recipe_id", "batches", "selections", "tool_instance_id", "expected_quote", "cut_style"},
    "set_cooking_heat": {"session_id", "heat"},
    "cancel_cooking": {"session_id"},
    "request_dish_review": {"instance_id"},
    "cancel_dish_review": {"review_id"},
    "claim_dish_review": {"review_id"},
    "open_player_stall": set(),
    "list_stall_dish": {"instance_id", "quantity"},
    "withdraw_stall_dish": {"menu_id"},
    "upgrade_player_stall": {"expected_quote"},
    "prepare_business_orders": set(),
}
COLLECTION_LOCATIONS = {"采集食材": "FOREST_EDGE", "采集林下食材": "FOREST_UNDERGROWTH"}
from .professional_catalog import PROFESSIONAL_COLLECT_TYPES
COLLECTION_LOCATIONS.update({name: "DEEP_FOREST" for name in PROFESSIONAL_COLLECT_TYPES})
LOCATIONS["prepare_professional_contracts"] = {"GENERAL_STORE"}
from .player_farm import ACTIONS as FARM_ACTIONS
COOKING_CONTROLS = {"start_cooking", "set_cooking_heat", "cancel_cooking"}
from .plant_catalog import ACTIONS as PLANT_ACTIONS
PARAMS.update(PLANT_ACTIONS)
PARAMS.update(FARM_ACTIONS)
from player.tutorials import CONTROL_PARAMS as TUTORIAL_PARAMS
PARAMS.update(TUTORIAL_PARAMS)
from player.arrival import CONTROL_PARAMS as ARRIVAL_PARAMS
PARAMS.update(ARRIVAL_PARAMS)
LOCATIONS.update({action: {"BILL_FARM"} for action in FARM_ACTIONS})


class ActionError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


def fail(code, message):
    raise ActionError(code, message)


def positive(value):
    if type(value) is not int or not 0 < value <= 1000000:
        fail("INVALID_PARAMS", "数量必须是正整数。")
    return value


def key(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        fail("INVALID_PARAMS", "请求或目标标识无效。")


def market_key(value):
    if type(value) not in (str, int) or not str(value).isascii() or not str(value).isdigit() or len(str(value)) > 18:
        fail("INVALID_PARAMS", "挂单标识无效。")
    return int(value)


def response(request_id, code="OK", message="操作完成。", data=None):
    return dict(success=code == "OK", result_code=code, message=message,
                request_id=str(request_id or ""), data=data or {})


def _batch_data(slot, minute, names=None):
    return dict(instance_id=str(slot.instance_id), item_id=slot.item_id,
        item_name=(names or {}).get(slot.item_id, str(slot.item_id)), quantity=slot.item_amount,
        quality=slot.quality, remaining_uses=slot.remaining_uses, expires_world_minute=None,
        remaining_freshness_minutes=None, freshness_unknown=False,
        usable=slot.item_amount > 0)


def _consume(bag, item_id, quantity, minute, *, instance_id=None, min_quality=1, min_remaining=0, perishable=False):
    slots = InventorySlot.objects.select_for_update().filter(bag=bag, item_id=item_id, item_amount__gt=0)
    if instance_id:
        slots = slots.filter(instance_id=key(instance_id))
    rows = list(slots.order_by("quality", "slot_index"))
    usable = [s for s in rows if s.quality >= min_quality]
    if sum(s.item_amount for s in usable) < quantity:
        fail("INSUFFICIENT_QUALIFIED_ITEMS", "背包中符合数量、品质和保鲜要求的物品不足。")
    result = []
    for slot in usable:
        take = min(quantity, slot.item_amount)
        slot.item_amount -= take
        slot.save(update_fields=["item_amount"])
        result.append((slot, take))
        quantity -= take
        if quantity == 0:
            break
    return result


def _record(player, rid, kind, item_id, quantity, earned_gold, batches, clock, location):
    snapshots = [{**_batch_data(slot, clock.world_minute), "quantity": amount} for slot, amount in batches]
    row = VillagePlayerContribution.objects.create(player=player, request_id=rid, kind=kind,
        item_id=item_id, quantity=quantity, earned_gold=earned_gold, batches=snapshots, world_minute=clock.world_minute)
    item = Item.objects.filter(pk=item_id).first()
    summary = f"{player.username}贡献{item.item_name if item else item_id}×{quantity}"
    if kind == "fund":
        summary = f"{player.username}贡献工程金币×{quantity}"
    event = VillageWorldEvent.objects.create(dedupe_key=f"player:{player.pk}:{rid}",
        request_id=rid, event_type="PLAYER_VILLAGE_"+kind.upper(), world_minute=clock.world_minute,
        time_revision=clock.time_revision, player_ids=[str(player.pk)], location_code=location,
        participant_npc_ids=[], visibility="PUBLIC",
        player_summary=summary, hub_summary=summary,
        public_data=dict(player_id=str(player.pk), kind=kind, item_id=item_id, quantity=quantity, earned_gold=earned_gold))
    from .phase4_visibility import enqueue_event_deliveries
    enqueue_event_deliveries(event)
    return row


def _affair_projection(affair):
    from .affairs import project_affair
    data = project_affair(affair)
    names = dict(Item.objects.filter(is_deleted=False).values_list("item_id", "item_name"))
    for entry in data.get("materials", []):
        entry["item_name"] = names.get(entry["item_id"], str(entry["item_id"]))
    data["fund"] = data.get("gold", {})
    return data


def _project(minute):
    """Compatibility projection for older clients; affairs own all progress."""
    affair = VillageAffair.objects.filter(pk="BRIDGE_REPAIR").first()
    if affair:
        return _affair_projection(affair), None, None
    return dict(available=False, reason="村庄事务尚未初始化。", materials=[],
                meals=dict(target=20, submitted=0, remaining=20, item_ids=list(MEALS))), None, None


def _get_affair(params, minute):
    from .affairs import ensure_bridge_affair
    if not VillageSimulationConfig.objects.filter(config_key="default", village_story_enabled=True).exists():
        fail("FEATURE_DISABLED", "村庄事务尚未开放。")
    affair_id = params.get("affair_id", "BRIDGE_REPAIR")
    if not isinstance(affair_id, str) or not affair_id or len(affair_id) > 64:
        fail("INVALID_PARAMS", "事务标识无效。")
    if affair_id == "BRIDGE_REPAIR":
        ensure_bridge_affair(minute)
    affair = VillageAffair.objects.select_for_update().filter(pk=affair_id, kind="RESOURCE_PROJECT").first()
    if not affair:
        fail("AFFAIR_NOT_FOUND", "事务不存在。")
    return affair


def overview(player, bag, minute, *, read_only=False, compact=False, plant_result=None, store_only=False, farm_only=False):
    from player.progression import growth_snapshot
    from player.mastery import mastery_overview
    from .cooperation import cooperation_overview
    from .professional_contracts import professional_contracts_overview
    from player.starter import starter_overview
    from .player_gathering import gathering_overview
    from .production_guide import production_overview
    from .player_cooking import cooking_overview
    from .player_dish_reviews import review_overview
    from .player_hunting import hunting_overview
    from .player_processing import processing_overview
    from restaurant.player_stall import business_overview
    from .business_orders import business_orders_overview
    from .advanced_gathering import collection_points_projection
    from .daily_events import project_daily_events
    from inventory.batches import project_item_attributes
    from .lucy_restaurant import public_meal_quota
    from .player_farm import farm_overview
    from .affairs import project_affairs
    from .personal_commissions import list_commissions
    from .cooperation import decorate_commission_rewards
    from .models import VillageAffairContribution, VillageStore
    from inventory.models import CollectionPoint
    from player.models import PlayerState
    from player.services import recover_action_points
    if read_only or compact:
        state = PlayerState.objects.filter(player_id=str(player.pk)).first() or PlayerState(player_id=str(player.pk))
        if store_only and not read_only:
            if not state.pk:
                state.save()
            recover_action_points(state)
    else:
        # Initialization/recovery is short; building panels must not hold the writer lock.
        with transaction.atomic():
            clock = GlobalGameTimeState.objects.select_for_update().get(pk=1)
            minute = clock.world_minute
            state, _ = PlayerState.objects.get_or_create(player_id=str(player.pk))
            state = PlayerState.objects.select_for_update().get(pk=state.pk)
            recover_action_points(state)
            from .affairs import ensure_bridge_affair
            ensure_bridge_affair(minute)
            from .personal_commissions import expire_commissions
            from .starter_supply import expire_supply
            from .player_farm import maintain_leases
            expire_commissions(minute)
            expire_supply(minute)
            maintain_leases(minute, clock.time_revision)
            from .production_tools import _initial_tool_listing
            store = VillageStore.objects.select_for_update().filter(pk="GENERAL_STORE").first()
            if store:
                for item in Item.objects.filter(pk__in=(4001, 4101), is_deleted=False):
                    _initial_tool_listing(store, item.pk, max(1, item.item_price), item.max_use_count)
    from .player_affair_labor import current_labor
    slots = bag.slots.filter(item_amount__gt=0).select_related("usage", "dish", "review") if bag.pk else []
    if plant_result is not None:
        changed = [row["instance_id"] for row in plant_result["collected_items"]]
        if plant_result.get("tool_used"):
            changed.append(plant_result["tool_used"]["instance_id"])
        slots = slots.filter(instance_id__in=changed)
    slots = list(slots)
    item_rows = Item.objects.filter(is_deleted=False)
    if plant_result is not None or store_only or farm_only:
        item_rows = item_rows.filter(pk__in={slot.item_id for slot in slots})
    items = {item.pk: item for item in item_rows}
    names = {item_id: item.item_name for item_id, item in items.items()}
    from .player_store import store_sale_quote
    if bag.pk:
        bag.refresh_from_db()
    from .plant_gathering import overview as plant_overview
    growth = growth_snapshot(player, read_only=read_only or compact, compact=plant_result is not None or store_only or farm_only)
    gathering_level = next(skill["level"] for skill in growth["skills"] if skill["code"] == "gathering")
    projection = dict(world_minute=minute, player_id=str(player.pk), player_name=player.username, money=bag.money,
        action_points=state.action_points, action_points_max=state.action_points_max,
        growth=growth,
        inventory=[{**_batch_data(s, minute, names),
            "attributes": project_item_attributes(s, items.get(s.item_id), minute),
            "item_type": items[s.item_id].item_type if s.item_id in items else "",
            "general_store_sale": store_sale_quote(s, items.get(s.item_id), minute)}
            for s in slots])
    if store_only or farm_only:
        # Site receipts refresh assets and onboarding without rebuilding unrelated panels.
        from player.tutorials import tutorial_overview
        projection["starter"] = starter_overview(player, bag, minute, read_only=read_only)
        if farm_only:
            projection["farm"] = farm_overview(player, bag, minute, read_only=read_only, growth=growth, slots=slots)
            tutorial_context = projection
        else:
            from .models import VillageFarmPlot
            from .player_farm import project_plot
            general_store = VillageStore.objects.filter(pk="GENERAL_STORE").values("status", "account__balance").first()
            projection["general_store"] = dict(status=general_store["status"], coin_balance=general_store["account__balance"]) if general_store else None
            projection["starter"]["gathering"] = gathering_overview(player, bag, minute)
            tutorial_context = dict(projection, farm=dict(plots=[project_plot(plot, minute)
                for plot in VillageFarmPlot.objects.select_related("lessee_player").filter(lessee_player=player)]))
        projection["tutorial"] = tutorial_overview(player, bag, minute, tutorial_context, read_only=read_only)
        return projection
    projection["plants"] = plant_overview(player, bag, minute, level=gathering_level,
        node_id=plant_result["node_id"] if plant_result is not None else None)
    # Collection acknowledges assets immediately; the existing client refresh loads the other panels afterward.
    if plant_result is not None:
        return projection
    commissions = list_commissions(minute, actor_type="PLAYER", actor_id=str(player.pk))
    cooperation = cooperation_overview(player, minute, read_only=read_only)
    decorate_commission_rewards(commissions, player, cooperation)
    from .task_progress import decorate_task_progress
    decorate_task_progress([], commissions)
    starter = starter_overview(player, bag, minute, read_only=read_only)
    starter["gathering"] = gathering_overview(player, bag, minute)
    projection.update(starter=starter, cooperation=cooperation, commissions=commissions,
        my_commissions=[c for c in commissions
            if c.get("actor_type") == "PLAYER" and str(c.get("actor_id")) == str(player.pk)])
    if compact:
        return projection
    general_store = VillageStore.objects.filter(pk="GENERAL_STORE").values("status", "account__balance").first()
    affairs = project_affairs(minute)
    decorate_task_progress(affairs, [])
    contributions = dict(delivery_quantity=0, earned_gold=0, item_quantity=0,
                         material_quantity=0, meal_quantity=0, event_count=0)
    for row in VillagePlayerContribution.objects.filter(player=player).values("kind").annotate(
            quantity=models.Sum("quantity"), gold=models.Sum("earned_gold"), count=models.Count("pk")):
        contributions[row["kind"]+"_quantity"] = row["quantity"]
        contributions["earned_gold"] += row["gold"]
        contributions["event_count"] += row["count"]
    contributions["labor_quantity"] = VillageAffairContribution.objects.filter(
        actor_type="PLAYER", actor_id=str(player.pk), kind="labor").aggregate(n=models.Sum("quantity"))["n"] or 0
    market = []
    for listing in MarketListing.objects.filter(status=MarketListingStatus.ACTIVE, remaining_amount__gt=0).exclude(seller=player).order_by("price", "pk")[:100]:
        batch = _listing_batch(listing)
        expiry = batch.get("expiresWorldMinute")
        if listing.expires_at and listing.expires_at <= timezone.now():
            continue
        market.append(dict(listing_id=str(listing.pk), item_id=listing.item_id, item_name=listing.item_name_snapshot,
            remaining_quantity=listing.remaining_amount, unit_price=listing.price, quality=listing.quality, expires_world_minute=None))
    from .production_tools import tools_overview
    tool_state = tools_overview(player, bag, minute, read_only=read_only)
    projection.update(
        mastery=mastery_overview(player, bag, minute, read_only=read_only),
        professional_contracts=professional_contracts_overview(player, minute, read_only=read_only),
        production=production_overview(player, bag, minute, read_only=read_only, tool_state=tool_state),
        collection_points=collection_points_projection(player, bag, minute, tool_state=tool_state),
        cooking=cooking_overview(player, bag, minute, read_only=read_only, tool_state=tool_state),
        reviews=review_overview(player, bag, minute, read_only=read_only),
        hunting=hunting_overview(player, bag, minute, read_only=read_only),
        processing=processing_overview(player, bag, minute, read_only=read_only),
        business=business_overview(player, bag, minute, read_only=read_only),
        business_orders=business_orders_overview(player, minute, read_only=read_only),
        contributions=contributions, project=_project(minute)[0], market=market,
        affairs=affairs, daily_events=project_daily_events(minute),
        public_meals=public_meal_quota(minute, player_id=player.pk),
        my_labor=current_labor(player), farm=farm_overview(player, bag, minute, read_only=read_only,
            growth=growth, tool_state=tool_state, slots=slots),
        general_store=dict(status=general_store["status"], coin_balance=general_store["account__balance"]) if general_store else None)
    from .caravan_orders import orders_overview
    projection["caravan_orders"] = orders_overview("PLAYER", str(player.pk), minute)
    from .career_development import project_career_development
    projection["career_development"] = project_career_development(minute)
    from player.tutorials import tutorial_overview
    projection["tutorial"] = tutorial_overview(player, bag, minute, projection, read_only=read_only)
    from player.arrival import arrival_overview
    projection["arrival"] = arrival_overview(player)
    return projection


def _execute(player, bag, action, p, rid, location, clock, authority_session_id=None):
    minute = clock.world_minute
    if action in LOCATIONS and location not in LOCATIONS[action]:
        fail("LOCATION_INVALID", "请前往对应地点再操作。")
    if action == "overview":
        return {}
    if action == "deliver_caravan_order":
        from .caravan_orders import deliver_order
        result = deliver_order("PLAYER", str(player.pk), p, minute, rid, location)
        if str(result.get("result_code")) not in {"OK", "ResultCode.OK"}:
            fail(result.get("result_code", "CARAVAN_ORDER_FAILED"),
                 result.get("message", "本次商队订单交货未完成。"))
        return result.get("data", {})
    if action in ARRIVAL_PARAMS:
        from player.arrival import control, ArrivalError
        try:
            return control(player, action, p)
        except ArrivalError as exc:
            fail(exc.code, exc.message)
    if action in TUTORIAL_PARAMS:
        from player.tutorials import control, TutorialError
        try:
            return control(player, action, p, bag)
        except TutorialError as exc:
            fail(exc.code, exc.message)
    if action in {"start_mastery", "contribute_mastery", "claim_mastery"}:
        from player.mastery import MasteryError, execute_mastery_action
        if action == "contribute_mastery":
            sites = {"gathering": "GENERAL_STORE", "farming": "BILL_FARM", "forestry": "BUTCHER_YARD", "cooking": "RESTAURANT"}
            if p.get("skill_code") not in sites or location != sites[p["skill_code"]]:
                fail("LOCATION_INVALID", "Mastery samples require the actual recipient location.")
        try:
            return execute_mastery_action(player, bag, action, p, rid, clock)
        except MasteryError as exc:
            fail(exc.code, exc.message)
    if action == "prepare_professional_contracts":
        from .professional_contracts import ProfessionalContractsError, execute_prepare_professional_contracts
        try:
            return execute_prepare_professional_contracts(player, bag, p, rid, clock)
        except ProfessionalContractsError as exc:
            fail(exc.code, exc.message)
    if action in {"claim_starter_hunting_kit", "repair_hunting_weapon", "start_safe_rest", "finish_safe_rest"}:
        from .player_hunting import HuntingError, execute_hunting_action
        try:
            return execute_hunting_action(player, bag, action, p, rid, clock)
        except HuntingError as exc:
            fail(exc.code, exc.message)
    if action in {"start_game_processing", "cancel_game_processing", "claim_game_processing"}:
        from .player_processing import ProcessingError, execute_processing_action
        try:
            return execute_processing_action(player, bag, action, p, rid, clock)
        except ProcessingError as exc:
            fail(exc.code, exc.message)
    if action in {"open_player_stall", "list_stall_dish", "withdraw_stall_dish", "upgrade_player_stall"}:
        from restaurant.player_stall import StallError, execute_stall_action
        try:
            return execute_stall_action(player, bag, action, p, rid, clock)
        except StallError as exc:
            fail(exc.code, exc.message)
    if action == "prepare_business_orders":
        from .business_orders import BusinessOrdersError, execute_business_orders
        try:
            return execute_business_orders(player, bag, p, rid, clock)
        except BusinessOrdersError as exc:
            fail(exc.code, exc.message)
    if action == "cook_dish":
        from .player_cooking import CookingError, execute_player_cooking
        try:
            return execute_player_cooking(player, bag, p, rid, clock)
        except CookingError as exc:
            fail(exc.code, exc.message)
    if action in COOKING_CONTROLS:
        from .player_cooking import CookingError
        from .player_cooking_sessions import control_session
        try:
            return control_session(player, bag, action, p, minute)
        except CookingError as exc:
            fail(exc.code, exc.message)
    if action in {"request_dish_review", "cancel_dish_review", "claim_dish_review"}:
        from .player_dish_reviews import ReviewError, execute_review_action
        try:
            return execute_review_action(player, bag, action, p, rid, clock)
        except ReviewError as exc:
            fail(exc.code, exc.message)
    if action in {"repair_tool", "upgrade_tool", "claim_starter_axe"}:
        from .production_tools import ToolError, execute_tool_action
        try:
            return execute_tool_action(player, bag, action, p, rid, clock)
        except ToolError as exc:
            fail(exc.code, exc.message)
    if action == "claim_goal_rewards":
        # post_action_progress settles only this player's proven, unpaid goals.
        return {}
    if action == "track_goal":
        from player.starter import StarterError, track_goal
        try:
            return track_goal(player, p.get("goal_code"))
        except StarterError as exc:
            fail(exc.code, exc.message)
    if action.startswith("supply_"):
        from .starter_supply import SupplyError, execute_supply
        try:
            return execute_supply(player, bag, action, p, rid, minute)
        except SupplyError as exc:
            fail(exc.code, exc.message)
    if action in FARM_ACTIONS:
        from .player_farm import execute_farm_action
        return execute_farm_action(player, bag, action, p, rid, clock)
    if action.startswith("commission_"):
        from .personal_commissions import (CommissionError, accept_commission, contribute_commission,
                                           abandon_commission)
        commission_id = key(p.get("commission_id"))
        try:
            if action == "commission_labor_start":
                from .player_affair_labor import start_commission_labor
                return start_commission_labor(player, commission_id, rid, location, clock,
                                               authority_session_id, p.get("condition_id"))
            if action == "commission_accept":
                result = accept_commission(commission_id, "PLAYER", str(player.pk), rid, minute)
            elif action == "commission_abandon":
                result = abandon_commission(commission_id, "PLAYER", str(player.pk), rid, minute)
            else:
                result = contribute_commission(commission_id, "PLAYER", str(player.pk), p.get("condition_id"),
                    positive(p.get("quantity")), rid, minute,
                    item_id=p.get("item_id"), instance_id=p.get("instance_id"), location=location)
            return dict(result, earned_gold=result.get("reward_gold", 0)
                        if action == "commission_contribute" and result.get("status") == "COMPLETED" else 0)
        except CommissionError as exc:
            fail(exc.code, exc.message)
    if action in PLANT_ACTIONS or action == "collect" and "node_id" in p:
        from .plant_gathering import PlantError, execute as execute_plant
        try:
            return execute_plant(player, bag, action, p, rid, location, minute, authority_session_id)
        except PlantError as exc:
            fail(exc.code, exc.message)
    # Compatibility for trusted service integrations. The current Env only authorizes
    # player collection against a real node_id (including receipt-only retries).
    if action == "collect":
        from inventory.models import CollectionPoint
        from inventory.views import _build_collect_pool
        from player.models import PlayerState
        from player.services import recover_action_points
        collect_type = p.get("collect_type")
        if collect_type == "采集肉类":
            fail("REAL_HUNT_REQUIRED", "请在猎场攻击真实动物，击杀后自动获得猎物。")
        if not isinstance(collect_type, str) or collect_type not in COLLECTION_LOCATIONS:
            fail("INVALID_PARAMS", "采集类型无效。")
        if location != COLLECTION_LOCATIONS[collect_type]:
            fail("LOCATION_INVALID", "请前往对应野外采集地点。")
        point = CollectionPoint.objects.filter(collect_type=collect_type).first()
        if not point or point.action_cost <= 0 or point.collect_count <= 0:
            fail("COLLECTION_UNAVAILABLE", "采集点尚未配置。")
        state, _ = PlayerState.objects.get_or_create(player_id=str(player.pk))
        state = PlayerState.objects.select_for_update().get(pk=state.pk)
        recover_action_points(state)
        if state.action_points < point.action_cost:
            fail("INSUFFICIENT_ACTION_POINTS", "行动力不足。")
        from .advanced_gathering import is_professional_collection, prepare_professional_collection, finish_professional_collection
        professional = None
        if is_professional_collection(collect_type):
            from .advanced_gathering import AdvancedGatheringError
            try:
                professional = prepare_professional_collection(player, bag, dict(p, collect_type=collect_type), minute)
            except AdvancedGatheringError as exc:
                fail(exc.code, exc.message)
            item = Item.objects.get(pk=professional["item_id"], is_deleted=False)
            pool, weights, error = [dict(item=item, min_amount=professional["quantity"], max_amount=professional["quantity"])], [1], None
        else:
            pool, weights, error = _build_collect_pool(player, point)
        if error:
            fail("COLLECTION_UNAVAILABLE", "当前等级没有可采集物品。")
        from .player_gathering import GatheringError, prepare_targeted_collection, wear_tool
        from .advanced_gathering import COLLECT_TYPE, AdvancedGatheringError, prepare_collection, consume_resource
        resource = None
        try:
            if professional:
                tool, resource = professional["tool"], professional["resource"]
            elif collect_type == COLLECT_TYPE:
                pool, weights, tool, resource = prepare_collection(player, bag, p, pool, weights, minute)
            else:
                pool, weights, tool = prepare_targeted_collection(player, bag, p, pool, weights, minute=minute)
        except (GatheringError, AdvancedGatheringError) as exc:
            fail(exc.code, exc.message)
        valid = [(entry, weight) for entry, weight in zip(pool, weights)
                 if weight > 0 and not entry["item"].is_deleted and 0 < entry["min_amount"] <= entry["max_amount"]]
        if not valid:
            fail("COLLECTION_UNAVAILABLE", "当前等级没有可采集物品。")
        state.action_points -= point.action_cost
        state.action_points_recovered_at = timezone.now()
        state.save(update_fields=["action_points", "action_points_recovered_at"])
        next_index = (bag.slots.aggregate(n=models.Max("slot_index"))["n"] or 0)+1
        collected = []
        from player.equipment import stats_for
        tool_stats = stats_for(tool) if tool else None
        remaining_resource = resource.remaining_quantity if resource else None
        for _ in range(min(point.collect_count, len(valid))):
            if remaining_resource is not None and remaining_resource <= 0:
                break
            selected = random.choices(valid, weights=[v[1] for v in valid], k=1)[0]
            valid.remove(selected)
            entry = selected[0]
            item = entry["item"]
            amount = random.randint(entry["min_amount"], entry["max_amount"])
            if remaining_resource is not None:
                amount = min(amount, remaining_resource)
                remaining_resource -= amount
            quality = production_quality(player_skill_level(player, "gathering"),
                quality_rng(rid, "COLLECT:" + str(next_index)), (tool_stats or {}).get("quality_bonus_chance", 0))
            from inventory.storage import add_inventory_items
            for slot, added in add_inventory_items(bag, item.pk, amount, attributes={"quality": quality,
                    "produced_world_minute": minute, "source_request_id": rid,
                    "item_bag_name": f"{item.item_name}_{next_index}"}):
                collected.append({**_batch_data(slot, minute, {item.pk: item.item_name}), "quantity": added})
            next_index += 1
        from player.progression import settle_growth
        if professional:
            finish_professional_collection(resource, sum(b["quantity"] for b in collected))
        elif resource:
            consume_resource(resource, sum(b["quantity"] for b in collected))
        from player.models import ItemGameplayDefinition
        from player.equipment import stats_for
        tool_tier = stats_for(tool)["tier"] if tool else None
        return dict(collected_items=collected, action_points=state.action_points, action_cost=point.action_cost,
                    category=p.get("category"), tool_used=wear_tool(tool) if tool is not None else None,
                    growth_rewards=[settle_growth(player, source_type="professional_collect" if professional else "advanced_collect" if resource else "collect", source_id=rid,
                        facts=dict(collect_type=collect_type, category=p.get("category"),
                                   tool_item_id=tool.item_id if tool is not None else None,
                                   tool_tier=tool_tier, source_request_id=str(rid), produced_world_minute=minute,
                                   outputs=[{key: batch.get(key) for key in ("instance_id", "item_id", "quantity", "quality", "produced_world_minute", "expires_world_minute", "source_request_id")} for batch in collected]))])
    if action == "contribute":
        from .affairs import AffairError, contribute, validate_contribution
        kind, quantity = p.get("kind"), positive(p.get("quantity"))
        if kind not in ("item", "material", "meal", "fund", "gold"):
            fail("INVALID_PARAMS", "贡献类型无效。")
        contribution_kind = "gold" if kind == "fund" else kind
        item_id = positive(p.get("item_id")) if kind in ("item", "material", "meal") else None
        affair = _get_affair(p, minute)
        try:
            condition = validate_contribution(affair, contribution_kind, quantity, item_id=item_id,
                                              condition_id=p.get("condition_id"))
            if condition["locations"] and location not in condition["locations"]:
                fail("LOCATION_INVALID", "请前往该条件指定的交付地点。")
            batches = []
            if contribution_kind == "gold":
                if bag.money < quantity:
                    fail("INSUFFICIENT_MONEY", "钱包中的金币不足。")
                bag.money -= quantity
                bag._money_change_source = "village_affair_contribution"
                bag.save(update_fields=["money"])
            else:
                batches = _consume(bag, item_id, quantity, minute, instance_id=p.get("instance_id"),
                                   min_quality=condition["min_quality"],
                                   min_remaining=condition["min_remaining_minutes"],
                                   perishable=condition["requires_freshness"] or condition["min_remaining_minutes"] > 0)
            result = contribute(affair, contribution_kind, quantity, item_id=item_id, world_minute=minute,
                                condition_id=condition["id"], request_id=rid, player_id=str(player.pk))
        except AffairError as exc:
            fail(exc.code, exc.message)
        _record(player, rid, "fund" if contribution_kind == "gold" else kind, item_id or 0,
                quantity, 0, batches, clock, location)
        return dict(result or {}, affair_id=affair.pk, condition_id=condition["id"],
                    kind=kind, item_id=item_id, quantity=quantity)
    if action == "labor_start":
        from .player_affair_labor import start_labor
        return start_labor(player, _get_affair(p, minute), rid, location, clock, authority_session_id,
                           condition_id=p.get("condition_id"))
    if action == "labor_cancel":
        from .player_affair_labor import cancel_labor
        return dict(labor=cancel_labor(player, labor_id=key(p["labor_id"]) if "labor_id" in p else None))
    if action == "buy_store":
        from .player_store import buy_from_store
        return buy_from_store(player, bag, p, rid, location, clock)
    if action == "sell_store":
        from .player_store import sell_to_store
        return sell_to_store(player, bag, p, rid, location, clock)
    if action == "list_marketplace":
        quantity, price = positive(p.get("quantity")), positive(p.get("unit_price"))
        listing, error = create_listing(player, key(p.get("instance_id")), quantity, price)
        if error:
            fail("MARKET_LIST_FAILED", error)
        bag.refresh_from_db()
        return dict(listing_id=str(listing.pk), quantity=quantity, unit_price=price,
                    listing=serialize_listing(listing, player), bag=serialize_bag(bag))
    if action == "delist_marketplace":
        listing_id = market_key(p.get("listing_id"))
        returned, error = delist_listing(player, listing_id)
        if error:
            fail("MARKET_DELIST_FAILED", error)
        listing = MarketListing.objects.get(pk=listing_id)
        return dict(listing_id=str(listing_id), listing=serialize_listing(listing, player), bag=serialize_bag(returned))
    if action == "buy_marketplace":
        quantity = positive(p.get("quantity"))
        listing_id = market_key(p.get("listing_id"))
        listing = MarketListing.objects.select_for_update().filter(pk=listing_id).first()
        if not listing:
            fail("LISTING_NOT_FOUND", "挂单已不存在。")
        if "expected_unit_price" in p and (type(p["expected_unit_price"]) is not int or p["expected_unit_price"] != listing.price):
            fail("PRICE_CHANGED", "价格已变化，请刷新后确认。")
        result, error = buy_listing(player, listing_id, quantity)
        if error:
            fail("MARKET_PURCHASE_FAILED", error)
        return dict(transaction_id=str(result["transaction"].pk), listing_id=str(listing_id),
                    quantity=quantity, spent_gold=result["transaction"].total_price,
                    unit_price=listing.price, total_price=result["transaction"].total_price,
                    transaction=serialize_transaction(result["transaction"]),
                    listing=serialize_listing(result["listing"], player), bag=serialize_bag(result["bag"]),
                    purchased_batches=result.get("purchased_batches", []),
                    affected_npc_ids=[listing.seller_npc.agent_id] if listing.seller_npc_id else [])


def execute_player_action(payload, *, timings=None):
    # Queries neither queue behind a SQLite IMMEDIATE transaction nor store a large receipt.
    query = isinstance(payload, dict) and payload.get("action") in ("overview", "plant_state", "preview_cooking", "preview_game_processing")
    tracked = isinstance(payload, dict) and payload.get("action") in {"plant_start", "plant_cancel", "collect", "buy_store", "sell_store", *FARM_ACTIONS}
    started = perf_counter()
    if tracked:
        connection.ensure_connection()
    connected = perf_counter()
    with nullcontext() if query else transaction.atomic():
        acquired = perf_counter()
        response = _execute_player_action(payload, query=query)
        executed = perf_counter()
    finished = perf_counter()
    elapsed = (finished - started) * 1000
    if tracked and timings is not None:
        timings.update(db_connect=(connected-started)*1000, db_wait=(acquired-connected)*1000,
                       execute=(executed-acquired)*1000, commit=(finished-executed)*1000, total=elapsed)
    if tracked and elapsed >= 250:
        logging.getLogger(__name__).warning(
            "[%s] step=backend action=%s request=%s elapsed_ms=%.1f transaction_wait_ms=%.1f",
            "FarmActionTiming" if payload.get("action") in FARM_ACTIONS else "StoreActionTiming" if payload.get("action") in {"buy_store", "sell_store"} else "PlantActionTiming",
            payload.get("action"), payload.get("request_id"), elapsed, (acquired - connected) * 1000)
    return response


def _execute_player_action(payload, *, query=False):
    rid = payload.get("request_id") if isinstance(payload, dict) else ""
    try:
        if not isinstance(payload, dict) or set(payload)-{"player_id", "request_id", "action", "params", "current_location_code", "replay_only", "read_only", "authority_session_id"}:
            fail("INVALID_PARAMS", "请求参数无效。")
        rid = key(rid)
        player_id = payload.get("player_id")
        if type(player_id) not in (str, int) or not str(player_id).isascii() or not str(player_id).isdigit() or len(str(player_id)) > 18:
            fail("PLAYER_NOT_FOUND", "玩家身份无效。")
        # Same first lock as NPC economics/marketplace, before any private bags.
        clocks = GlobalGameTimeState.objects if query else GlobalGameTimeState.objects.select_for_update()
        clock = clocks.filter(pk=1).first()
        if clock is None:
            fail("WORLD_NOT_READY", "村庄时间尚未初始化。")
        player = GameAccount.objects.filter(pk=int(player_id), is_active=True, is_ai=False).first()
        if not player:
            fail("PLAYER_NOT_FOUND", "玩家身份无效。")
        action, params = payload.get("action"), payload.get("params", {})
        if not isinstance(action, str) or action not in PARAMS or not isinstance(params, dict) or set(params)-PARAMS[action]:
            fail("INVALID_PARAMS", "动作参数无效。")
        if action == "overview" and "section" in params and params["section"] != "farm":
            fail("INVALID_PARAMS", "总览分区无效。")
        digest = hashlib.sha256(json.dumps(dict(action=action, params=params), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        old = VillagePlayerActionReceipt.objects.filter(player=player, request_id=rid).first()
        if old:
            if old.payload_hash != digest:
                fail("REQUEST_CONFLICT", "同一请求编号不能用于不同操作。")
            from inventory.batches import normalize_legacy_attributes
            answer = normalize_legacy_attributes(copy.deepcopy(old.response))
            if action in ("labor_start", "commission_labor_start", "labor_cancel"):
                from .player_affair_labor import current_labor, labor_data
                from .models import VillagePlayerAffairLabor
                # A saved start receipt must never resurrect completed labor.
                answer["data"]["current_labor"] = current_labor(player)
                saved = answer["data"].get("result") or {}
                if action == "labor_cancel":
                    saved = saved.get("labor") or {}
                answer["data"]["receipt_labor"] = labor_data(VillagePlayerAffairLabor.objects.filter(
                    player=player, pk=saved.get("labor_id")).first()) if saved.get("labor_id") else None
                current = answer["data"]["current_labor"]
                if current and current["labor_id"] == saved.get("labor_id"):
                    answer["data"]["receipt_labor"] = current
            if action in FARM_ACTIONS or action in ARRIVAL_PARAMS or action in {"buy_store", "sell_store"} or answer.get("data", {}).get("overview_partial"):
                answer["replayed"] = True
            return answer
        if "read_only" in payload and type(payload["read_only"]) is not bool:
            fail("INVALID_PARAMS", "观察上下文无效。")
        if action == "plant_state":
            if not VillageSimulationConfig.objects.filter(config_key="default", village_economy_enabled=True).exists():
                return response(rid, "FEATURE_DISABLED", "Village economy is unavailable.")
            from .plant_gathering import overview as plant_overview
            bag = InventoryBag.objects.filter(player=player, character_id=player.username).first()
            bag = bag or InventoryBag(player=player, character_id=player.username)
            return response(rid, data=dict(plants=plant_overview(player, bag, clock.world_minute)))
        if action == "preview_game_processing":
            if not VillageSimulationConfig.objects.filter(config_key="default", village_economy_enabled=True).exists():
                return response(rid, "FEATURE_DISABLED", "Village economy is unavailable.")
            from .player_processing import ProcessingError, execute_processing_action
            bag = InventoryBag.objects.filter(player=player, character_id=player.username).first()
            bag = bag or InventoryBag(player=player, character_id=player.username)
            try:
                preview = execute_processing_action(player, bag, action, params, rid, clock)
            except ProcessingError as exc:
                return response(rid, exc.code, exc.message)
            return response(rid, data=dict(result=preview))
        if action == "preview_cooking":
            if not VillageSimulationConfig.objects.filter(config_key="default", village_economy_enabled=True).exists():
                return response(rid, "FEATURE_DISABLED", "Village economy is unavailable.")
            from .player_cooking import CookingError, preview_cooking
            bag = InventoryBag.objects.filter(player=player, character_id=player.username).first()
            bag = bag or InventoryBag(player=player, character_id=player.username)
            try:
                preview = preview_cooking(player, bag, params, clock.world_minute)
            except CookingError as exc:
                return response(rid, exc.code, exc.message)
            return response(rid, data=dict(result=dict(preview=preview)))
        if payload.get("read_only") is True:
            if action != "overview":
                fail("MODE_READ_ONLY", "观察模式不能发起交易。")
            bag = InventoryBag.objects.filter(player=player, character_id=player.username).first()
            bag = bag or InventoryBag(player=player, character_id=player.username)
            farm_only = params.get("section") == "farm"
            return response(rid, data=dict(result={}, overview=overview(player, bag, clock.world_minute,
                read_only=True, farm_only=farm_only), **({"overview_partial": True} if farm_only else {})))
        if payload.get("replay_only") is True:
            fail("LOCATION_REQUIRED", "请前往现场再执行操作。")
        if "replay_only" in payload and type(payload["replay_only"]) is not bool:
            fail("INVALID_PARAMS", "重试上下文无效。")
        if not isinstance(payload.get("current_location_code", ""), str):
            fail("INVALID_PARAMS", "地点上下文无效。")
        cfg = VillageSimulationConfig.objects.filter(config_key="default", village_economy_enabled=True).first()
        if not cfg:
            fail("FEATURE_DISABLED", "村庄经济尚未开放。")
        if action in ARRIVAL_PARAMS:
            # Room allocation waits for this receipt. No economy maintenance, inventory
            # initialization or panel projection belongs on the opening's critical path.
            from player.arrival import arrival_overview
            try:
                result = _execute(player, None, action, params, rid, payload.get("current_location_code", ""), clock)
                answer = response(rid, data=dict(result=result, overview=dict(arrival=arrival_overview(player)),
                    overview_partial=True))
            except ActionError as exc:
                answer = response(rid, exc.code, exc.message)
            VillagePlayerActionReceipt.objects.create(player=player, request_id=rid, payload_hash=digest,
                action=action, response=answer, world_minute=clock.world_minute)
            return answer
        if action == "overview":
            if params.get("section") == "farm":
                bag = InventoryBag.objects.filter(player=player, character_id=player.username).first()
                bag = bag or InventoryBag(player=player, character_id=player.username)
                return response(rid, data=dict(result={}, overview_partial=True,
                    overview=overview(player, bag, clock.world_minute, compact=True, farm_only=True)))
            bag, _ = get_player_main_bag(player)
            return response(rid, data=dict(result={}, overview=overview(player, bag, clock.world_minute)))
        if action in COOKING_CONTROLS:
            # Process-only controls never tick the economy, debit inventory, recover AP,
            # run tutorials, or rebuild a full cooking/equipment projection.
            try:
                with transaction.atomic():
                    bag = InventoryBag.objects.select_for_update().filter(player=player, character_id=player.username).first()
                    bag = bag or InventoryBag(player=player, character_id=player.username)
                    result = _execute(player, bag, action, params, rid, payload.get("current_location_code", ""), clock)
                answer = response(rid, data=dict(result=result, cooking_control=True))
            except ActionError as exc:
                answer = response(rid, exc.code, exc.message)
            VillagePlayerActionReceipt.objects.create(player=player, request_id=rid, payload_hash=digest,
                action=action, response=answer, world_minute=clock.world_minute)
            return answer
        if action in {"plant_start", "plant_cancel"}:
            # A lease neither moves inventory nor advances the world economy.
            # Avoid rebuilding every gameplay panel just to begin/cancel a timer.
            from .plant_gathering import overview as plant_overview
            try:
                with transaction.atomic():
                    bag, _ = get_player_main_bag(player)
                    bag = InventoryBag.objects.select_for_update().get(pk=bag.pk)
                    result = _execute(player, bag, action, params, rid, payload.get("current_location_code", ""),
                        clock, payload.get("authority_session_id"))
                answer = response(rid, data=dict(result=result, plants=plant_overview(player, bag, clock.world_minute,
                    node_id=params.get("node_id") or result.get("node_id"))))
            except ActionError as exc:
                answer = response(rid, exc.code, exc.message)
            VillagePlayerActionReceipt.objects.create(player=player, request_id=rid, payload_hash=digest,
                action=action, response=answer, world_minute=clock.world_minute)
            return answer
        compact = action in {"commission_accept", "commission_contribute", "commission_abandon"} or action == "collect" and "node_id" in params
        store_settlement = action in {"buy_store", "sell_store"} and payload.get("current_location_code") == "GENERAL_STORE"
        farm_settlement = action in FARM_ACTIONS
        if not compact or action.startswith("commission_"):
            # Commission expiry refunds escrow even when the requested contribution is rejected.
            from .personal_commissions import expire_commissions
            expire_commissions(clock.world_minute)
        if not compact:
            from .starter_supply import expire_supply
            expire_supply(clock.world_minute)
            from .player_farm import maintain_leases
            maintain_leases(clock.world_minute, clock.time_revision)
        try:
            with transaction.atomic():
                bag, _ = get_player_main_bag(player)
                bag = InventoryBag.objects.select_for_update().get(pk=bag.pk)
                result = _execute(player, bag, action, params, rid, payload.get("current_location_code", ""), clock, payload.get("authority_session_id"))
                if action != "overview" and action not in TUTORIAL_PARAMS and action not in ARRIVAL_PARAMS:
                    from player.starter import post_action_progress
                    progress = post_action_progress(player, bag, action, params, result, rid, clock.world_minute)
                    result.setdefault("growth_rewards", []).extend(progress.pop("growth_rewards", []))
                    result.update(progress)
                    from player.tutorials import observe_action
                    observe_action(player, bag, action, result, rid)
                    if action == "collect":
                        from .starter_supply import ensure_supply_batch
                        ensure_supply_batch(clock.world_minute)
                        if not compact:
                            from .player_gathering import GatheringError, ensure_gathering_catalog
                            from .production_tools import ToolError, ensure_production_catalog
                            try:
                                ensure_gathering_catalog(clock.world_minute)
                                ensure_production_catalog(clock.world_minute)
                            except (GatheringError, ToolError) as exc:
                                fail(exc.code, exc.message)
                    elif action in {"lease_farm_plot", "claim_starter_axe", "repair_tool", "upgrade_tool"}:
                        from .production_tools import ToolError, ensure_production_catalog
                        try:
                            ensure_production_catalog(clock.world_minute)
                        except ToolError as exc:
                            fail(exc.code, exc.message)
                    # Plant pickups only change this player's assets; world events are evaluated by the world tick.
                    if action != "collect" or "node_id" not in params:
                        from .daily_events import evaluate_daily_events
                        evaluate_daily_events(clock.world_minute)
            plant_result = result if action == "collect" and "node_id" in params else None
            data = dict(result=result, overview=overview(player, bag, clock.world_minute, compact=compact or store_settlement or farm_settlement,
                plant_result=plant_result, store_only=store_settlement, farm_only=farm_settlement))
            if store_settlement:
                data["store_settlement"] = True
            if farm_settlement:
                data["farm_settlement"] = True
            if plant_result is not None:
                data["inventory_partial"] = True
            if compact or store_settlement or farm_settlement:
                data["overview_partial"] = True
            answer = response(rid, data=data)
        except ActionError as exc:
            data = {}
            if store_settlement or farm_settlement:
                current_bag = InventoryBag.objects.filter(pk=bag.pk).first() or InventoryBag(player=player, character_id=player.username)
                data = {"store_settlement" if store_settlement else "farm_settlement": True, "overview_partial": True,
                    "overview": overview(player, current_bag, clock.world_minute, read_only=store_settlement,
                        compact=True, store_only=store_settlement, farm_only=farm_settlement)}
            elif action == "buy_store":
                # Failed claims still refresh the caller's quota and funds snapshot.
                data["overview"] = overview(player, bag, clock.world_minute, read_only=True)
            if not store_settlement and action == "sell_store":
                # Failed savepoints may have changed the in-memory bag. Reload
                # before returning the new quote/version for a fresh confirmation.
                existing_bag = InventoryBag.objects.filter(pk=bag.pk).first()
                if existing_bag:
                    data["overview"] = overview(player, existing_bag, clock.world_minute)
                else:
                    empty_bag = InventoryBag(player=player, character_id=player.username)
                    data["overview"] = overview(player, empty_bag, clock.world_minute, read_only=True)
            answer = response(rid, exc.code, exc.message, data=data)
        VillagePlayerActionReceipt.objects.create(player=player, request_id=rid, payload_hash=digest,
            action=action, response=answer, world_minute=clock.world_minute)
        return answer
    except ActionError as exc:
        return response(rid, exc.code, exc.message)
