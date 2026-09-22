"""Trusted Env player actions, serialized with the existing world/economy lock.

All financial promises live in commission escrow. No client supplies time, NPC identity,
price, reward, or stock. A failed action rolls back before its receipt is saved.
"""
import copy
import hashlib
import json
import random
import uuid

from django.db import models, transaction
from django.utils import timezone

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
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
    "buy_marketplace": {"MARKET_STREET"},
    "list_marketplace": {"MARKET_STREET"},
    "delist_marketplace": {"MARKET_STREET"},
    "buy_store": {"GENERAL_STORE", "RESTAURANT"},
    "sell_store": {"GENERAL_STORE"},
}
PARAMS = {
    "overview": set(),
    "contribute": {"affair_id", "condition_id", "kind", "item_id", "quantity", "instance_id"},
    "labor_start": {"affair_id", "condition_id"}, "labor_cancel": set(),
    "commission_accept": {"commission_id"},
    "commission_contribute": {"commission_id", "condition_id", "quantity", "item_id", "instance_id"},
    "commission_labor_start": {"commission_id", "condition_id"},
    "commission_abandon": {"commission_id"},
    "buy_marketplace": {"listing_id", "quantity", "expected_unit_price"},
    "list_marketplace": {"instance_id", "quantity", "unit_price"},
    "delist_marketplace": {"listing_id"},
    "buy_store": {"listing_id", "quantity", "expected_unit_price"},
    "sell_store": {"instance_id", "quantity", "expected_unit_price"},
    "collect": {"collect_type"},
}
COLLECTION_LOCATIONS = {"采集食材": "FOREST_EDGE"}
from .player_farm import ACTIONS as FARM_ACTIONS
PARAMS.update(FARM_ACTIONS)
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
        quality=slot.quality, remaining_uses=slot.remaining_uses, expires_world_minute=slot.expires_world_minute,
        remaining_freshness_minutes=None if slot.expires_world_minute is None else max(0, slot.expires_world_minute-minute),
        freshness_unknown=slot.freshness_unknown,
        usable=not slot.freshness_unknown and (slot.expires_world_minute is None or slot.expires_world_minute > minute))


def _consume(bag, item_id, quantity, minute, *, instance_id=None, min_quality=1, min_remaining=0, perishable=False):
    slots = InventorySlot.objects.select_for_update().filter(bag=bag, item_id=item_id, item_amount__gt=0)
    if instance_id:
        slots = slots.filter(instance_id=key(instance_id))
    rows = list(slots.order_by(models.F("expires_world_minute").asc(nulls_last=True), "slot_index"))
    usable = [s for s in rows if not s.freshness_unknown and s.quality >= min_quality
              and (s.expires_world_minute is not None or not perishable)
              and (s.expires_world_minute is None or
                   (s.expires_world_minute > minute and s.expires_world_minute >= minute + min_remaining))]
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


def overview(player, bag, minute, *, read_only=False):
    from .lucy_restaurant import public_meal_quota
    from .player_farm import farm_overview
    from .affairs import project_affairs
    from .personal_commissions import list_commissions
    from .models import VillageAffairContribution
    from inventory.models import CollectionPoint
    from player.models import PlayerState
    from player.services import recover_action_points
    if read_only:
        state = PlayerState.objects.filter(player_id=str(player.pk)).first() or PlayerState(player_id=str(player.pk))
    else:
        state, _ = PlayerState.objects.get_or_create(player_id=str(player.pk))
        state = PlayerState.objects.select_for_update().get(pk=state.pk)
        recover_action_points(state)
    from .player_affair_labor import current_labor
    if not read_only:
        from .affairs import ensure_bridge_affair
        ensure_bridge_affair(minute)
    items = {item.pk: item for item in Item.objects.filter(is_deleted=False)}
    names = {item_id: item.item_name for item_id, item in items.items()}
    from .player_store import store_sale_quote
    from .models import VillageStore
    general_store = VillageStore.objects.filter(pk="GENERAL_STORE").values("status", "account__balance").first()
    commissions = list_commissions(minute, actor_type="PLAYER", actor_id=str(player.pk))
    affairs = project_affairs(minute)
    from .task_progress import decorate_task_progress
    decorate_task_progress(affairs, commissions)
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
        if batch.get("freshnessUnknown") or (expiry is not None and expiry <= minute) or (listing.expires_at and listing.expires_at <= timezone.now()):
            continue
        market.append(dict(listing_id=str(listing.pk), item_id=listing.item_id, item_name=listing.item_name_snapshot,
            remaining_quantity=listing.remaining_amount, unit_price=listing.price, quality=listing.quality, expires_world_minute=expiry))
    if bag.pk:
        bag.refresh_from_db()
    return dict(world_minute=minute, player_id=str(player.pk), player_name=player.username, money=bag.money,
        action_points=state.action_points, action_points_max=state.action_points_max,
        collection_points=[dict(collect_type=c.collect_type, location_code=COLLECTION_LOCATIONS[c.collect_type],
            action_cost=c.action_cost, collect_count=c.collect_count) for c in CollectionPoint.objects.filter(collect_type__in=COLLECTION_LOCATIONS)],
        contributions=contributions, project=_project(minute)[0], market=market,
        commissions=commissions, my_commissions=[c for c in commissions
            if c.get("actor_type") == "PLAYER" and str(c.get("actor_id")) == str(player.pk)],
        affairs=affairs, public_meals=public_meal_quota(minute, player_id=player.pk),
        my_labor=current_labor(player), farm=farm_overview(player, bag, minute, read_only=read_only),
        general_store=dict(status=general_store["status"], coin_balance=general_store["account__balance"]) if general_store else None,
        inventory=[{**_batch_data(s, minute, names),
            "item_type": items[s.item_id].item_type if s.item_id in items else "",
            "general_store_sale": store_sale_quote(s, items.get(s.item_id), minute)}
            for s in bag.slots.filter(item_amount__gt=0).select_related("usage", "dish", "review")] if bag.pk else [])


def _execute(player, bag, action, p, rid, location, clock, authority_session_id=None):
    minute = clock.world_minute
    if action in LOCATIONS and location not in LOCATIONS[action]:
        fail("LOCATION_INVALID", "请前往对应地点再操作。")
    if action == "overview":
        return {}
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
        pool, weights, error = _build_collect_pool(player, point)
        if error:
            fail("COLLECTION_UNAVAILABLE", "当前等级没有可采集物品。")
        valid = [(entry, weight) for entry, weight in zip(pool, weights)
                 if weight > 0 and not entry["item"].is_deleted and 0 < entry["min_amount"] <= entry["max_amount"]]
        if not valid:
            fail("COLLECTION_UNAVAILABLE", "当前等级没有可采集物品。")
        state.action_points -= point.action_cost
        state.action_points_recovered_at = timezone.now()
        state.save(update_fields=["action_points", "action_points_recovered_at"])
        next_index = (bag.slots.aggregate(n=models.Max("slot_index"))["n"] or 0)+1
        collected = []
        for _ in range(min(point.collect_count, len(valid))):
            selected = random.choices(valid, weights=[v[1] for v in valid], k=1)[0]
            valid.remove(selected)
            entry = selected[0]
            item = entry["item"]
            amount = random.randint(entry["min_amount"], entry["max_amount"])
            # New production has explicit server provenance and freshness. Never
            # merge it into a historical/expired stack or refresh old shelf life.
            slot = InventorySlot.objects.create(bag=bag, slot_index=next_index, item_id=item.pk,
                item_amount=amount, quality=3, produced_world_minute=minute,
                expires_world_minute=minute+2880, source_request_id=rid,
                item_bag_name=f"{item.item_name}_{next_index}")
            next_index += 1
            collected.append(_batch_data(slot, minute, {item.pk: item.item_name}))
        return dict(collected_items=collected, action_points=state.action_points, action_cost=point.action_cost)
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
        return dict(labor=cancel_labor(player))
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
                    affected_npc_ids=[listing.seller_npc.agent_id] if listing.seller_npc_id else [])


@transaction.atomic
def execute_player_action(payload):
    rid = payload.get("request_id") if isinstance(payload, dict) else ""
    try:
        if not isinstance(payload, dict) or set(payload)-{"player_id", "request_id", "action", "params", "current_location_code", "replay_only", "read_only", "authority_session_id"}:
            fail("INVALID_PARAMS", "请求参数无效。")
        rid = key(rid)
        player_id = payload.get("player_id")
        if type(player_id) not in (str, int) or not str(player_id).isascii() or not str(player_id).isdigit() or len(str(player_id)) > 18:
            fail("PLAYER_NOT_FOUND", "玩家身份无效。")
        # Same first lock as NPC economics/marketplace, before any private bags.
        clock = GlobalGameTimeState.objects.select_for_update().filter(pk=1).first()
        if clock is None:
            fail("WORLD_NOT_READY", "村庄时间尚未初始化。")
        player = GameAccount.objects.filter(pk=int(player_id), is_active=True, is_ai=False).first()
        if not player:
            fail("PLAYER_NOT_FOUND", "玩家身份无效。")
        action, params = payload.get("action"), payload.get("params", {})
        if not isinstance(action, str) or action not in PARAMS or not isinstance(params, dict) or set(params)-PARAMS[action]:
            fail("INVALID_PARAMS", "动作参数无效。")
        digest = hashlib.sha256(json.dumps(dict(action=action, params=params), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        old = VillagePlayerActionReceipt.objects.filter(player=player, request_id=rid).first()
        if old:
            if old.payload_hash != digest:
                fail("REQUEST_CONFLICT", "同一请求编号不能用于不同操作。")
            answer = copy.deepcopy(old.response)
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
            if action in FARM_ACTIONS:
                answer["replayed"] = True
            return answer
        if "read_only" in payload and type(payload["read_only"]) is not bool:
            fail("INVALID_PARAMS", "观察上下文无效。")
        if payload.get("read_only") is True:
            if action != "overview":
                fail("MODE_READ_ONLY", "观察模式不能发起交易。")
            bag = InventoryBag.objects.filter(player=player, character_id=player.username).first()
            bag = bag or InventoryBag(player=player, character_id=player.username)
            return response(rid, data=dict(result={}, overview=overview(player, bag, clock.world_minute, read_only=True)))
        if payload.get("replay_only") is True:
            fail("LOCATION_REQUIRED", "请前往现场再执行操作。")
        if "replay_only" in payload and type(payload["replay_only"]) is not bool:
            fail("INVALID_PARAMS", "重试上下文无效。")
        if not isinstance(payload.get("current_location_code", ""), str):
            fail("INVALID_PARAMS", "地点上下文无效。")
        cfg = VillageSimulationConfig.objects.filter(config_key="default", village_economy_enabled=True).first()
        if not cfg:
            fail("FEATURE_DISABLED", "村庄经济尚未开放。")
        # Expiry is world maintenance: it survives a rejected player action.
        from .personal_commissions import expire_commissions
        expire_commissions(clock.world_minute)
        from .player_farm import maintain_leases
        maintain_leases(clock.world_minute, clock.time_revision)
        try:
            with transaction.atomic():
                bag, _ = get_player_main_bag(player)
                bag = InventoryBag.objects.select_for_update().get(pk=bag.pk)
                result = _execute(player, bag, action, params, rid, payload.get("current_location_code", ""), clock, payload.get("authority_session_id"))
            answer = response(rid, data=dict(result=result, overview=overview(player, bag, clock.world_minute)))
        except ActionError as exc:
            data = {}
            if action == "buy_store":
                # Failed claims still refresh the caller's quota and funds snapshot.
                data["overview"] = overview(player, bag, clock.world_minute, read_only=True)
            if action in FARM_ACTIONS or action == "sell_store":
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
