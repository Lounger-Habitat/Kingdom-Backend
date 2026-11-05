"""Private growth projection and settlement. Only trusted successful businesses award XP.

The caller's asset transaction encloses this service transaction. Locking the
account serializes all growth sources for a player, including first-time setup.
"""
from copy import deepcopy

from django.db import transaction

from account.models import GameAccount
from inventory.models import PlayerCollectionPoint
from .models import (ItemGameplayDefinition, PlayerGrowthState, PlayerMilestoneProgress,
                     PlayerSkillProgress, SkillRewardReceipt)
from .progression_rules import (LEGACY_FOOD_LEVELS, LEVEL_TOTALS, MAX_XP, REWARD_RULES, RULES_VERSION, SKILLS, SKILL_CODES, legacy_entitlements,
    level_for_xp, skill_data)


def _legacy_points(player):
    return [dict(point_id=row.collection_point_id, collect_type=row.collection_point.collect_type,
                 level=row.current_level, overrides_preserved=True)
            for row in PlayerCollectionPoint.objects.filter(player=player)
            .select_related("collection_point").order_by("collection_point_id")]


def _legacy_floor(points):
    level = max((LEGACY_FOOD_LEVELS.get(p["level"], 1) for p in points
                 if p["collect_type"] == "采集食材"), default=1)
    return LEVEL_TOTALS[level - 1]


@transaction.atomic
def ensure_growth(player):
    GameAccount.objects.select_for_update().only("pk").get(pk=player.pk)
    state, _ = PlayerGrowthState.objects.select_for_update().get_or_create(player=player)
    if state.legacy_migration_version >= 1:
        return state
    points = _legacy_points(player)
    for code, _ in SKILLS:
        progress, _ = PlayerSkillProgress.objects.get_or_create(player=player, skill_code=code)
        if code == "gathering" and progress.total_xp < _legacy_floor(points):
            progress.total_xp = _legacy_floor(points)
            progress.save(update_fields=["total_xp"])
    state.legacy_entitlements = legacy_entitlements(points)
    state.legacy_migration_version = 1
    state.revision += 1
    state.save(update_fields=["legacy_entitlements", "legacy_migration_version", "revision"])
    return state


def _milestone_data(row):
    return dict(code=row.code, title=row.title, source_type=row.source_type, source_id=row.source_id,
                completed_at=row.completed_at.isoformat(), rules_version=row.rules_version)


def growth_snapshot(player, *, read_only=False, compact=False, state=None):
    if state is None:
        state = PlayerGrowthState.objects.filter(player=player).first() if read_only else ensure_growth(player)
    points = _legacy_points(player) if state is None or state.legacy_migration_version < 1 else None
    totals = dict(PlayerSkillProgress.objects.filter(player=player).values_list("skill_code", "total_xp"))
    if points is not None:
        totals["gathering"] = max(totals.get("gathering", 0), _legacy_floor(points))
    result = dict(schema_version=1, revision=state.revision if state else 0, rules_version=RULES_VERSION,
        skills=[skill_data(code, name, totals.get(code, 0)) for code, name in SKILLS],
        milestones=[_milestone_data(row) for row in PlayerMilestoneProgress.objects.filter(player=player).order_by("pk")],
        legacy_entitlements=deepcopy(state.legacy_entitlements) if points is None else legacy_entitlements(points))
    if not compact:
        result["equipment_definitions"] = [dict(item_id=d.item_id, item_name=d.item.item_name, capability_code=d.capability_code,
            tier=d.tier, required_skill=d.required_skill, required_level=d.required_level, effects=deepcopy(d.effects),
            maximum_uses=d.item.max_use_count, rules_version=d.rules_version,
            runtime_enforced=d.capability_code in {"targeted_gathering", "farming", "forestry", "cooking"})
            for d in ItemGameplayDefinition.objects.select_related("item").filter(item__is_deleted=False).order_by("item_id")]
    return result


@transaction.atomic
def award_skill(player, *, source_type, source_id, skill_code, xp, milestone=None, facts=None):
    """Internal primitive for trusted business code, with no public XP endpoint.

    Source identity must be the durable business fact (not a new delivery ID on
    each retry). Caller asset writes and this award share the outer transaction.
    """
    source_id = str(source_id)
    if (not isinstance(source_type, str) or not 0 < len(source_type) <= 40
            or not source_id or len(source_id) > 128 or skill_code not in SKILL_CODES
            or type(xp) is not int or xp < 0):
        raise ValueError("Invalid growth source")
    if milestone is not None and (not isinstance(milestone, dict)
            or not isinstance(milestone.get("code"), str) or not 0 < len(milestone["code"]) <= 50
            or not isinstance(milestone.get("title"), str) or not 0 < len(milestone["title"]) <= 100
            or type(milestone.get("xp")) is not int or milestone["xp"] < 0):
        raise ValueError("Invalid growth milestone")
    if facts is not None and not isinstance(facts, dict):
        raise ValueError("Invalid production facts")
    state = ensure_growth(player)
    old = SkillRewardReceipt.objects.filter(player=player, source_type=source_type, source_id=source_id,
                                            skill_code=skill_code).first()
    if old:
        return deepcopy(old.result)
    progress = PlayerSkillProgress.objects.select_for_update().get(player=player, skill_code=skill_code)
    before = level_for_xp(progress.total_xp)
    milestones = []
    remaining = max(0, MAX_XP - progress.total_xp)
    bonus = 0
    if milestone is not None:
        row, first = PlayerMilestoneProgress.objects.get_or_create(player=player, code=milestone["code"],
            defaults=dict(title=milestone["title"], source_type=source_type, source_id=source_id, rules_version=RULES_VERSION))
        if first:
            bonus = min(milestone["xp"], max(0, remaining - xp))
            milestones.append(dict(_milestone_data(row), xp_reward=bonus))
    actual = min(xp + bonus, remaining)
    progress.total_xp += actual
    progress.rules_version = RULES_VERSION
    progress.save(update_fields=["total_xp", "rules_version"])
    # Each newly settled source increments the version, even at the XP cap: a
    # first-time milestone and receipt are still new personal growth facts.
    state.revision += 1
    state.save(update_fields=["revision"])
    result = dict(skill_code=skill_code, xp_gained=actual, level_before=before,
                  level_after=level_for_xp(progress.total_xp), total_xp=progress.total_xp,
                  new_unlocks=[], milestones=milestones)
    if facts is not None:
        result["facts"] = deepcopy(facts)
    SkillRewardReceipt.objects.create(player=player, source_type=source_type, source_id=source_id,
        skill_code=skill_code, rules_version=RULES_VERSION, result=deepcopy(result))
    from .tutorials import observe_skill
    observe_skill(player, source_type, source_id, result)
    return result


def settle_growth(player, *, source_type, source_id, facts=None):
    """Live route wrapper: only server-defined, released event rules qualify."""
    if source_type not in REWARD_RULES:
        raise ValueError("Growth source is not released")
    rule = REWARD_RULES[source_type]
    return award_skill(player, source_type=source_type, source_id=source_id, skill_code=rule["skill_code"],
        xp=rule["xp"], milestone=dict(code=rule["milestone_code"], title=rule["milestone_title"], xp=rule["milestone_xp"]), facts=facts)
