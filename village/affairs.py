"""Shared, transactional reducer for public affairs with sequential stages.

Only progress.conditions is authoritative. Legacy totals are derived compatibility
fields for the existing bridge story and older clients.
"""
from copy import deepcopy
import re

from .lucy_catalog import MEALS
from .models import VillageAffair, VillageAffairContribution, VillageStoryFact, VillageStoryMeeting
from .phase3_catalog import REPAIR_WOOD_ITEM_ID

BRIDGE_AFFAIR_CODE = "BRIDGE_REPAIR"
RESOURCE_ACTIONS = {"DONATE_REPAIR_RESOURCES", "PROVIDE_REPAIR_MEAL", "REPAIR_LABOR", "TRANSPORT_REPAIR_MATERIALS"}
DEFAULT_REQUIREMENTS = {"materials": {str(REPAIR_WOOD_ITEM_ID): 100}, "meals": 20, "gold": 500, "labor": 20}


class AffairError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def legacy_definition(requirements, locations=None):
    """Convert historical projects without losing their stage ordering or IDs."""
    locations = list(locations or [])
    preparation = [dict(id=f"legacy_material_{item}", type="item", item_ids=[int(item)],
                        target=target, locations=locations)
                   for item, target in requirements.get("materials", {}).items() if target]
    if requirements.get("meals"):
        preparation.append(dict(id="legacy_meals", type="item", label="成品餐食", item_ids=list(MEALS),
            target=requirements["meals"], locations=locations,
            min_quality=1, requires_freshness=True))
    if requirements.get("gold"):
        preparation.append(dict(id="legacy_gold", type="gold", target=requirements["gold"], locations=locations))
    stages = []
    if preparation:
        stages.append(dict(id="legacy_preparation", title="筹备", conditions=preparation))
    if requirements.get("labor"):
        stages.append(dict(id="legacy_work", title="劳动", conditions=[dict(id="legacy_labor", type="labor",
            target=requirements["labor"], unit_minutes=60, locations=locations)]))
    return {"stages": stages}


def normalize_definition(requirements, locations=None, *, validate_references=True):
    """Validate and normalize admin definitions or the historical project shape."""
    from items.models import Item
    from .models import VillageLocationDefinition
    def invalid(message="事务完成条件不合法。"):
        raise AffairError("INVALID_AFFAIR_DEFINITION", message)
    if locations is None:
        locations = []
    if not isinstance(locations, list) or any(not isinstance(x, str) or not x for x in locations):
        invalid("事务默认地点不合法。")
    if not isinstance(requirements, dict):
        invalid()
    if "stages" not in requirements:
        if set(requirements) - {"materials", "meals", "gold", "labor"}:
            invalid()
        materials = requirements.get("materials", {})
        if (not isinstance(materials, dict) or any(not str(item).isdigit() or int(item) <= 0
                or type(amount) is not int or amount <= 0 for item, amount in materials.items())
                or any(type(requirements.get(key, 0)) is not int or requirements.get(key, 0) < 0 for key in ("meals", "gold", "labor"))):
            invalid("资源目标必须是有效整数。")
        if locations is not None and (not isinstance(locations, list) or any(not isinstance(x, str) for x in locations)):
            invalid("地点不合法。")
        requirements = legacy_definition(requirements, locations)
    if set(requirements) != {"stages"} or not isinstance(requirements["stages"], list) or not requirements["stages"]:
        invalid("事务至少需要一个阶段，每个阶段至少需要一个完成条件。")
    stage_ids, condition_ids, item_ids, location_codes = set(), set(), set(), set(locations)
    result = []
    def identifier(value, seen):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", value) or value in seen:
            invalid("阶段和条件的标识必须有效且不能重复。")
        seen.add(value)
        return value
    for index, stage in enumerate(requirements["stages"]):
        if not isinstance(stage, dict) or set(stage) - {"id", "title", "conditions", "location_code"}:
            invalid()
        stage_id = identifier(stage.get("id"), stage_ids)
        title = stage.get("title", "完成条件" if len(requirements["stages"]) == 1 else f"阶段 {index + 1}")
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 100:
            invalid("阶段名称不能为空或超过 100 个字符。")
        if not isinstance(stage.get("conditions"), list) or not stage["conditions"]:
            invalid("每个阶段至少需要一个完成条件。")
        stage_location = stage.get("location_code", "")
        if not isinstance(stage_location, str):
            invalid("阶段地点不合法。")
        places = [stage_location] if stage_location else list(locations)
        location_codes.update(places)
        conditions = []
        for source in stage["conditions"]:
            if not isinstance(source, dict):
                invalid()
            condition_id = identifier(source.get("id"), condition_ids)
            kind = source.get("type")
            common = {"id", "type", "label", "target", "locations", "location_code"}
            allowed = common | ({"item_ids", "min_quality", "requires_freshness", "min_remaining_minutes"} if kind == "item"
                                else {"unit_minutes"} if kind == "labor" else set())
            if kind not in {"item", "gold", "labor"} or set(source) - allowed:
                invalid("条件类型或字段不合法。")
            if type(source.get("target")) is not int or source["target"] <= 0:
                invalid("每个完成条件的目标必须是正整数。")
            label = source.get("label", "")
            if not isinstance(label, str) or len(label) > 100:
                invalid("条件名称不合法。")
            # An explicit worksite can override a stage's default. The historical
            # derived `locations` list remains output-only for old definitions.
            condition_location = source.get("location_code", "")
            if not isinstance(condition_location, str):
                invalid("条件地点不合法。")
            condition_places = [condition_location] if condition_location else places
            condition = dict(id=condition_id, type=kind, label=label.strip(), target=source["target"], locations=list(dict.fromkeys(condition_places)))
            if condition_location:
                condition["location_code"] = condition_location
            location_codes.update(condition_places)
            if kind == "item":
                ids = source.get("item_ids")
                quality, freshness, remaining = source.get("min_quality", 0), source.get("requires_freshness", False), source.get("min_remaining_minutes", 0)
                if (not isinstance(ids, list) or not ids or any(type(x) is not int or x <= 0 for x in ids)
                        or type(quality) is not int or not 0 <= quality <= 6 or type(freshness) is not bool
                        or type(remaining) is not int or remaining < 0):
                    invalid("物品、品质或保鲜要求不合法。")
                condition.update(item_ids=list(dict.fromkeys(ids)), min_quality=quality,
                    requires_freshness=freshness, min_remaining_minutes=remaining)
                item_ids.update(ids)
            elif kind == "labor":
                minutes = source.get("unit_minutes", 60)
                if type(minutes) is not int or minutes <= 0:
                    invalid("每份劳动时长必须是正整数。")
                condition["unit_minutes"] = minutes
            conditions.append(condition)
        result.append(dict(id=stage_id, title=title.strip(), location_code=stage_location, conditions=conditions))
    if validate_references and (
        set(Item.objects.filter(pk__in=item_ids, is_deleted=False).values_list("item_id", flat=True)) != item_ids
        or set(VillageLocationDefinition.objects.filter(location_code__in=location_codes, is_active=True).values_list("location_code", flat=True)) != location_codes
    ):
        invalid("所选物品或地点不存在或已停用。")
    return {"stages": result}


def definition(row):
    return normalize_definition(row.requirements, row.locations, validate_references=False)


def condition_progress(row):
    if "conditions" in row.progress:
        return dict(row.progress["conditions"])
    progress = row.progress
    return {**{f"legacy_material_{item}": amount for item, amount in progress.get("materials", {}).items()},
            "legacy_meals": progress.get("meals", 0), "legacy_gold": progress.get("gold", 0),
            "legacy_labor": progress.get("labor", 0)}


def _legacy_totals(requirements, progress):
    totals = {"materials": {}, "meals": 0, "gold": 0, "labor": 0}
    for stage in requirements["stages"]:
        for condition in stage["conditions"]:
            amount = progress.get(condition["id"], 0)
            kind = condition["type"]
            if kind in {"gold", "labor"}:
                totals[kind] += amount
            elif condition["id"] == "legacy_meals":
                totals["meals"] += amount
            elif len(condition["item_ids"]) == 1:
                item = str(condition["item_ids"][0])
                totals["materials"][item] = totals["materials"].get(item, 0) + amount
    return totals


def active_stage(row):
    if row.status == "COMPLETED":
        return None
    progress = condition_progress(row)
    return next((stage for stage in definition(row)["stages"]
                 if any(progress.get(c["id"], 0) < c["target"] for c in stage["conditions"])), None)


def create_resource_affair(code, title, requirements, locations=None, world_minute=0):
    """Create an immutable definition once; retries never reset contributions."""
    if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code):
        raise AffairError("INVALID_AFFAIR_DEFINITION", "事务标识不合法。")
    existing = VillageAffair.objects.select_for_update().filter(pk=code).first()
    if existing:
        return existing
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 100 or type(world_minute) is not int or world_minute < 0:
        raise AffairError("INVALID_AFFAIR_DEFINITION", "事务名称或时间不合法。")
    req = normalize_definition(requirements, locations)
    progress = {"conditions": {}, **_legacy_totals(req, {})}
    places = list(dict.fromkeys(locations or []))
    return VillageAffair.objects.create(code=code, title=title.strip(), requirements=req, progress=progress,
        locations=places, status="ACTIVE", opened_world_minute=world_minute)


def bridge_defaults(world_minute, fact=None):
    repaired = fact is not None and fact.bridge_condition == "REPAIRED"
    req = legacy_definition(DEFAULT_REQUIREMENTS)
    for stage in req["stages"]:
        stage["location_code"] = "OLD_BRIDGE" if stage["id"] == "legacy_work" else "REPAIR_YARD"
    req = normalize_definition(req, ["REPAIR_YARD", "OLD_BRIDGE"], validate_references=False)
    progress = {"legacy_material_1002": fact.repair_wood if fact else 0, "legacy_meals": fact.community_meals if fact else 0,
                "legacy_gold": 0, "legacy_labor": fact.repair_labor_hours if fact else 0}
    return dict(title="修复旧桥", kind="RESOURCE_PROJECT", status="COMPLETED" if repaired else "ACTIVE",
        requirements=req, progress={"conditions": progress, **_legacy_totals(req, progress)}, locations=["REPAIR_YARD", "OLD_BRIDGE"],
        opened_world_minute=world_minute, completed_world_minute=world_minute if repaired else None)


def ensure_bridge_affair(world_minute, arc=None, fact=None):
    row = VillageAffair.objects.select_for_update().filter(pk=BRIDGE_AFFAIR_CODE).first()
    if row is None:
        if fact is None:
            fact = VillageStoryFact.objects.filter(arc_id="BROKEN_TRADE_ROUTE_V1").first()
        row = VillageAffair.objects.create(code=BRIDGE_AFFAIR_CODE, **bridge_defaults(world_minute, fact))
    sync_bridge_fact(row, fact)
    return row


def get_affair(code, world_minute, *, arc=None, fact=None):
    row = VillageAffair.objects.select_for_update().filter(pk=code, kind="RESOURCE_PROJECT").first()
    if row is None:
        raise AffairError("AFFAIR_NOT_FOUND", "事务不存在。")
    return row


def resolve_condition(row, condition_id=None, kind=None, item_id=None):
    """Resolve only an open, unmet condition. Missing IDs are legacy shorthand."""
    if row.status == "COMPLETED":
        raise AffairError("AFFAIR_COMPLETED", "事务已经完成。")
    canonical_kind = {"material": "item", "meal": "item", "fund": "gold"}.get(kind, kind)
    stages = definition(row)["stages"]
    stage = active_stage(row)
    candidates = [c for s in stages for c in s["conditions"] if condition_id is None or c["id"] == condition_id]
    if condition_id is not None and not candidates:
        raise AffairError("AFFAIR_CONDITION_NOT_FOUND", "事务条件不存在。")
    if canonical_kind:
        candidates = [c for c in candidates if c["type"] == canonical_kind]
    if item_id is not None:
        candidates = [c for c in candidates if item_id in c.get("item_ids", [])]
    if kind == "meal" and condition_id is None:
        candidates = [c for c in candidates if c["id"] == "legacy_meals"]
    if not candidates:
        raise AffairError("INVALID_MATERIAL" if canonical_kind == "item" else "INVALID_PARAMS", "该条件不接受这种贡献。")
    opened = [c for c in candidates if stage and c in stage["conditions"]]
    if not opened:
        progress = condition_progress(row)
        if all(progress.get(c["id"], 0) >= c["target"] for c in candidates):
            raise AffairError("PROJECT_NEED_FILLED", "此条件已经完成。")
        raise AffairError("AFFAIR_PREPARATION_INCOMPLETE" if canonical_kind == "labor" else "AFFAIR_CONDITION_LOCKED", "请先完成前面的阶段。")
    progress = condition_progress(row)
    remaining = [c for c in opened if progress.get(c["id"], 0) < c["target"]]
    if not remaining:
        raise AffairError("PROJECT_NEED_FILLED", "此条件已经完成。")
    if len(remaining) != 1:
        raise AffairError("AFFAIR_CONDITION_REQUIRED", "有多个可贡献的条件，请指定条件标识。")
    return remaining[0]


def preparation_ready(row):
    if row.status == "COMPLETED":
        return True
    stage = active_stage(row)
    return bool(stage and any(c["type"] == "labor" for c in stage["conditions"]))


def labor_unavailable(row, condition_id=None):
    try:
        resolve_condition(row, condition_id, "labor")
    except AffairError as exc:
        return exc.code
    return None


def validate_contribution(row, kind, quantity, item_id=None, condition_id=None):
    if type(quantity) is not int or quantity <= 0:
        raise AffairError("INVALID_PARAMS", "贡献数量必须是正整数。")
    condition = resolve_condition(row, condition_id, kind, item_id)
    if condition["type"] == "labor" and quantity != 1:
        raise AffairError("INVALID_PARAMS", "每次劳动必须完成一份。")
    if quantity > max(0, condition["target"] - condition_progress(row).get(condition["id"], 0)):
        raise AffairError("PROJECT_NEED_FILLED", "贡献超过条件剩余需要。")
    return condition


def project_affair_event(row, projection=None):
    """Public facts at this version, without copying the authoritative progress."""
    projection = project_affair(row) if projection is None else projection
    needs = []
    for condition in projection["conditions"]:
        if not condition["available"]:
            continue
        items = condition.get("item_ids", [])
        needs.append(dict(condition_id=condition["condition_id"], label=condition["label"],
            kind=condition["type"], item_id=items[0] if len(items) == 1 else None,
            remaining=condition["remaining"], unit_minutes=condition.get("unit_minutes"),
            locations=list(condition["locations"])))
    return dict(affair_id=row.code, stage_id=projection["active_stage_id"],
        current_stage_title=projection["current_stage_title"], open_needs=needs,
        completion_effect=projection["completion_effect"], version=row.version)


def _event(row, world_minute, request_id, actor_npc=None, player_id=None, *, progress_only=False):
    from game_time.models import GlobalGameTimeState
    from .models import VillageWorldEvent
    from .phase4_visibility import enqueue_event_deliveries
    clock = GlobalGameTimeState.objects.get(pk=1)
    stage = active_stage(row)
    event_stage = stage or definition(row)["stages"][-1]
    summary = f"{row.title}已完成。" if stage is None else f"{row.title}进入“{stage['title']}”阶段。"
    from .farm_irrigation import IRRIGATION_AFFAIR_CODE, COMPLETION_EFFECT
    if stage is None and row.code == IRRIGATION_AFFAIR_CODE:
        summary += COMPLETION_EFFECT
    suffix = "COMPLETED" if stage is None else f"stage:{stage['id']}"
    if progress_only:
        suffix = f"progress:{row.version}"
        summary = f"{row.title}进度已更新。"
    projection = project_affair(row)
    facts = project_affair_event(row, projection)
    if row.code == BRIDGE_AFFAIR_CODE:
        if stage is None:
            from .models import VillageSimulationConfig
            capacity = VillageSimulationConfig.objects.filter(config_key="default").values_list("caravan_normal_capacity", flat=True).first() or 60
            summary = f"{row.title}已完成，旧桥恢复通行；后续商队使用正常商路，按当前配置每趟进口总量为{capacity}件。"
        else:
            remaining = "、".join(f"{need['remaining']}金币" if need['kind'] == "gold" else f"{need['label']}{need['remaining']}份"
                for need in facts["open_needs"])
            summary = f"{row.title}当前为“{facts['current_stage_title']}”阶段；尚需{remaining}。"
        contribution = row.contributions.filter(request_id=request_id).order_by("pk").last()
        if contribution:
            label = next((condition["label"] for entry in projection["stages"]
                for condition in entry["conditions"] if condition["condition_id"] == contribution.condition_id), contribution.kind)
            actor = actor_npc.name if actor_npc else "一名玩家"
            amount = f"{contribution.quantity}金币" if contribution.kind == "gold" else f"{label}{contribution.quantity}份"
            summary = f"{actor}贡献了{amount}。" + summary
    summary = summary[:256]
    event, created = VillageWorldEvent.objects.get_or_create(dedupe_key=f"affair:{row.code}:{suffix}", defaults=dict(
        event_type="VILLAGE_AFFAIR_PROGRESS" if progress_only else "VILLAGE_AFFAIR_COMPLETED" if stage is None else "VILLAGE_AFFAIR_READY",
        world_minute=world_minute, time_revision=clock.time_revision, request_id=request_id, actor_npc=actor_npc,
        player_ids=[str(player_id)] if player_id is not None else [], participant_npc_ids=[str(actor_npc.agent_id)] if actor_npc else [],
        location_code=(event_stage["conditions"][0]["locations"] or [""])[0],
        visibility="PUBLIC", player_summary=summary, hub_summary=summary,
        public_data={**facts, "status": row.status, "summary": summary,
            "bridge_condition": "REPAIRED" if row.code == BRIDGE_AFFAIR_CODE and stage is None else None}))
    if created:
        enqueue_event_deliveries(event)


def sync_bridge_fact(row, fact=None):
    if row.code != BRIDGE_AFFAIR_CODE:
        return
    fact = fact or VillageStoryFact.objects.select_for_update().filter(arc_id="BROKEN_TRADE_ROUTE_V1").first()
    if fact is None:
        return
    progress = condition_progress(row)
    fact.repair_wood = progress.get("legacy_material_1002", 0)
    fact.community_meals = progress.get("legacy_meals", 0)
    fact.repair_fund = progress.get("legacy_gold", 0)
    fact.repair_labor_hours = progress.get("legacy_labor", 0)
    fact.bridge_condition = ("REPAIRED" if row.status == "COMPLETED" else
        "REPAIRING" if preparation_ready(row) and fact.repair_labor_hours else "MATERIALS_READY" if preparation_ready(row) else "BROKEN")
    fact.save(update_fields=["repair_wood", "community_meals", "repair_fund", "repair_labor_hours", "bridge_condition", "updated_at"])


def contribute(row, kind, quantity, *, item_id=None, condition_id=None, world_minute, request_id, actor_npc=None, player_id=None):
    kind = "gold" if kind == "fund" else kind
    actor_type, actor_id = ("PLAYER", str(player_id)) if player_id is not None else ("NPC", str(actor_npc.agent_id))
    aliases = ["item", "material", "meal"] if kind in {"item", "material", "meal"} else [kind]
    previous = VillageAffairContribution.objects.filter(affair=row, request_id=request_id, kind__in=aliases, item_id=item_id or 0)
    if condition_id is not None:
        previous = previous.filter(condition_id=condition_id)
    previous = previous.first()
    if previous:
        if (previous.quantity, previous.actor_type, previous.actor_id) != (quantity, actor_type, actor_id):
            raise AffairError("REQUEST_CONFLICT", "同一请求不能改变贡献。")
        return project_affair(row)
    condition = validate_contribution(row, kind, quantity, item_id, condition_id)
    old_stage = active_stage(row)
    progress = condition_progress(row)
    progress[condition["id"]] = progress.get(condition["id"], 0) + quantity
    row.requirements = definition(row)
    row.progress = {"conditions": progress, **_legacy_totals(row.requirements, progress)}
    next_stage = active_stage(row)
    row.status = "ACTIVE" if next_stage else "COMPLETED"
    if next_stage is None:
        row.completed_world_minute = world_minute
    row.version += 1
    row.save(update_fields=["requirements", "progress", "status", "completed_world_minute", "version"])
    VillageAffairContribution.objects.create(affair=row, request_id=request_id, condition_id=condition["id"], actor_type=actor_type,
        actor_id=actor_id, kind=kind, item_id=item_id or 0, quantity=quantity, world_minute=world_minute)
    sync_bridge_fact(row)
    _event(row, world_minute, request_id, actor_npc, player_id,
           progress_only=(old_stage or {}).get("id") == (next_stage or {}).get("id"))
    return project_affair(row)


def complete_labor(row, *, condition_id=None, world_minute, request_id, actor_npc=None, player_id=None):
    return contribute(row, "labor", 1, condition_id=condition_id, world_minute=world_minute, request_id=request_id,
        actor_npc=actor_npc, player_id=player_id)


def project_affair(row):
    from items.models import Item
    from .farm_irrigation import IRRIGATION_AFFAIR_CODE, COMPLETION_EFFECT
    req, progress = definition(row), condition_progress(row)
    active = active_stage(row)
    ids = {item for s in req["stages"] for c in s["conditions"] for item in c.get("item_ids", [])}
    names = dict(Item.objects.filter(pk__in=ids).values_list("item_id", "item_name"))
    stages, after_active = [], False
    for stage in req["stages"]:
        status = "ACTIVE" if active and stage["id"] == active["id"] else "LOCKED" if after_active else "COMPLETED"
        conditions = []
        for raw in stage["conditions"]:
            condition = deepcopy(raw)
            submitted = progress.get(raw["id"], 0)
            item_names = [names.get(item, str(item)) for item in raw.get("item_ids", [])]
            condition.update(condition_id=raw["id"], stage_id=stage["id"], submitted=submitted,
                remaining=max(0, raw["target"] - submitted), available=status == "ACTIVE" and submitted < raw["target"],
                label=raw.get("label") or (" / ".join(item_names) if raw["type"] == "item" else "金币" if raw["type"] == "gold" else "劳动"))
            if raw["type"] == "item":
                condition.update(item_names=item_names, items=[dict(item_id=item, item_name=names.get(item, str(item))) for item in raw["item_ids"]])
            conditions.append(condition)
        stages.append(dict(id=stage["id"], title=stage["title"], status=status, conditions=conditions,
            location_code=stage.get("location_code", ""), locations=list(dict.fromkeys(p for c in conditions for p in c["locations"]))))
        if status == "ACTIVE":
            after_active = True
    current = next((s["conditions"] for s in stages if s["status"] == "ACTIVE"), [])
    all_conditions = [c for s in stages for c in s["conditions"]]
    def portion(kind, label, conditions):
        target = sum(c["target"] for c in conditions)
        submitted = sum(c["submitted"] for c in conditions)
        return dict(kind=kind, label=label, target=target, submitted=submitted, remaining=max(0, target-submitted))
    materials = [dict(portion("material", c["label"], [c]), item_id=c["item_ids"][0], item_name=c["label"], condition_id=c["id"])
                 for c in all_conditions if c["type"] == "item" and c["id"] != "legacy_meals" and len(c["item_ids"]) == 1]
    meal_conditions = [c for c in all_conditions if c["id"] == "legacy_meals"]
    meals = portion("meal", "成品餐食", meal_conditions)
    meals.update(item_ids=list(MEALS), min_quality=1, min_remaining_minutes=0, requires_freshness=True)
    labor_conditions = [c for c in all_conditions if c["type"] == "labor"]
    current_labor = next((c for c in current if c["type"] == "labor" and c["available"]), None)
    labor = portion("labor", "劳动", labor_conditions)
    labor.update(completed=labor["submitted"], duration_minutes=(current_labor or {}).get("unit_minutes", 60),
        unit_minutes=(current_labor or {}).get("unit_minutes", 60), locations=(current_labor or {}).get("locations", []))
    return dict(affair_id=row.code, code=row.code, kind=row.kind, title=row.title,
        completion_effect=COMPLETION_EFFECT if row.code == IRRIGATION_AFFAIR_CODE else "恢复旧桥通行和正常商路" if row.code == BRIDGE_AFFAIR_CODE else "完成此事务并记录完成事件",
        stage="COMPLETED" if row.status == "COMPLETED" else "ACTIVE", status="COMPLETED" if row.status == "COMPLETED" else "ACTIVE",
        stages=stages, conditions=current, active_stage_id=active["id"] if active else None,
        current_stage_title=active["title"] if active else "已完成", available=row.status != "COMPLETED",
        reason="事务已经完成。" if row.status == "COMPLETED" else "", requirements=deepcopy(req),
        progress={"conditions": progress, **_legacy_totals(req, progress)}, materials=materials, meals=meals,
        gold=portion("gold", "金币", [c for c in all_conditions if c["type"] == "gold"]), labor=labor,
        labor_available=current_labor is not None, locations=list(row.locations), contribution_locations=list(row.locations),
        default_location_code=(row.locations or [""])[0], default_locations=list(row.locations),
        location_code=(row.locations or [""])[0], meal_location_code=(row.locations or [""])[0], version=row.version, story_version=row.version,
        opened_world_minute=row.opened_world_minute, completed_world_minute=row.completed_world_minute,
        bridge_condition=("REPAIRED" if row.status == "COMPLETED" else "REPAIRING" if preparation_ready(row) and progress.get("legacy_labor", 0)
            else "MATERIALS_READY" if preparation_ready(row) else "BROKEN") if row.code == BRIDGE_AFFAIR_CODE else None)


def project_affairs(world_minute):
    """Read-only projection; inspecting never starts or settles an affair."""
    rows = list(VillageAffair.objects.all().order_by("opened_world_minute", "code"))
    return [project_affair(row) for row in rows if row.kind == "RESOURCE_PROJECT"]
