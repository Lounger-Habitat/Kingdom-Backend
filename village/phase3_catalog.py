"""Phase 3 item / tool / recipe catalog.

Hard-coded constants for the phase-3 economy loop only.
Do NOT add database-backed configuration admin for these values.
"""

# ── Farm crops (PLANT / HARVEST) ──────────────────────────────────
#
# Corn keeps the shipped balance exactly.  The other three crops deliberately
# use the same first-pass timing/yield/seed-return rules so balancing stays in
# this one small catalog instead of leaking into the transaction handler.
CORN_SEED_ITEM_ID = 1051
CORN_OUTPUT_ITEM_ID = 1030
CORN_GROWTH_MINUTES = 1440
CORN_YIELD_MIN = 4
CORN_YIELD_MAX = 6
CORN_GUARANTEED_SEED_RETURN = 1
CORN_BONUS_SEED_CHANCE = 0.25

WHEAT_SEED_ITEM_ID = 29301
CARROT_SEED_ITEM_ID = 29302
POTATO_SEED_ITEM_ID = 29303

FARM_CROP_RULES = {
    "CORN": {
        "display_name": "玉米",
        "seed_item_id": CORN_SEED_ITEM_ID,
        "output_item_id": CORN_OUTPUT_ITEM_ID,
        "growth_minutes": CORN_GROWTH_MINUTES,
        "yield_min": CORN_YIELD_MIN,
        "yield_max": CORN_YIELD_MAX,
        "guaranteed_seed_return": CORN_GUARANTEED_SEED_RETURN,
        "bonus_seed_chance": CORN_BONUS_SEED_CHANCE,
    },
    "WHEAT": {
        "display_name": "小麦",
        "seed_item_id": WHEAT_SEED_ITEM_ID,
        "output_item_id": 1005,
        "growth_minutes": 1440,
        "yield_min": 4,
        "yield_max": 6,
        "guaranteed_seed_return": 1,
        "bonus_seed_chance": 0.25,
    },
    "CARROT": {
        "display_name": "胡萝卜",
        "seed_item_id": CARROT_SEED_ITEM_ID,
        "output_item_id": 1025,
        "growth_minutes": 1440,
        "yield_min": 4,
        "yield_max": 6,
        "guaranteed_seed_return": 1,
        "bonus_seed_chance": 0.25,
    },
    "POTATO": {
        "display_name": "土豆",
        "seed_item_id": POTATO_SEED_ITEM_ID,
        "output_item_id": 1042,
        "growth_minutes": 1440,
        "yield_min": 4,
        "yield_max": 6,
        "guaranteed_seed_return": 1,
        "bonus_seed_chance": 0.25,
    },
}

FARM_SEED_ITEM_DEFINITIONS = {
    WHEAT_SEED_ITEM_ID: ("小麦种子", 5, "Bill农田种植小麦所需的种子，可由Nora通过商队进货。"),
    CARROT_SEED_ITEM_ID: ("胡萝卜种子", 5, "Bill农田种植胡萝卜所需的种子，可由Nora通过商队进货。"),
    POTATO_SEED_ITEM_ID: ("土豆种薯", 5, "Bill农田种植土豆所需的种薯，可由Nora通过商队进货。"),
}


def farm_crop_catalog():
    """Serializable crop rules exposed by farm snapshots and agent queries."""
    return {
        crop_code: {
            "crop_code": crop_code,
            **rule,
        }
        for crop_code, rule in FARM_CROP_RULES.items()
    }

# ── Hunt meat ──────────────────────────────────────────────────────
# Current game meat items: tenderloin, ribs, chicken, pork belly, beef.
HUNT_MEAT_ITEM_IDS = (1009, 1034, 1056, 1057, 1077)
HUNT_MEAT_YIELD_MIN = 3
HUNT_MEAT_YIELD_MAX = 5
HUNT_MEAT_SPOILAGE_MINUTES = 2880

# Jack v1: one hunted animal is processed into existing cuts and trade goods.
HUNT_ANIMALS_CODE = "HUNT_ANIMALS"
PREY_CHICKEN, PREY_PIG, PREY_CATTLE = 29101, 29102, 29103
FEATHER, ROUGH_LEATHER = 29104, 29105
PREY_RECIPES = {
    PREY_CHICKEN: {"name": "鸡（待处理猎物）", "weight": 60, "outputs": {1056: 3, FEATHER: 1}},
    PREY_PIG: {"name": "猪（待处理猎物）", "weight": 30, "outputs": {1009: 1, 1034: 1, 1057: 2, ROUGH_LEATHER: 1}},
    PREY_CATTLE: {"name": "牛（待处理猎物）", "weight": 10, "outputs": {1077: 6, ROUGH_LEATHER: 2}},
}
PREY_ITEM_IDS = frozenset(PREY_RECIPES)
BYPRODUCT_ITEM_IDS = frozenset({FEATHER, ROUGH_LEATHER})
JACK_ITEM_DEFINITIONS = {
    PREY_CHICKEN: ("鸡（待处理猎物）", 12, "猎获的鸡。由Jack在屠宰棚处理为鸡肉和羽毛；不可直接食用。"),
    PREY_PIG: ("猪（待处理猎物）", 30, "猎获的猪。由Jack处理为里脊、排骨、五花肉和粗皮革；不可直接食用。"),
    PREY_CATTLE: ("牛（待处理猎物）", 70, "猎获的牛。由Jack处理为牛肉和粗皮革；不可直接食用。"),
    FEATHER: ("羽毛", 3, "整理成包的羽毛，可出售给杂货店或有实际需求的买家。"),
    ROUGH_LEATHER: ("粗皮革", 8, "猎物加工时整理出的皮材，可出售给杂货店或用于贸易。"),
}

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
REPAIR_WOOD_ITEM_ID = 1002
REPAIR_METAL_ITEM_ID = 4003

# ── Recipe whitelist ───────────────────────────────────────────────
from .lucy_catalog import RECIPE_DEFINITIONS, MEALS, RELIEF_PACK
RECIPE_WHITELIST = frozenset({2, 3, 6, 12, 16, 27}) | frozenset(RECIPE_DEFINITIONS)

# ── Dish spoilage ──────────────────────────────────────────────────
DISH_SPOILAGE_MINUTES = 2880

# ── Safe stocks (item_id → safe_stock) ─────────────────────────────
SAFE_STOCKS = {
    1009: 8,    # 里脊肉
    1034: 8,    # 排骨
    1003: 10,   # 南瓜种子
    1022: 10,   # 浆果（当前数据标为种子）
    1051: 10,   # 玉米种子
    WHEAT_SEED_ITEM_ID: 10,
    CARROT_SEED_ITEM_ID: 10,
    POTATO_SEED_ITEM_ID: 10,
    1030: 10,   # 玉米
    1056: 8,    # 鸡肉
    1057: 8,    # 五花肉
    1077: 8,    # 牛肉
    1040: 6,    # 红辣椒
    1049: 6,    # 盐
    1026: 6,    # 奶酪
    1050: 6,    # 糖
    1058: 6,    # 酱油
    1076: 6,    # 豆瓣酱
    1099: 6,    # 醋
    4001: 1,    # 阶段三农具
    4002: 1,    # 阶段三猎具
    1053: 4,    # 辣子鸡丁 (recipe 2 output)
    1067: 4,    # 奶酪焗玉米 (recipe 12 output)
}

# ── Base prices (item_id → base_price) ─────────────────────────────
BASE_PRICES = {
    1005: 6,    # 小麦（与现有 Item 目录价格一致）
    1009: 12,   # 里脊肉
    1034: 12,   # 排骨
    1003: 5,    # 南瓜种子
    1022: 5,    # 浆果（当前数据标为种子）
    1051: 5,    # 玉米种子
    WHEAT_SEED_ITEM_ID: 5,
    CARROT_SEED_ITEM_ID: 5,
    POTATO_SEED_ITEM_ID: 5,
    1030: 8,    # 玉米
    1025: 4,    # 胡萝卜（与现有 Item 目录价格一致）
    1042: 4,    # 土豆（与现有 Item 目录价格一致）
    1056: 12,   # 鸡肉
    1057: 12,   # 五花肉
    1077: 12,   # 牛肉
    1040: 6,    # 红辣椒
    1049: 2,    # 盐
    1026: 10,   # 奶酪
    1050: 4,    # 糖
    1058: 3,    # 酱油
    1076: 3,    # 豆瓣酱
    1099: 2,    # 醋
    1053: 25,   # 辣子鸡丁
    1067: 22,   # 奶酪焗玉米
    4001: 50,   # 阶段三农具
    4002: 50,   # 阶段三猎具
    1033: 3,   # 大蒜，肉类菜谱补给
    1038: 4,   # 洋葱，肉类菜谱补给
    1054: 35,  # 红烧肉
    1061: 81,  # 红烧排骨
    1071: 86,  # 糖醋里脊
    1108: 73,  # 洋葱炒牛肉
    FEATHER: 3,
    ROUGH_LEATHER: 8,
}

# ── Import whitelist (item_id) ─────────────────────────────────────
IMPORT_WHITELIST = frozenset({
    1003, 1022, 1051, WHEAT_SEED_ITEM_ID, CARROT_SEED_ITEM_ID, POTATO_SEED_ITEM_ID,  # 种子
    1026, 1040, 1049, 1050, 1058, 1076, 1099,  # 奶酪和调味料
    FARM_TOOL_ITEM_ID, HUNT_TOOL_ITEM_ID,
    1033, 1038,  # 大蒜、洋葱供Lucy肉菜使用
})
# 种子和调味料来自当前 ItemDataList；工具为阶段三农具和阶段三猎具。

# ── Export whitelist (item_id) ─────────────────────────────────────
EXPORT_WHITELIST = frozenset({1005, 1009, 1025, 1030, 1034, 1042, 1056, 1057, 1077, 1053, 1067, FEATHER, ROUGH_LEATHER})
# 肉类、玉米及阶段三配方成品均可对外出货。

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
CARAVAN_COOLDOWN_DAYS = 2  # Legacy compatibility only; daily visits are defined in caravan.py.

# ── Traveler max sales per day-close ───────────────────────────────
TRAVELER_MAX_SALES = 2

# Legacy value retained for historical integrations only. Current daily wages
# use public_finance.meal_reference; no action pays this constant.
MARK_SALARY = 1
