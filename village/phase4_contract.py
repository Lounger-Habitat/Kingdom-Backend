"""Frozen Phase 4 social-domain values and small validation helpers."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Iterable, Mapping

from .phase3_catalog import BASE_PRICES


class CommitmentType(str, Enum):
    DELIVERY = "DELIVERY"
    REPAYMENT = "REPAYMENT"
    DONATION = "DONATION"
    JOINT_WORK = "JOINT_WORK"
    MEETING = "MEETING"


class CommitmentStatus(str, Enum):
    OPEN = "OPEN"
    COMPLETED = "COMPLETED"
    BREACHED = "BREACHED"
    CANCELLED = "CANCELLED"


class RelationshipCategory(str, Enum):
    DELIVERY = "DELIVERY"
    EMERGENCY_SUPPLY = "EMERGENCY_SUPPLY"
    BREACH = "BREACH"
    JOINT_WORK = "JOINT_WORK"
    RESCUE_GIFT = "RESCUE_GIFT"


class Visibility(str, Enum):
    PUBLIC = "PUBLIC"
    PARTICIPANTS = "PARTICIPANTS"
    PRESENT = "PRESENT"
    PLAYER_PRIVATE = "PLAYER_PRIVATE"
    SYSTEM = "SYSTEM"


FORMAL_NPC_IDS = frozenset({"10011", "10012", "10013", "10014", "10015"})
RELATIONSHIP_FIELDS = ("affinity", "trust", "obligation", "conflict")
RELATIONSHIP_LIMITS = {
    "affinity": (-100, 100),
    "trust": (-100, 100),
    "obligation": (-100, 100),
    "conflict": (0, 100),
}

# The seed command owns persistence.  Keeping the values here prevents the
# reducer/tests from growing a second source of truth for the frozen setup.
NPC_RELATIONSHIP_SEEDS = {
    ("10011", "10012"): (45, 35, -10, 5),
    ("10012", "10011"): (55, 50, 25, 0),
    ("10011", "10014"): (65, 40, 0, 25),
    ("10014", "10011"): (70, 45, 20, 20),
    ("10011", "10015"): (-10, 15, 45, 40),
    ("10015", "10011"): (5, 35, -35, 20),
    ("10012", "10015"): (-20, 5, 0, 45),
    ("10015", "10012"): (-5, 10, 0, 30),
    ("10014", "10013"): (30, 55, 10, 15),
    ("10013", "10014"): (25, 60, 0, 10),
    ("10013", "10015"): (0, 30, 0, 35),
    ("10015", "10013"): (-5, 25, 0, 40),
}

RELATIONSHIP_RULES = {
    "DELIVERY": {"trust": 3},
    # Emergency delivery keeps the normal delivery trust gain and adds the
    # low-stock obligation/affinity gains in the same audited source change.
    "EMERGENCY_SUPPLY": {"trust": 3, "obligation": 4, "affinity": 3},
    "BREACH": {"trust": -5, "conflict": 3},
    "JOINT_WORK": {"trust": 3, "affinity": 2},
    "RESCUE_GIFT": {"obligation": 3, "affinity": 2},
}

FACT_CODES = frozenset({
    "BRIDGE_FOUNDATION_WARNING",
    "FOREST_OLD_ROUTE",
    "DEFERRED_BRIDGE_BUDGET",
    "BORROWED_RESERVE_SEEDS",
    "EXPENSIVE_CARAVAN_OPTION",
})


def is_formal_npc(npc_id: Any) -> bool:
    return str(npc_id) in FORMAL_NPC_IDS


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def commitment_target_errors(commitment_type: str, target: Any) -> list[dict[str, str]]:
    """Give the Agent field-specific errors without changing the target rules."""
    if not isinstance(target, dict):
        return [{"field": "target", "message": "必须是结构化对象"}]
    try:
        kind = CommitmentType(str(commitment_type).upper())
    except ValueError:
        return [{"field": "commitment_type", "message": "不支持的承诺类型"}]
    fields = {
        CommitmentType.DELIVERY: {"item_id", "amount", "gold_amount"},
        CommitmentType.REPAYMENT: {"gold_amount"},
        CommitmentType.JOINT_WORK: {"location_code", "work_code"},
        CommitmentType.MEETING: {"location_code"},
    }
    if kind is CommitmentType.DONATION:
        if target.get("asset_kind") not in ("GOLD", "ITEM"):
            return [{"field": "target.asset_kind", "message": "必须是GOLD或ITEM"}]
        required = {"asset_kind", "gold_amount"} if target["asset_kind"] == "GOLD" else {"asset_kind", "item_id", "amount"}
    else:
        required = fields[kind]
    errors = [{"field": f"target.{key}", "message": "缺少必填字段"} for key in sorted(required - set(target))]
    errors += [{"field": f"target.{key}", "message": "此承诺类型不接受该字段"} for key in sorted(set(target) - required)]
    for key in sorted(required & set(target)):
        value = target[key]
        if key in {"item_id", "amount", "gold_amount"} and not _positive_int(value):
            errors.append({"field": f"target.{key}", "message": "必须是正整数"})
        elif key == "item_id" and value not in BASE_PRICES:
            errors.append({"field": "target.item_id", "message": "不在承诺物品白名单；请按工具schema选择"})
        elif key in {"location_code", "work_code"} and (not isinstance(value, str) or not value.strip()):
            errors.append({"field": f"target.{key}", "message": "必须是非空字符串"})
    return errors


def validate_commitment_target(commitment_type: str, target: Any) -> Dict[str, Any] | None:
    """Return a normalized target or ``None`` for an invalid target."""
    if commitment_target_errors(commitment_type, target):
        return None
    return dict(target)


def relationship_snapshot(relationship: Any) -> Dict[str, int]:
    return {field: int(getattr(relationship, field)) for field in RELATIONSHIP_FIELDS}


def relationship_label(snapshot: Mapping[str, int], has_open_commitment: bool = False) -> str:
    if snapshot["conflict"] >= 40:
        return "TENSE"
    if snapshot["trust"] >= 50:
        return "TRUSTED"
    if snapshot["affinity"] >= 30:
        return "FAMILIAR"
    if snapshot["affinity"] <= -20 or snapshot["trust"] <= -20:
        return "DISTANT"
    return "NEUTRAL"


def normalize_delta(delta: Mapping[str, Any]) -> Dict[str, int]:
    return {
        field: int(delta.get(field, 0))
        for field in RELATIONSHIP_FIELDS
        if int(delta.get(field, 0)) != 0
    }
