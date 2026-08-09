BASE_ACTIONS = [
    "move",
    "observe",
    "checkSelfBag",
    "checkDialogCondition",
    "speak",
    "give",
]


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
        "capabilities": BASE_ACTIONS + ["farmVegetables", "npcTrade", "publicDonate"],
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
        "personal_goal": "稳定肉类供应并确认森林旧路风险",
        "capabilities": BASE_ACTIONS + ["processMeat", "npcTrade", "explore"],
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
        "capabilities": BASE_ACTIONS + ["submitDishReview", "reviewPublicLedger", "attendCouncil"],
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
        "capabilities": BASE_ACTIONS + ["purchase", "cook", "manageMenu", "setRestaurantOpen"],
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
        "capabilities": BASE_ACTIONS + ["browseShop", "npcTrade", "manageMarket", "importGoods", "exportGoods"],
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
    # Phase one is the supported runtime.  Keep every later-stage capability
    # disabled, but do not seed a fresh environment into the retired
    # phase-zero/legacy command path.
    "village_simulation_enabled": True,
    "village_needs_enabled": False,
    "village_economy_enabled": False,
    "village_social_enabled": False,
    "village_story_enabled": False,
    "village_hub_command_adapter_enabled": True,
    "village_offline_catchup_enabled": False,
    "village_debug_overlay_enabled": False,
    "debug_logs_enabled": False,
}


TEST_NPC_ISOLATION = [
    ("10012", "TEST_10012", "jack_test", 999_999),
    ("10013", "TEST_10013", "mark_test", 5_689),
    ("10014", "TEST_10014", "lucy_test", 1_000_000),
]
