"""Shared quality-only production and pricing; historical freshness is audit data."""
import math
import hashlib
import hmac
import random

RULE_VERSION = "quality-v2"
PRODUCTION_WEIGHTS = {
    1: (10, 30, 50, 10, 0, 0),
    4: (5, 15, 45, 30, 5, 0),
    7: (2, 8, 30, 45, 14, 1),
    10: (1, 4, 20, 50, 23, 2),
}
INGREDIENT_COEFFICIENTS = {1: .6, 2: .8, 3: 1., 4: 1.2, 5: 1.5, 6: 1.8}
DISH_COEFFICIENTS = {1: .3, 2: .7, 3: 1., 4: 1.5, 5: 2., 6: 3.}
INGREDIENT_TYPES = frozenset(("食材", "肉类", "蔬菜", "水果"))
NEUTRAL_INGREDIENT_IDS = frozenset((1049, 1050, 1058, 1076, 1099))


def quality_rng(identity, channel):
    """Stable server-private draws: choosing a client request UUID cannot choose its grade."""
    from django.conf import settings
    seed = hmac.new(settings.SECRET_KEY.encode(), f"{RULE_VERSION}:{channel}:{identity}".encode(), hashlib.sha256).digest()
    return random.Random(int.from_bytes(seed, "big"))


def valid_quality(value):
    return type(value) is int and 1 <= value <= 6


def has_quality(item):
    return bool(item and (item.item_type in INGREDIENT_TYPES or item.item_type == "食物"
                         or item.quality_enabled))


def production_quality(level, rng, bonus=0):
    tier = max(key for key in PRODUCTION_WEIGHTS if key <= max(1, min(10, level)))
    roll, total, quality = rng.random() * 100, 0, 6
    for quality, weight in enumerate(PRODUCTION_WEIGHTS[tier], 1):
        total += weight
        if roll < total:
            break
    if quality < 6 and rng.random() < max(0, min(1, bonus)):
        quality += 1
    return quality


def player_skill_level(player, skill):
    from player.progression import growth_snapshot
    return next(row["level"] for row in growth_snapshot(player, read_only=True)["skills"] if row["code"] == skill)


def ingredient_average(rows):
    count = total = 0
    for row in rows:
        if row["item_id"] in NEUTRAL_INGREDIENT_IDS or row.get("item_type") == "调料类":
            continue
        amount = row.get("selected_quantity", row.get("quantity", row.get("amount", 0)))
        if not amount:
            continue
        if not valid_quality(row.get("quality")):
            raise ValueError("INGREDIENT_QUALITY_INVALID")
        total += row["quality"] * amount
        count += amount
    return total / count if count else 3


def cooking_quality(base, average, level, tool_bonus, rng):
    score = max(1, min(6, base + .6 * (average - 3)))
    quality = math.floor(score)
    if rng.random() < score - quality:
        quality += 1
    chance = min(.35, .02 * (max(1, level) - 1) + max(0, tool_bonus))
    return min(6, quality + int(quality < 6 and rng.random() < chance))


def quality_price(base, quality, *, dish=False):
    if not valid_quality(quality):
        raise ValueError("DISH_QUALITY_INVALID" if dish else "ITEM_QUALITY_INVALID")
    coefficients = DISH_COEFFICIENTS if dish else INGREDIENT_COEFFICIENTS
    return max(1, math.floor(base * coefficients[quality] + .5))


def item_price(item, quality, base=None):
    base = item.item_price if base is None else base
    return quality_price(base, quality, dish=item.item_type == "食物") if has_quality(item) else base
