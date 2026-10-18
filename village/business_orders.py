"""Finite Lucy procurement, backed by her actual commission escrow and needs."""
import uuid

from django.db import models, transaction

from game_time.models import GlobalGameTimeState
from inventory.models import NpcGlobalInventory
from items.models import Item
from player.models import PlayerGrowthState, PlayerSkillProgress
from player.progression_rules import level_for_xp
from .models import (NpcProfile, VillageBusinessOrderDay, VillageBusinessOrderLink,
                     VillagePersonalCommission)

DAILY_BUDGET, MAX_OPEN, DAILY_COMPLETIONS = 120, 4, 2
LOCATION, LIFETIME = "RESTAURANT", 1440
TEMPLATES = (
    dict(code="CORN", title="饭店基础备货：鲜玉米", level=1, reward=24, items=((1030, 4, True),)),
    dict(code="WOOD", title="饭店基础备货：木材", level=1, reward=40, items=((1002, 4, False),)),
    dict(code="VEGETABLES", title="饭店组合备货：三种蔬菜", level=2, reward=32,
         items=((1030, 2, True), (1025, 2, True), (1042, 2, True))),
    dict(code="FRESH_MEALS", title="饭店鲜餐备货：两种料理", level=4, reward=48,
         items=((29201, 2, True), (29204, 2, True))),
)


def publication_templates(minute):
    """World-day priority, never a player/request random draw."""
    preferred, other = (TEMPLATES[2], TEMPLATES[3]) if minute // 1440 % 2 == 0 else (TEMPLATES[3], TEMPLATES[2])
    return (preferred, TEMPLATES[0], TEMPLATES[1], other)


class BusinessOrdersError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def _level(player):
    xp = PlayerSkillProgress.objects.filter(player=player, skill_code="business").values_list("total_xp", flat=True).first() or 0
    return level_for_xp(xp)


def _links():
    return VillageBusinessOrderLink.objects.select_related("commission", "commission__publisher")


def _completed(player_id, minute):
    day = minute // 1440
    return _links().filter(commission__actor_type="PLAYER", commission__actor_id=str(player_id),
        commission__status="COMPLETED", commission__completed_world_minute__gte=day * 1440,
        commission__completed_world_minute__lt=(day + 1) * 1440).count()


def business_order_eligibility(row, player, minute, *, accepting=False):
    """None means an ordinary autonomous NPC commission: its rules stay intact."""
    link = VillageBusinessOrderLink.objects.filter(commission=row).first()
    if link is None:
        return None
    if _level(player) < link.required_business_level:
        return "BUSINESS_LEVEL_REQUIRED"
    if _completed(player.pk, minute) >= DAILY_COMPLETIONS:
        return "BUSINESS_DAILY_LIMIT"
    if accepting and _links().filter(commission__actor_type="PLAYER", commission__actor_id=str(player.pk),
            commission__status="ACTIVE", commission__expires_world_minute__gt=minute).exclude(commission=row).exists():
        return "BUSINESS_ORDER_ACTIVE"
    return ""


def decorate_commission(data, row, actor_type, actor_id):
    """One shared authority projection for the existing commission board/UI."""
    link = VillageBusinessOrderLink.objects.filter(commission=row).first()
    if link is None:
        return
    data.update(business_order=True, template_code=link.template_code,
        required_business_level=link.required_business_level)
    clock = GlobalGameTimeState.objects.filter(pk=1).first()
    minute = clock.world_minute if clock else row.opened_world_minute
    from account.models import GameAccount
    player = GameAccount.objects.filter(pk=actor_id).first() if actor_type == "PLAYER" and str(actor_id).isdigit() else None
    reason = ("COMMISSION_CLOSED" if row.expires_world_minute <= minute or row.status not in ("OPEN", "ACTIVE")
        else business_order_eligibility(row, player, minute, accepting=True) if player else "")
    if reason:
        data.update(can_accept=False, reason=reason, unavailable_reason=reason)
        if row.status == "ACTIVE" and data.get("is_mine"):
            data["can_act"] = False
    data.setdefault("held_stale", False)
    data.setdefault("held_reason", "")
    data.setdefault("advice", "")


def _context(player, minute):
    day = minute // 1440
    today = VillageBusinessOrderDay.objects.filter(pk=day).first()
    publisher = NpcProfile.objects.select_related("npc").filter(npc__agent_id="10014", is_active=True,
        npc__is_test_only=False).first()
    inventory = NpcGlobalInventory.objects.filter(npc=publisher.npc).first() if publisher else None
    from .phase3_asset_service import get_available_money, get_available_slot_amount
    available_gold = get_available_money(inventory) if inventory else 0
    links = list(_links().filter(commission__status__in=("OPEN", "ACTIVE"),
        commission__expires_world_minute__gt=minute).order_by("commission__opened_world_minute", "commission_id"))
    incoming = {}
    promised_orders = VillagePersonalCommission.objects.filter(publisher=publisher.npc,
        status__in=("OPEN", "ACTIVE"), expires_world_minute__gt=minute) if publisher else []
    for order in promised_orders:
        for stage in order.requirements.get("stages", []):
            for condition in stage.get("conditions", []):
                ids = condition.get("item_ids", [])
                if condition.get("type") == "item" and len(ids) == 1:
                    incoming[ids[0]] = incoming.get(ids[0], 0) + condition["target"]
    stocks = {}
    for template in TEMPLATES:
        for item_id, _, fresh in template["items"]:
            key = (item_id, fresh)
            if key in stocks:
                continue
            slots = inventory.slots.filter(item_id=item_id, item_amount__gt=0) if inventory else []
            stocks[key] = sum(get_available_slot_amount(slot, minute) for slot in slots
                if not slot.freshness_unknown and slot.quality >= 3
                and (not fresh or slot.expires_world_minute is not None and slot.expires_world_minute >= minute + 60))
    published = set(VillageBusinessOrderLink.objects.filter(day_id=day).values_list("template_code", flat=True))
    catalog = Item.objects.in_bulk({item_id for template in TEMPLATES for item_id, _, _ in template["items"]})
    level = _level(player)
    completed = _completed(player.pk, minute)
    active = next((link.commission for link in links if link.commission.actor_type == "PLAYER"
                   and link.commission.actor_id == str(player.pk)), None)
    spent = today.published_gold if today else 0
    from .personal_commissions import MAX_OPEN_PER_NPC
    base_reason = ("LUCY_UNAVAILABLE" if not publisher or not inventory
        or "PUBLISH_PERSONAL_COMMISSION" not in publisher.capabilities else
        "BUSINESS_DAILY_LIMIT" if completed >= DAILY_COMPLETIONS else
        "BUSINESS_OPEN_LIMIT" if len(links) >= MAX_OPEN else
        "COMMISSION_LIMIT" if len(promised_orders) >= MAX_OPEN_PER_NPC else "")
    templates = []
    for template in publication_templates(minute):
        requirements = []
        for item_id, quantity, fresh in template["items"]:
            item = catalog.get(item_id)
            stock, promised = stocks[(item_id, fresh)], incoming.get(item_id, 0)
            requirements.append(dict(item_id=item_id, item_name=item.item_name if item else str(item_id),
                quantity=quantity, min_quality=3, requires_freshness=fresh, min_remaining_minutes=60 if fresh else 0,
                available_stock=stock, incoming_quantity=promised, target_stock=quantity,
                shortage=max(0, quantity - stock - promised)))
        catalog_present = all(catalog.get(r["item_id"]) and not catalog[r["item_id"]].is_deleted for r in requirements)
        valid_prices = catalog_present and all(catalog[r["item_id"]].item_price > 0 for r in requirements)
        reference_value = sum(catalog[r["item_id"]].item_price * r["quantity"] for r in requirements) if valid_prices else None
        # Exact integer ceil(value * 1.2); preview and publication use one quote.
        reward = max(template["reward"], (reference_value * 6 + 4) // 5) if valid_prices else 0
        reason = ("TEMPLATE_PREPARED" if template["code"] in published else base_reason or
            ("BUSINESS_LEVEL_REQUIRED" if level < template["level"] else
             "ITEM_CATALOG_MISSING" if not catalog_present else
             "ITEM_PRICE_UNAVAILABLE" if not valid_prices else
             "STOCK_SUFFICIENT" if all(r["shortage"] == 0 for r in requirements) else
             "PARTIAL_STOCK_GAP" if any(r["shortage"] < r["quantity"] for r in requirements) else
             "BUSINESS_BUDGET_EXHAUSTED" if spent + reward > DAILY_BUDGET else
             "LUCY_BUDGET_UNAVAILABLE" if available_gold < reward else ""))
        templates.append(dict(code=template["code"], title=template["title"], required_level=template["level"],
            reward_gold=reward, base_reward_gold=template["reward"], reference_value=reference_value,
            pricing_basis="MAX_BASE_OR_120_PERCENT_CATALOG", requirements=requirements, publishable=not reason, reason=reason))
    return dict(day=day, today=today, publisher=publisher, inventory=inventory, available_gold=available_gold,
        links=links, level=level, completed=completed, active=active, spent=spent, templates=templates)


def business_orders_overview(player, minute, read_only=False):
    """No template publication, expiry, wallet reservation, or growth creation."""
    ctx = _context(player, minute)
    orders = []
    from .personal_commissions import held_batch_issues
    for link in ctx["links"]:
        row = link.commission
        mine = row.actor_type == "PLAYER" and row.actor_id == str(player.pk)
        issues = held_batch_issues(row, minute) if mine else []
        reason = ("MODE_READ_ONLY" if read_only else "COMMISSION_ALREADY_ACCEPTED" if row.status != "OPEN" else
                  business_order_eligibility(row, player, minute, accepting=True))
        orders.append(dict(commission_id=str(row.pk), template_code=link.template_code,
            required_level=link.required_business_level, status=row.status, can_accept=not reason,
            reason=reason or "", held_stale=bool(issues), held_reason=issues[0]["reason"] if issues else "",
            advice="先前托管物资已不满足要求，请弃单原样取回。" if issues else ""))
    publishable = any(t["publishable"] for t in ctx["templates"])
    reason = "MODE_READ_ONLY" if read_only else "" if publishable else next(
        (t["reason"] for t in ctx["templates"] if t["reason"] not in ("TEMPLATE_PREPARED", "BUSINESS_LEVEL_REQUIRED")),
        "NO_PREPARABLE_DEMAND")
    versions = VillageBusinessOrderDay.objects.aggregate(n=models.Sum("revision"))["n"] or 0
    versions += VillageBusinessOrderLink.objects.aggregate(n=models.Sum("commission__version"))["n"] or 0
    return dict(revision=PlayerGrowthState.objects.filter(player=player).values_list("revision", flat=True).first() or 0,
        shared_revision=versions, observed_world_minute=minute, day_index=ctx["day"],
        can_prepare=publishable and not read_only, reason=reason, budget_limit=DAILY_BUDGET,
        published_gold=ctx["spent"], budget_remaining=DAILY_BUDGET - ctx["spent"],
        available_publisher_gold=ctx["available_gold"], max_open=MAX_OPEN, open_count=len(ctx["links"]),
        skill_level=ctx["level"], daily_limit=DAILY_COMPLETIONS, completed_today=ctx["completed"],
        remaining_completions=max(0, DAILY_COMPLETIONS - ctx["completed"]),
        active_commission_id=str(ctx["active"].pk) if ctx["active"] else None,
        orders=orders, templates=ctx["templates"],
        rotation=dict(day_index=ctx["day"], priority_template_code=publication_templates(minute)[0]["code"],
                      publication_order=[t["code"] for t in publication_templates(minute)]))


@transaction.atomic
def execute_business_orders(player, bag, params, rid, clock):
    if not isinstance(params, dict) or params or not bag.pk or bag.player_id != player.pk or bag.character_id != player.username:
        raise BusinessOrdersError("INVALID_PARAMS", "请使用本人主背包执行备货准备。")
    authoritative = GlobalGameTimeState.objects.select_for_update().filter(pk=1).first()
    if not authoritative or authoritative.world_minute != clock.world_minute:
        raise BusinessOrdersError("WORLD_TIME_CHANGED", "世界时间已变化，请刷新备货。")
    minute, day = clock.world_minute, clock.world_minute // 1440
    from .personal_commissions import CommissionError, expire_commissions, publish_commission
    expire_commissions(minute)
    created_ids, spent = [], 0
    for template in publication_templates(minute):
        ctx = _context(player, minute)
        projection = next(t for t in ctx["templates"] if t["code"] == template["code"])
        if not projection["publishable"]:
            continue
        budget, _ = VillageBusinessOrderDay.objects.select_for_update().get_or_create(day_index=day,
            defaults=dict(last_prepared_world_minute=minute))
        conditions = [dict(id=f"item_{r['item_id']}", type="item", item_ids=[r["item_id"]],
            target=r["quantity"], min_quality=r["min_quality"], requires_freshness=r["requires_freshness"],
            min_remaining_minutes=r["min_remaining_minutes"]) for r in projection["requirements"]]
        request_id = uuid.uuid5(uuid.NAMESPACE_URL, f"g4-lucy-order:{day}:{template['code']}")
        try:
            result = publish_commission(ctx["publisher"].npc, dict(title=template["title"],
                description="真实库存缺口备货；整单达标后结算，允许采购合作。每人本线路每天最多完成2单。",
                reward_gold=projection["reward_gold"], valid_for_minutes=LIFETIME, default_location_code=LOCATION,
                stages=[dict(id="stock", title="整单备齐后结算", conditions=conditions)]), request_id, minute)
        except CommissionError as exc:
            raise BusinessOrdersError(exc.code, exc.message) from exc
        row = VillagePersonalCommission.objects.get(pk=result["commission_id"])
        VillageBusinessOrderLink.objects.create(commission=row, day=budget, template_code=template["code"],
            required_business_level=template["level"])
        budget.published_gold += row.reward_gold
        budget.last_prepared_world_minute = minute
        budget.revision += 1
        budget.save(update_fields=["published_gold", "last_prepared_world_minute", "revision"])
        created_ids.append(str(row.pk))
        spent += row.reward_gold
    snapshot = business_orders_overview(player, minute)
    # A repeated explicit prepare is a successful no-op; templates and money
    # never refresh. The action receipt retains the original publication result.
    if not created_ids and not VillageBusinessOrderDay.objects.filter(pk=day).exists():
        raise BusinessOrdersError(snapshot["reason"], "当前没有可由Lucy真实库存与预算支持的备货，请查看缺口。")
    return dict(published_commission_ids=created_ids, spent_gold=spent, business_orders=snapshot)
