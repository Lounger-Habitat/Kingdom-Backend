"""Communal, funded merchant orders. Reads never publish or settle anything.

Every mutation takes the existing world clock lock first. Merchant procurement
has a bounded external budget per three-day period, independent of village cash.
"""
from copy import deepcopy
import hashlib
import json
import uuid

from django.db import models, transaction

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.models import InventoryBag, InventorySlot, NpcGlobalInventory, NpcGlobalInventorySlot
from items.models import Item
from items.quality import item_price, valid_quality
from .caravan_order_models import (VillageCaravanOrder, VillageCaravanOrderLine,
    VillageCaravanOrderWindow, VillageCaravanOrderReceipt, VillageCaravanOrderDelivery,
    VillageCaravanOrderCompletion, VillageCaravanReward, VillageCaravanVisitAllocation)
from .models import (EconomicLedgerEntry, NpcRuntimeState, VillageCaravanVisit,
    VillageSimulationConfig, VillageStoreInventorySlot, VillageWorldEvent)
from .phase3_asset_service import _ok, _fail, get_available_slot_amount
from .phase3_catalog import EXPORT_WHITELIST, SAFE_STOCKS

DAY = 1440
PERIOD_DAYS = 3
MERCHANT_BUDGET_PER_PERIOD = 10000
ACTIVE = ("ANNOUNCED", "OPEN")
FORMAL_NPCS = frozenset({"10011", "10012", "10013", "10014", "10015"})


def _clock():
    return GlobalGameTimeState.objects.select_for_update().get(pk=1)


def _event(kind, key, minute, data):
    clock = GlobalGameTimeState.objects.get(pk=1)
    event, created = VillageWorldEvent.objects.get_or_create(dedupe_key=key, defaults=dict(
        event_type=kind, world_minute=minute, time_revision=clock.time_revision,
        location_code="TRADE_ROAD", visibility="PUBLIC", public_data=data,
        player_summary=data.get("message", "商队收购订单状态更新。"),
        hub_summary=data.get("message", "商队收购订单状态更新。")))
    if created:
        from .phase4_visibility import enqueue_event_deliveries
        enqueue_event_deliveries(event)
    return event


def _available(slot, minute):
    # Freshness was retired globally; historical dates remain provenance only.
    if slot.produced_world_minute is not None and slot.produced_world_minute > minute:
        return 0
    if not valid_quality(slot.quality):
        return 0
    if isinstance(slot, NpcGlobalInventorySlot):
        return get_available_slot_amount(slot, minute)
    return max(0, slot.item_amount)


def _village_stock(item_id, minute):
    personal = NpcGlobalInventorySlot.objects.filter(item_id=item_id, item_amount__gt=0,
        bag__npc__agent_id__in=FORMAL_NPCS, bag__npc__is_test_only=False)
    players = InventorySlot.objects.filter(item_id=item_id, item_amount__gt=0,
        bag__player__is_active=True, bag__player__is_ai=False,
        bag__character_id=models.F("bag__player__username"))
    stores = VillageStoreInventorySlot.objects.filter(item_id=item_id, item_amount__gt=0)
    return (sum(_available(s, minute) for s in personal)
        + sum(_available(s, minute) for s in players)
        + sum(max(0, s.item_amount - s.reserved_amount) for s in stores))


@transaction.atomic
def expire_orders(world_minute):
    minute = _clock().world_minute
    expired = list(VillageCaravanOrder.objects.select_for_update().filter(
        status__in=ACTIVE, deadline_world_minute__lte=minute))
    for order in expired:
        order.status, order.active_key = "EXPIRED", None
        order.save(update_fields=["status", "active_key"])
        _event("CARAVAN_ORDER_EXPIRED", f"caravan-order:{order.pk}:expired", minute,
            {"order_id": str(order.pk), "budget_spent": order.budget_spent,
             "message": "商队收购订单已到期；已成交货款保留。"})
    return len(expired)


def _reward_terms(cfg, period, item_ids):
    from .caravan import item_pool
    buy = dict(percent=25, max_bonus=20, base_capacity=cfg.caravan_export_capacity,
        bonus_capacity=min(20, cfg.caravan_export_capacity // 4), item_ids=list(item_ids))
    cargo = dict(percent=25, max_bonus=20, base_capacity=cfg.caravan_normal_capacity,
        bonus_capacity=min(20, cfg.caravan_normal_capacity // 4))
    pool = list(Item.objects.filter(pk__in=item_pool(cfg), is_deleted=False).order_by("item_id"))
    # Two guaranteed new kinds need at least one legal baseline kind as well.
    if period % 3 == 2 and len(pool) >= 3:
        from .professional_catalog import PROFESSIONAL_IMPORT_LIMITS
        extra = [item for item in pool if PROFESSIONAL_IMPORT_LIMITS.get(item.pk, 3) >= 3][-2:]
        if len(extra) == 2:
            return {"extra_kinds": [{"item_id": item.pk, "minimum_quantity": 3} for item in extra],
                    "max_import_bonus": 6, "cash_bonus_percent": 0}
    if period % 3 == 1 and cargo["bonus_capacity"]:
        return {"import_capacity": cargo, "cash_bonus_percent": 0}
    return {"buy_capacity": buy, "cash_bonus_percent": 0} if buy["bonus_capacity"] else {}


def _procurement_plan(cfg, period, minute, deadline):
    from .caravan import item_pool
    from .phase3_public_economy import _get_published_price
    from .career_catalog import NEW_CONTENT_ITEM_IDS
    from .career_development import procurement_candidates, recommendation_context
    from .career_trade import THEMES, item_theme, local_stock, forecast_supply
    theme, label = THEMES[period % len(THEMES)]
    imports = set(item_pool(cfg))
    eligible = set(cfg.caravan_export_item_ids) & EXPORT_WHITELIST
    career = {row["item_id"]: row for row in procurement_candidates(minute)}
    rows = []
    strict = set()
    for item in Item.objects.filter(pk__in=eligible, is_deleted=False).order_by("item_id"):
        local_only = item.pk in NEW_CONTENT_ITEM_IDS or item.pk in imports
        if item.pk in NEW_CONTENT_ITEM_IDS and item.pk not in career:
            continue
        stock = local_stock(item.pk, minute) if local_only else _village_stock(item.pk, minute)
        surplus = max(0, stock - SAFE_STOCKS.get(item.pk, 0))
        trial = bool(career.get(item.pk, {}).get("trial"))
        if item.pk in career:
            potential = forecast_supply(item.pk, minute, deadline)
            target = min(4 if trial else 12, surplus + potential)
        else:
            target = min(12, surplus // 2) if surplus >= 4 else 0
        if target < 2:
            continue
        base = max(1, (_get_published_price(item.pk) * 115 + 50) // 100)
        rows.append(dict(item=item, target=target, trial=trial,
            prices={str(q): item_price(item, q, base) for q in range(1, 7)}))
        if local_only:
            strict.add(item.pk)
    # Rotate peers within a theme; a new product cannot monopolize every order.
    rows.sort(key=lambda row: (item_theme(row["item"].pk) != theme, row["item"].pk))
    same = [row for row in rows if item_theme(row["item"].pk) == theme]
    other = [row for row in rows if item_theme(row["item"].pk) != theme]
    if same:
        shift = period % len(same)
        same = same[shift:] + same[:shift]
    if other:
        shift = period % len(other)
        other = other[shift:] + other[:shift]
    rows = same + other
    review = recommendation_context(minute)
    selected = []
    recommendation = next((row for item_id in review.get("item_ids", [])
        for row in rows if row["item"].pk == item_id), None)
    if recommendation:
        selected.append(recommendation)
    if not any(row["trial"] for row in selected):
        trial = next((row for row in rows if row["trial"] and row not in selected), None)
        if trial:
            selected.append(trial)
    for row in rows:
        if len(selected) >= 2 + period % 3:
            break
        if row not in selected and not row["trial"]:
            selected.append(row)
    ids = {row["item"].pk for row in selected}
    snapshot = dict(version=1, theme=theme, theme_label=label,
        trial_item_ids=[row["item"].pk for row in selected if row["trial"]],
        local_source_item_ids=sorted(ids & strict),
        recommendation_item_id=recommendation["item"].pk if recommendation else None,
        review_track_code=review.get("track_code", "") if recommendation else "")
    return [(row["item"], row["target"], row["prices"]) for row in selected], snapshot


@transaction.atomic
def ensure_orders(world_minute):
    """Authority maintenance only; feature-off preserves already published terms."""
    from .caravan import ARRIVAL, DEPARTURE, current_route, item_pool
    minute = _clock().world_minute
    expire_orders(minute)
    current = VillageCaravanOrder.objects.select_for_update().filter(status__in=ACTIVE).first()
    if current:
        if minute >= current.windows.first().day_index * DAY + ARRIVAL and current.status == "ANNOUNCED":
            current.status = "OPEN"
            current.save(update_fields=["status"])
        return current
    cfg = VillageSimulationConfig.objects.filter(config_key="default").first()
    period, day = minute // (PERIOD_DAYS * DAY), minute // DAY
    if (not cfg or not cfg.village_economy_enabled or not getattr(cfg, "caravan_orders_enabled", False)
            or current_route() != "NORMAL" or cfg.caravan_export_capacity < 4
            or VillageCaravanOrder.objects.filter(period_index=period).exists()):
        return None
    first_day = max(day + 1, (minute + DAY - ARRIVAL + DAY - 1) // DAY)
    deadline = (first_day + 1) * DAY + DEPARTURE
    candidates, procurement = _procurement_plan(cfg, period, minute, deadline)
    if len(candidates) < 2:
        return None
    # Keep the entire order feasible in either scheduled window. The unused
    # reserved share is protected from ordinary exports in both visits.
    capacity = cfg.caravan_export_capacity
    while sum(row[1] for row in candidates) > capacity and any(row[1] > 2 for row in candidates):
        index = max(range(len(candidates)), key=lambda i: candidates[i][1])
        item, target, prices = candidates[index]
        candidates[index] = item, target - 1, prices
    total = sum(row[1] for row in candidates)
    budget = sum(target * max(prices.values()) for _, target, prices in candidates)
    rewards = _reward_terms(cfg, period, [item.pk for item, _, _ in candidates])
    if total > capacity or not 0 < budget <= MERCHANT_BUDGET_PER_PERIOD or not rewards:
        return None
    order = VillageCaravanOrder.objects.create(period_index=period,
        announcement_world_minute=minute, deadline_world_minute=deadline,
        budget_total=budget, rewards=rewards, procurement_snapshot=procurement)
    for item, target, prices in candidates:
        VillageCaravanOrderLine.objects.create(order=order, item_id=item.pk, target_quantity=target,
                                               min_quality=1, quality_prices=prices)
    for target_day in (first_day, first_day + 1):
        VillageCaravanOrderWindow.objects.create(order=order, day_index=target_day, reserved_capacity=total)
    _event("CARAVAN_ORDER_PUBLISHED", f"caravan-order:{order.pk}:published", minute,
        {"order_id": str(order.pk), "deadline_world_minute": order.deadline_world_minute,
         "theme": procurement["theme"], "theme_label": procurement["theme_label"],
         "trial_item_ids": procurement["trial_item_ids"],
         "message": f"商队公布{procurement['theme_label']}订单，合格交货逐笔付款，交齐后获得下趟公共奖励。"})
    return order


def _reserved_for_day(day):
    result = 0
    for window in VillageCaravanOrderWindow.objects.select_related("order").filter(
            day_index=day, order__status__in=ACTIVE):
        remaining = sum(line.target_quantity - line.delivered_quantity for line in window.order.lines.all())
        result += min(window.reserved_capacity, remaining)
    return result


@transaction.atomic
def apply_visit_rewards(visit):
    """Called once during real visit generation, under the same world lock.

    Repeated calls and previously generated visits cannot consume a reward.
    Emergency/NONE visits leave vouchers pending.
    """
    clock = _clock()
    old = VillageCaravanVisitAllocation.objects.filter(visit=visit).first()
    if old:
        return old
    cfg = VillageSimulationConfig.objects.filter(config_key="default").first()
    base_import = (cfg.caravan_normal_capacity if cfg and visit.route == "NORMAL"
                   else sum(visit.initial_stock.values()))
    base_export = visit.export_capacity
    allocation = VillageCaravanVisitAllocation.objects.create(visit=visit,
        base_export_capacity=base_export, base_import_capacity=base_import,
        base_export_used=visit.exported_amount,
        order_reserved_capacity=min(base_export, _reserved_for_day(visit.pk)) if visit.route == "NORMAL" else 0)
    if (visit.route != "NORMAL" or visit.created_world_minute is None or not visit.initial_stock or not cfg
            or visit.created_world_minute != clock.world_minute):
        return allocation
    from .caravan import item_pool
    from .professional_catalog import PROFESSIONAL_IMPORT_LIMITS
    legal = set(Item.objects.filter(pk__in=item_pool(cfg), is_deleted=False).values_list("pk", flat=True))
    vouchers = list(VillageCaravanReward.objects.select_for_update().filter(status="PENDING",
        earned_world_minute__lte=visit.created_world_minute).order_by("earned_world_minute", "pk"))
    usable, buy, cargo, buy_items, extra = [], 0, 0, set(), []
    for voucher in vouchers:
        terms = voucher.terms
        kinds = terms.get("extra_kinds", [])
        if kinds and (any(row["item_id"] not in legal or PROFESSIONAL_IMPORT_LIMITS.get(row["item_id"], 3) < 3
                         for row in kinds) or not (legal - {row["item_id"] for row in kinds})):
            continue
        usable.append(voucher)
        purchase = terms.get("buy_capacity", {})
        offered = min(20, base_export // 4, purchase.get("bonus_capacity", 0))
        if offered > buy:
            buy, buy_items = offered, set(purchase.get("item_ids", []))
        elif offered == buy and offered:
            buy_items.update(purchase.get("item_ids", []))
        cargo = max(cargo, min(20, base_import // 4, terms.get("import_capacity", {}).get("bonus_capacity", 0)))
        if kinds and not extra:
            extra = deepcopy(kinds[:2])
    if not usable:
        return allocation
    # Kinds consume the same additional cargo allowance as an import reward.
    # For a kinds-only reward the frozen allowance is six units.
    kind_units = sum(row["minimum_quantity"] for row in extra)
    frozen_kind_cap = max((v.terms.get("max_import_bonus", 0) for v in usable), default=0)
    bonus_import = min(20, max(cargo, min(frozen_kind_cap, kind_units)))
    if extra and bonus_import < kind_units:
        # Cannot silently replace a published promise with fewer units.
        return allocation
    stock = dict(visit.initial_stock)
    if extra:
        extra_ids = {row["item_id"] for row in extra}
        displaced = sum(stock.pop(str(item), 0) for item in extra_ids)
        baseline = sorted(legal - extra_ids)
        for _ in range(displaced):
            available = [item for item in baseline if stock.get(str(item), 0) < PROFESSIONAL_IMPORT_LIMITS.get(item, base_import)]
            if not available:
                return allocation
            key = str(min(available, key=lambda item: (stock.get(str(item), 0), item)))
            stock[key] = stock.get(key, 0) + 1
        for row in extra:
            stock[str(row["item_id"])] = row["minimum_quantity"]
    for _ in range(bonus_import - kind_units):
        requested = [item for item in visit.requested_items if item in legal]
        options = requested + [item for item in sorted(legal) if item not in requested]
        options = [item for item in options if stock.get(str(item), 0) < PROFESSIONAL_IMPORT_LIMITS.get(item, base_import + bonus_import)]
        if not options:
            return allocation
        key = str(options[0])
        stock[key] = stock.get(key, 0) + 1
    allocation.bonus_export_capacity = buy
    allocation.bonus_export_item_ids = sorted(buy_items)
    allocation.bonus_import_capacity = bonus_import
    allocation.reward_snapshot = dict(reward_ids=[v.pk for v in usable], buy_capacity=buy,
        buy_item_ids=sorted(buy_items), import_capacity=bonus_import, extra_kinds=extra,
        base_import_capacity=base_import, base_export_capacity=base_export)
    allocation.save()
    visit.stock = stock
    visit.initial_stock = dict(stock)
    visit.export_capacity = base_export + buy
    visit.save(update_fields=["stock", "initial_stock", "export_capacity"])
    for voucher in usable:
        voucher.status, voucher.visit, voucher.applied_world_minute = "APPLIED", visit, visit.created_world_minute
        voucher.save(update_fields=["status", "visit", "applied_world_minute"])
    _event("CARAVAN_ORDER_REWARD_APPLIED", f"caravan-visit:{visit.pk}:order-rewards", visit.created_world_minute,
        {"day_index": visit.pk, **allocation.reward_snapshot, "message": "商队已兑现整单奖励，增加本趟可用交易机会。"})
    return allocation


def export_allowance(visit, item_id=None):
    """Base capacity excludes the order reservation; bonus is named-goods-only."""
    allocation = VillageCaravanVisitAllocation.objects.filter(visit=visit).first()
    if not allocation:
        return max(0, visit.export_capacity - visit.exported_amount)
    base = max(0, allocation.base_export_capacity - allocation.order_reserved_capacity - allocation.base_export_used)
    bonus = max(0, allocation.bonus_export_capacity - allocation.bonus_export_used)
    return base + (bonus if item_id is None or item_id in allocation.bonus_export_item_ids else 0)


def record_ordinary_export(visit, item_id, quantity):
    allocation = VillageCaravanVisitAllocation.objects.select_for_update().filter(visit=visit).first()
    if not allocation:
        return
    # Spend the restricted bonus first for eligible goods; unrelated goods can
    # never use it, even after the shared base is exhausted.
    bonus = min(quantity, allocation.bonus_export_capacity - allocation.bonus_export_used) if item_id in allocation.bonus_export_item_ids else 0
    allocation.bonus_export_used += bonus
    allocation.base_export_used += quantity - bonus
    allocation.save(update_fields=["bonus_export_used", "base_export_used"])


def _actor(actor_type, actor_id, minute, location=None, lock=False):
    actor_id = str(actor_id)
    if actor_type == "PUBLIC":
        return None, None, "LOCATION_REQUIRED"
    if actor_type == "NPC":
        rows = NpcRuntimeState.objects.select_related("npc")
        if lock:
            rows = rows.select_for_update(of=("self",))
        runtime = rows.filter(npc__agent_id=actor_id, npc__is_test_only=False).first() if actor_id in FORMAL_NPCS else None
        if not runtime:
            return None, None, "CARAVAN_ORDER_FORBIDDEN"
        from .runtime_services import CommandService
        if "DELIVER_CARAVAN_ORDER" not in CommandService.allowed_actions_for(runtime.npc):
            return None, None, "CARAVAN_ORDER_FORBIDDEN"
        if runtime.action_status != "IDLE" or runtime.active_command_id is not None or (runtime.away_until_world_minute or 0) > minute:
            return None, None, "NPC_BUSY"
        error = CommandService.unavailable_reason("DELIVER_CARAVAN_ORDER", runtime)
        if error:
            return None, None, error
        if location is not None and (runtime.current_location_code != "TRADE_ROAD" or location != runtime.current_location_code):
            return None, None, "LOCATION_INVALID"
        bags = NpcGlobalInventory.objects.select_for_update() if lock else NpcGlobalInventory.objects
        return runtime.npc, bags.filter(npc=runtime.npc).first(), None
    if actor_type == "PLAYER":
        if not actor_id.isascii() or not actor_id.isdecimal() or len(actor_id) > 18:
            return None, None, "PLAYER_NOT_FOUND"
        player = GameAccount.objects.filter(pk=actor_id, is_active=True, is_ai=False).first()
        if player is None:
            return None, None, "PLAYER_NOT_FOUND"
        from player.models import PlayerHuntingState
        health = PlayerHuntingState.objects.filter(player=player).first()
        if health and (health.current_health <= 0 or health.recovery_ready_world_minute is not None):
            return None, None, "PLAYER_DOWNED" if health.current_health <= 0 else "PLAYER_RESTING"
        if location is not None and location != "TRADE_ROAD":
            return None, None, "LOCATION_INVALID"
        bags = InventoryBag.objects.select_for_update() if lock else InventoryBag.objects
        return player, bags.filter(player=player, character_id=player.username).first(), None
    return None, None, "CARAVAN_ORDER_FORBIDDEN"


def _window_reason(order, minute):
    from .caravan import ARRIVAL, DEPARTURE
    if minute >= order.deadline_world_minute:
        return "CARAVAN_ORDER_EXPIRED"
    if order.status not in ACTIVE:
        return "CARAVAN_ORDER_CLOSED"
    if not order.windows.filter(day_index=minute // DAY).exists():
        return "CARAVAN_ORDER_NOT_OPEN"
    visit = VillageCaravanVisit.objects.filter(pk=minute // DAY).first()
    return "" if (visit and visit.route == "NORMAL" and visit.location_code == "TRADE_ROAD"
                  and ARRIVAL <= minute % DAY < DEPARTURE) else "CARAVAN_CLOSED"


def _order_data(order, actor_type, bag, minute, actor_error=""):
    from .caravan import ARRIVAL, DEPARTURE
    from .career_trade import is_local_production, local_stock
    reason = _window_reason(order, minute) or actor_error
    procurement = order.procurement_snapshot or {}
    local_ids = set(procurement.get("local_source_item_ids", []))
    lines = []
    names = dict(Item.objects.filter(pk__in=[line.item_id for line in order.lines.all()]).values_list("pk", "item_name"))
    for line in order.lines.all():
        remaining = line.target_quantity - line.delivered_quantity
        stock = local_stock(line.item_id, minute) if line.item_id in local_ids else _village_stock(line.item_id, minute)
        village_surplus = max(0, stock - SAFE_STOCKS.get(line.item_id, 0)) if bag else 0
        batches = []
        if bag:
            for slot in bag.slots.filter(item_id=line.item_id, item_amount__gt=0).order_by("quality", "slot_index"):
                available = _available(slot, minute) if slot.quality >= line.min_quality else 0
                if line.item_id in local_ids and not is_local_production(slot):
                    available = 0
                price = line.quality_prices.get(str(slot.quality), 0)
                if available and price:
                    deliverable = min(remaining, available, village_surplus)
                    batches.append(dict(instance_id=str(slot.instance_id), quality=slot.quality,
                        available_quantity=deliverable, owned_available_quantity=available,
                        unit_price=price, preview_income=price * deliverable))
        lines.append(dict(line_id=line.pk, item_id=line.item_id, item_name=names.get(line.item_id, str(line.item_id)),
            target_quantity=line.target_quantity, delivered_quantity=line.delivered_quantity,
            local_production_required=line.item_id in local_ids,
            trial_item=line.item_id in procurement.get("trial_item_ids", []),
            remaining_quantity=remaining, min_quality=line.min_quality, quality_prices=deepcopy(line.quality_prices),
            available_quantity=min(remaining, village_surplus, sum(row["available_quantity"] for row in batches)), deliverable_batches=batches))
    status = order.status
    if status in ACTIVE:
        status = "EXPIRED" if minute >= order.deadline_world_minute else "OPEN" if minute >= order.windows.first().day_index * DAY + ARRIVAL else "ANNOUNCED"
    return dict(order_id=str(order.pk), status=status,
        theme=procurement.get("theme", ""), theme_label=procurement.get("theme_label", "全村收购"),
        trial_item_ids=procurement.get("trial_item_ids", []),
        procurement_snapshot=deepcopy(procurement),
        announcement_world_minute=order.announcement_world_minute, deadline_world_minute=order.deadline_world_minute,
        delivery_windows=[dict(day_index=w.day_index, arrival_world_minute=w.day_index * DAY + ARRIVAL,
            departure_world_minute=w.day_index * DAY + DEPARTURE, reserved_capacity=w.reserved_capacity,
            delivered_quantity=w.delivered_quantity) for w in order.windows.all()],
        location_code="TRADE_ROAD", can_deliver=not reason, reason=reason,
        budget_total=order.budget_total, budget_spent=order.budget_spent,
        budget_remaining=order.budget_total - order.budget_spent, budget_source=order.budget_source,
        rewards=deepcopy(order.rewards), lines=lines, completed_world_minute=order.completed_world_minute)


def orders_overview(actor_type, actor_id, world_minute):
    """Pure projection, including only the authenticated actor's inventory."""
    cfg = VillageSimulationConfig.objects.filter(config_key="default").first()
    _, bag, error = _actor(actor_type, actor_id, world_minute)
    orders = VillageCaravanOrder.objects.prefetch_related("lines", "windows").order_by("-announcement_world_minute")
    active = orders.filter(status__in=ACTIVE, deadline_world_minute__gt=world_minute).first()
    recent = list(orders.exclude(pk=active.pk)[:3]) if active else list(orders[:3])
    return dict(enabled=bool(cfg and getattr(cfg, "caravan_orders_enabled", False)), world_minute=world_minute,
        active_order=_order_data(active, actor_type, bag, world_minute, error or "") if active else None,
        recent_orders=[_order_data(row, actor_type, None, world_minute) for row in recent],
        pending_rewards=[dict(reward_id=row.pk, order_id=str(row.completion.order_id),
            earned_world_minute=row.earned_world_minute, rewards=deepcopy(row.terms)) for row in
            VillageCaravanReward.objects.select_related("completion").filter(status="PENDING").order_by("pk")])


@transaction.atomic
def deliver_order(actor_type, actor_id, params, world_minute, request_id, current_location_code):
    """Exactly one authenticated, precise-batch settlement; failures move no assets."""
    clock = _clock()
    minute, actor_id = clock.world_minute, str(actor_id)
    try:
        rid = uuid.UUID(str(request_id))
    except (ValueError, TypeError, AttributeError):
        return _fail("INVALID_PARAMS", message="请求标识无效。")
    original_rid = rid
    if actor_type == "PLAYER":
        rid = uuid.uuid5(uuid.NAMESPACE_URL, f"caravan-order:PLAYER:{actor_id}:{rid}")
    digest = hashlib.sha256(json.dumps(dict(actor_type=actor_type, actor_id=actor_id, params=params),
        sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    prior = VillageCaravanOrderReceipt.objects.filter(pk=rid).first()
    if prior is None and actor_type == "PLAYER":
        # Retain old successful/failing client retries after introducing the
        # same per-player namespace as the surrounding player action service.
        prior = VillageCaravanOrderReceipt.objects.filter(pk=original_rid,
            actor_type="PLAYER", actor_id=actor_id).first()
    if prior:
        if prior.payload_hash != digest:
            return _fail("REQUEST_CONFLICT", message="同一请求编号不能用于不同交货。")
        answer = deepcopy(prior.response)
        answer["replayed"] = True
        return answer
    def reject(code, data=None):
        message = "此订单只收有真实本地生产记录的批次，当前物品来源不符合要求。" if code == "CARAVAN_ORDER_LOCAL_PRODUCTION_REQUIRED" else code
        answer = _fail(code, message=message, world_minute=minute, request_id=str(rid), data=data or {})
        VillageCaravanOrderReceipt.objects.create(request_id=rid, actor_type=actor_type, actor_id=actor_id,
            payload_hash=digest, response=answer, world_minute=minute)
        return answer
    if (not isinstance(params, dict) or set(params) - {"order_id", "item_id", "instance_id", "quantity", "expected_unit_price", "expected_asset_version"}
            or type(params.get("quantity")) is not int or not 0 < params["quantity"] <= 1000000
            or type(params.get("item_id")) is not int):
        return reject("INVALID_PARAMS")
    try:
        oid, instance_id = uuid.UUID(str(params.get("order_id"))), uuid.UUID(str(params.get("instance_id")))
    except (ValueError, TypeError, AttributeError):
        return reject("INVALID_PARAMS")
    order = VillageCaravanOrder.objects.select_for_update().filter(pk=oid).first()
    if order is None:
        return reject("CARAVAN_ORDER_NOT_FOUND")
    reason = _window_reason(order, minute)
    if reason:
        return reject(reason)
    actor, bag, reason = _actor(actor_type, actor_id, minute, current_location_code, lock=True)
    if reason or bag is None:
        return reject(reason or "CARAVAN_ORDER_FORBIDDEN")
    line = VillageCaravanOrderLine.objects.select_for_update().filter(order=order, item_id=params["item_id"]).first()
    if line is None:
        return reject("CARAVAN_ORDER_ITEM_INVALID")
    quantity = params["quantity"]
    remaining = line.target_quantity - line.delivered_quantity
    if quantity > remaining:
        return reject("CARAVAN_ORDER_REMAINING_CHANGED", {"remaining_quantity": remaining, "item_id": line.item_id})
    slot = bag.slots.select_for_update().filter(instance_id=instance_id, item_id=line.item_id).first()
    from .career_trade import is_local_production, local_stock
    local_only = line.item_id in (order.procurement_snapshot or {}).get("local_source_item_ids", [])
    if slot is not None and local_only and not is_local_production(slot):
        return reject("CARAVAN_ORDER_LOCAL_PRODUCTION_REQUIRED")
    if slot is None:
        return reject("CARAVAN_ORDER_ITEM_NOT_OWNED")
    available = _available(slot, minute) if slot.quality >= line.min_quality else 0
    price = line.quality_prices.get(str(slot.quality))
    if available < quantity or price is None:
        return reject("INSUFFICIENT_QUALIFIED_ITEMS", {"available_quantity": available})
    if "expected_unit_price" in params and (type(params["expected_unit_price"]) is not int or params["expected_unit_price"] != price):
        return reject("PRICE_CHANGED", {"unit_price": price})
    if actor_type == "NPC" and params.get("expected_asset_version", bag.asset_version) != bag.asset_version:
        return reject("P3_ASSET_VERSION_CONFLICT")
    stock = local_stock(line.item_id, minute) if local_only else _village_stock(line.item_id, minute)
    surplus = max(0, stock - SAFE_STOCKS.get(line.item_id, 0))
    if quantity > surplus:
        return reject("EXPORT_SAFETY_STOCK_REQUIRED", {"maximum_amount": surplus})
    visit = VillageCaravanVisit.objects.select_for_update().get(pk=minute // DAY)
    allocation = VillageCaravanVisitAllocation.objects.select_for_update().filter(visit=visit).first()
    window = VillageCaravanOrderWindow.objects.select_for_update().get(order=order, day_index=visit.pk)
    if allocation is None or quantity > allocation.order_reserved_capacity - allocation.order_used:
        return reject("CARAVAN_ORDER_CAPACITY_EXCEEDED")
    income = price * quantity
    if income > order.budget_total - order.budget_spent or bag.money > 2147483647 - income:
        return reject("CARAVAN_ORDER_BUDGET_EXCEEDED")
    snapshot = dict(instance_id=str(slot.instance_id), item_id=slot.item_id, quantity=quantity,
        quality=slot.quality, produced_world_minute=slot.produced_world_minute,
        source_request_id=str(slot.source_request_id) if slot.source_request_id else None)
    slot.item_amount -= quantity
    slot.save(update_fields=["item_amount"])
    bag.money += income
    if actor_type == "NPC":
        bag.asset_version += 1
        bag.save(update_fields=["money", "asset_version", "updated_at"])
    else:
        bag._money_change_source = "caravan_order"
        bag.save(update_fields=["money"])
    line.delivered_quantity += quantity
    line.save(update_fields=["delivered_quantity"])
    order.budget_spent += income
    completed = not order.lines.filter(delivered_quantity__lt=models.F("target_quantity")).exists()
    if completed:
        order.status, order.active_key, order.completed_world_minute = "COMPLETED", None, minute
    else:
        order.status = "OPEN"
    order.save(update_fields=["budget_spent", "status", "active_key", "completed_world_minute"])
    allocation.order_used += quantity
    allocation.save(update_fields=["order_used"])
    visit.exported_amount += quantity
    visit.save(update_fields=["exported_amount"])
    window.delivered_quantity += quantity
    window.save(update_fields=["delivered_quantity"])
    owner_key = actor_id if actor_type == "NPC" else "PLAYER:" + actor_id
    effects = {owner_key: {"money": income, str(line.item_id): -quantity}}
    answer = _ok(world_minute=minute, request_id=str(rid), message="订单交货成功，货款已到账。",
        asset_versions={actor_id: bag.asset_version} if actor_type == "NPC" else {},
        effects=effects, external_delta=income, ledger_recorded=True,
        data=dict(order_id=str(order.pk), line_id=line.pk, item_id=line.item_id,
            item_name=Item.objects.filter(pk=line.item_id).values_list("item_name", flat=True).first() or str(line.item_id),
            instance_id=str(slot.instance_id), quantity=quantity,
            quality=slot.quality, unit_price=price, total_income=income, remaining_quantity=remaining - quantity,
            order_status=order.status, completed=completed, world_minute=minute,
            rewards=deepcopy(order.rewards) if completed else {}))
    receipt = VillageCaravanOrderReceipt.objects.create(request_id=rid, actor_type=actor_type, actor_id=actor_id,
        payload_hash=digest, response=answer, world_minute=minute)
    VillageCaravanOrderDelivery.objects.create(receipt=receipt, line=line, visit=visit, actor_type=actor_type,
        actor_id=actor_id, source_instance_id=instance_id, quantity=quantity, quality=slot.quality,
        unit_price=price, total_income=income, batch_snapshot=snapshot, world_minute=minute)
    ledger = EconomicLedgerEntry.objects.create(request_id=rid, entry_type="DELIVER_CARAVAN_ORDER",
        actor_npc=actor if actor_type == "NPC" else None, changes=effects, external_delta=income, world_minute=minute)
    from .career_trade import record_sale
    record_sale(actor_type, actor_id, rid, [(slot, quantity)], minute,
        income=income, external=True, source="CARAVAN_ORDER")
    if completed:
        completion = VillageCaravanOrderCompletion.objects.create(order=order, request_id=rid, world_minute=minute)
        VillageCaravanReward.objects.create(completion=completion, terms=deepcopy(order.rewards), earned_world_minute=minute)
        event = _event("CARAVAN_ORDER_COMPLETED", f"caravan-order:{order.pk}:completed", minute,
            {"order_id": str(order.pk), "rewards": order.rewards, "message": "全村商队订单已交齐，公共奖励将在下次正常到访兑现。"})
        ledger.event = event
        ledger.save(update_fields=["event"])
    elif line.delivered_quantity == line.target_quantity:
        _event("CARAVAN_ORDER_LINE_FILLED", f"caravan-order-line:{line.pk}:filled", minute,
            {"order_id": str(order.pk), "item_id": line.item_id, "message": "商队订单的一项商品已收满。"})
    return answer
