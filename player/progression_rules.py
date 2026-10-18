"""Versioned G0 rules. Unreleased content is descriptive, never an active bonus."""
from copy import deepcopy

RULES_VERSION = 2
SKILLS = (
    ("gathering", "采集"), ("farming", "种植"), ("forestry", "伐木"),
    ("hunting", "狩猎"), ("cooking", "烹饪"), ("business", "经营"),
)
SKILL_CODES = tuple(code for code, _ in SKILLS)
LEVEL_COSTS = (80, 140, 220, 340, 500, 700, 950, 1250, 1600)
LEVEL_TOTALS = (0, 80, 220, 440, 780, 1280, 1980, 2930, 4180, 5780)
MAX_XP = LEVEL_TOTALS[-1]
LEGACY_FOOD_LEVELS = {1: 1, 2: 2, 3: 4, 4: 6, 5: 7}
COLLECT_XP = 10
FIRST_GATHERING_XP = 20
FIRST_GATHERING = "first_gathering"
BUSINESS_ORDER_XP = 20
REWARD_RULES = {"collect": {"skill_code": "gathering", "xp": COLLECT_XP,
    "milestone_code": FIRST_GATHERING, "milestone_title": "第一次采集", "milestone_xp": FIRST_GATHERING_XP}}

_UNLOCKS = {
    "gathering": ((2, "targeted_gathering", "类别采集资格（需采集工具包）"), (4, "advanced_gathering", "进阶资源与改良工具包"),
                  (7, "professional_gathering", "深林原料与专业工具包"), (10, "gathering_mastery", "采集精通挑战")),
    "farming": ((2, "seed_return", "返种概率改善"), (4, "advanced_crops", "新作物与改良农具"),
                (7, "professional_crops", "专业作物与农具"), (10, "farming_mastery", "种植精通挑战")),
    "forestry": ((2, "felling_efficiency", "普通树作业改善"), (4, "hardwood", "硬木与改良斧"),
                 (7, "professional_wood", "专业木材与斧"), (10, "forestry_mastery", "伐木精通挑战")),
    "hunting": ((2, "bow_preparation", "满弦准备改善"), (4, "deer_challenge", "改良武器与鹿的挑战"),
                (7, "north_hunting", "专业武器与北坡挑战"), (10, "hunting_mastery", "狩猎精通挑战")),
    "cooking": ((2, "basic_quality", "合格基础菜品质保障"), (4, "advanced_cooking", "复杂料理与批次制作"),
                (7, "professional_cooking", "专业炊具与料理"), (10, "cooking_mastery", "烹饪精通挑战")),
    "business": ((2, "combined_orders", "组合备货订单"), (4, "player_stall", "玩家摊位"),
                 (7, "professional_contracts", "专业供应合同"), (10, "business_mastery", "专业经营目标")),
}


def level_for_xp(total_xp):
    return max(i + 1 for i, floor in enumerate(LEVEL_TOTALS) if total_xp >= floor)


def skill_data(code, name, total_xp):
    total_xp = min(MAX_XP, max(0, total_xp))
    level = level_for_xp(total_xp)
    effects = []
    if code == "gathering" and level >= 2:
        effects.append(dict(code="targeted_gathering", title="可使用有耐久的采集工具包选择食材类别"))
    if code == "business":
        effects.append(dict(code="supply_experience", title="完成真实需求订单可积累经营经验"))
    unlocks = []
    for required, key, title in _UNLOCKS[code]:
        implemented = key == "targeted_gathering"
        available = implemented and level >= required
        unlocks.append(dict(code=key, title=title, required_level=required, implemented=implemented,
                            available=available, reason=None if available else
                            "SKILL_LEVEL_REQUIRED" if implemented else "CONTENT_NOT_RELEASED"))
    return dict(code=code, name=name, level=level, total_xp=total_xp,
                xp_in_level=total_xp - LEVEL_TOTALS[level - 1] if level < 10 else 0,
                xp_to_next_level=LEVEL_COSTS[level - 1] if level < 10 else 0,
                max_level=10, progression_enabled=code in {"gathering", "business"}, effects=effects,
                unlocks=unlocks)


def legacy_entitlements(points):
    """Retain source records; this snapshot grants no new hunting/cooking facts."""
    return dict(collection_points=deepcopy(points), collection_pool_policy="PRESERVE_EXISTING",
                recipes_policy="PRESERVE_KNOWN_AND_VISIBILITY", restaurant_policy="PRESERVE_FACILITY_LEVEL",
                weapon_policy="PRESERVE_LEGACY_CONTROLS", tool_durability_policy="EXISTING_USAGE_COMPONENT")
