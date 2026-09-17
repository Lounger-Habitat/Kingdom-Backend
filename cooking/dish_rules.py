"""Shared, side-effect-free cooking and review arithmetic (RNG is explicit)."""
import random

QUALITY_COEFFICIENTS = {1: .3, 2: .7, 3: 1., 4: 1.5, 5: 2., 6: 3.}


def cook_time_bonus(actual, standard):
    difference = abs(actual - standard)
    return 2 if difference == 0 else 1 if difference <= 6 else 0 if difference <= 35 else -1


def calculate_quality(recipe, amounts, exact, seconds, rng=random):
    quality = recipe.default_dish_quality
    for ingredient in recipe.ingredients.all():
        count = amounts.get(ingredient.item_id, 0)
        quality -= 3 if count <= 0 else 1 if count != ingredient.count else 0
    roll = rng.randint(0, 99)
    quality += 0 if roll < 70 else -1 if roll < 88 else 1
    quality += cook_time_bonus(seconds, recipe.default_cook_time)
    return max(2 if exact else 1, min(6, quality))


def overall_score(scores, difficulty, rng=random):
    bonus = (difficulty - 1) * 5
    weighted = sum(scores[key] * weight for key, weight in
                   (("color", .2), ("aroma", .2), ("taste", .4), ("presentation", .2)))
    return round(max(0, min(100, bonus + weighted / 10 * (100 - bonus) + rng.uniform(-3, 3))) * 10) / 10


def default_value(item, quality):
    if quality not in QUALITY_COEFFICIENTS:
        raise ValueError("DISH_QUALITY_INVALID")
    return max(1, round(round(item.item_price * item.sell_percentage) * QUALITY_COEFFICIENTS[quality]))


def review_value(value, score):
    return max(1, round(value * (.6 + score / 100)))
