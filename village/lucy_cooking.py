"""Lucy production, owned samples, and Mark's structured Agent/LLM replies."""
import copy
import math
import uuid
from types import SimpleNamespace

from django.db import transaction
from inventory.models import NpcGlobalInventory, NpcGlobalInventorySlot
from items.models import Item
from recipes.models import Recipe
from cooking.dish_rules import calculate_quality, default_value, overall_score, review_value
from .lucy_catalog import RELIEF_PACK
from .models import NpcCookingCounter, VillageDishReview, VillageAccount
from .phase3_asset_service import (_ok, _fail, add_batch, consume_fefo, get_available_slot_amount,
                                  deterministic_rng, BATCH_ITEM_ATTRIBUTES)
from .phase3_catalog import RECIPE_WHITELIST, DISH_SPOILAGE_MINUTES
from .public_finance import mark_unavailable


def _pity(npc, quality, seconds, standard, rng):
    from cooking.models import PityConfig, _default_config
    config = PityConfig.objects.filter(pk=1).first() or SimpleNamespace(**_default_config())
    counter, _ = NpcCookingCounter.objects.select_for_update().get_or_create(npc=npc)
    for tier in (4, 5, 6):
        setattr(counter, f"count_{tier}", getattr(counter, f"count_{tier}") + 1)
    for tier in (6, 5, 4):
        index = getattr(counter, f"count_{tier}") - getattr(config, f"quality_{tier}_normal_phase")
        chances = getattr(config, f"quality_{tier}_ramp_probabilities")
        if quality < tier and index >= 0 and (index >= len(chances) or rng.randint(1, 100) <= chances[index]):
            quality = tier
            radius = getattr(config, f"quality_{tier}_time_range")
            seconds = max(0, standard + (rng.randint(-radius, radius) if radius else 0))
            break
    for tier in (4, 5, 6):
        if quality >= tier:
            setattr(counter, f"count_{tier}", 0)
    counter.save()
    return quality, seconds


def _missing_ingredients(inventory, recipe, minute):
    missing = []
    for ingredient in recipe.ingredients.all():
        slots = inventory.slots.select_for_update().filter(item_id=ingredient.item_id, item_amount__gt=0)
        available = sum(get_available_slot_amount(s, minute) for s in slots)
        if available < ingredient.count:
            missing.append(dict(item_id=ingredient.item_id, item_name=ingredient.item_name,
                required=ingredient.count, available=available, shortage=ingredient.count - available))
    if missing:
        return _fail("P3_INSUFFICIENT_ITEM", data={"recipe_id": recipe.pk,
            "recipe_name": recipe.recipe_name, "missing_items": missing,
            "items_consumed": [], "failure_kind": "MISSING_INGREDIENTS",
            "retry_condition": "所列缺料均补齐为本人背包中的可用材料后，才可重试此配方。",
            "instruction": "材料未补齐时不要重复调用同一配方；可采购缺料、选择材料足够的其他配方，或等待补货。仅时间流逝不会补齐材料。"})
    return None


@transaction.atomic
def cook_preflight(npc_id, params, minute):
    """Reject unavailable recipes before starting the timed physical action."""
    if str(npc_id) != "10014":
        return _fail("P3_FORBIDDEN_ACTOR")
    recipe_id = params.get("recipe_id")
    if type(recipe_id) is not int or recipe_id not in RECIPE_WHITELIST:
        return _fail("P3_RECIPE_NOT_ALLOWED")
    recipe = Recipe.objects.prefetch_related("ingredients").filter(pk=recipe_id).first()
    if recipe is None or not recipe.ingredients.all() or recipe.output_quantity < 1 or not Item.objects.filter(
        item_id=recipe.item_id, is_deleted=False,
    ).exists():
        return _fail("P3_RECIPE_NOT_ALLOWED")
    inventory = NpcGlobalInventory.objects.select_for_update().get(npc__agent_id=str(npc_id))
    failure = _missing_ingredients(inventory, recipe, minute)
    if failure:
        failure["data"]["tool_error"] = {"executed": False, "automatic_retry_safe": False}
    return failure


def cook(npc_id, params, minute, request_id):
    if str(npc_id) != "10014":
        return _fail("P3_FORBIDDEN_ACTOR")
    if type(params.get("recipe_id")) is not int or params["recipe_id"] not in RECIPE_WHITELIST:
        return _fail("P3_RECIPE_NOT_ALLOWED", data={"invalid_field": "recipe_id",
            "received_value": params.get("recipe_id"), "query_action": "GET_RULE",
            "instruction": "菜谱编号无效。使用餐厅查询实际返回的recipe_id，不按名称猜编号。"})
    with transaction.atomic():
        inventory = NpcGlobalInventory.objects.select_for_update().get(npc__agent_id=str(npc_id))
        if params.get("expected_asset_version", inventory.asset_version) != inventory.asset_version:
            return _fail("P3_ASSET_VERSION_CONFLICT")
        recipe = Recipe.objects.prefetch_related("ingredients").filter(pk=params["recipe_id"]).first()
        ingredients = list(recipe.ingredients.all()) if recipe else []
        if not ingredients or recipe.output_quantity < 1:
            return _fail("P3_RECIPE_NOT_ALLOWED")
        item = Item.objects.filter(item_id=recipe.item_id, is_deleted=False).first()
        if item is None:
            return _fail("P3_RECIPE_NOT_ALLOWED")
        # Recheck at settlement: ingredients may have expired or been used since start.
        failure = _missing_ingredients(inventory, recipe, minute)
        if failure:
            return failure
        inputs = []
        for ingredient in ingredients:
            try:
                consumed = consume_fefo(npc_id, (ingredient.item_id,), ingredient.count, minute)
            except ValueError:
                transaction.set_rollback(True)
                return _fail("P3_INSUFFICIENT_ITEM")
            inputs.append(dict(item_id=ingredient.item_id, name=ingredient.item_name, amount=ingredient.count,
                batches=[dict(instance_id=str(s.instance_id), amount=n, produced_world_minute=s.produced_world_minute,
                              expires_world_minute=s.expires_world_minute) for s, n in consumed]))
        rng = deterministic_rng(request_id, "LUCY_COOK_V1", minute // 1440)
        cold = recipe.item_id == RELIEF_PACK
        seconds = 0 if cold else max(0, recipe.default_cook_time + rng.randint(-40, 40))
        quality = 3 if cold else calculate_quality(recipe, {i.item_id: i.count for i in ingredients}, True, seconds, rng)
        if not cold:
            quality, seconds = _pity(inventory.npc, quality, seconds, recipe.default_cook_time, rng)
        recipe_info = {key: getattr(recipe, key) for key in ("cuisine", "dish_category", "serving_style",
                       "taste_profile", "difficulty", "default_cook_time", "version")}
        metadata = dict(rule_version="lucy-dish-v1", maker_npc_id=str(npc_id), recipe_id=recipe.pk,
            recipe_name=recipe.recipe_name, recipe=recipe_info, ingredients=inputs, base_quality=3,
            cook_time_source="NO_HEAT" if cold else "RECIPE_RANDOM_SECONDS", production_request_id=str(request_id))
        slot = add_batch(inventory, recipe.item_id, recipe.output_quantity, minute,
            minute + DISH_SPOILAGE_MINUTES, request_id, item_attributes=dict(dish_metadata=metadata,
            dish_quality=quality, cook_time=seconds, ingredient="、".join(f"{i.item_name}×{i.count}" for i in ingredients),
            item_bag_name=recipe.recipe_name, rated=False, rating_price=-1))
        inventory.asset_version += 1
        inventory.save(update_fields=["asset_version", "updated_at"])
        output = dict(instance_id=str(slot.instance_id), item_id=slot.item_id, amount=slot.item_amount,
            name=item.item_name,
            dish_quality=quality, cook_time_seconds=seconds, dish_metadata=metadata,
            default_value=None if cold else default_value(item, quality), expires_world_minute=slot.expires_world_minute)
    return _ok(asset_versions={str(npc_id): inventory.asset_version},
        effects={"items_consumed": inputs, "items_added": [output]}, data={"dish": output}, external_delta=0)


def reservation(slot, minute):
    rows = VillageDishReview.objects.filter(slot=slot, status="PENDING")
    if minute is not None:
        rows = rows.filter(expires_world_minute__gt=minute)
    return rows.count()


def _id(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def fixed_price(item_id, attributes):
    if item_id == RELIEF_PACK or not Recipe.objects.filter(pk__in=RECIPE_WHITELIST, item_id=item_id).exists():
        raise ValueError("RESTAURANT_DISH_REQUIRED")
    item = Item.objects.filter(item_id=item_id, is_deleted=False).first()
    if item is None:
        raise ValueError("RESTAURANT_DISH_REQUIRED")
    if attributes.get("rated"):
        review_id = _id((attributes.get("dish_metadata") or {}).get("review_id"))
        price = attributes.get("rating_price")
        if type(price) is not int or price <= 0 or not review_id or not VillageDishReview.objects.filter(pk=review_id, status="COMPLETED").exists():
            raise ValueError("DISH_REVIEW_INCOMPLETE")
        return price
    return default_value(item, attributes.get("dish_quality", 3))


def request_review(npc_id, params, minute, request_id):
    if str(npc_id) != "10014":
        return _fail("P3_FORBIDDEN_ACTOR")
    error = mark_unavailable(minute)
    if error:
        return _fail(error)
    inventory = NpcGlobalInventory.objects.select_for_update().get(npc__agent_id=str(npc_id))
    slot = inventory.slots.select_for_update().filter(instance_id=_id(params.get("food_instance_id"))).first()
    if not slot or get_available_slot_amount(slot, minute) < 1:
        return _fail("P3_INSUFFICIENT_ITEM", data={"food_instance_id": str(params.get("food_instance_id", "")),
            "required_amount": 1, "available_amount": get_available_slot_amount(slot, minute) if slot else 0})
    if slot.item_id == RELIEF_PACK or not Recipe.objects.filter(pk__in=RECIPE_WHITELIST, item_id=slot.item_id).exists():
        return _fail("RESTAURANT_DISH_REQUIRED")
    if slot.rated and (slot.dish_metadata or {}).get("review_id"):
        return _fail("DISH_ALREADY_REVIEWED")
    if slot.item_amount > 1:
        slot.item_amount -= 1
        slot.save(update_fields=["item_amount"])
        slot = add_batch(inventory, slot.item_id, 1, slot.produced_world_minute, slot.expires_world_minute,
            request_id, item_attributes={k: copy.deepcopy(getattr(slot, k)) for k in BATCH_ITEM_ATTRIBUTES})
    snapshot = dict(instance_id=str(slot.instance_id), item_id=slot.item_id,
        name=Item.objects.get(item_id=slot.item_id).item_name, quality=slot.dish_quality,
        cook_time_seconds=slot.cook_time, ingredient=slot.ingredient, metadata=copy.deepcopy(slot.dish_metadata),
        historical_information_incomplete=not bool(slot.dish_metadata.get("recipe")))
    expiry = min(minute + 180, slot.expires_world_minute) if slot.expires_world_minute is not None else minute + 180
    row = VillageDishReview.objects.create(owner=inventory.npc, slot=slot, request_id=request_id,
        snapshot=snapshot, created_world_minute=minute, expires_world_minute=expiry)
    inventory.asset_version += 1
    inventory.save(update_fields=["asset_version", "updated_at"])
    data = {"review_id": str(row.pk), "status": row.status, "reserved_amount": 1, "target_npc_id": "10013", "dish": snapshot, "expires_world_minute": expiry,
            "next_action": "Mark使用INSPECT_RATEABLE_DISHES读取真实菜品，以LLM完成四项1–10分及评语，再RATE_DISH回传。"}
    return _ok(data=data, effects={"dish_review_requested": data}, participant_npc_ids=["10014", "10013"],
               event_visibility="PARTICIPANTS", external_delta=0, asset_versions={str(npc_id): inventory.asset_version})


def cancel_review(npc_id, params, minute, request_id):
    row = VillageDishReview.objects.select_for_update().filter(pk=_id(params.get("review_id")), owner__agent_id=str(npc_id)).first()
    if not row or row.status != "PENDING":
        return _fail("DISH_REVIEW_NOT_PENDING", data={"review_id": str(params.get("review_id", "")),
            "review_status": row.status if row else "NOT_FOUND"})
    released_amount = int(row.expires_world_minute > minute)
    row.status = "CANCELLED"
    row.save(update_fields=["status"])
    inventory = NpcGlobalInventory.objects.select_for_update().get(npc=row.owner)
    inventory.asset_version += 1
    inventory.save(update_fields=["asset_version", "updated_at"])
    return _ok(data={"review_id": str(row.pk), "status": row.status, "food_instance_id": str(row.slot.instance_id),
        "item_id": row.slot.item_id, "released_amount": released_amount, "available_amount": get_available_slot_amount(row.slot, minute)}, external_delta=0,
               event_visibility="PARTICIPANTS", participant_npc_ids=["10014", "10013"],
               asset_versions={str(npc_id): inventory.asset_version})


def rate_dish(npc_id, params, minute, request_id):
    if str(npc_id) != "10013":
        return _fail("P3_FORBIDDEN_ACTOR")
    error = mark_unavailable(minute)
    if error:
        return _fail(error)
    scores, comment = params.get("scores"), params.get("comment")
    keys = {"color", "aroma", "taste", "presentation"}
    if not isinstance(scores, dict) or set(scores) != keys or any(type(x) not in (int, float) or not math.isfinite(x) or not 1 <= x <= 10 for x in scores.values()):
        return _fail("DISH_REVIEW_INVALID_SCORES", data={"invalid_field": "scores",
            "allowed_values": {key: "1–10的有限数值" for key in sorted(keys)}})
    if not isinstance(comment, str) or not comment.strip() or len(comment) > 2000:
        return _fail("DISH_REVIEW_INVALID_COMMENT", data={"invalid_field": "comment", "allowed_values": "1–2000字非空评语"})
    feedback = params.get("customer_feedback", "")
    if not isinstance(feedback, str) or len(feedback) > 2000:
        return _fail("DISH_REVIEW_INVALID_COMMENT")
    row = VillageDishReview.objects.select_for_update().filter(pk=_id(params.get("review_id")), target_npc_id=str(npc_id)).first()
    if not row or row.status != "PENDING":
        return _fail("DISH_REVIEW_NOT_PENDING", data={"review_id": str(params.get("review_id", "")),
            "review_status": row.status if row else "NOT_FOUND"})
    if row.expires_world_minute <= minute:
        return _fail("DISH_REVIEW_EXPIRED", data={"review_id": str(row.pk), "expires_world_minute": row.expires_world_minute})
    inventory = NpcGlobalInventory.objects.select_for_update().get(npc=row.owner)
    slot = NpcGlobalInventorySlot.objects.select_for_update().get(pk=row.slot_id)
    if slot.bag_id != inventory.pk or slot.item_amount != 1 or slot.freshness_unknown or (slot.expires_world_minute is not None and slot.expires_world_minute <= minute):
        return _fail("DISH_REVIEW_SAMPLE_UNAVAILABLE")
    difficulty = ((row.snapshot.get("metadata") or {}).get("recipe") or {}).get("difficulty", 1)
    score = overall_score(scores, difficulty, deterministic_rng(row.request_id, "DISH_REVIEW_V1", row.created_world_minute // 1440))
    item = Item.objects.filter(item_id=slot.item_id, is_deleted=False).first()
    if item is None:
        return _fail("DISH_REVIEW_SAMPLE_UNAVAILABLE")
    value = default_value(item, slot.dish_quality)
    slot.rated, slot.overall_score, slot.rating_price, slot.review_comment = True, score, review_value(value, score), comment
    slot.dish_metadata = dict(slot.dish_metadata, review_id=str(row.pk), scores=scores, price_rule="score-v1",
                              review_source="MARK_AGENT_LLM", customer_feedback=feedback)
    slot.save(update_fields=["rated", "overall_score", "rating_price", "review_comment", "dish_metadata"])
    row.status, row.scores, row.comment, row.customer_feedback = "COMPLETED", scores, comment, feedback
    row.completed_world_minute, row.completed_request_id = minute, request_id
    row.save()
    inventory.asset_version += 1
    inventory.save(update_fields=["asset_version", "updated_at"])
    changes = {"dish_rated": dict(review_id=str(row.pk), status=row.status, item_id=slot.item_id, food_instance_id=str(slot.instance_id),
        overall_score=score, rating_price=slot.rating_price, scores=scores, comment=comment)}
    return _ok(data=changes["dish_rated"], effects=changes, participant_npc_ids=["10013", str(row.owner.agent_id)], event_visibility="PARTICIPANTS",
        external_delta=0, asset_versions={str(row.owner.agent_id): inventory.asset_version})


def pending_reviews(npc, minute, review_id=None):
    rows = VillageDishReview.objects.select_related("owner", "slot")
    rows = rows.filter(target_npc_id="10013") if str(npc.agent_id) == "10013" else rows.filter(owner=npc)
    if review_id is not None:
        row = rows.filter(pk=_id(review_id)).first()
        if row is None:
            return {"filter": {"review_id": str(review_id)}, "reviews": [], "total_count": 0,
                    "has_more": False, "review_status": "NOT_FOUND"}
        status = "EXPIRED" if row.status == "PENDING" and row.expires_world_minute <= minute else row.status
        review = dict(review_id=str(row.pk), status=status, owner_npc_id=str(row.owner.agent_id),
                      dish=row.snapshot, expires_world_minute=row.expires_world_minute)
        if status == "COMPLETED":
            # Read the original receipt, not a sample that could have been consumed since rating.
            from .models import VillageActionReceipt
            receipt = VillageActionReceipt.objects.filter(request_id=row.completed_request_id).first()
            result = (receipt.response_data or {}).get("data", {}) if receipt else {}
            review.update(completed_world_minute=row.completed_world_minute)
            for key in ("overall_score", "rating_price", "food_instance_id", "item_id", "item_name"):
                if key in result:
                    review[key] = result[key]
        return {"filter": {"review_id": str(review_id)}, "reviews": [review], "total_count": 1, "has_more": False}
    rows = rows.filter(status="PENDING", expires_world_minute__gt=minute)
    reviews = [dict(review_id=str(r.pk), owner_npc_id=str(r.owner.agent_id), dish=r.snapshot,
        expires_world_minute=r.expires_world_minute, response_action="RATE_DISH",
        response_fields={"scores": {k: "1–10" for k in ("color", "aroma", "taste", "presentation")}, "comment": "必填评语"}) for r in rows]
    return {"filter": {"review_status": "PENDING_UNEXPIRED"}, "reviews": reviews,
            "total_count": len(reviews), "has_more": False}
