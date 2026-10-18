"""Small server-owned G3 rollout rules, independent of client recipe knowledge."""
VILLAGE_RECIPES = {**{key: (7, 3) for key in (29251, 29252, 29253)}, 29201: (1, 1), 29231: (2, 1),
    **{key: (4, 2) for key in (*range(29211, 29217), *range(29221, 29227))}}
NONPERISHABLE_IDS = frozenset({1049, 1050, 1058, 1076, 1099})
RULE_VERSION = "player-cooking-v1"
XP_PER_BATCH = 20
FIRST_COOK_BONUS = 20
AP_PER_BATCH = 5
SHELF_LIFE = 2880
