"""Authored development milestones, settled from real business receipts.

All mutations share the authority clock lock. Reads never install definitions,
grant trial stock, manufacture sales, or settle an unlock.
"""
from copy import deepcopy
import hashlib
import json

from django.db import transaction

from .career_development_models import (VillageCareerDevelopment, VillageCareerEvidence,
    VillageCareerProductionReservation)

DAY = 1440
REVIEW_PERIOD = 6 * DAY
TRIAL_LIMIT = 24
NEW_ITEM_IDS = frozenset({1047, 29402, 29403, 29404, 29405, 29406, 29407, 29408, 29409})
SEED_TO_CONTENT = {29411: 1047, 29412: 29402}
CONTENT_NAMES = {1047: "西红柿", 29402: "大豆", 29403: "猪肝", 29404: "鸡胗",
    29405: "兽骨", 29406: "鹿皮", 29407: "番茄猪肝汤", 29408: "黄豆炖肉", 29409: "葱香鸡胗"}

# These are content definitions, not a general-purpose task language.
TRACKS = {
    "BILL_1": dict(owner_npc_id="10011", owner_name="Bill", title="试种番茄", preparation_title="准备番茄试种", phase=1,
        location_code="BILL_FARM", items=[1047], wood=4, gold=20, labor=1,
        completion_effect="开放西红柿常规种植及其新品供货渠道"),
    "BILL_2": dict(owner_npc_id="10011", owner_name="Bill", title="试种大豆", preparation_title="准备大豆试种", phase=2,
        location_code="BILL_FARM", items=[29402], wood=4, gold=20, labor=1,
        completion_effect="开放大豆常规种植及其新品供货渠道"),
    "JACK_1": dict(owner_npc_id="10012", owner_name="Jack", title="扩建加工棚", preparation_title="准备扩建加工棚", phase=1,
        location_code="BUTCHER_YARD", items=[29403, 29405], wood=20, gold=80, labor=3,
        completion_effect="开放猪肝、兽骨的常规分割产出"),
    "JACK_2": dict(owner_npc_id="10012", owner_name="Jack", title="改进猎物分割", phase=2,
        location_code="BUTCHER_YARD", items=[29404, 29406],
        completion_effect="开放鸡胗、鹿皮的常规分割产出"),
    "LUCY_1": dict(owner_npc_id="10014", owner_name="Lucy", title="改造灶间并试卖新汤", preparation_title="改造饭店灶间", phase=1,
        location_code="RESTAURANT", items=[29407], wood=12, gold=60, labor=2,
        completion_effect="开放番茄猪肝汤正式菜单"),
    "LUCY_2": dict(owner_npc_id="10014", owner_name="Lucy", title="试卖黄豆炖肉与葱香鸡胗", phase=2,
        location_code="RESTAURANT", items=[29408, 29409],
        completion_effect="开放黄豆炖肉、葱香鸡胗正式菜单"),
    "NORA_1": dict(owner_npc_id="10015", owner_name="Nora", title="拓展新品销路", phase=1,
        location_code="TRADE_ROAD", items=[],
        completion_effect="记录首次新品对外成交，并延续有限采购轮换"),
}
ITEM_TRACK = {item: code for code, spec in TRACKS.items() for item in spec["items"]}


def enabled():
    from .models import VillageSimulationConfig
    return VillageSimulationConfig.objects.filter(config_key="default", career_development_enabled=True).exists()


def _clock_lock():
    from game_time.models import GlobalGameTimeState
    return GlobalGameTimeState.objects.select_for_update().filter(pk=1).first()


def _minute():
    from game_time.models import GlobalGameTimeState
    return GlobalGameTimeState.objects.filter(pk=1).values_list("world_minute", flat=True).first() or 0


def _spec(track):
    if track.code.startswith("MARK_"):
        return dict(owner_npc_id="10013", owner_name="Mark", title=track.title, phase=track.phase,
            location_code="RECORD_HALL", items=[], completion_effect="保存真实菜品品鉴，最多影响下一期订单一个候选条目")
    return TRACKS[track.code]


def _complete(code):
    return VillageCareerDevelopment.objects.filter(code=code, status="COMPLETED").exists()


def _prerequisite_reason(code):
    from .models import VillageFarmPlot
    if code == "BILL_1":
        from .farm_expansion import irrigation_ready
        if not irrigation_ready():
            return "CAREER_IRRIGATION_REQUIRED", "请先完成原农田水渠通水。"
        if not VillageFarmPlot.objects.filter(expansion_site_code__in=["E", "F"],
                reclamation_status="READY", lease_enabled=False).exists():
            return "CAREER_NEW_FIELD_REQUIRED", "请先开垦至少一块 E/F 区村庄田。"
    previous = {"BILL_2": "BILL_1", "JACK_2": "JACK_1", "LUCY_2": "LUCY_1"}.get(code)
    if previous and not _complete(previous):
        return "CAREER_PREVIOUS_PHASE_REQUIRED", "请先完成前一项事务。"
    needed = {"LUCY_1": (1047, 29403), "LUCY_2": (29402, 29404)}.get(code, ())
    if any(content_access(item)["status"] not in {"TRIAL", "COMPLETED"} for item in needed):
        return "CAREER_INGREDIENT_SUPPLY_REQUIRED", "请先开放对应新原料的试生产或常规供应。"
    if code == "NORA_1":
        from .caravan import current_route
        if current_route() != "NORMAL":
            return "CAREER_NORMAL_ROUTE_REQUIRED", "请先修复旧桥并开放正常商路。"
    return "", ""


def _trial_used(item_id):
    # Evidence is immutable and globally unique per successful settlement.
    return sum(int(items.get(str(item_id), 0)) for items in VillageCareerEvidence.objects.filter(
        kind="PRODUCTION").values_list("items", flat=True))


def _trial_reserved(item_id):
    return sum(VillageCareerProductionReservation.objects.filter(item_id=item_id,
        status="RESERVED").values_list("quantity", flat=True))


def content_access(item_id):
    """Production permission only; old stock and old recipes remain usable."""
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        item_id = 0
    item_id = SEED_TO_CONTENT.get(item_id, item_id)
    if item_id not in ITEM_TRACK:
        return dict(can_produce=True, unlocked=True, status="COMPLETED", blocked_reason="",
            unavailable_reason="", trial_remaining=TRIAL_LIMIT)
    track = VillageCareerDevelopment.objects.filter(pk=ITEM_TRACK[item_id]).first()
    if track and track.status == "COMPLETED":
        return dict(can_produce=True, unlocked=True, status="COMPLETED", blocked_reason="",
            unavailable_reason="", trial_remaining=TRIAL_LIMIT)
    feature = enabled()
    trial = bool(track and track.status == "TRIAL" and feature)
    remaining = max(0, TRIAL_LIMIT - _trial_used(item_id) - _trial_reserved(item_id)) if trial else 0
    return dict(can_produce=trial and remaining > 0, unlocked=False,
        status=track.status if track else "LOCKED",
        blocked_reason="" if trial and remaining else "CAREER_TRIAL_LIMIT" if trial else
            "CAREER_CONTENT_LOCKED" if feature else "CAREER_DEVELOPMENT_DISABLED",
        unavailable_reason="" if trial and remaining else "本次试生产额度已用完，请完成真实销售推进正式开放。" if trial else
            "请先完成对应事务的准备。" if feature else "新事务暂未开放。", trial_remaining=remaining)


@transaction.atomic
def reserve_trial_production(item_id, quantity, request_id, world_minute):
    """Reserve maximum output before paying inputs; caller rolls back on failure."""
    _clock_lock()
    item_id, quantity, request_id = int(item_id), int(quantity), str(request_id)
    if item_id not in ITEM_TRACK:
        return True
    if quantity <= 0 or len(request_id) > 128:
        return False
    old = VillageCareerProductionReservation.objects.select_for_update().filter(
        request_id=request_id, item_id=item_id).first()
    if old:
        return old.quantity == quantity and old.status in {"RESERVED", "CONSUMED"}
    access = content_access(item_id)
    if access["unlocked"]:
        return True
    if not access["can_produce"] or quantity > access["trial_remaining"]:
        return False
    VillageCareerProductionReservation.objects.create(request_id=request_id, item_id=item_id,
        quantity=quantity, world_minute=world_minute)
    return True


@transaction.atomic
def release_trial_production(request_id, item_id=None):
    """Only unproduced reservations return to the finite trial allowance."""
    _clock_lock()
    rows = VillageCareerProductionReservation.objects.filter(request_id=str(request_id), status="RESERVED")
    if item_id is not None:
        rows = rows.filter(item_id=int(item_id))
    return rows.update(status="RELEASED", settled_world_minute=_minute())


def _preparation(spec):
    return {"stages": [dict(id="materials", title="筹备材料与资金", conditions=[
        dict(id="wood", type="item", label="建设木材", item_ids=[1002], target=spec["wood"]),
        dict(id="gold", type="gold", label="建设资金", target=spec["gold"])]),
        dict(id="work", title="现场准备", conditions=[dict(id="labor", type="labor",
            label="现场劳动", target=spec["labor"], unit_minutes=30)])]}


def _event(track, minute, kind):
    from .models import VillageWorldEvent
    from .phase4_visibility import enqueue_event_deliveries
    titles = {"CAREER_AVAILABLE": "现可参与准备", "CAREER_TRIAL_OPENED": "已开放有限试生产",
        "CAREER_COMPLETED": "已完成，常规生产选项已开放", "CAREER_REVIEW_COMPLETED": "已完成真实菜品品鉴"}
    clock = _clock_lock()
    summary = f"{track.title}{titles[kind]}。"
    if kind == "CAREER_TRIAL_OPENED" and track.code.startswith("MARK_"):
        summary = f"{track.title}现可提交真实样品并开展独立品鉴。"
    elif kind == "CAREER_TRIAL_OPENED" and track.code == "NORA_1":
        summary = f"{track.title}现可对接真实供给与有限商队采购。"
    elif kind == "CAREER_COMPLETED" and track.code == "NORA_1":
        summary = f"{track.title}已完成，首次新品对外成交已记入回顾。"
    data = dict(track_code=track.code, owner_npc_id=track.owner_npc_id, title=track.title,
        status=track.status, phase=track.phase, item_ids=list(_spec(track)["items"]),
        completion_effect=_spec(track)["completion_effect"], message=summary)
    event, created = VillageWorldEvent.objects.get_or_create(dedupe_key=f"career:{track.code}:{kind}",
        defaults=dict(event_type=kind, world_minute=minute, time_revision=clock.time_revision if clock else 0,
            location_code=track.location_code, visibility="PUBLIC", player_summary=summary,
            hub_summary=summary, public_data=data))
    if created:
        enqueue_event_deliveries(event)


def _install(minute, codes):
    from .affairs import create_resource_affair
    from .models import VillageLocationDefinition
    created_count = 0
    for code, spec in TRACKS.items():
        if code not in codes:
            continue
        if not VillageLocationDefinition.objects.filter(location_code=spec["location_code"], is_active=True).exists():
            continue
        affair = create_resource_affair("CAREER_" + code + "_PREP", spec["preparation_title"],
            _preparation(spec), [spec["location_code"]], minute) if spec.get("wood") else None
        _, created = VillageCareerDevelopment.objects.get_or_create(code=code, defaults=dict(
            **{key: spec[key] for key in ("owner_npc_id", "owner_name", "title", "phase", "location_code")},
            preparation_affair=affair))
        created_count += int(created)
    return created_count


def _install_review(minute, tracks):
    """At most one unfinished review; a new period never discards old work."""
    from .models import VillageLocationDefinition
    period = minute // REVIEW_PERIOD
    code = f"MARK_{period:06d}"
    if any(track.code == code or (track.code.startswith("MARK_") and track.status != "COMPLETED") for track in tracks):
        return False
    if not VillageLocationDefinition.objects.filter(location_code="RECORD_HALL", is_active=True).exists():
        return False
    display_phase = VillageCareerDevelopment.objects.filter(code__startswith="MARK_").count() + 1
    _, created = VillageCareerDevelopment.objects.get_or_create(code=code, defaults=dict(
        owner_npc_id="10013", owner_name="Mark", title=f"第{display_phase}期菜品品鉴",
        phase=display_phase, location_code="RECORD_HALL"))
    return created


@transaction.atomic
def refresh_career_titles():
    """Refresh installed labels only; preserve Mark's existing display ordinals."""
    _clock_lock()
    from django.db.models import Q
    from .models import VillageAffair
    tracks = list(VillageCareerDevelopment.objects.select_for_update().filter(
        Q(code__in=TRACKS) | Q(code__startswith="MARK_")))
    changed, preparation_names, mark_count = [], {}, 0
    for track in tracks:
        if track.code.startswith("MARK_"):
            # Epoch-based internal codes never determine a player-visible number.
            title = f"第{track.phase}期菜品品鉴"
        else:
            spec = TRACKS[track.code]
            title = spec["title"]
            if track.preparation_affair_id and spec.get("preparation_title"):
                preparation_names[track.preparation_affair_id] = spec["preparation_title"]
        if track.title != title:
            track.title = title
            changed.append(track)
            mark_count += int(track.code.startswith("MARK_"))
    if changed:
        VillageCareerDevelopment.objects.bulk_update(changed, ["title"])
    affairs = list(VillageAffair.objects.select_for_update().filter(pk__in=preparation_names).only("code", "title"))
    renamed_affairs = []
    for affair in affairs:
        if affair.title != preparation_names[affair.pk]:
            affair.title = preparation_names[affair.pk]
            renamed_affairs.append(affair)
    if renamed_affairs:
        VillageAffair.objects.bulk_update(renamed_affairs, ["title"])
    return dict(track_titles_updated=len(changed), mark_titles_updated=mark_count,
        preparation_titles_updated=len(renamed_affairs))


def _needs_sync(tracks):
    """Cheap state checks also recover earned work after a feature pause."""
    from .models import VillageFarmPlot
    states = {track.code: track.status for track in tracks}
    for track in tracks:
        if track.status == "COMPLETED":
            continue
        if track.status == "TRIAL" and _is_complete(track):
            return True
        if track.status == "PREPARING" and track.preparation_affair.status == "COMPLETED":
            return True
        if track.status != "LOCKED":
            continue
        code = track.code
        if code == "JACK_1" or code.startswith("MARK_"):
            return True
        if code == "BILL_1":
            # Usually no new field exists yet, so avoid loading irrigation's
            # full condition tree on every authority tick.
            if VillageFarmPlot.objects.filter(expansion_site_code__in=["E", "F"],
                    reclamation_status="READY", lease_enabled=False).exists() and not _prerequisite_reason(code)[0]:
                return True
        elif code == "NORA_1":
            if not _prerequisite_reason(code)[0]:
                return True
        else:
            previous = {"BILL_2": "BILL_1", "JACK_2": "JACK_1", "LUCY_2": "LUCY_1"}.get(code)
            if previous and states.get(previous) != "COMPLETED":
                continue
            supply = {"LUCY_1": ("BILL_1", "JACK_1"), "LUCY_2": ("BILL_2", "JACK_2")}.get(code, ())
            if all(states.get(parent) in {"TRIAL", "COMPLETED"} for parent in supply):
                return True
    return False


def _requirements(track):
    spec, progress = _spec(track), track.progress or {}
    result = []
    def add(code, label, current, target):
        result.append(dict(code=code, label=label, current=current, target=target, completed=current >= target))
    if track.code.startswith("MARK_"):
        add("reviews", "真实评鉴不同菜品", len(progress.get("reviewed_items", [])), 2)
    elif track.code == "NORA_1":
        add("external_sales", "新品实际对外成交", sum(progress.get("external_sales", {}).values()), 1)
    else:
        for item in spec["items"]:
            add(f"production_{item}", "真实生产" + CONTENT_NAMES[item], int(progress.get("production", {}).get(str(item), 0)), 1)
        add("sales", "新品实际销售", sum(progress.get("sales", {}).values()), 3 if track.code.startswith("LUCY_") else 1)
    return result


def _is_complete(track):
    return all(row["completed"] for row in _requirements(track))


@transaction.atomic
def sync_career_development(affair, world_minute):
    """Apply only earned stages; existing completion survives a feature pause."""
    if affair is not None and affair.code not in {"BRIDGE_REPAIR", "FARM_IRRIGATION"} and not affair.code.startswith(
            ("CAREER_", "FARM_EXPANSION_")):
        return
    _clock_lock()
    if not enabled():
        return
    from .farm_expansion import _complete as affair_complete
    tracks = list(VillageCareerDevelopment.objects.select_for_update().select_related("preparation_affair").order_by("code"))
    for track in tracks:
        if track.status == "COMPLETED":
            continue
        old = track.status
        if old == "LOCKED":
            blocked, _ = _prerequisite_reason(track.code)
            if blocked:
                continue
            track.opened_world_minute = world_minute
            track.status = "PREPARING" if track.preparation_affair_id else "TRIAL"
            track.version += 1
            track.save(update_fields=["opened_world_minute", "status", "version"])
            _event(track, world_minute, "CAREER_AVAILABLE")
        if track.status == "PREPARING" and affair_complete(track.preparation_affair):
            track.status = "TRIAL"
        if track.status == "TRIAL":
            if track.trial_world_minute is None:
                track.trial_world_minute = world_minute
                track.version += 1
                track.save(update_fields=["status", "trial_world_minute", "version"])
                _event(track, world_minute, "CAREER_TRIAL_OPENED")
            if _is_complete(track):
                track.status = "COMPLETED"
                track.completed_world_minute = world_minute
                track.version += 1
                track.save(update_fields=["status", "completed_world_minute", "version"])
                _event(track, world_minute, "CAREER_REVIEW_COMPLETED" if track.code.startswith("MARK_") else "CAREER_COMPLETED")


@transaction.atomic
def ensure_career_development(world_minute, *, bootstrap=False):
    """Light authority maintenance; callers request full projections explicitly."""
    _clock_lock()
    result = dict(enabled=enabled(), installed=False, review_created=False, synchronized=False)
    if not result["enabled"]:
        return result
    from django.db.models import Q
    current_review = f"MARK_{world_minute // REVIEW_PERIOD:06d}"
    # Historical completed reviews are irrelevant to authority maintenance.
    tracks = list(VillageCareerDevelopment.objects.filter(Q(code__in=TRACKS) | Q(code=current_review)
        | (Q(code__startswith="MARK_") & ~Q(status="COMPLETED"))).select_related("preparation_affair").only(
            "code", "status", "progress", "title", "phase", "preparation_affair_id", "preparation_affair__status"))
    missing = set(TRACKS) - {track.code for track in tracks}
    if missing:
        result["installed"] = bool(_install(world_minute, missing))
    result["review_created"] = _install_review(world_minute, tracks)
    if bootstrap:
        refresh_career_titles()
    if bootstrap or result["installed"] or result["review_created"] or _needs_sync(tracks):
        sync_career_development(None, world_minute)
        result["synchronized"] = True
    return result


@transaction.atomic
def record_business_evidence(kind, request_id, actor_type, actor_id, items, world_minute, metadata=None):
    """Trusted settlement hook. It never charges money or creates inventory."""
    _clock_lock()
    if kind not in {"PRODUCTION", "SALE", "REVIEW"}:
        raise ValueError("Unsupported career evidence kind")
    normalized = {str(int(key)): int(value) for key, value in items.items() if int(value) > 0}
    metadata = deepcopy(metadata or {})
    if not normalized:
        return False
    # All real production can prove local provenance. Only new-item sales feed
    # career milestones; old reviews are still useful to Mark.
    if kind == "SALE" and not (set(map(int, normalized)) & NEW_ITEM_IDS):
        return False
    if not enabled() and not VillageCareerDevelopment.objects.exists():
        return False
    payload = dict(actor_type=str(actor_type), actor_id=str(actor_id), items=normalized, metadata=metadata)
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    evidence, created = VillageCareerEvidence.objects.get_or_create(kind=kind, request_id=str(request_id),
        actor_type=str(actor_type), actor_id=str(actor_id), defaults=dict(
            items=normalized, metadata=metadata, payload_hash=digest, world_minute=world_minute))
    if not created:
        if evidence.payload_hash != digest:
            raise ValueError("Career evidence request conflict")
        return False
    if kind == "PRODUCTION":
        if not (set(map(int, normalized)) & NEW_ITEM_IDS):
            return True  # Keep provenance; old products never reserve trial capacity.
        default_reservation_id = f"PLAYER:{actor_id}:{request_id}" if str(actor_type) == "PLAYER" else str(request_id)
        reservation_id = str(metadata.get("reservation_request_id", default_reservation_id))
        VillageCareerProductionReservation.objects.filter(request_id=reservation_id,
            item_id__in=list(map(int, normalized)), status="RESERVED").update(
                status="CONSUMED", settled_world_minute=world_minute)
    # A feature pause stops new preparation and formal unlocks, while already
    # started work and published sales retain their truthful progress.
    sync_career_development(None, world_minute)
    for track in VillageCareerDevelopment.objects.select_for_update().filter(status="TRIAL"):
        if track.trial_world_minute is None or world_minute < track.trial_world_minute:
            continue
        progress = deepcopy(track.progress or {})
        changed = False
        if track.code.startswith("MARK_"):
            if kind == "REVIEW":
                reviewed = set(progress.get("reviewed_items", []))
                scores = dict(progress.get("review_scores", {}))
                for item in normalized:
                    reviewed.add(int(item))
                    score = metadata.get("overall_score", 0)
                    if isinstance(score, (int, float)):
                        scores[item] = max(scores.get(item, 0), score)
                progress.update(reviewed_items=sorted(reviewed), review_scores=scores)
                changed = True
        elif track.code == "NORA_1":
            if kind == "SALE" and metadata.get("external") is True and metadata.get("provenance_valid") is not False:
                counts = progress.setdefault("external_sales", {})
                for item, amount in normalized.items():
                    if int(item) in NEW_ITEM_IDS:
                        counts[item] = counts.get(item, 0) + amount
                        changed = True
        elif kind in {"PRODUCTION", "SALE"}:
            paid = not (kind == "SALE" and (metadata.get("income") == 0 or metadata.get("unit_price") == 0
                or metadata.get("provenance_valid") is False))
            if paid:
                key = "production" if kind == "PRODUCTION" else "sales"
                counts = progress.setdefault(key, {})
                for item in _spec(track)["items"]:
                    amount = normalized.get(str(item), 0)
                    if amount:
                        counts[str(item)] = counts.get(str(item), 0) + amount
                        changed = True
        if changed:
            track.progress = progress
            track.version += 1
            track.save(update_fields=["progress", "version"])
    sync_career_development(None, world_minute)
    return True


def affair_blocked_reason(affair):
    if not affair.code.startswith("CAREER_") or affair.status == "COMPLETED":
        return "", ""
    if not enabled():
        return "CAREER_DEVELOPMENT_DISABLED", "新事务暂未开放。"
    track = VillageCareerDevelopment.objects.filter(preparation_affair=affair).first()
    if track is None:
        return "CAREER_DEFINITION_UNAVAILABLE", "对应事务尚未同步。"
    if track.status == "LOCKED":
        return _prerequisite_reason(track.code)
    return "", ""


def affair_metadata(affair):
    if not affair.code.startswith("CAREER_"):
        return {}
    track = VillageCareerDevelopment.objects.filter(preparation_affair=affair).first()
    if track is None:
        return {}
    return dict(career_track_code=track.code, owner_npc_id=track.owner_npc_id,
        completion_effect="完成准备后开放有限试生产，真实生产销售后正式解锁")


def _hint(track, blocked_message):
    if track.status == "COMPLETED":
        return "本项事务已完成，可按现有条件继续生产、经营和生活。"
    if blocked_message:
        return blocked_message
    if track.status == "PREPARING":
        return "到事务指定地点贡献本人有权使用的材料、金币或现场劳动。"
    if track.code.startswith("MARK_"):
        return "接收两种不同真实菜品，按现有品鉴入口独立评分；低分同样是有效记录。"
    if track.code == "NORA_1":
        return "对接新产品的真实供给与商队采购，完成第一笔新品对外成交。"
    if track.code.startswith("BILL_"):
        return "实际取得少量种子，使用可经营空田试种、收获，再完成一次合格销售。"
    if track.code.startswith("JACK_"):
        return "取得合法真实猎物并试处理，产出本期肉类和副产品，再完成一次合格销售。"
    return "按本期配方采购原料、试制菜品，并完成至少三份实际付费销售。"


def project_career_development(world_minute=None):
    from .affairs import project_affair
    from items.models import Item
    minute = _minute() if world_minute is None else world_minute
    feature = enabled()
    names = dict(Item.objects.filter(pk__in=NEW_ITEM_IDS).values_list("item_id", "item_name"))
    result = dict(enabled=feature, trial_quantity_limit=TRIAL_LIMIT, tracks=[],
        recommended_item_ids=recommended_item_ids(minute))
    for track in VillageCareerDevelopment.objects.select_related("preparation_affair").all():
        blocked, message = ("CAREER_DEVELOPMENT_DISABLED", "新事务暂未开放。") if not feature and track.status != "COMPLETED" else (
            _prerequisite_reason(track.code) if track.status == "LOCKED" else ("", ""))
        unlocks = [dict(item_id=item, item_name=names.get(item, CONTENT_NAMES.get(item, str(item))), **content_access(item))
            for item in _spec(track)["items"]]
        result["tracks"].append(dict(code=track.code, owner_npc_id=track.owner_npc_id,
            owner_name=track.owner_name, title=track.title, phase=track.phase, status=track.status,
            blocked_reason=blocked, unavailable_reason=message, location_code=track.location_code,
            affair_id=track.preparation_affair_id or "",
            affair=project_affair(track.preparation_affair) if track.preparation_affair_id else None,
            unlocks=unlocks, requirements=_requirements(track), completion_effect=_spec(track)["completion_effect"],
            participation_hint=_hint(track, message), version=track.version,
            opened_world_minute=track.opened_world_minute, trial_world_minute=track.trial_world_minute,
            completed_world_minute=track.completed_world_minute))
    return result


def procurement_candidates(world_minute):
    """Candidates only; the buyer still checks actual supply, budget and deadline."""
    result = []
    for item in sorted(NEW_ITEM_IDS):
        access = content_access(item)
        # An exhausted trial still needs a buyer for its already produced stock.
        if access["unlocked"] or (enabled() and access["status"] == "TRIAL"):
            result.append(dict(item_id=item, track_code=ITEM_TRACK[item], trial=not access["unlocked"],
                max_quantity=4 if not access["unlocked"] else 12))
    return result


def has_production_evidence(source_request_id, item_id):
    """Match immutable batch provenance to an actual successful production."""
    if not source_request_id:
        return False
    source, key = str(source_request_id), str(int(item_id))
    production = VillageCareerEvidence.objects.filter(kind="PRODUCTION", items__has_key=key)
    # The common path uses the evidence uniqueness index; delayed work may
    # retain a different original source UUID in its frozen metadata.
    if production.filter(request_id=source).exists() or production.filter(metadata__source_request_id=source).exists():
        return True
    rows = production.filter(metadata__source_request_ids__isnull=False).values_list("request_id", "items", "metadata")
    for rid, items, metadata in rows:
        sources = metadata.get("source_request_ids", [])
        if not isinstance(sources, list):
            sources = []
        if int(items.get(key, 0)) > 0 and (source == rid or source == str(metadata.get("source_request_id", ""))
                or source in [str(value) for value in sources]):
            return True
    return False


def recommendation_context(world_minute):
    """One completed review influences one future order that actually uses it."""
    last = VillageCareerDevelopment.objects.filter(code__startswith="MARK_", status="COMPLETED",
        completed_world_minute__lte=world_minute,
        completed_world_minute__gte=max(0, world_minute - REVIEW_PERIOD)).order_by("-completed_world_minute", "-code").first()
    if last is None:
        return dict(track_code="", item_ids=[])
    from .models import VillageCaravanOrder
    if VillageCaravanOrder.objects.filter(procurement_snapshot__review_track_code=last.code).exists():
        return dict(track_code="", item_ids=[])
    scores = (last.progress or {}).get("review_scores", {})
    # The buyer decides its own quality and price conditions. This is ordering only.
    return dict(track_code=last.code,
        item_ids=[int(item) for item, _ in sorted(scores.items(), key=lambda row: (-row[1], int(row[0])))])


def recommended_item_ids(world_minute):
    return recommendation_context(world_minute)["item_ids"]
