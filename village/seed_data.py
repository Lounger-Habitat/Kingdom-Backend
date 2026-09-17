BASE_ACTIONS = [
    "MOVE", "WAIT", "GO_HOME",
    "EAT", "REST", "SLEEP", "WORK_PLACEHOLDER",
    "PROPOSE_TRADE", "RESPOND_TRADE", "CANCEL_TRADE_OFFER",
    "BUY_FROM_STORE", "BUY_FROM_MARKETPLACE", "SELL_ON_MARKETPLACE",
    "DELIST_FROM_MARKETPLACE", "BROWSE_MARKETPLACE",
    "CHECK_MY_MARKETPLACE_LISTINGS", "CHECK_MARKETPLACE_HISTORY",
    "INSPECT_STORE_INVENTORY",
    "INSPECT_SOCIAL_STATE", "CREATE_COMMITMENT", "CANCEL_COMMITMENT",
    "PERFORM_COMMITMENT", "STORY_VOTE", "STORY_ACT",
    "OBSERVE", "INSPECT_NEARBY", "INSPECT_INVENTORY",
    "INSPECT_TRADE_OFFERS", "INSPECT_STORY_STATE", "INSPECT_RELEVANT_STORIES",
    "INSPECT_STORY_DETAIL",
]


# 订单流动作（CREATE_ORDER/CANCEL_ORDER/START_DELIVERY）在指令目录中有定义，
# 但刻意不授予任何 NPC：保持与线上 NpcProfile.capabilities 一致，重置/重播种
# 不得新增指令。如要启用，先在线上 DB 授权再同步到这里。
NPC_SEEDS = [
    {
        "agent_id": "10011",
        "name": "bill",
        "display_name": "Bill",
        "occupation": "farmer",
        "home_location_code": "BILL_HOME",
        "work_location_code": "BILL_FARM",
        "social_location_code": "VILLAGE_SQUARE",
        "personality_traits": ["practical", "reserved", "responsible"],
        "personal_goal": "维持三块农田生产并保住家庭储备",
        "capabilities": BASE_ACTIONS + [
            "PLANT", "HARVEST",
            "INSPECT_FARM_STATE",
        ],
        "money": 200,
        "items": [(1003, 12), (1051, 18), (1019, 4), (1023, 3)],
    },
    {
        "agent_id": "10012",
        "name": "jack",
        "display_name": "Jack",
        "occupation": "hunter",
        "home_location_code": "JACK_HOME",
        "work_location_code": "BUTCHER_YARD",
        "social_location_code": "RESTAURANT",
        "personality_traits": ["independent", "wry", "cautious"],
        "personal_goal": "狩猎并处理猎物，出售肉类和副产品，购买工具食物并维持生活",
        "capabilities": BASE_ACTIONS + [
            "HUNT", "PROCESS_GAME",
        ],
        "money": 240,
        "items": [(1009, 4), (1034, 3), (1056, 6), (1019, 4)],
    },
    {
        "agent_id": "10013",
        "name": "mark",
        "display_name": "Mark",
        "occupation": "clerk_reviewer",
        "home_location_code": "RECORD_HALL",
        "work_location_code": "RECORD_HALL",
        "social_location_code": "VILLAGE_SQUARE",
        "personality_traits": ["rational", "restrained", "responsible", "cautious"],
        "personal_goal": "公开筹集修桥资金并维持村庄秩序",
        "capabilities": BASE_ACTIONS + [
            "RATE_DISH", "PUBLISH_PRICE_INDEX", "AUDIT_PUBLIC_FUND",
            "INSPECT_RATEABLE_DISHES", "INSPECT_PUBLIC_ECONOMY_STATE",
        ],
        "money": 120,
        "items": [(1019, 3)],
    },
    {
        "agent_id": "10014",
        "name": "lucy",
        "display_name": "Lucy",
        "occupation": "restaurateur",
        "home_location_code": "RESTAURANT",
        "work_location_code": "RESTAURANT",
        "social_location_code": "VILLAGE_SQUARE",
        "personality_traits": ["creative", "ambitious", "warm", "stubborn"],
        "personal_goal": "让饭店在商路中断期间持续供应村民",
        "capabilities": BASE_ACTIONS + [
            "COOK", "WITHDRAW_STORE_FUNDS", "REQUEST_DISH_REVIEW", "CANCEL_DISH_REVIEW",
            "LIST_STORE_ITEM", "DELIST_STORE_ITEM",
            "INSPECT_RESTAURANT_STATE",
        ],
        "money": 300,
        "items": [(1025, 10), (1042, 10), (1056, 8), (1057, 6), (1049, 8), (1058, 6)],
    },
    {
        "agent_id": "10015",
        "name": "nora",
        "display_name": "Nora",
        "occupation": "merchant",
        "home_location_code": "GENERAL_STORE",
        "work_location_code": "GENERAL_STORE",
        "social_location_code": "MARKET_STREET",
        "personality_traits": ["shrewd", "practical", "persuasive", "controlling"],
        "personal_goal": "恢复商路并建立可信的村庄供货网络",
        "capabilities": BASE_ACTIONS + [
            "IMPORT_GOODS", "EXPORT_GOODS", "CONTACT_CARAVAN", "WITHDRAW_STORE_FUNDS", "DEPOSIT_STORE_FUNDS", "WITHDRAW_STORE_ITEM", "SET_STORE_LIMITS",
            "LIST_STORE_ITEM", "UPDATE_STORE_PRICE", "DELIST_STORE_ITEM",
            "INSPECT_MARKET_STATE",
            "INSPECT_CARAVAN_REQUESTS",
        ],
        "money": 450,
        "items": [(1003, 30), (1051, 36), (1049, 12), (1050, 8), (1058, 8), (1076, 6), (1002, 10)],
    },
]


LOCATION_SEEDS = [
    ("VILLAGE_SQUARE", "村庄广场", (0, 0, 0), "public", 20),
    ("MARKET_STREET", "市场街", (12, 0, -4), "market", 16),
    ("RESTAURANT", "Lucy 饭店", (-3, 0, 19), "home_work", 12),
    ("RECORD_HALL", "Mark 品鉴处与议事厅", (-22, 0, 11), "home_work", 12),
    ("GENERAL_STORE", "Nora 杂货铺", (21, 0, 11), "home_work", 8),
    ("BILL_HOME", "Bill 农舍", (-31, 0, -34), "home", 4),
    ("BILL_FARM", "Bill 农田", (-32, 0, -59), "work", 8),
    ("JACK_HOME", "Jack 林边小屋", (-62, 2, 34), "home", 4),
    ("BUTCHER_YARD", "Jack 屠宰棚", (-49, 1, 20), "work", 4),
    ("EMERGENCY_TRADE_POST", "林边应急商队接货点", (-94.98, 2.4783, 38.74), "market", 3),
    ("FOREST_EDGE", "森林边缘", (-81, 3, 40), "resource", 8),
    ("HUNTING_GATE", "猎场入口", (-91, 4, 47), "offmap_gate", 2),
    ("RIVER_EAST", "东侧河道", (69, -1, 0), "boundary", 0),
    ("OLD_BRIDGE", "旧桥", (69, 0, 0), "story", 3),
    ("REPAIR_YARD", "修桥料场", (48, 0, -11), "story_work", 8),
    ("TRADE_ROAD", "外部商路", (92, 0, 0), "offmap_gate", 4),
]


SIMULATION_DEFAULTS = {
    "tick_interval_seconds": 5,
    "hub_command_timeout_seconds": 30,
    "command_queue_limit": 8,
    "day_start_minute": 360,
    "day_close_minute": 1320,
    "max_catchup_days": 3,
    # Phase one is the supported default runtime.  Later-stage capabilities
    # remain independently switchable for explicit phase integration runs.
    "village_simulation_enabled": True,
    "village_needs_enabled": False,
    "village_economy_enabled": False,
    "village_social_enabled": False,
    "village_story_enabled": False,
    "village_hub_command_adapter_enabled": True,
    "village_offline_catchup_enabled": False,
    "village_presentation_enabled": False,
    "village_debug_overlay_enabled": False,
    "debug_logs_enabled": False,
}


TEST_NPC_ISOLATION = [
    ("10012", "TEST_10012", "jack_test", 999_999),
    ("10013", "TEST_10013", "mark_test", 5_689),
    ("10014", "TEST_10014", "lucy_test", 1_000_000),
]


# ── Phase 3: Farm plot seeds ───────────────────────────────────────

FARM_PLOT_SEEDS = [
    {"plot_code": code}
    for region in ("A", "B", "C", "D")
    for code in (
        ([f"FARM_PLOT_{region}"] if region != "D" else ["FARM_PLOT_D_01"])
        + [f"FARM_PLOT_{region}_{index:02d}" for index in range(2, 10)]
    )
]

# The base Unity catalog already has the four harvested crops, but only corn
# has a seed item.  These village-only seed definitions use the crop icon as a
# temporary visual and are also supplied by Nora's caravan import whitelist.
FARM_ITEM_SEEDS = [
    {
        "item_id": 29301,
        "item_name": "小麦种子",
        "item_type": "Seed",
        "item_description": "Bill农田种植小麦所需的种子，可由Nora通过商队进货。",
        "item_price": 5,
        "icon_source_item_id": 1005,
    },
    {
        "item_id": 29302,
        "item_name": "胡萝卜种子",
        "item_type": "Seed",
        "item_description": "Bill农田种植胡萝卜所需的种子，可由Nora通过商队进货。",
        "item_price": 5,
        "icon_source_item_id": 1025,
    },
    {
        "item_id": 29303,
        "item_name": "土豆种薯",
        "item_type": "Seed",
        "item_description": "Bill农田种植土豆所需的种薯，可由Nora通过商队进货。",
        "item_price": 5,
        "icon_source_item_id": 1042,
    },
]


# ── Phase 3: Village account seeds ────────────────────────────────

VILLAGE_ACCOUNT_SEEDS = [
    {"account_code": "PUBLIC_FUND", "balance": 0},
]


# ── Phase 3: Tool item definitions ────────────────────────────────
# These items must exist in the Item table before Phase 3 seed runs.

TOOL_ITEM_SEEDS = [
    {
        "item_id": 4001,
        "item_name": "阶段三农具",
        "item_type": "HoeTool",
        "item_description": "阶段三农田专用工具，可使用20次",
        "item_price": 50,
        "collectable": False,
        "max_use_count": 20,
    },
    {
        "item_id": 4002,
        "item_name": "阶段三猎具",
        "item_type": "CollectTool",
        "item_description": "阶段三狩猎专用工具，可使用20次",
        "item_price": 50,
        "collectable": False,
        "max_use_count": 20,
    },
    {
        "item_id": 4003,
        "item_name": "修桥金属件",
        "item_type": "RepairMaterial",
        "item_description": "断桥主线固定使用的金属维修件",
        "item_price": 40,
        "collectable": False,
    },
]


# ── Phase 3: Initial tool grants per NPC ──────────────────────────
# (agent_id, tool_item_id, amount, remaining_uses)

INITIAL_TOOL_GRANTS = [
    ("10011", 4001, 1, 20),   # Bill: 1 农具
    ("10012", 4002, 1, 20),   # Jack: 1 猎具
    ("10015", 4001, 3, 20),   # Nora: 3 农具
    ("10015", 4002, 3, 20),   # Nora: 3 猎具
]


# ── Phase 3: Initial supplementary grants ─────────────────────────
# Seeds and ingredients granted to NPCs for the economy loop.
# (agent_id, item_id, amount)

INITIAL_SUPPLEMENTARY_GRANTS = [
    ("10011", 1051, 6),   # Bill: 6 玉米种子
    ("10014", 1040, 4),   # Lucy: 4 红辣椒
    ("10014", 1026, 4),   # Lucy: 4 奶酪
    ("10014", 1050, 4),   # Lucy: 4 糖
]


# ── Phase 4: directed relationship seeds ──────────────────────────
# Every ordered pair is present.  Unlisted story edges are neutral.
NPC_RELATIONSHIP_SEEDS = [
    ("10011", "10012", 45, 35, -10, 5),
    ("10011", "10013", 0, 0, 0, 0),
    ("10012", "10011", 55, 50, 25, 0),
    ("10012", "10013", 0, 0, 0, 0),
    ("10012", "10014", 0, 0, 0, 0),
    ("10011", "10014", 65, 40, 0, 25),
    ("10014", "10011", 70, 45, 20, 20),
    ("10014", "10012", 0, 0, 0, 0),
    ("10014", "10015", 0, 0, 0, 0),
    ("10011", "10015", -10, 15, 45, 40),
    ("10015", "10011", 5, 35, -35, 20),
    ("10012", "10015", -20, 5, 0, 45),
    ("10015", "10012", -5, 10, 0, 30),
    ("10013", "10011", 0, 0, 0, 0),
    ("10013", "10012", 0, 0, 0, 0),
    ("10014", "10013", 30, 55, 10, 15),
    ("10013", "10014", 25, 60, 0, 10),
    ("10013", "10015", 0, 30, 0, 35),
    ("10015", "10013", -5, 25, 0, 40),
    ("10015", "10014", 0, 0, 0, 0),
]

FORMAL_NPC_AGENT_IDS = tuple(seed["agent_id"] for seed in NPC_SEEDS)
PHASE4_ACTION_DURATION_SEEDS = {
    "CREATE_COMMITMENT": 1,
    "CANCEL_COMMITMENT": 1,
    "PERFORM_COMMITMENT": 1,
    "JOINT_WORK": 120,
    "MEETING": 30,
}
