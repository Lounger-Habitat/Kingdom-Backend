"""Persistent private dish samples. Only Mark's trusted NPC command can rate."""
import copy
import math
import uuid

from django.db import transaction

from account.models import GameAccount
from cooking.dish_rules import default_value, overall_score, review_value
from inventory.components import BATCH_COPY_FIELDS, copy_batch_attributes
from inventory.models import InventoryBag, InventorySlot
from items.models import Item, ItemType
from player.models import PlayerGrowthState
from player.progression import award_skill, ensure_growth
from .models import NpcRuntimeState, VillagePlayerDishReview, VillageWorldEvent


class ReviewError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def _fail(code, message):
    raise ReviewError(code, message)


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        _fail("INVALID_PARAMS", "样品或点评编号无效。")


def _mark_reason(minute):
    # Keep this query read-only, including when no runtime row exists.
    runtime = NpcRuntimeState.objects.filter(npc__agent_id="10013").first()
    if runtime is None:
        return "STORY_FORBIDDEN_ACTOR"
    if runtime.action_status == "AWAY" or (runtime.away_until_world_minute is not None
                                            and runtime.away_until_world_minute > minute):
        return "NPC_AWAY"
    if runtime.health_fp <= 300 or runtime.action_status == "SLEEPING":
        return "MARK_UNAVAILABLE"
    return ""


def _sample_reason(slot, item, minute):
    if not item or item.is_deleted or item.item_type != ItemType.FOOD or slot.item_id in (1078, 29241):
        return "RESTAURANT_DISH_REQUIRED"
    if slot.item_amount < 1:
        return "DISH_REVIEW_SAMPLE_UNAVAILABLE"
    if slot.rated:
        return "DISH_ALREADY_REVIEWED"
    if not isinstance(slot.dish_metadata, dict):
        return "DISH_METADATA_INVALID"
    if slot.freshness_unknown:
        return "DISH_FRESHNESS_UNKNOWN"
    if slot.expires_world_minute is not None and slot.expires_world_minute <= minute:
        return "DISH_REVIEW_SAMPLE_EXPIRED"
    if type(slot.dish_quality) is not int or slot.dish_quality not in range(1, 7):
        return "DISH_QUALITY_INVALID"
    return ""


def _dish(slot, item=None, *, own=False):
    return dict(instance_id=str(slot.instance_id), item_id=slot.item_id,
        item_name=item.item_name if item else slot.item_bag_name, amount=slot.item_amount,
        dish_quality=slot.dish_quality, quality=slot.quality, rated=slot.rated,
        overall_score=slot.overall_score, rating_price=slot.rating_price,
        ingredient=slot.ingredient, cook_time=slot.cook_time,
        produced_world_minute=slot.produced_world_minute, expires_world_minute=slot.expires_world_minute,
        freshness_unknown=slot.freshness_unknown, own_production_verified=own)


def _status(row, minute):
    return "EXPIRED" if row.status == "PENDING" and row.expires_world_minute <= minute else row.status


def _entry(row, minute):
    status = _status(row, minute)
    dish = _dish(row.sample_slot, own=row.own_production_verified)
    dish.update(item_name=row.snapshot.get("item_name", dish["item_name"]), amount=1,
                rated=status == "COMPLETED", overall_score=row.overall_score, rating_price=row.rating_price)
    return dict(review_id=str(row.pk), status=status, dish=dish,
        scores=copy.deepcopy(row.scores), overall_score=row.overall_score, comment=row.comment,
        rating_price=row.rating_price, expires_world_minute=row.expires_world_minute,
        created_world_minute=row.created_world_minute, completed_world_minute=row.completed_world_minute,
        claimed_world_minute=row.claimed_world_minute,
        own_production_verified=row.own_production_verified,
        xp_reward=(row.growth_reward or {}).get("xp_gained", 0),
        can_cancel=status == "PENDING" and row.claimed_world_minute is None,
        can_claim=status in ("COMPLETED", "CANCELLED", "EXPIRED") and row.claimed_world_minute is None)


def review_overview(player, bag, minute, read_only=False):
    """Observation never creates growth, closes expiry, or returns held items."""
    rows = list(VillagePlayerDishReview.objects.filter(player=player)
                .select_related("sample_slot").order_by("-created_world_minute", "review_id"))
    reason = _mark_reason(minute)
    pending = any(_status(row, minute) == "PENDING" for row in rows)
    samples = []
    if bag.pk and bag.player_id == player.pk and bag.character_id == player.username:
        slots = list(bag.slots.filter(item_amount__gt=0).order_by("slot_index"))
        items = Item.objects.in_bulk({slot.item_id for slot in slots})
        for slot in slots:
            item = items.get(slot.item_id)
            if not item or item.item_type != ItemType.FOOD:
                continue
            blocked = _sample_reason(slot, item, minute) or ("DISH_REVIEW_PENDING" if pending else reason)
            sample = _dish(slot, item)
            sample.update(eligible=not blocked and not read_only, reason="MODE_READ_ONLY" if read_only and not blocked else blocked)
            samples.append(sample)
    return dict(revision=PlayerGrowthState.objects.filter(player=player).values_list("revision", flat=True).first() or 0,
        observed_world_minute=minute,
        mark_available=not reason, unavailable_reason=reason,
        entries=[_entry(row, minute) for row in rows], samples=samples)


def _bump(player):
    state = ensure_growth(player)
    state.revision += 1
    state.save(update_fields=["revision"])


def _next_index(bag):
    # Main bags can contain empty slots and non-contiguous indices.
    return max(bag.slots.values_list("slot_index", flat=True), default=-1) + 1


def _snapshot(slot, item):
    attrs = {name: copy.deepcopy(getattr(slot, name)) for name in BATCH_COPY_FIELDS}
    if attrs["source_request_id"] is not None:
        attrs["source_request_id"] = str(attrs["source_request_id"])
    return dict(item_id=slot.item_id, item_name=item.item_name, item_bag_name=slot.item_bag_name,
                attributes=attrs)


def _held_slot(row):
    slot = InventorySlot.objects.select_for_update().select_related("bag").get(pk=row.sample_slot_id)
    if (slot.bag.player_id != row.player_id or slot.bag.character_id != f"dish-review:{row.pk}"
            or slot.item_amount != 1 or slot.item_id != row.snapshot.get("item_id")):
        _fail("DISH_REVIEW_SAMPLE_UNAVAILABLE", "托管样品不完整，请刷新状态。")
    return slot


def _notify_mark(row, clock, request_id):
    from .phase4_visibility import enqueue_event_deliveries
    event = VillageWorldEvent.objects.create(dedupe_key=f"player-review:{row.pk}:{row.status}",
        event_type="PLAYER_DISH_REVIEW_" + ("REQUESTED" if row.status == "PENDING" else row.status),
        world_minute=clock.world_minute, time_revision=clock.time_revision, request_id=request_id,
        participant_npc_ids=["10013"], visibility="PARTICIPANTS", location_code="RECORD_HALL",
        private_data=_mark_entry(row, clock.world_minute),
        hub_summary="玩家送来一份真实托管菜品，请查询INSPECT_RATEABLE_DISHES后正式点评。" if row.status == "PENDING" else "玩家取消了一份送评样品，该review_id不再接受评分。")
    enqueue_event_deliveries(event)


@transaction.atomic
def execute_review_action(player, bag, action, params, rid, clock):
    minute = clock.world_minute
    GameAccount.objects.select_for_update().get(pk=player.pk)
    if not bag.pk or bag.player_id != player.pk or bag.character_id != player.username:
        _fail("PLAYER_BAG_REQUIRED", "请使用本人的主背包。")
    rid = _uuid(rid)
    if action == "request_dish_review":
        if set(params) != {"instance_id"}:
            _fail("INVALID_PARAMS", "请选择一份真实菜品。")
        source_id = _uuid(params.get("instance_id"))
        old = VillagePlayerDishReview.objects.select_related("sample_slot").filter(player=player, request_id=rid).first()
        if old:
            if old.source_instance_id != source_id:
                _fail("REQUEST_CONFLICT", "同一请求不能更换样品。")
            return dict(review=_entry(old, minute))
        reason = _mark_reason(minute)
        if reason:
            _fail(reason, "Mark目前无法接受点评，请稍后再来。")
        pending = VillagePlayerDishReview.objects.select_for_update().filter(player=player, status="PENDING")
        pending.filter(expires_world_minute__lte=minute).update(status="EXPIRED")
        if pending.exists():
            _fail("DISH_REVIEW_PENDING", "已有一份待评样品，请先等待或取消。")
        slot = bag.slots.select_for_update().filter(instance_id=source_id, item_amount__gt=0).first()
        if not slot:
            _fail("DISH_REVIEW_SAMPLE_UNAVAILABLE", "本人主背包中没有这份样品。")
        item = Item.objects.filter(pk=slot.item_id).first()
        reason = _sample_reason(slot, item, minute)
        if reason:
            _fail(reason, "此样品当前无法送评，请检查鲜期和点评状态。")
        review_id = uuid.uuid4()
        escrow = InventoryBag.objects.create(player=player, character_id=f"dish-review:{review_id}")
        held = InventorySlot(bag=escrow, slot_index=0, item_id=slot.item_id,
                             item_amount=1, item_bag_name=slot.item_bag_name, hide_in_shop=slot.hide_in_shop)
        copy_batch_attributes(slot, held)
        held.save()
        row = VillagePlayerDishReview.objects.create(review_id=review_id, player=player, sample_slot=held,
            source_instance_id=source_id, request_id=rid, snapshot=_snapshot(slot, item),
            created_world_minute=minute,
            expires_world_minute=min(minute + 180, slot.expires_world_minute) if slot.expires_world_minute is not None else minute + 180)
        slot.item_amount -= 1
        slot.save(update_fields=["item_amount"])
        _bump(player)
        _notify_mark(row, clock, rid)
        return dict(review=_entry(row, minute))
    if action not in ("cancel_dish_review", "claim_dish_review") or set(params) != {"review_id"}:
        _fail("INVALID_PARAMS", "点评动作参数无效。")
    row = VillagePlayerDishReview.objects.select_for_update().select_related("sample_slot").filter(
        pk=_uuid(params.get("review_id")), player=player).first()
    if row is None:
        _fail("DISH_REVIEW_NOT_FOUND", "没有这份本人的点评记录。")
    status = _status(row, minute)
    if action == "cancel_dish_review":
        if status != "PENDING":
            _fail("DISH_REVIEW_NOT_PENDING", "这份点评已结束，可以查看或领取样品。")
        row.status, row.cancelled_world_minute, row.cancel_request_id = "CANCELLED", minute, rid
        row.save(update_fields=["status", "cancelled_world_minute", "cancel_request_id"])
        _notify_mark(row, clock, rid)
    else:
        if row.claimed_world_minute is not None:
            _fail("DISH_REVIEW_ALREADY_CLAIMED", "这份样品已领回。")
        if status == "PENDING":
            _fail("DISH_REVIEW_NOT_READY", "样品正在等待Mark点评，可先取消再领取。")
        held = _held_slot(row)
        returned = InventorySlot(bag=bag, slot_index=_next_index(bag), item_id=held.item_id,
            item_amount=1, item_bag_name=held.item_bag_name, hide_in_shop=held.hide_in_shop)
        copy_batch_attributes(held, returned)
        returned.save()
        held.item_amount = 0
        held.save(update_fields=["item_amount"])
        row.status, row.claimed_world_minute, row.claim_request_id = status, minute, rid
        row.claimed_instance_id = returned.instance_id
        row.save(update_fields=["status", "claimed_world_minute", "claim_request_id", "claimed_instance_id"])
    _bump(player)
    return dict(review=_entry(row, minute), returned_instance_id=str(row.claimed_instance_id) if row.claimed_instance_id else None)


def _mark_entry(row, minute):
    """Only Mark receives the recipe context; source batch IDs stay private."""
    attrs = row.snapshot.get("attributes", {})
    metadata = attrs.get("dish_metadata") or {}
    recipe = metadata.get("recipe") if isinstance(metadata.get("recipe"), dict) else {}
    ingredients = metadata.get("ingredients") if isinstance(metadata.get("ingredients"), list) else []
    inputs = [dict(item_id=i.get("item_id"), name=i.get("name", i.get("item_name")), amount=i.get("amount", i.get("quantity")))
              for i in ingredients if isinstance(i, dict)]
    dish = dict(item_id=row.snapshot.get("item_id"), name=row.snapshot.get("item_name"),
        quality=attrs.get("quality", 3), cook_time_seconds=attrs.get("cook_time", 0),
        ingredient=attrs.get("ingredient", ""), recipe=copy.deepcopy(recipe), ingredients=inputs,
        historical_information_incomplete=not bool(recipe))
    return dict(review_id=str(row.pk), owner_type="PLAYER", status=_status(row, minute), dish=dish,
        expires_world_minute=row.expires_world_minute, response_action="RATE_DISH",
        response_fields=dict(scores={k: "1–10" for k in ("color", "aroma", "taste", "presentation")}, comment="必填评语"),
        scores=copy.deepcopy(row.scores), overall_score=row.overall_score, rating_price=row.rating_price,
        comment=row.comment, completed_world_minute=row.completed_world_minute)


def mark_player_reviews(npc, minute, review_id=None):
    if str(npc.agent_id) != "10013":
        return []
    rows = VillagePlayerDishReview.objects.all()
    if review_id is not None:
        try:
            key = uuid.UUID(str(review_id))
        except (ValueError, TypeError, AttributeError):
            return []
        rows = rows.filter(pk=key)
    else:
        rows = rows.filter(status="PENDING", expires_world_minute__gt=minute)
    return [_mark_entry(row, minute) for row in rows.order_by("created_world_minute", "review_id")]


@transaction.atomic
def complete_player_review(npc_id, params, minute, request_id):
    """Internal adapter for RATE_DISH, never exposed as a player action."""
    if str(npc_id) != "10013":
        _fail("P3_FORBIDDEN_ACTOR", "只有Mark可以正式点评。")
    reason = _mark_reason(minute)
    if reason:
        _fail(reason, "Mark目前无法点评。")
    scores, comment = params.get("scores"), params.get("comment")
    if (not isinstance(scores, dict) or set(scores) != {"color", "aroma", "taste", "presentation"}
            or any(type(v) not in (int, float) or not math.isfinite(v) or not 1 <= v <= 10 for v in scores.values())):
        _fail("DISH_REVIEW_INVALID_SCORES", "四项评分都须为1至10的有限数值。")
    feedback = params.get("customer_feedback", "")
    if not isinstance(comment, str) or not comment.strip() or len(comment) > 2000 or not isinstance(feedback, str) or len(feedback) > 2000:
        _fail("DISH_REVIEW_INVALID_COMMENT", "请填写1至2000字的正式评语。")
    row = VillagePlayerDishReview.objects.filter(pk=_uuid(params.get("review_id"))).first()
    if row is None:
        _fail("DISH_REVIEW_NOT_FOUND", "点评记录不存在。")
    player = GameAccount.objects.select_for_update().get(pk=row.player_id)
    row = VillagePlayerDishReview.objects.select_for_update().get(pk=row.pk)
    if _status(row, minute) == "EXPIRED":
        _fail("DISH_REVIEW_EXPIRED", "样品已过送评期限。")
    if row.status != "PENDING" or row.claimed_world_minute is not None:
        _fail("DISH_REVIEW_NOT_PENDING", "该样品已结束点评。")
    slot = _held_slot(row)
    item = Item.objects.filter(pk=slot.item_id).first()
    if _sample_reason(slot, item, minute):
        _fail("DISH_REVIEW_SAMPLE_UNAVAILABLE", "样品已不可点评。")
    from .player_cooking import is_player_cooked
    from .phase3_asset_service import deterministic_rng
    own = is_player_cooked(slot, player)
    recipe = (row.snapshot.get("attributes", {}).get("dish_metadata") or {}).get("recipe")
    difficulty = recipe.get("difficulty", 1) if isinstance(recipe, dict) else 1
    if type(difficulty) not in (int, float) or not math.isfinite(difficulty) or not 1 <= difficulty <= 10:
        difficulty = 1
    score = overall_score(scores, difficulty, deterministic_rng(row.request_id, "DISH_REVIEW_V1", row.created_world_minute // 1440))
    price = review_value(default_value(item, slot.dish_quality), score)
    slot.rated, slot.overall_score, slot.rating_price, slot.review_comment = True, score, price, comment
    slot.dish_metadata = dict(slot.dish_metadata or {}, review_id=str(row.pk), scores=copy.deepcopy(scores),
        price_rule="score-v1", review_source="MARK_AGENT_LLM", customer_feedback=feedback)
    slot.save(update_fields=["rated", "overall_score", "rating_price", "review_comment", "dish_metadata"])
    row.status, row.scores, row.comment, row.customer_feedback = "COMPLETED", copy.deepcopy(scores), comment, feedback
    row.overall_score, row.rating_price, row.own_production_verified = score, price, own
    row.completed_world_minute, row.completed_request_id = minute, request_id
    if own:
        row.growth_reward = award_skill(player, source_type="dish_review", source_id=str(row.pk), skill_code="cooking", xp=0,
            milestone=dict(code="first_formal_dish_review", title="首次自制菜品正式点评", xp=20),
            facts=dict(review_id=str(row.pk), production_request_id=(slot.dish_metadata or {}).get("production_request_id"),
                       item_id=slot.item_id, own_production_verified=True))
    else:
        _bump(player)
    row.save()
    # Polling exposes the durable result to the owner. No private batch or XP is
    # put in NPC events, and no repeat action is invented for reward delivery.
    return dict(review_id=str(row.pk), owner_type="PLAYER", status=row.status, item_id=slot.item_id,
                overall_score=score, rating_price=price, scores=copy.deepcopy(scores), comment=comment)
