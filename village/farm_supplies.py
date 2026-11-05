"""One persistent receipt book for E/F supplies; earned work determines consumption.

The existing world-clock lock serializes deposits and work starts/settlement.
Live player sessions and NPC commands reserve a work unit until completion,
cancellation or presence expiry. No second inventory debit happens at work time.
"""
from datetime import timedelta

from django.db.models import F
from django.utils import timezone

from .models import NpcCommandState, VillageAffair, VillagePlayerAffairLabor

SUPPLY_CODE = "FARM_EXPANSION_SUPPLY"
DEPOT_LOCATION = "FARM_EXPANSION_DEPOT"
DEPOT_POSITION = (-29.8, .01, -78.4)
WORK_CODES = tuple(f"FARM_EXPANSION_{site}_{suffix}"
                   for site in ("E", "F") for suffix in ("PREP", *(f"{i:02d}" for i in range(1, 10))))
WOOD_CONDITION, GOLD_CONDITION = "shared_wood", "shared_gold"


def requirements():
    return {"stages": [dict(id="supply", title="公共备料", conditions=[
        dict(id=WOOD_CONDITION, type="item", label="公共木材", item_ids=[1002], target=60),
        dict(id=GOLD_CONDITION, type="gold", label="公共资金", target=40),
    ])]}


def work_cost(code, condition_id):
    if code not in WORK_CODES:
        return {}
    if code.endswith("_PREP"):
        return {"entrance_labor": {"wood": 6}, "water_labor": {"wood": 6, "gold": 20}}.get(condition_id, {})
    return {"wood": 2} if condition_id == "leveling_labor" else {}


def ensure_supply(world_minute):
    """Called inside expansion setup's clock transaction; import old paid receipts once."""
    from .affairs import create_resource_affair, condition_progress, _legacy_totals
    pool = create_resource_affair(SUPPLY_CODE, "农田公共备料", requirements(), [DEPOT_LOCATION], world_minute)
    if not pool.progress.get("shared_depot_imported"):
        amounts = condition_progress(pool)
        imported = {WOOD_CONDITION: 0, GOLD_CONDITION: 0}
        for row in VillageAffair.objects.select_for_update().filter(pk__in=WORK_CODES):
            progress = condition_progress(row)
            imported[WOOD_CONDITION] += sum(max(0, progress.get(key, 0))
                                           for key in ("entrance_wood", "water_wood", "leveling_wood"))
            imported[GOLD_CONDITION] += max(0, progress.get("water_gold", 0))
        for key, value in imported.items():
            amounts[key] = amounts.get(key, 0) + value
        pool.progress = {**pool.progress, "shared_depot_imported": True, "legacy_supplies": imported,
                         "conditions": amounts, **_legacy_totals(pool.requirements, amounts)}
        pool.status = "COMPLETED" if amounts.get(WOOD_CONDITION, 0) >= 60 and amounts.get(GOLD_CONDITION, 0) >= 40 else "ACTIVE"
        if pool.status == "COMPLETED":
            pool.completed_world_minute = world_minute
        pool.version += 1
        pool.save(update_fields=["progress", "status", "completed_world_minute", "version"])
    return pool


def stock_state():
    """Read only. Completed work and live reservations share one authoritative balance."""
    from .affairs import condition_progress
    from .player_affair_labor import LEASE_SECONDS
    rows = {row.pk: row for row in VillageAffair.objects.filter(pk__in=(*WORK_CODES, SUPPLY_CODE))}
    pool = rows.get(SUPPLY_CODE)
    receipts = condition_progress(pool) if pool else {}
    submitted = {"wood": max(0, receipts.get(WOOD_CONDITION, 0)), "gold": max(0, receipts.get(GOLD_CONDITION, 0))}
    used = {"wood": 0, "gold": 0}
    progress = {code: condition_progress(row) for code, row in rows.items()}
    for code in WORK_CODES:
        for condition in ("entrance_labor", "water_labor", "leveling_labor"):
            if progress.get(code, {}).get(condition, 0) >= 1:
                for kind, quantity in work_cost(code, condition).items():
                    used[kind] += quantity
    reservations = {}

    def reserve(code, condition):
        if code not in rows or progress.get(code, {}).get(condition, 0) >= 1:
            return
        cost = work_cost(code, condition)
        if cost:
            # Several participants on the same single work unit share its reservation.
            reservations[(code, condition)] = cost

    for code, condition in VillagePlayerAffairLabor.objects.filter(affair_id__in=WORK_CODES, status="ACTIVE",
            last_seen_at__gte=timezone.now()-timedelta(seconds=LEASE_SECONDS)).values_list("affair_id", "condition_id"):
        reserve(code, condition)
    commands = NpcCommandState.objects.filter(kind="CONTRIBUTE_TO_AFFAIR", status="RUNNING",
        started_world_minute__isnull=False, npc__village_runtime__active_command_id=F("command_id"),
        npc__village_runtime__action_status="ACTING").values_list("sanitized_parameters", flat=True)
    for params in commands:
        if params.get("kind") == "labor":
            reserve(params.get("affair_id"), params.get("condition_id"))
    reserved = {kind: sum(cost.get(kind, 0) for cost in reservations.values()) for kind in submitted}
    stock = {kind: max(0, submitted[kind]-used[kind]) for kind in submitted}
    available = {kind: max(0, stock[kind]-reserved[kind]) for kind in submitted}
    return dict(pool=pool, submitted=submitted, used=used, stock=stock, reserved=reserved,
                available=available, reservations=reservations)


def work_ready(code, condition_id, state=None):
    cost = work_cost(code, condition_id)
    if not cost:
        return True, ""
    state = state if state is not None else stock_state()
    own = state["reservations"].get((code, condition_id), {})
    missing = []
    for kind, quantity in cost.items():
        short = quantity-state["available"][kind]-own.get(kind, 0)
        if short > 0:
            missing.append(f"{short}份木头" if kind == "wood" else f"{short}金币")
    return (False, "公共库存不足，还缺"+"、".join(missing)+"，请到农田公共备料点补交。") if missing else (True, "")


def touch_supply():
    VillageAffair.objects.filter(pk=SUPPLY_CODE).update(version=F("version")+1)


def supply_snapshot(state):
    """Small standalone scene receipt also carried by individual work receipts."""
    pool = state["pool"]
    if pool is None:
        return None
    conditions = []
    for kind, cid, target in (("wood", WOOD_CONDITION, 60), ("gold", GOLD_CONDITION, 40)):
        condition = dict(id=cid, condition_id=cid, type="item" if kind == "wood" else "gold",
            label="公共木材" if kind == "wood" else "公共资金", target=target,
            submitted=state["submitted"][kind], used_quantity=state["used"][kind],
            stock_quantity=state["stock"][kind], reserved_quantity=state["reserved"][kind],
            available_quantity=state["available"][kind], locations=[DEPOT_LOCATION])
        if kind == "wood":
            condition["item_ids"] = [1002]
        conditions.append(condition)
    return dict(affair_id=SUPPLY_CODE, title=pool.title, version=pool.version, status=pool.status,
                shared_stock=True, available=True, location_code=DEPOT_LOCATION,
                stages=[dict(id="supply", title="公共库存", status="ACTIVE", conditions=conditions)])
