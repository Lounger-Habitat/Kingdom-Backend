"""Phase 3 frozen contract constants for village economy actions.

All action names, param whitelists, result codes, and status enums
are defined here and must NOT be modified after the phase-3 contract
fixture is frozen.  Unity and Backend share these definitions.
"""

from enum import Enum

# ── Schema versions ────────────────────────────────────────────────
VILLAGE_ECONOMY_ACTION_V1 = "village.economy-action.v1"
VILLAGE_ECONOMY_RESULT_V1 = "village.economy-result.v1"


# ── Action names ───────────────────────────────────────────────────
class ActionName(str, Enum):
    PLANT = "PLANT"
    HARVEST = "HARVEST"
    HUNT = "HUNT"
    COOK = "COOK"
    CREATE_ORDER = "CREATE_ORDER"
    CANCEL_ORDER = "CANCEL_ORDER"
    START_DELIVERY = "START_DELIVERY"
    IMPORT_GOODS = "IMPORT_GOODS"
    EXPORT_GOODS = "EXPORT_GOODS"
    RATE_DISH = "RATE_DISH"
    PUBLISH_PRICE_INDEX = "PUBLISH_PRICE_INDEX"
    AUDIT_PUBLIC_FUND = "AUDIT_PUBLIC_FUND"
    EAT = "EAT"
    COMMUNITY_MEAL = "COMMUNITY_MEAL"
    PROPOSE_TRADE = "PROPOSE_TRADE"
    RESPOND_TRADE = "RESPOND_TRADE"
    CANCEL_TRADE_OFFER = "CANCEL_TRADE_OFFER"
    LIST_STORE_ITEM = "LIST_STORE_ITEM"
    UPDATE_STORE_PRICE = "UPDATE_STORE_PRICE"
    DELIST_STORE_ITEM = "DELIST_STORE_ITEM"
    BUY_FROM_STORE = "BUY_FROM_STORE"


# ── Read-only query names ─────────────────────────────────────────
# INSPECT_STORE_INVENTORY is a read-only store query, NOT a Phase 3
# command.  It never enters the economy settlement path, never writes a
# receipt/ledger/world event, and must not be added to ActionName.
INSPECT_STORE_INVENTORY_QUERY = "INSPECT_STORE_INVENTORY"


# ── Result codes ───────────────────────────────────────────────────
class ResultCode(str, Enum):
    OK = "OK"
    P3_FEATURE_DISABLED = "P3_FEATURE_DISABLED"
    P3_FORBIDDEN_ACTOR = "P3_FORBIDDEN_ACTOR"
    P3_INVALID_PARAMS = "P3_INVALID_PARAMS"
    P3_WRONG_LOCATION = "P3_WRONG_LOCATION"
    P3_ASSET_VERSION_CONFLICT = "P3_ASSET_VERSION_CONFLICT"
    P3_INSUFFICIENT_ITEM = "P3_INSUFFICIENT_ITEM"
    P3_INSUFFICIENT_MONEY = "P3_INSUFFICIENT_MONEY"
    P3_TOOL_BROKEN = "P3_TOOL_BROKEN"
    P3_PLOT_NOT_READY = "P3_PLOT_NOT_READY"
    P3_RECIPE_NOT_ALLOWED = "P3_RECIPE_NOT_ALLOWED"
    P3_ORDER_NOT_OPEN = "P3_ORDER_NOT_OPEN"
    P3_DELIVERY_NOT_READY = "P3_DELIVERY_NOT_READY"
    P3_PRICE_UNAVAILABLE = "P3_PRICE_UNAVAILABLE"
    P3_CARAVAN_COOLDOWN = "P3_CARAVAN_COOLDOWN"
    P3_REQUEST_CONFLICT = "P3_REQUEST_CONFLICT"
    P3_DAY_NOT_READY = "P3_DAY_NOT_READY"
    TRADE_TARGET_NOT_FOUND = "TRADE_TARGET_NOT_FOUND"
    TRADE_TARGET_REJECTED = "TRADE_TARGET_REJECTED"
    TRADE_OFFER_NOT_FOUND = "TRADE_OFFER_NOT_FOUND"
    TRADE_OFFER_EXPIRED = "TRADE_OFFER_EXPIRED"
    TRADE_OFFER_ALREADY_RESOLVED = "TRADE_OFFER_ALREADY_RESOLVED"
    TRADE_RESPONSE_FORBIDDEN = "TRADE_RESPONSE_FORBIDDEN"
    TRADE_CANCEL_FORBIDDEN = "TRADE_CANCEL_FORBIDDEN"
    TRADE_NOT_COLOCATED = "TRADE_NOT_COLOCATED"
    TRADE_WRONG_LOCATION = "TRADE_WRONG_LOCATION"
    TRADE_STORE_CLOSED = "TRADE_STORE_CLOSED"
    TRADE_STORE_FORBIDDEN = "TRADE_STORE_FORBIDDEN"
    TRADE_LISTING_NOT_FOUND = "TRADE_LISTING_NOT_FOUND"
    TRADE_LISTING_NOT_ACTIVE = "TRADE_LISTING_NOT_ACTIVE"
    TRADE_LISTING_PRICE_CONFLICT = "TRADE_LISTING_PRICE_CONFLICT"
    TRADE_INVALID_CUSTOM_PRICE = "TRADE_INVALID_CUSTOM_PRICE"
    TRADE_INVALID_FEE_CONFIG = "TRADE_INVALID_FEE_CONFIG"
    TRADE_PUBLIC_FUND_NOT_FOUND = "TRADE_PUBLIC_FUND_NOT_FOUND"
    TRADE_ITEM_NOT_FOR_SALE = "TRADE_ITEM_NOT_FOR_SALE"
    TRADE_INSUFFICIENT_MONEY = "TRADE_INSUFFICIENT_MONEY"
    TRADE_INSUFFICIENT_ITEM = "TRADE_INSUFFICIENT_ITEM"
    TRADE_ASSET_VERSION_CONFLICT = "TRADE_ASSET_VERSION_CONFLICT"
    TRADE_STORE_VERSION_CONFLICT = "TRADE_STORE_VERSION_CONFLICT"
    TRADE_REQUEST_CONFLICT = "TRADE_REQUEST_CONFLICT"


# ── Side enum ──────────────────────────────────────────────────────
class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


# ── Order statuses ─────────────────────────────────────────────────
class OrderStatus(str, Enum):
    OPEN = "OPEN"
    MATCHED = "MATCHED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


# ── Delivery statuses ──────────────────────────────────────────────
class DeliveryStatus(str, Enum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


# ── Farm plot statuses ─────────────────────────────────────────────
class FarmPlotStatus(str, Enum):
    EMPTY = "EMPTY"
    GROWING = "GROWING"
    MATURE = "MATURE"


# ── Day snapshot statuses ──────────────────────────────────────────
class DaySnapshotStatus(str, Enum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"


# ── Restaurant statuses ────────────────────────────────────────────
class RestaurantStatus(str, Enum):
    OPEN = "OPEN"
    LOW_STOCK = "LOW_STOCK"
    SOLD_OUT = "SOLD_OUT"


# ── Store statuses ─────────────────────────────────────────────────
class StoreStatus(str, Enum):
    NORMAL = "NORMAL"
    LOW_STOCK = "LOW_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"


# ── Store operational statuses ─────────────────────────────────────
class StoreOperationalStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    SUSPENDED = "SUSPENDED"


# ── Store listing statuses ─────────────────────────────────────────
class ListingStatus(str, Enum):
    ACTIVE = "ACTIVE"
    SOLD_OUT = "SOLD_OUT"
    DELISTED = "DELISTED"


# ── Store price modes ──────────────────────────────────────────────
class PriceMode(str, Enum):
    ORIGINAL = "ORIGINAL"
    CUSTOM = "CUSTOM"


# ── Trade offer statuses ───────────────────────────────────────────
class TradeOfferStatus(str, Enum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


# ── Plot codes ─────────────────────────────────────────────────────
PLOT_CODES = ("FARM_PLOT_A", "FARM_PLOT_B", "FARM_PLOT_C")

# ── Crop codes ─────────────────────────────────────────────────────
CROP_CODES = ("CORN",)

# ── Hunt codes ─────────────────────────────────────────────────────
HUNT_CODES = ("HUNT_CHICKEN",)

# ── Delivery points ────────────────────────────────────────────────
DELIVERY_POINTS = ("MARKET_STREET", "RESTAURANT", "GENERAL_STORE")

# ── Recipe whitelist ───────────────────────────────────────────────
RECIPE_WHITELIST = frozenset({2, 12})

# ── Score range ────────────────────────────────────────────────────
SCORE_MIN = 1
SCORE_MAX = 5

# ── Actor whitelist per action ─────────────────────────────────────
# Maps action name to set of allowed NPC agent_id strings.
ACTOR_WHITELIST = {
    ActionName.PLANT: {"10011"},
    ActionName.HARVEST: {"10011"},
    ActionName.HUNT: {"10012"},
    ActionName.COOK: {"10014"},
    ActionName.CREATE_ORDER: {"10011", "10012", "10014", "10015"},
    ActionName.CANCEL_ORDER: set(),  # any NPC who owns the order
    ActionName.START_DELIVERY: set(),  # seller of the delivery
    ActionName.IMPORT_GOODS: {"10015"},
    ActionName.EXPORT_GOODS: {"10015"},
    ActionName.RATE_DISH: {"10013"},
    ActionName.PUBLISH_PRICE_INDEX: {"10013"},
    ActionName.AUDIT_PUBLIC_FUND: {"10013"},
    ActionName.EAT: set(),  # any NPC
    ActionName.COMMUNITY_MEAL: set(),  # any NPC
    ActionName.PROPOSE_TRADE: set(),  # any formal NPC
    ActionName.RESPOND_TRADE: set(),  # runtime-validated as the offer seller
    ActionName.CANCEL_TRADE_OFFER: set(),  # runtime-validated as the offer buyer
    ActionName.LIST_STORE_ITEM: {"10014", "10015"},  # store operators only
    ActionName.UPDATE_STORE_PRICE: {"10014", "10015"},
    ActionName.DELIST_STORE_ITEM: {"10014", "10015"},
    ActionName.BUY_FROM_STORE: set(),  # any non-operator NPC
}

# ── Location requirements per action ───────────────────────────────
LOCATION_REQUIREMENTS = {
    ActionName.PLANT: "BILL_FARM",
    ActionName.HARVEST: "BILL_FARM",
    ActionName.HUNT: "HUNTING_GATE",
    ActionName.COOK: "RESTAURANT",
    ActionName.CREATE_ORDER: None,  # any valid location
    ActionName.CANCEL_ORDER: None,
    ActionName.START_DELIVERY: None,  # validated against delivery_point_code
    ActionName.IMPORT_GOODS: "TRADE_ROAD",
    ActionName.EXPORT_GOODS: "TRADE_ROAD",
    ActionName.RATE_DISH: "RECORD_HALL",
    ActionName.PUBLISH_PRICE_INDEX: "RECORD_HALL",
    ActionName.AUDIT_PUBLIC_FUND: "RECORD_HALL",
    ActionName.EAT: None,  # any location
    ActionName.COMMUNITY_MEAL: "RESTAURANT",
    ActionName.PROPOSE_TRADE: None,  # runtime-validated: both NPCs colocated
    ActionName.RESPOND_TRADE: None,  # runtime-validated: both NPCs colocated
    ActionName.CANCEL_TRADE_OFFER: None,
    ActionName.LIST_STORE_ITEM: None,  # runtime-validated: operator at own store
    ActionName.UPDATE_STORE_PRICE: None,  # runtime-validated: operator at own store
    ActionName.DELIST_STORE_ITEM: None,  # runtime-validated: operator at own store
    ActionName.BUY_FROM_STORE: None,  # runtime-validated: buyer at target store
}

# ── Duration per action (world minutes) ────────────────────────────
ACTION_DURATION = {
    ActionName.PLANT: 60,
    ActionName.HARVEST: 90,
    ActionName.HUNT: 120,
    ActionName.COOK: 90,
    ActionName.CREATE_ORDER: 1,
    ActionName.CANCEL_ORDER: 1,
    ActionName.START_DELIVERY: 1,
    ActionName.IMPORT_GOODS: 120,
    ActionName.EXPORT_GOODS: 120,
    ActionName.RATE_DISH: 30,
    ActionName.PUBLISH_PRICE_INDEX: 30,
    ActionName.AUDIT_PUBLIC_FUND: 30,
    ActionName.EAT: 30,
    ActionName.COMMUNITY_MEAL: 30,
    ActionName.PROPOSE_TRADE: 1,
    ActionName.RESPOND_TRADE: 1,
    ActionName.CANCEL_TRADE_OFFER: 1,
    ActionName.LIST_STORE_ITEM: 1,
    ActionName.UPDATE_STORE_PRICE: 1,
    ActionName.DELIST_STORE_ITEM: 1,
    ActionName.BUY_FROM_STORE: 1,
}

# ── Param whitelists per action ────────────────────────────────────
PARAM_WHITELIST = {
    ActionName.PLANT: frozenset({"plot_code", "crop_code", "expected_asset_version"}),
    ActionName.HARVEST: frozenset({"plot_code", "expected_asset_version"}),
    ActionName.HUNT: frozenset({"hunt_code", "expected_asset_version"}),
    ActionName.COOK: frozenset({"recipe_id", "expected_asset_version"}),
    ActionName.CREATE_ORDER: frozenset({
        "side", "item_id", "amount", "unit_price",
        "delivery_point_code", "expires_world_minute",
        "expected_asset_version",
    }),
    ActionName.CANCEL_ORDER: frozenset({"order_id"}),
    ActionName.START_DELIVERY: frozenset({
        "delivery_id", "buyer_npc_id", "seller_npc_id",
        "delivery_point_code",
    }),
    ActionName.IMPORT_GOODS: frozenset({"item_id", "amount"}),
    ActionName.EXPORT_GOODS: frozenset({"item_id", "amount", "expected_asset_version"}),
    ActionName.RATE_DISH: frozenset({"owner_npc_id", "food_instance_id", "score"}),
    ActionName.PUBLISH_PRICE_INDEX: frozenset({"day_index"}),
    ActionName.AUDIT_PUBLIC_FUND: frozenset({"day_index"}),
    ActionName.EAT: frozenset({"source", "food_instance_id", "expected_asset_version"}),
    ActionName.COMMUNITY_MEAL: frozenset({"expected_asset_version"}),
    ActionName.PROPOSE_TRADE: frozenset({
        "target_npc_id", "item_id", "amount", "gold_amount",
        "conversation_id", "expected_seller_asset_version", "expires_world_minute",
    }),
    ActionName.RESPOND_TRADE: frozenset({"offer_id", "decision"}),
    ActionName.CANCEL_TRADE_OFFER: frozenset({"offer_id"}),
    ActionName.LIST_STORE_ITEM: frozenset({
        "store_code", "item_id", "amount", "price_mode", "custom_unit_price",
        "expected_operator_asset_version", "expected_store_inventory_version",
    }),
    ActionName.UPDATE_STORE_PRICE: frozenset({
        "listing_id", "price_mode", "custom_unit_price", "expected_store_inventory_version",
    }),
    ActionName.DELIST_STORE_ITEM: frozenset({
        "listing_id", "expected_operator_asset_version", "expected_store_inventory_version",
    }),
    ActionName.BUY_FROM_STORE: frozenset({
        "listing_id", "amount", "expected_store_inventory_version", "expected_buyer_asset_version",
    }),
}


# ── Canonical result code set (for tests / fixture) ───────────────
ALL_RESULT_CODES = frozenset(rc.value for rc in ResultCode)

# ── Canonical action name set (for tests / fixture) ───────────────
ALL_ACTION_NAMES = frozenset(an.value for an in ActionName)
