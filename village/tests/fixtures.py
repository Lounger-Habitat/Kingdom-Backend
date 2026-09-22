"""
阶段一：Backend 测试 fixture
与 Doc/村庄计划/fixtures/ 保持一致
"""

# 稳定 NPC ID
NPC_IDS = {
    "bill": "10011",
    "jack": "10012",
    "mark": "10013",
    "lucy": "10014",
    "nora": "10015",
}

# 稳定地点码
LOCATION_CODES = [
    "VILLAGE_SQUARE",
    "IRONSMITH",
    "JACK_HOME",
    "TAVERN",
    "SHOP",
    "LUXURY_VILLA",
    "BILL_HOME",
    "BUTCHER_YARD",
    "KITCHEN",
    "TASTING_ROOM",
    "LUCY_HOME",
    "RESTAURANT",
    "MARKET",
    "FOREST_EDGE",
    "RIVER_EAST",
]

# Command DTO fixture
COMMAND_FIXTURE = {
    "id": "550e8400-e29b-41d4-a716-446655440001",
    "timestamp": 1786200000000,
    "type": "message",
    "sender": "10011",
    "recipient": "env-server-001",
    "payload": {
        "type": "action",
        "content": {
            "schema_version": "village.command.v1",
            "id": "550e8400-e29b-41d4-a716-446655440002",
            "request_id": "550e8400-e29b-41d4-a716-446655440003",
            "name": "move",
            "params": {"location_code": "VILLAGE_SQUARE"},
            "expected_state_version": 17,
            "queue_mode": "STRICT",
            "not_before_world_minute": 12480,
            "expires_world_minute": 12540,
            "ttl_ms": 30000,
            "reason_code": "HUB_REQUEST",
        },
    },
}

# Outcome DTO fixture
OUTCOME_FIXTURE = {
    "id": "550e8400-e29b-41d4-a716-446655440010",
    "timestamp": 1786200060000,
    "type": "message",
    "sender": "env-server-001",
    "recipient": "10011",
    "payload": {
        "type": "event",
        "content": {
            "schema_version": "village.outcome.v1",
            "ref_id": "550e8400-e29b-41d4-a716-446655440002",
            "original_message_id": "550e8400-e29b-41d4-a716-446655440001",
            "request_id": "550e8400-e29b-41d4-a716-446655440003",
            "npc_id": "10011",
            "success": True,
            "result_code": "MOVE_COMPLETED",
            "message": "Bill 已到达村庄广场",
            "world_event_id": "550e8400-e29b-41d4-a716-446655440020",
            "world_minute": 12481,
            "time_revision": 5,
            "state_version": 18,
            "retryable": False,
            "effects": {},
            "data": {
                "location_code": "VILLAGE_SQUARE",
                "point_code": "VILLAGE_SQUARE",
                "action_status": "IDLE",
            },
        },
    },
}

# Event DTO fixture
EVENT_FIXTURE = {
    "id": "550e8400-e29b-41d4-a716-446655440030",
    "timestamp": 1786200060000,
    "type": "message",
    "sender": "env-server-001",
    "recipient": "10011",
    "payload": {
        "type": "event",
        "content": {
            "schema_version": "village.event.v1",
            "event_id": "550e8400-e29b-41d4-a716-446655440020",
            "event_sequence": 42,
            "event_type": "NPC_MOVED",
            "world_minute": 12481,
            "time_revision": 5,
            "actor_npc_id": "10011",
            "participant_npc_ids": ["10011"],
            "location_code": "VILLAGE_SQUARE",
            "point_code": "VILLAGE_SQUARE",
            "command_id": "550e8400-e29b-41d4-a716-446655440002",
            "request_id": "550e8400-e29b-41d4-a716-446655440003",
            "cause_event_id": None,
            "visibility": "PUBLIC",
            "public_status_code": "IDLE",
            "public_data": {},
            "summary": "Bill 到达村庄广场",
        },
    },
}

# VILLAGE_DELIVERY_ACK fixture
DELIVERY_ACK_FIXTURE = {
    "type": "message",
    "sender": "10011",
    "recipient": "env-server-001",
    "payload": {
        "type": "event",
        "content": {
            "id": "550e8400-e29b-41d4-a716-446655440040",
            "name": "VILLAGE_DELIVERY_ACK",
            "data": {
                "delivery_id": "550e8400-e29b-41d4-a716-446655440010",
            },
        },
    },
}

# 稳定结果码
SUCCESS_CODES = [
    "COMMAND_ACCEPTED",
    "COMMAND_REPLAYED",
    "MOVE_COMPLETED",
    "GO_HOME_COMPLETED",
    "COMMAND_CANCELLED_ON_RECOVERY",
]

FAILURE_CODES = [
    # 协议和身份
    "PROTOCOL_VERSION_UNSUPPORTED",
    "INVALID_ENVELOPE",
    "INVALID_COMMAND_SCHEMA",
    "SENDER_BINDING_INVALID",
    "WRONG_RECIPIENT",
    # ID 和幂等
    "MESSAGE_ID_CONFLICT",
    "COMMAND_ID_CONFLICT",
    "REQUEST_ID_CONFLICT",
    # TTL
    "MESSAGE_EXPIRED",
    "CLOCK_SKEW",
    # 动作和队列
    "UNKNOWN_ACTION",
    "ACTION_NOT_ALLOWED",
    "QUEUE_FULL",
    "NPC_BUSY",
    # 版本
    "STALE_STATE_VERSION",
    "STALE_TIME_REVISION",
    "WORLD_TIME_REGRESSION",
    # 执行
    "INVALID_LOCATION",
    "TARGET_UNREACHABLE",
    "ACTION_TIMEOUT",
    # 预约
    "RESERVATION_CONFLICT",
    # Backend
    "BACKEND_UNAVAILABLE",
    "CHECKPOINT_FIELD_NOT_ALLOWED",
    "BOOTSTRAP_NPC_SET_MISMATCH",
    # 阶段边界
    "ASSET_EFFECT_NOT_ALLOWED",
    # Outbox
    "OUTBOX_ACK_UNKNOWN",
    "OUTBOX_DELIVERY_FAILED",
]

# 阶段一白名单动作
ALLOWED_ACTIONS = ["MOVE", "GO_HOME"]

# 事件类型
EVENT_TYPES = [
    "NPC_MOVED",
    "NPC_ACTION_STARTED",
    "NPC_ACTION_COMPLETED",
    "NPC_ACTION_FAILED",
    "NPC_ACTION_RECOVERED",
    "DAY_CHANGED",
    "ENV_SERVER_RESTARTED",
]

# world_minute 计算常量
WORLD_EPOCH_ORDINAL = 1  # 0001-01-01 的 ordinal
MINUTES_PER_DAY = 1440
