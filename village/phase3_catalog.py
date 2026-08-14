"""Phase 3 item / tool / recipe catalog.

Hard-coded constants for the phase-3 economy loop only.
Do NOT add database-backed configuration admin for these values.
"""

# ── Corn (PLANT / HARVEST) ─────────────────────────────────────────
CORN_SEED_ITEM_ID = 1051
CORN_OUTPUT_ITEM_ID = 1030
CORN_GROWTH_MINUTES = 2880
CORN_YIELD_MIN = 4
CORN_YIELD_MAX = 6
CORN_GUARANTEED_SEED_RETURN = 1
CORN_BONUS_SEED_CHANCE = 0.25

# ── Hunt chicken ───────────────────────────────────────────────────
HUNT_CHICKEN_OUTPUT_ITEM_ID = 1056
HUNT_CHICKEN_YIELD_MIN = 3
HUNT_CHICKEN_YIELD_MAX = 5
HUNT_CHICKEN_SPOILAGE_MINUTES = 2880
HUNT_CHICKEN_LEATHER_ITEM_ID = None  # disabled this phase

# ── Tools ──────────────────────────────────────────────────────────
FARM_TOOL_ITEM_ID = 4001
FARM_TOOL_NAME = "阶段三农具"
FARM_TOOL_ITEM_TYPE = "HoeTool"
FARM_TOOL_MAX_USE_COUNT = 20

HUNT_TOOL_ITEM_ID = 4002
HUNT_TOOL_NAME = "阶段三猎具"
HUNT_TOOL_ITEM_TYPE = "CollectTool"
HUNT_TOOL_MAX_USE_COUNT = 20

TOOL_ITEM_IDS = (FARM_TOOL_ITEM_ID, HUNT_TOOL_ITEM_ID)

# ── Recipe whitelist ───────────────────────────────────────────────
RECIPE_WHITELIST = frozenset({2, 12})

# ── Dish spoilage ──────────────────────────────────────────────────
DISH_SPOILAGE_MINUTES = 2880

# ── Safe stocks (item_id → safe_stock) ─────────────────────────────
SAFE_STOCKS = {
    1051: 10,   # 玉米种子
    1030: 10,   # 玉米
    1056: 8,    # 鸡肉
    1040: 6,    # 红辣椒
    1026: 6,    # 奶酪
    1050: 6,    # 糖
    1053: 4,    # 辣子鸡丁 (recipe 2 output)
    1067: 4,    # 奶酪焗玉米 (recipe 12 output)
}

# ── Base prices (item_id → base_price) ─────────────────────────────
BASE_PRICES = {
    1051: 5,    # 玉米种子
    1030: 8,    # 玉米
    1056: 12,   # 鸡肉
    1040: 6,    # 红辣椒
    1026: 10,   # 奶酪
    1050: 4,    # 糖
    1053: 25,   # 辣子鸡丁
    1067: 22,   # 奶酪焗玉米
    4001: 50,   # 阶段三农具
    4002: 50,   # 阶段三猎具
}

# ── Import whitelist (item_id) ─────────────────────────────────────
IMPORT_WHITELIST = frozenset({1040, 1026, 1050})
# 1040 = 红辣椒, 1026 = 奶酪, 1050 = 糖

# ── Export whitelist (item_id) ─────────────────────────────────────
EXPORT_WHITELIST = frozenset({1030, 1056, 1053, 1067})
# 1030 = 玉米, 1056 = 鸡肉, 1053 = 辣子鸡丁, 1067 = 奶酪焗玉米

# ── NPC starting money (from seed_data) ────────────────────────────
NPC_STARTING_MONEY = {
    "10011": 200,   # Bill
    "10012": 240,   # Jack
    "10013": 120,   # Mark
    "10014": 300,   # Lucy
    "10015": 450,   # Nora
}

# ── Community meal payment ─────────────────────────────────────────
COMMUNITY_MEAL_PAYMENT_PER_DISH = 1

# ── Caravan cooldown (days) ────────────────────────────────────────
CARAVAN_COOLDOWN_DAYS = 3

# ── Traveler max sales per day-close ───────────────────────────────
TRAVELER_MAX_SALES = 2

# ── Public fund salary per Mark action ─────────────────────────────
MARK_SALARY = 1
