"""Fact-driven, configurable invitations. No inventory, money or NPC commands are changed.

Write callers hold the authoritative clock lock and invoke evaluation after settlement.
Projections are read-only; replayed requests do not open a second invitation.
"""
import hashlib
import json
from copy import deepcopy

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition, NpcGlobalInventorySlot
from .models import VillageDailyEvent, VillageDailyEventTemplate, VillageSimulationConfig


PARAMETERS = {
    "INVENTORY_SHORTAGE": {"npc_id", "item_ids", "minimum", "location_code"},
    "RECIPE_PREPARATION": {"npc_id", "recipe_ids", "location_code"},
    "FARM_PREPARATION": {"npc_id", "seed_item_ids", "tool_item_id", "location_code"},
    "PERISHABLE_STOCK": {"npc_id", "item_ids", "warning_minutes", "location_code"},
    "DISH_REVIEW": {"npc_id", "location_code"},
    "CARAVAN_VISIT": {"npc_id"},
    "PUBLIC_MEAL": {"npc_id", "store_code", "location_code"},
    "AFFAIR_STAGE": {"affair_id"},
    "NORMAL_CARAVAN_RETURN": {"affair_id"},
}
ONE_SHOT_RULES = {"DISH_REVIEW", "CARAVAN_VISIT", "PUBLIC_MEAL", "AFFAIR_STAGE", "NORMAL_CARAVAN_RETURN"}


def validate_template(template, *, check_references=False):
    p = template.parameters
    if template.rule_type not in PARAMETERS or not isinstance(p, dict) or set(p) - PARAMETERS[template.rule_type]:
        raise ValidationError({"parameters": "规则或参数名称无效；请参照日常事件配置说明。"})
    if template.npc_scope not in {"PERSONAL", "PUBLIC"}:
        raise ValidationError({"npc_scope": "请选择个人或公共NPC通知范围。"})
    if template.npc_scope == "PERSONAL" and not p.get("npc_id"):
        raise ValidationError({"npc_scope": "个人事件必须指定对应NPC；公共事务请选择公共范围。"})
    if type(template.invitation_minutes) is not int or template.invitation_minutes < 1:
        raise ValidationError({"invitation_minutes": "邀请期必须为正游戏分钟。"})
    if type(template.cooldown_minutes) is not int or template.cooldown_minutes < 0:
        raise ValidationError({"cooldown_minutes": "提醒间隔不能为负数。"})
    for key in ("minimum", "warning_minutes", "tool_item_id"):
        if key in p and (type(p[key]) is not int or p[key] < 1):
            raise ValidationError({"parameters": f"{key} 必须为正整数。"})
    for key in ("item_ids", "seed_item_ids", "recipe_ids"):
        if key in p and (not isinstance(p[key], list) or not p[key] or len(p[key]) > 100
                         or any(type(i) is not int or i < 1 for i in p[key])):
            raise ValidationError({"parameters": f"{key} 必须为非空物品/配方ID数组（最多100项）。"})
    if template.rule_type in {"INVENTORY_SHORTAGE", "PERISHABLE_STOCK"} and "item_ids" not in p:
        raise ValidationError({"parameters": "该规则需要 item_ids。"})
    if template.rule_type in {"AFFAIR_STAGE", "NORMAL_CARAVAN_RETURN"} and not p.get("affair_id"):
        raise ValidationError({"parameters": "该规则需要 affair_id。"})
    if template.rule_type not in {"AFFAIR_STAGE", "NORMAL_CARAVAN_RETURN"}:
        if not isinstance(p.get("npc_id"), str) or not p["npc_id"]:
            raise ValidationError({"parameters": "该规则需要字符串 npc_id。"})
    for key in ("location_code", "store_code", "affair_id"):
        if key in p and (not isinstance(p[key], str) or not p[key] or len(p[key]) > 64):
            raise ValidationError({"parameters": f"{key} 必须为有效代码。"})
    from .phase3_catalog import FARM_CROP_RULES, FARM_TOOL_ITEM_ID, RECIPE_WHITELIST
    required_npc = {"FARM_PREPARATION": "10011", "DISH_REVIEW": "10013", "CARAVAN_VISIT": "10015", "PUBLIC_MEAL": "10014"}.get(template.rule_type)
    if required_npc and p["npc_id"] != required_npc:
        raise ValidationError({"parameters": "当前业务规则只允许该角色执行此事项。"})
    if template.rule_type == "RECIPE_PREPARATION" and not set(p.get("recipe_ids", RECIPE_WHITELIST)).issubset(RECIPE_WHITELIST):
        raise ValidationError({"parameters": "recipe_ids 必须属于现有可执行菜谱目录。"})
    if template.rule_type == "FARM_PREPARATION" and (p.get("tool_item_id", FARM_TOOL_ITEM_ID) != FARM_TOOL_ITEM_ID
            or not set(p.get("seed_item_ids", [r["seed_item_id"] for r in FARM_CROP_RULES.values()])).issubset(
                {r["seed_item_id"] for r in FARM_CROP_RULES.values()})):
        raise ValidationError({"parameters": "农具和种子必须属于现有农活目录。"})
    if template.rule_type == "PUBLIC_MEAL" and p.get("store_code", "RESTAURANT_STORE") != "RESTAURANT_STORE":
        raise ValidationError({"parameters": "公共餐只能来自Lucy经营的饭店。"})
    if check_references:
        from items.models import Item
        from recipes.models import Recipe
        from .models import VillageLocationDefinition
        if p.get("location_code") and not VillageLocationDefinition.objects.filter(location_code=p["location_code"], is_active=True).exists():
            raise ValidationError({"parameters": "location_code 不属于现有启用地点。"})
        if p.get("npc_id") and not NpcDefinition.objects.filter(agent_id=p["npc_id"], is_test_only=False).exists():
            raise ValidationError({"parameters": "npc_id 不属于现有正式NPC。"})
        for key in ("item_ids", "seed_item_ids"):
            if key in p and Item.objects.filter(pk__in=set(p[key]), is_deleted=False).count() != len(set(p[key])):
                raise ValidationError({"parameters": f"{key} 含不存在或已删除的物品。"})
        if "recipe_ids" in p and Recipe.objects.filter(pk__in=set(p["recipe_ids"])).count() != len(set(p["recipe_ids"])):
            raise ValidationError({"parameters": "recipe_ids 含不存在的配方。"})


def _inventory(npc_id, minute):
    from .phase3_asset_service import get_available_slot_amount
    rows = list(NpcGlobalInventorySlot.objects.filter(bag__npc__agent_id=npc_id, item_amount__gt=0))
    amounts = {}
    for row in rows:
        amounts[row.item_id] = amounts.get(row.item_id, 0) + get_available_slot_amount(row, minute)
    return rows, amounts


def _observation(source_id, p, *, facts=None, until=None, location=None, affair_id=""):
    return dict(source_id=str(source_id), npc_id=p.get("npc_id", ""),
        location_code=location or p.get("location_code", ""), facts=facts or {},
        until=until, affair_id=affair_id)


def _review_usable(review, minute):
    from items.models import Item
    slot = review.slot
    return (slot.bag.npc_id == review.owner_id and slot.item_amount == 1 and not slot.freshness_unknown
        and (slot.expires_world_minute is None or slot.expires_world_minute > minute)
        and Item.objects.filter(pk=slot.item_id, is_deleted=False).exists())


def _observe(rule, p, minute):
    """At most twenty sources per template. Missing business data means no opportunity."""
    from .models import VillageAffair, VillageCaravanVisit, VillageDishReview, VillageFarmPlot, VillageStore
    if p.get("npc_id") and not NpcDefinition.objects.filter(agent_id=p["npc_id"], is_test_only=False).exists():
        return {}
    found = []
    if rule == "INVENTORY_SHORTAGE":
        _, amounts = _inventory(p["npc_id"], minute)
        missing = [i for i in p["item_ids"] if amounts.get(i, 0) < p.get("minimum", 1)]
        if missing:
            found.append(_observation(p["npc_id"], p, facts={"missing_item_ids": missing}))
    elif rule == "RECIPE_PREPARATION":
        from recipes.models import Recipe
        from .phase3_catalog import RECIPE_WHITELIST
        _, amounts = _inventory(p["npc_id"], minute)
        recipes = list(Recipe.objects.prefetch_related("ingredients").filter(
            pk__in=p.get("recipe_ids", RECIPE_WHITELIST)))
        valid = [list(r.ingredients.all()) for r in recipes if r.ingredients.exists()]
        if valid and not any(all(amounts.get(i.item_id, 0) >= i.count for i in ingredients) for ingredients in valid):
            found.append(_observation(p["npc_id"], p))
    elif rule == "FARM_PREPARATION":
        from .phase3_catalog import FARM_CROP_RULES, FARM_TOOL_ITEM_ID
        from .phase3_contract import PLOT_CODES
        from inventory.models import NpcGlobalInventory
        plots = VillageFarmPlot.objects.filter(plot_code__in=PLOT_CODES, status="EMPTY", lessee_player__isnull=True, lease_enabled=False)
        bag = NpcGlobalInventory.objects.filter(npc__agent_id=p["npc_id"]).first()
        _, amounts = _inventory(p["npc_id"], minute)
        seeds = p.get("seed_item_ids", [r["seed_item_id"] for r in FARM_CROP_RULES.values()])
        tools = [r for r in bag.slots.filter(item_id=p.get("tool_item_id", FARM_TOOL_ITEM_ID),
                    item_amount__gt=0, usage__remaining__gt=0)] if bag else []
        from .phase3_asset_service import get_available_slot_amount
        usable_tool = any(get_available_slot_amount(r, minute) > 0 for r in tools)
        if plots.exists() and bag and (not any(amounts.get(i, 0) for i in seeds) or not usable_tool):
            found.append(_observation(p["npc_id"], p, facts={"plot_codes": list(plots.values_list("plot_code", flat=True))}))
    elif rule == "PERISHABLE_STOCK":
        from .phase3_asset_service import get_available_slot_amount
        rows, _ = _inventory(p["npc_id"], minute)
        near = [r for r in rows if r.item_id in p["item_ids"] and r.expires_world_minute is not None
                and minute < r.expires_world_minute <= minute + p.get("warning_minutes", 600)
                and get_available_slot_amount(r, minute) > 0]
        if near:
            found.append(_observation(p["npc_id"], p, facts={"batch_ids": [str(r.instance_id) for r in near]},
                                      until=min(r.expires_world_minute for r in near)))
    elif rule == "DISH_REVIEW":
        for r in VillageDishReview.objects.select_related("slot__bag").filter(target_npc_id=p["npc_id"], status="PENDING",
                expires_world_minute__gt=minute).order_by("created_world_minute")[:20]:
            if _review_usable(r, minute):
                found.append(_observation(r.pk, p, until=min(r.expires_world_minute, r.slot.expires_world_minute)
                                          if r.slot.expires_world_minute is not None else r.expires_world_minute))
    elif rule in {"CARAVAN_VISIT", "NORMAL_CARAVAN_RETURN"}:
        from .caravan import ARRIVAL, DEPARTURE
        day, clock = divmod(minute, 1440)
        visit = VillageCaravanVisit.objects.filter(pk=day).first()
        if visit and visit.route != "NONE" and ARRIVAL <= clock < DEPARTURE:
            if rule == "CARAVAN_VISIT":
                from .models import VillageWorldEvent
                already_imported = VillageWorldEvent.objects.filter(event_type="ECONOMY_IMPORT_GOODS",
                    actor_npc__agent_id=p["npc_id"], world_minute__gte=day*1440, world_minute__lt=(day+1)*1440).exists()
                if any(visit.stock.values()) and not already_imported:
                    found.append(_observation(day, p, until=day * 1440 + DEPARTURE, location=visit.location_code))
            else:
                affair = VillageAffair.objects.filter(pk=p["affair_id"], status="COMPLETED").first()
                # The visit's route is frozen; choose the first actual normal visit after completion.
                first = VillageCaravanVisit.objects.filter(route="NORMAL",
                    created_world_minute__gte=affair.completed_world_minute
                    ).order_by("day_index").first() if affair and affair.completed_world_minute is not None else None
                if first and first.pk == day:
                    found.append(_observation("first", p, facts={"day_index": day},
                        until=day * 1440 + DEPARTURE, location=visit.location_code, affair_id=affair.pk))
    elif rule == "PUBLIC_MEAL":
        from .lucy_restaurant import public_meal_quota, store_available
        from .lucy_catalog import PUBLIC_MEALS
        from .models import VillageStoreListing, VillageStoreInventorySlot, VillageStoreAccount
        store = VillageStore.objects.filter(pk=p.get("store_code", "RESTAURANT_STORE"), status="OPEN",
                                             operator_npc__agent_id=p["npc_id"]).first()
        quota = public_meal_quota(minute)
        if store and VillageStoreAccount.objects.filter(store=store).exists() and quota["remaining_village_today"] > 0:
            # Observe the same stock/provenance/subsidy conditions as settlement, without claiming a portion.
            for listing in VillageStoreListing.objects.filter(store=store, status="ACTIVE", unit_price=0).exclude(public_meal_code=""):
                spec = PUBLIC_MEALS.get(listing.public_meal_code)
                if not spec or spec["item_id"] != listing.item_id or quota["public_fund_balance"] < spec["subsidy"]:
                    continue
                for slot in VillageStoreInventorySlot.objects.filter(listing=listing, item_amount__gt=0):
                    if slot.item_id == spec["item_id"] and store_available(slot, minute) > 0:
                        metadata = slot.dish_metadata or {}
                        if metadata.get("maker_npc_id") == "10014" and not metadata.get("public_meal_claim"):
                            found.append(_observation(minute // 1440, p,
                                until=(minute // 1440 + 1) * 1440, location=store.location_code))
                            break
                if found:
                    break
    elif rule == "AFFAIR_STAGE":
        from .affairs import active_stage
        affair = VillageAffair.objects.filter(pk=p["affair_id"], status="ACTIVE").first()
        stage = active_stage(affair) if affair else None
        if stage:
            location = next((c["locations"][0] for c in stage["conditions"] if c.get("locations")), "")
            found.append(_observation(f"{affair.pk}:{stage['id']}", p,
                facts={"stage_id": stage["id"]}, location=location, affair_id=affair.pk))
    return {r["source_id"]: r for r in found}


def _end_fact(row, current, minute):
    """Return an observed business result before checking the invitation timer."""
    from .models import VillageAffair, VillageDishReview, VillageWorldEvent
    rule, p = row.rule_snapshot["rule_type"], row.rule_snapshot["parameters"]
    if rule == "PERISHABLE_STOCK":
        rows = list(NpcGlobalInventorySlot.objects.filter(bag__npc__agent_id=row.npc_id,
            instance_id__in=row.facts.get("batch_ids", []), item_amount__gt=0))
        if any(r.expires_world_minute is not None and r.expires_world_minute <= minute for r in rows):
            return "EXPIRED", "本次关注的鲜货已到保鲜期限，邀请结束。"
        if not rows:
            return "RESOLVED", "本次关注的鲜货已从原库存处理或转出，去向以实际生产和交易记录为准。"
    elif rule == "DISH_REVIEW":
        review = VillageDishReview.objects.filter(pk=row.source_id).first()
        if review and review.status == "COMPLETED":
            return "RESOLVED", row.rule_snapshot["resolved_summary"]
        if not review or review.status != "PENDING" or review.expires_world_minute <= minute:
            return "EXPIRED", "这次送评已取消或样品已到期，邀请结束。"
        if not _review_usable(review, minute):
            return "EXPIRED", "这次送评样品已不可用品鉴，邀请结束。"
    elif rule in {"CARAVAN_VISIT", "NORMAL_CARAVAN_RETURN"}:
        day = int(row.source_id) if rule == "CARAVAN_VISIT" else row.facts["day_index"]
        imports = VillageWorldEvent.objects.filter(event_type="ECONOMY_IMPORT_GOODS", actor_npc__agent_id=p.get("npc_id", "10015"),
                world_minute__gte=day*1440, world_minute__lt=(day+1)*1440)
        if rule == "CARAVAN_VISIT":
            imports = imports.filter(event_sequence__gt=row.facts.get("baseline_event_sequence", 0))
        if imports.exists():
            return "RESOLVED", row.rule_snapshot["resolved_summary"]
        if row.source_id not in current:
            return "EXPIRED", "这趟商队的本次参与机会已结束；采购结果以实际交易记录为准。"
    elif rule == "AFFAIR_STAGE":
        affair = VillageAffair.objects.filter(pk=row.affair_id).first()
        from .affairs import active_stage
        stage = active_stage(affair) if affair else None
        if affair and (affair.status == "COMPLETED" or stage and stage["id"] != row.facts.get("stage_id")):
            return "RESOLVED", "本阶段条件已实际完成，工程已进入下一阶段或完工。"
        if not affair:
            return "EXPIRED", "对应事务已不可用，本次邀请结束。"
    elif rule == "PUBLIC_MEAL":
        from .models import VillageCommunityMealDailyUsage
        usage = VillageCommunityMealDailyUsage.objects.filter(day_index=int(row.source_id)).first()
        if usage and usage.total_meals > row.facts.get("claimed_at_open", 0):
            return "RESOLVED", "本次公共餐已有实际领取；后续是否供餐按库存、额度和公款决定。"
        if row.source_id not in current:
            return "EXPIRED", "本次公共餐已不可领取，邀请结束。"
    elif row.source_id not in current:
        # Shortage disappearance alone must not claim a successful meal or review.
        if not NpcDefinition.objects.filter(agent_id=row.npc_id, is_test_only=False).exists():
            return "EXPIRED", "对应人物已不可用，本次邀请结束。"
        if rule == "RECIPE_PREPARATION":
            from recipes.models import Recipe
            from .phase3_catalog import RECIPE_WHITELIST
            _, amounts = _inventory(row.npc_id, minute)
            recipes = Recipe.objects.prefetch_related("ingredients").filter(pk__in=p.get("recipe_ids", RECIPE_WHITELIST))
            if not any((ingredients := list(r.ingredients.all())) and
                    all(amounts.get(i.item_id, 0) >= i.count for i in ingredients) for r in recipes):
                return "EXPIRED", "对应菜谱或备料条件已不可用，本次邀请结束。"
        if rule == "FARM_PREPARATION":
            from .models import VillageFarmPlot
            plots = list(VillageFarmPlot.objects.filter(plot_code__in=row.facts.get("plot_codes", [])))
            if not plots or all(r.lessee_player_id is not None or r.lease_enabled for r in plots):
                return "EXPIRED", "原空田已转为其他安排，本次农活邀请结束。"
        return "RESOLVED", row.rule_snapshot["resolved_summary"]
    return None


def project_daily_event(row):
    from .phase4_visibility import daily_story_npc_scope
    result = dict(id=str(row.pk), template_code=row.template_id, title=row.title, status=row.status,
        summary=row.summary, participation_hint=row.participation_hint, resolution_summary=row.resolution_summary,
        npc_id=row.npc_id, location_code=row.location_code, source_type=row.rule_snapshot["rule_type"],
        source_id=row.source_id, affair_id=row.affair_id, opened_world_minute=row.opened_world_minute,
        invite_until_world_minute=row.invite_until_world_minute, closed_world_minute=row.closed_world_minute)
    if "npc_scope" in row.rule_snapshot:
        result["npc_scope"] = row.rule_snapshot["npc_scope"]
    result["npc_scope"] = daily_story_npc_scope(result)
    return result


def project_daily_events(world_minute=None, npc_id=None, limit=20):
    rows = VillageDailyEvent.objects.all()
    if npc_id is not None:
        # Explicit scope is frozen when the invitation opens. Only legacy rows
        # without that key use the old rule's public/owner classification.
        legacy = ~Q(rule_snapshot__has_key="npc_scope")
        rows = rows.filter(Q(rule_snapshot__npc_scope="PUBLIC")
            | Q(rule_snapshot__npc_scope="PERSONAL", npc_id=str(npc_id))
            | (legacy & (Q(npc_id=str(npc_id))
                | Q(rule_snapshot__rule_type__in=["PUBLIC_MEAL", "AFFAIR_STAGE", "NORMAL_CARAVAN_RETURN"]))))
    if world_minute is not None:
        rows = rows.filter(opened_world_minute__lte=world_minute)
    # Always keep active opportunities ahead of recaps even after a burst of closures.
    active = list(rows.filter(status="OPEN").order_by("-opened_world_minute", "-id")[:limit])
    recent = list(rows.exclude(status="OPEN").order_by("-closed_world_minute", "-id")[:max(0, limit-len(active))])
    return [project_daily_event(r) for r in active + recent]


def _publish(row, revision):
    from .models import VillageWorldEvent
    from .phase4_visibility import enqueue_event_deliveries
    suffix = {"OPEN": "OPENED", "RESOLVED": "RESOLVED", "EXPIRED": "EXPIRED"}[row.status]
    summary = row.summary if row.status == "OPEN" else row.resolution_summary
    event, created = VillageWorldEvent.objects.get_or_create(dedupe_key=f"daily:{row.pk}:{suffix}", defaults=dict(
        event_type=f"STORY_DAILY_{suffix}", world_minute=row.closed_world_minute if row.status != "OPEN" else row.opened_world_minute,
        time_revision=revision, visibility="PUBLIC", location_code=row.location_code,
        participant_npc_ids=[row.npc_id] if row.npc_id else [], player_summary=summary, hub_summary=summary,
        public_data={"story_event": project_daily_event(row)}))
    if created:
        enqueue_event_deliveries(event)


def _close(row, ending, minute, revision):
    row.status, row.resolution_summary = ending
    row.closed_world_minute = minute
    row.active_key = None
    row.save(update_fields=["status", "resolution_summary", "closed_world_minute", "active_key"])
    _publish(row, revision)


@transaction.atomic
def evaluate_daily_events(world_minute=None, *, expire_only=False):
    clock = GlobalGameTimeState.objects.select_for_update().filter(pk=1).first()
    if clock is None:
        return
    minute = clock.world_minute
    if world_minute is not None and world_minute != minute:
        raise ValueError("Daily events require the current authoritative world minute")
    active_query = VillageDailyEvent.objects.select_for_update().filter(status="OPEN")
    if expire_only:
        active_query = active_query.filter(invite_until_world_minute__lte=minute)
    active = list(active_query)
    observations = {}

    def current_facts(rule, parameters):
        cache_key = (rule, json.dumps(parameters, sort_keys=True))
        if cache_key not in observations:
            observations[cache_key] = _observe(rule, parameters, minute)
        return observations[cache_key]

    for row in active:
        snapshot = row.rule_snapshot
        current = current_facts(snapshot["rule_type"], snapshot["parameters"])
        ending = _end_fact(row, current, minute)
        if not ending and minute >= row.invite_until_world_minute:
            ending = ("EXPIRED", "邀请期已结束，相关事务与日常生活继续；是否解决以实际业务记录为准。")
        if ending:
            _close(row, ending, minute, clock.time_revision)
    if expire_only or not VillageSimulationConfig.objects.filter(config_key="default", village_story_enabled=True).exists():
        return
    for template in VillageDailyEventTemplate.objects.filter(enabled=True).order_by("code"):
        # Admin validates the schema. Fail closed for invalid direct DB edits.
        try:
            validate_template(template)
        except ValidationError:
            continue
        for source_id, observed in current_facts(template.rule_type, template.parameters).items():
            previous = VillageDailyEvent.objects.filter(template=template, source_id=source_id).order_by("-opened_world_minute").first()
            if previous and (previous.status == "OPEN" or template.rule_type in ONE_SHOT_RULES
                             or minute < previous.opened_world_minute + template.cooldown_minutes):
                continue
            until = min(minute + template.invitation_minutes, observed["until"]) if observed["until"] is not None else minute + template.invitation_minutes
            if until <= minute:
                continue
            facts = deepcopy(observed["facts"])
            if template.rule_type == "CARAVAN_VISIT":
                from .models import VillageWorldEvent
                from django.db.models import Max
                facts["baseline_event_sequence"] = VillageWorldEvent.objects.aggregate(v=Max("event_sequence"))["v"] or 0
            if template.rule_type == "PUBLIC_MEAL":
                from .models import VillageCommunityMealDailyUsage
                usage = VillageCommunityMealDailyUsage.objects.filter(day_index=minute // 1440).first()
                facts["claimed_at_open"] = usage.total_meals if usage else 0
            row = VillageDailyEvent.objects.create(template=template, source_id=source_id,
                active_key=hashlib.sha256(f"{template.pk}:{source_id}".encode()).hexdigest(),
                title=template.title, summary=template.summary, participation_hint=template.participation_hint,
                npc_id=observed["npc_id"], location_code=observed["location_code"], affair_id=observed["affair_id"],
                facts=facts, rule_snapshot=dict(rule_type=template.rule_type, parameters=deepcopy(template.parameters),
                    resolved_summary=template.resolved_summary, npc_scope=template.npc_scope),
                opened_world_minute=minute, invite_until_world_minute=until)
            # A first-caravan recap may be discovered after its real import already
            # happened in this same transaction. Publish the result without a stale invitation.
            ending = _end_fact(row, {source_id: observed}, minute)
            if ending:
                _close(row, ending, minute, clock.time_revision)
            else:
                _publish(row, clock.time_revision)
