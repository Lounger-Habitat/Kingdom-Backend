"""Finite Nora contracts, linked to an actual frozen normal caravan visit."""
import uuid

from django.db import models, transaction

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.models import NpcGlobalInventory
from items.models import Item
from player.models import PlayerGrowthState, PlayerSkillProgress
from player.progression_rules import level_for_xp
from .cooperation import cooperation_score
from .models import (NpcProfile, VillageCaravanVisit, VillagePersonalCommission,
                     VillageProfessionalContractLink)
from .phase3_catalog import FEATHER, ROUGH_LEATHER

DAILY_BUDGET, MAX_OPEN, DAILY_COMPLETIONS = 180, 2, 2
LOCATION = "GENERAL_STORE"


class ProfessionalContractsError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def templates():
    # Imported lazily so authoring ownership stays with the professional catalog.
    from .professional_catalog import PROFESSIONAL_MUSHROOM, FOREST_BERRY, PROFESSIONAL_WOOD, PUMPKIN
    return (
        dict(code="FRESH_SUPPLY", title="正常商队专业鲜供", reward=72, score=0,
             items=((PROFESSIONAL_MUSHROOM, 2), (PUMPKIN, 2), (FOREST_BERRY, 2)), quality=4, remaining=300),
        dict(code="EQUIPMENT_SUPPLY", title="正常商队专业整备", reward=68, score=4,
             items=((PROFESSIONAL_WOOD, 2), (ROUGH_LEATHER, 2), (FEATHER, 2)), quality=3, remaining=0),
    )


def _level(player):
    xp = PlayerSkillProgress.objects.filter(player=player, skill_code="business").values_list("total_xp", flat=True).first() or 0
    return level_for_xp(xp)


def completed_professional_contracts(player, since_minute=0):
    return VillageProfessionalContractLink.objects.filter(commission__actor_type="PLAYER",
        commission__actor_id=str(player.pk), commission__status="COMPLETED",
        commission__completed_world_minute__gte=since_minute).select_related("commission", "visit")


def _completed_today(player, minute):
    return completed_professional_contracts(player, minute // 1440 * 1440).filter(
        commission__completed_world_minute__lt=(minute // 1440 + 1) * 1440).count()


def professional_contract_eligibility(row, player, minute, *, accepting=False):
    link = VillageProfessionalContractLink.objects.filter(commission=row).first()
    if link is None:
        return None
    if _level(player) < link.required_business_level:
        return "PROFESSIONAL_LEVEL_REQUIRED"
    if cooperation_score(player, "10015") < link.required_cooperation_score:
        return "COOPERATION_SCORE_REQUIRED"
    if _completed_today(player, minute) >= DAILY_COMPLETIONS:
        return "PROFESSIONAL_DAILY_LIMIT"
    if accepting and VillageProfessionalContractLink.objects.filter(commission__actor_type="PLAYER",
            commission__actor_id=str(player.pk), commission__status="ACTIVE",
            commission__expires_world_minute__gt=minute).exclude(commission=row).exists():
        return "PROFESSIONAL_CONTRACT_ACTIVE"
    return ""


def decorate_professional_contract(data, row, actor_type, actor_id):
    link = VillageProfessionalContractLink.objects.filter(commission=row).first()
    if link is None:
        return
    data.update(professional_contract=True, template_code=link.template_code,
        visit_day_index=link.visit_id, required_business_level=link.required_business_level,
        required_cooperation_score=link.required_cooperation_score)
    minute = GlobalGameTimeState.objects.filter(pk=1).values_list("world_minute", flat=True).first() or row.opened_world_minute
    player = GameAccount.objects.filter(pk=actor_id).first() if actor_type == "PLAYER" and str(actor_id).isdigit() else None
    reason = "COMMISSION_CLOSED" if row.status not in ("OPEN", "ACTIVE") or row.expires_world_minute <= minute else (
        professional_contract_eligibility(row, player, minute, accepting=True) if player else "")
    if reason:
        data.update(can_accept=False, reason=reason, unavailable_reason=reason)
        if data.get("is_mine") and row.status == "ACTIVE":
            data["can_act"] = False


def _context(player, minute):
    from .caravan import ARRIVAL, DEPARTURE
    from .personal_commissions import MAX_OPEN_PER_NPC
    from .phase3_asset_service import get_available_money, get_available_slot_amount
    day, hour = divmod(minute, 1440)
    visit = VillageCaravanVisit.objects.filter(pk=day, route="NORMAL").first()
    present = bool(visit and ARRIVAL <= hour < DEPARTURE)
    publisher = NpcProfile.objects.select_related("npc").filter(npc__agent_id="10015", is_active=True,
        npc__is_test_only=False).first()
    inventory = NpcGlobalInventory.objects.filter(npc=publisher.npc).first() if publisher else None
    available_gold = get_available_money(inventory) if inventory else 0
    links = VillageProfessionalContractLink.objects.select_related("commission").filter(
        commission__status__in=("OPEN", "ACTIVE"), commission__expires_world_minute__gt=minute)
    links = list(links.order_by("commission__opened_world_minute", "commission_id"))
    spent = VillageProfessionalContractLink.objects.filter(commission__opened_world_minute__gte=day * 1440,
        commission__opened_world_minute__lt=(day + 1) * 1440).aggregate(n=models.Sum("commission__reward_gold"))["n"] or 0
    prior_codes = set(VillageProfessionalContractLink.objects.filter(visit=visit).values_list("template_code", flat=True)) if visit else set()
    promised = VillagePersonalCommission.objects.filter(publisher=publisher.npc,
        status__in=("OPEN", "ACTIVE"), expires_world_minute__gt=minute) if publisher else []
    incoming = {}
    for row in promised:
        for stage in row.requirements.get("stages", []):
            for condition in stage.get("conditions", []):
                ids = condition.get("item_ids", [])
                if condition.get("type") == "item" and len(ids) == 1:
                    incoming[ids[0]] = incoming.get(ids[0], 0) + condition["target"]
    specs = templates()
    catalog = Item.objects.in_bulk({i for spec in specs for i, _ in spec["items"]})
    level, score = _level(player), cooperation_score(player, "10015")
    base_reason = ("NORMAL_CARAVAN_REQUIRED" if not present else "NORA_UNAVAILABLE" if not publisher or not inventory
        or "PUBLISH_PERSONAL_COMMISSION" not in publisher.capabilities else
        "PROFESSIONAL_LEVEL_REQUIRED" if level < 7 else
        "PROFESSIONAL_DAILY_LIMIT" if _completed_today(player, minute) >= DAILY_COMPLETIONS else
        "PROFESSIONAL_OPEN_LIMIT" if len(links) >= MAX_OPEN else
        "COMMISSION_LIMIT" if len(promised) >= MAX_OPEN_PER_NPC else "")
    projected = []
    for spec in specs:
        requirements = []
        for item_id, target in spec["items"]:
            stock = sum(get_available_slot_amount(slot, minute) for slot in inventory.slots.filter(item_id=item_id, item_amount__gt=0)
                if slot.quality >= spec['quality']) if inventory else 0
            gap = max(0, target - stock - incoming.get(item_id, 0))
            requirements.append(dict(item_id=item_id, item_name=catalog[item_id].item_name if item_id in catalog else str(item_id),
                target_stock=target, quantity=gap, shortage=gap, available_stock=stock,
                incoming_quantity=incoming.get(item_id, 0), min_quality=spec["quality"],
                requires_freshness=False, min_remaining_minutes=0))
        catalog_valid = all(i in catalog and not catalog[i].is_deleted and catalog[i].item_price > 0
                            for i, _ in spec["items"])
        full_value = sum(catalog[i].item_price * target for i, target in spec["items"]) if catalog_valid else 0
        missing_value = sum(catalog[r["item_id"]].item_price * r["quantity"] for r in requirements) if catalog_valid else 0
        reward = max(1, spec["reward"] * missing_value // full_value) if missing_value else 0
        for requirement in requirements:
            item = catalog.get(requirement["item_id"])
            requirement["catalog_unit_price"] = item.item_price if item and not item.is_deleted else None
        reason = ("TEMPLATE_PREPARED" if spec["code"] in prior_codes else base_reason or
            ("COOPERATION_SCORE_REQUIRED" if score < spec["score"] else
             "ITEM_CATALOG_MISSING" if any(i not in catalog or catalog[i].is_deleted for i, _ in spec["items"]) else
             "ITEM_PRICE_UNAVAILABLE" if not catalog_valid else
             "STOCK_SUFFICIENT" if not any(r["shortage"] for r in requirements) else
             "PROFESSIONAL_BUDGET_EXHAUSTED" if spent + reward > DAILY_BUDGET else
             "NORA_BUDGET_UNAVAILABLE" if available_gold < reward else ""))
        projected.append(dict(code=spec["code"], title=spec["title"], required_level=7,
            required_cooperation_score=spec["score"], base_reward_gold=spec["reward"], reward_gold=reward, requirements=requirements,
            pricing_basis="MISSING_CATALOG_VALUE", full_catalog_value=full_value if catalog_valid else None,
            missing_catalog_value=missing_value if catalog_valid else None,
            publishable=not reason, reason=reason))
    active = next((link.commission for link in links if link.commission.actor_type == "PLAYER"
                   and link.commission.actor_id == str(player.pk)), None)
    return dict(day=day, visit=visit, present=present, publisher=publisher, inventory=inventory,
        available_gold=available_gold, links=links, spent=spent, level=level, score=score, templates=projected, active=active)


def professional_contracts_overview(player, minute, read_only=False):
    from .personal_commissions import held_batch_issues
    ctx = _context(player, minute)
    orders = []
    for link in ctx["links"]:
        row = link.commission
        mine = row.actor_type == "PLAYER" and row.actor_id == str(player.pk)
        issues = held_batch_issues(row, minute) if mine else []
        reason = "MODE_READ_ONLY" if read_only else "COMMISSION_ALREADY_ACCEPTED" if row.status != "OPEN" else professional_contract_eligibility(row, player, minute, accepting=True)
        orders.append(dict(commission_id=str(row.pk), template_code=link.template_code, visit_day_index=link.visit_id,
            status=row.status, can_accept=not reason, reason=reason or "", is_mine=mine,
            held_stale=bool(issues), held_reason=issues[0]["reason"] if issues else "",
            advice="先前托管物资已不满足要求，请弃单原样取回。" if issues else ""))
    can_prepare = any(row["publishable"] for row in ctx["templates"])
    reason = "MODE_READ_ONLY" if read_only else "" if can_prepare else next(
        (row["reason"] for row in ctx["templates"] if row["reason"] != "TEMPLATE_PREPARED"), "NO_PREPARABLE_DEMAND")
    shared = VillageProfessionalContractLink.objects.aggregate(n=models.Sum("commission__version"))["n"] or 0
    shared += VillageProfessionalContractLink.objects.count()
    if ctx["inventory"]:
        shared += ctx["inventory"].asset_version
    return dict(revision=PlayerGrowthState.objects.filter(player=player).values_list("revision", flat=True).first() or 0,
        shared_revision=shared, observed_world_minute=minute, visit_day_index=ctx["visit"].pk if ctx["visit"] else None,
        route=ctx["visit"].route if ctx["visit"] else "", visit_present=ctx["present"], can_prepare=can_prepare and not read_only,
        reason=reason, budget_limit=DAILY_BUDGET, published_gold=ctx["spent"], budget_remaining=max(0, DAILY_BUDGET - ctx["spent"]),
        available_publisher_gold=ctx["available_gold"], max_open=MAX_OPEN, open_count=len(ctx["links"]),
        skill_level=ctx["level"], cooperation_score=ctx["score"], daily_limit=DAILY_COMPLETIONS,
        completed_today=_completed_today(player, minute), active_commission_id=str(ctx["active"].pk) if ctx["active"] else None,
        orders=orders, templates=ctx["templates"])


@transaction.atomic
def execute_prepare_professional_contracts(player, bag, params, rid, clock):
    if not isinstance(params, dict) or params or not bag.pk or bag.player_id != player.pk or bag.character_id != player.username:
        raise ProfessionalContractsError("INVALID_PARAMS", "请使用本人主背包准备专业合同。")
    authority = GlobalGameTimeState.objects.select_for_update().filter(pk=1).first()
    if not authority or authority.world_minute != clock.world_minute:
        raise ProfessionalContractsError("WORLD_TIME_CHANGED", "世界时间已变化，请刷新。")
    from .personal_commissions import CommissionError, expire_commissions, publish_commission
    from .caravan import DEPARTURE
    minute = authority.world_minute
    expire_commissions(minute)
    published, spent = [], 0
    for spec in templates():
        ctx = _context(player, minute)
        template = next(t for t in ctx["templates"] if t["code"] == spec["code"])
        if not template["publishable"]:
            continue
        conditions = [dict(id=f"item_{r['item_id']}", type="item", item_ids=[r["item_id"]], target=r["quantity"],
            min_quality=r["min_quality"], requires_freshness=r["requires_freshness"], min_remaining_minutes=r["min_remaining_minutes"])
            for r in template["requirements"] if r["quantity"]]
        request = uuid.uuid5(uuid.NAMESPACE_URL, f"g6-nora-professional:{ctx['visit'].pk}:{spec['code']}")
        try:
            result = publish_commission(ctx["publisher"].npc, dict(title=spec["title"],
                description="真实正常商队当趟备货；整单达标才结算，允许采购合作。物资交Nora本人，后续出口另按真实交易执行。",
                reward_gold=template["reward_gold"], valid_for_minutes=ctx["day"] * 1440 + DEPARTURE - minute,
                default_location_code=LOCATION, stages=[dict(id="stock", title="整单备齐后结算", conditions=conditions)]), request, minute)
        except CommissionError as exc:
            raise ProfessionalContractsError(exc.code, exc.message) from exc
        row = VillagePersonalCommission.objects.get(pk=result["commission_id"])
        VillageProfessionalContractLink.objects.create(commission=row, visit=ctx["visit"], template_code=spec["code"], required_cooperation_score=spec["score"])
        published.append(str(row.pk)); spent += row.reward_gold
    view = professional_contracts_overview(player, minute)
    if not published and not VillageProfessionalContractLink.objects.filter(visit_id=view["visit_day_index"]).exists():
        raise ProfessionalContractsError(view["reason"], "当前没有可由Nora真实缺口、当趟商队和钱包支持的专业合同。")
    return dict(published_commission_ids=published, spent_gold=spent, professional_contracts=view)
