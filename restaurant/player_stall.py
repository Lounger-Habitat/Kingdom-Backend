"""Batch-preserving stalls settled only by player actions and authoritative ticks."""
from copy import deepcopy
from datetime import date
import hashlib
import hmac
import json
import uuid

from django.conf import settings
from django.db import models, transaction

from game_time.models import GlobalGameTimeState
from inventory.components import BATCH_COPY_FIELDS, copy_batch_attributes
from inventory.models import InventoryBag, InventorySlot
from items.models import Item, ItemType
from player.models import PlayerGrowthState, PlayerRecipe, PlayerState
from player.progression import award_skill, growth_snapshot
from recipes.models import Recipe
from village.models import EconomicLedgerEntry, VillagePlayerActionReceipt, VillageSimulationConfig
from village.phase3_asset_service import deterministic_rng
from .models import (CustomerTransaction, DailyBusinessLog, MenuItem, PlayerRestaurantState,
                     RestaurantConfig, RestaurantHourlySlot, RestaurantLevelConfig, RestaurantSaleEvent)
from items.quality import quality_price


class StallError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def reject(code, message):
    raise StallError(code, message)


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        reject("INVALID_PARAMS", "物品批次标识无效。")


def _index(bag):
    value = bag.slots.aggregate(n=models.Max("slot_index"))["n"]
    return 0 if value is None else value + 1


def _skill(player):
    return next(row["level"] for row in growth_snapshot(player, read_only=True)["skills"] if row["code"] == "business")


def _config():
    # Model defaults are the read-only fallback, never get_instance() on queries.
    return RestaurantConfig.objects.filter(pk=1).first() or RestaurantConfig()


def _capacity(config, level):
    bonus = sum(row.reward_menu_capacity_bonus for row in RestaurantLevelConfig.objects.filter(level__lte=level))
    return max(0, config.base_menu_capacity + max(0, level - 1) * config.menu_capacity_per_level + bonus)


def _quota(config, level):
    bonus = sum(row.reward_customer_bonus for row in RestaurantLevelConfig.objects.filter(level__lte=level))
    return max(0, config.base_hourly_sales + max(0, level - 1) * config.hourly_sales_per_level + bonus)


def _price(slot, item, level):
    if slot.rated:
        if type(slot.rating_price) is not int or slot.rating_price <= 0 or not 0 <= slot.overall_score <= 100:
            return None
        base = slot.rating_price
    else:
        if item.item_price <= 0:
            return None
        base = quality_price(item.item_price, slot.quality, dish=True)
    bonus = sum(row.reward_price_bonus_pct for row in RestaurantLevelConfig.objects.filter(level__lte=level))
    return max(1, int(base * (100 + bonus) / 100))


def _reason(slot, item, minute):
    if not item or item.is_deleted or item.item_type != ItemType.FOOD:
        return "NOT_FOOD"
    if slot.item_amount <= 0:
        return "BATCH_EMPTY"
    if not 1 <= slot.quality <= 6:
        return "DISH_QUALITY_INVALID"
    if slot.produced_world_minute is not None and slot.produced_world_minute > minute:
        return "PROVENANCE_REQUIRED"
    metadata = slot.dish_metadata or {}
    if metadata.get("public_meal_claim") or metadata.get("public_meal_code") or slot.item_id == 29205:
        return "PUBLIC_MEAL_NOT_RESALE"
    return ""


def _batch_snapshot(slot):
    result = {field: deepcopy(getattr(slot, field)) for field in BATCH_COPY_FIELDS}
    result["source_request_id"] = str(slot.source_request_id) if slot.source_request_id else None
    return dict(result, instance_id=str(slot.instance_id), item_id=slot.item_id)


def _purchase_cost(player, slot):
    """Only a uniquely identified receipt batch proves a purchase price."""
    matches = []
    for receipt in VillagePlayerActionReceipt.objects.filter(player=player, action__in=["buy_store", "buy_marketplace"]):
        if not receipt.response.get("success"):
            continue
        result = receipt.response.get("data", {}).get("result", {})
        for batch in result.get("purchased_batches", []):
            if batch.get("instance_id") == str(slot.instance_id) and batch.get("item_id") == slot.item_id:
                amount = batch.get("quantity", batch.get("amount"))
                price = result.get("unit_price")
                if type(amount) is int and amount >= slot.item_amount and type(price) is int and price > 0:
                    matches.append((price, dict(source=receipt.action, request_id=str(receipt.request_id),
                        purchased_instance_id=str(slot.instance_id), purchased_quantity=amount)))
    # Merged/reused identities or several purchase prices are deliberately unknown.
    return matches[0] if len(matches) == 1 else (None, {})


def _row(slot, item, minute, level, cost=None, provenance=None):
    reason = _reason(slot, item, minute)
    price = _price(slot, item, level) if not reason else None
    if not reason and (price is None or price <= 0):
        reason = "PRICE_UNAVAILABLE"
    return dict(instance_id=str(slot.instance_id), item_id=slot.item_id, item_name=item.item_name if item else str(slot.item_id),
        quantity=slot.item_amount, quality=slot.quality, rated=slot.rated, score=slot.overall_score,
        unit_price=price, produced_world_minute=slot.produced_world_minute,
        expires_world_minute=None, freshness_unknown=False,
        usable=not reason, reason=reason, purchase_unit_cost=cost,
        total_purchase_cost=cost * slot.item_amount if cost is not None else None,
        cost_source=(provenance or {}).get("source", "UNKNOWN"))


def _material_rows(bag, item_id, minute):
    return [s for s in bag.slots.filter(item_id=item_id, item_amount__gt=0).order_by("slot_index")
            if True] if bag.pk else []


def _configured_items(rows):
    result = {}
    if not isinstance(rows, list):
        reject("CONFIG_INVALID", "设施物品配置无效。")
    for row in rows:
        if not isinstance(row, dict) or type(row.get("itemId")) is not int or type(row.get("amount")) is not int or row["amount"] <= 0:
            reject("CONFIG_INVALID", "设施物品配置无效。")
        result[row["itemId"]] = result.get(row["itemId"], 0) + row["amount"]
    return result


def _upgrade_quote(player, bag, state, minute, read_only=False):
    level = state.current_level if state else 1
    config = RestaurantLevelConfig.objects.filter(level=level + 1).first()
    required_skill = 2 if level + 1 == 2 else 4
    base = dict(available=False, reason="MAX_LEVEL", quote_hash=None, target_level=level + 1,
        required_skill_level=required_skill, gold_cost=0, required_ratings=0, required_sold=0,
        required_dish_ids=[], required_dish_scores=[], materials=[], reward_materials=[], reward_recipes=[],
        capacity_after=_capacity(_config(), level + 1), hourly_quota_after=_quota(_config(), level + 1))
    if not config:
        return base, []
    consumed, material_views, rewards, recipes, missing_material = {}, [], [], [], False
    try:
        costs = _configured_items(config.cost_items)
        if (not isinstance(config.required_dish_ids, list) or any(type(i) is not int for i in config.required_dish_ids)
                or not isinstance(config.required_dish_scores, list)):
            reject("CONFIG_INVALID", "设施菜品条件无效。")
        for item_id, quantity in costs.items():
            item = Item.objects.filter(pk=item_id, is_deleted=False).first()
            if not item:
                reject("CONFIG_INVALID", "设施消耗物品目录缺失。")
            rows = _material_rows(bag, item_id, minute)
            available = sum(s.item_amount for s in rows)
            material_views.append(dict(item_id=item_id, item_name=item.item_name, quantity=quantity, available_quantity=available))
            missing_material |= available < quantity
            left = quantity
            for slot in rows:
                take = min(slot.item_amount, left)
                if take:
                    consumed[slot.pk] = [slot, take]
                left -= take
                if not left:
                    break
        for requirement in config.required_dish_scores:
            if (not isinstance(requirement, dict) or type(requirement.get("itemId")) is not int
                    or type(requirement.get("minScore")) not in (int, float)):
                reject("CONFIG_INVALID", "设施菜品条件无效。")
            candidates = [s for s in _material_rows(bag, requirement["itemId"], minute)
                if s.rated and s.overall_score >= requirement["minScore"]
                and not _reason(s, Item.objects.filter(pk=s.item_id).first(), minute)
                and s.item_amount > consumed.get(s.pk, [None, 0])[1]]
            if not candidates:
                missing_material = True
            else:
                slot = min(candidates, key=lambda s: (s.overall_score, s.slot_index))
                consumed.setdefault(slot.pk, [slot, 0])[1] += 1
        for item_id, quantity in _configured_items(config.reward_materials).items():
            item = Item.objects.filter(pk=item_id, is_deleted=False).first()
            if not item:
                reject("CONFIG_INVALID", "设施奖励物品目录缺失。")
            rewards.append(dict(item_id=item_id, item_name=item.item_name, quantity=quantity))
        if not isinstance(config.reward_recipes, list):
            reject("RECIPE_CATALOG_UNAVAILABLE", "设施菜谱奖励无效。")
        for name in config.reward_recipes:
            candidates = list(Recipe.objects.filter(recipe_name=name)) if isinstance(name, str) else []
            if len(candidates) != 1:
                reject("RECIPE_CATALOG_UNAVAILABLE", "设施奖励菜谱尚未唯一安装。")
            recipes.append(dict(recipe_id=candidates[0].pk, recipe_name=name))
        if config.cost_money < 0 or config.required_dishes_sold < 0 or config.required_total_ratings < 0:
            reject("CONFIG_INVALID", "设施价格或条件无效。")
        reason = ("MODE_READ_ONLY" if read_only else "STALL_NOT_OPEN" if not state or state.stall_opened_world_minute is None else
            "SKILL_LEVEL_REQUIRED" if _skill(player) < required_skill else
            "RATINGS_REQUIRED" if state.total_ratings_received < config.required_total_ratings else
            "SALES_REQUIRED" if state.total_dishes_sold < config.required_dishes_sold else
            "DISH_SALES_REQUIRED" if set(config.required_dish_ids) - set(state.dishes_sold_set) else
            "INSUFFICIENT_MONEY" if bag.money < config.cost_money else "INSUFFICIENT_MATERIALS" if missing_material else "")
    except StallError as exc:
        reason = exc.code
    base.update(reason=reason, available=not reason, gold_cost=config.cost_money,
        required_ratings=config.required_total_ratings, required_sold=config.required_dishes_sold,
        required_dish_ids=config.required_dish_ids, required_dish_scores=config.required_dish_scores,
        materials=material_views, reward_materials=rewards, reward_recipes=recipes)
    payload = dict(configuration={f.name: getattr(config, f.name) for f in config._meta.concrete_fields},
        level=level, skill=_skill(player), money=bag.money, sold=state.total_dishes_sold if state else 0,
        ratings=state.total_ratings_received if state else 0, dish_sales=state.dishes_sold_set if state else [],
        inputs=[dict(instance_id=str(s.instance_id), item_id=s.item_id, quantity=q, amount=s.item_amount,
            quality=s.quality, score=s.overall_score, expiry=s.expires_world_minute) for s, q in consumed.values()],
        rewards=rewards, recipes=recipes)
    base["quote_hash"] = hmac.new(settings.SECRET_KEY.encode(), json.dumps(payload, sort_keys=True).encode(), hashlib.sha256).hexdigest()
    return base, list(consumed.values())


def business_trend(player, minute):
    """All actual dated sales, grouped by world day; undated history stays separate."""
    from django.db.models.functions import Cast
    aggregates = dict(sold=models.Count("pk"), revenue=models.Sum("amount_paid"),
        known_purchase_cost=models.Sum("unit_cost", filter=models.Q(unit_cost__gte=0)),
        unknown_cost_count=models.Count("pk", filter=models.Q(unit_cost__isnull=True) | models.Q(unit_cost__lt=0)))

    def project(row):
        sold, revenue = row.get("sold", 0) or 0, row.get("revenue", 0) or 0
        cost, unknown = row.get("known_purchase_cost", 0) or 0, row.get("unknown_cost_count", 0) or 0
        return dict(sold=sold, revenue=revenue, known_purchase_cost=cost, unknown_cost_count=unknown,
            purchase_margin_nullable=None if unknown else revenue-cost, cost_complete=unknown == 0)

    today = minute // 1440
    first_day = max(0, today - 6)
    transactions = CustomerTransaction.objects.filter(player=player)
    groups = transactions.filter(world_minute__gte=first_day*1440, world_minute__lte=minute).annotate(
        day_index=Cast(models.F("world_minute") / models.Value(1440), output_field=models.BigIntegerField()))
    groups = {row["day_index"]: row for row in groups.values("day_index").annotate(**aggregates).order_by("day_index")}
    return dict(from_day_index=first_day, to_day_index=today, observed_world_minute=minute,
        source="CUSTOMER_TRANSACTION", cost_basis="RECORDED_PURCHASE_COST_ONLY",
        cost_basis_note="仅统计真实成交及已记录采购成本；未知成本不按零计算。采购毛利不包含人工、自产原料、工具、租金等成本，不代表净利润。",
        days=[dict(day_index=day, **project(groups.get(day, {}))) for day in range(first_day, today+1)],
        legacy_undated=project(transactions.filter(world_minute__isnull=True).aggregate(**aggregates)))


def business_overview(player, bag, minute, read_only=False):
    state = PlayerRestaurantState.objects.filter(player=player).first()
    level, config = state.current_level if state else 1, _config()
    opened = bool(state and state.stall_opened_world_minute is not None)
    menus = list(MenuItem.objects.filter(player=player).select_related("batch_slot"))
    occupied = sum(max(0, menu.quantity) for menu in menus)
    capacity = _capacity(config, level)
    menu_views = []
    for menu in menus:
        if menu.batch_slot_id:
            row = _row(menu.batch_slot, Item.objects.filter(pk=menu.item_id).first(), minute, level,
                       menu.purchase_unit_cost, menu.cost_provenance)
            if not _escrow_valid(player, menu):
                row.update(usable=False, reason="BATCH_MISMATCH")
            row["menu_id"] = menu.pk
        else:
            row = dict(menu_id=menu.pk, instance_id=menu.instance_id, item_id=menu.item_id, item_name=menu.item_name,
                quantity=menu.quantity, quality=menu.dish_quality, rated=menu.rated, score=menu.overall_score,
                unit_price=None, produced_world_minute=None, expires_world_minute=None, freshness_unknown=True,
                usable=False, reason="BATCH_MISSING" if menu.listed_world_minute is not None else "LEGACY_BATCH_UNKNOWN",
                purchase_unit_cost=None, total_purchase_cost=None, cost_source="UNKNOWN")
        menu_views.append(row)
    inventory = []
    for slot in bag.slots.filter(item_amount__gt=0).order_by("slot_index") if bag.pk else []:
        item = Item.objects.filter(pk=slot.item_id, item_type=ItemType.FOOD).first()
        if not item:
            continue
        cost, provenance = _purchase_cost(player, slot)
        row = _row(slot, item, minute, level, cost, provenance)
        reason = row["reason"] or ("MODE_READ_ONLY" if read_only else "STALL_NOT_OPEN" if not opened else
            "MENU_CAPACITY_REACHED" if occupied >= capacity else "")
        row.update(reason=reason, usable=not reason, available_quantity=min(slot.item_amount, max(0, capacity - occupied)) if not reason else 0)
        inventory.append(row)
    transactions = CustomerTransaction.objects.filter(player=player)
    rows = list(transactions.order_by("-id")[:20])
    totals = transactions.aggregate(revenue=models.Sum("amount_paid"), known_cost=models.Sum("unit_cost"), sold=models.Count("id"))
    recorded_revenue = totals["revenue"] or 0
    revenue = state.total_gold_earned if state else recorded_revenue
    sold_count = state.total_dishes_sold if state else totals["sold"]
    complete = (not transactions.filter(unit_cost__isnull=True).exists()
                and revenue == recorded_revenue and sold_count == totals["sold"])
    known_cost = totals["known_cost"] or 0
    hour = (minute % 1440) // 60
    return dict(revision=PlayerGrowthState.objects.filter(player=player).values_list("revision", flat=True).first() or 0,
        observed_world_minute=minute, is_open=opened, is_operating=opened and config.opening_hour <= hour < config.closing_hour,
        level=level, skill_level=_skill(player), capacity=capacity, occupied=occupied, hourly_quota=_quota(config, level),
        opening_hour=config.opening_hour, closing_hour=config.closing_hour, menu=menu_views, inventory=inventory,
        upgrade=_upgrade_quote(player, bag, state, minute, read_only)[0],
        summary=dict(revenue=revenue, known_cost=known_cost, net_income_nullable=revenue-known_cost if complete else None,
            cost_complete=complete, sold_count=sold_count),
        trend=business_trend(player, minute),
        history=[dict(id=t.pk, world_minute=t.world_minute, item_id=t.item_id, item_name=t.item_name,
            quantity=1, amount_paid=t.amount_paid, unit_cost=t.unit_cost, rating=t.rating,
            comment=t.review_comment, feedback_source=t.feedback_source) for t in rows])


def _escrow_code(player):
    return f"stall:{player.pk}"


def _escrow_valid(player, menu):
    slot = menu.batch_slot
    return bool(slot and slot.bag.player_id == player.pk and slot.bag.character_id == _escrow_code(player)
                and slot.item_id == menu.item_id and slot.item_amount == menu.quantity and menu.quantity > 0)


def withdraw_menu_batch(player, bag, menu):
    """Also used by the legacy return path, so toggling modes cannot wash a batch."""
    if menu.player_id != player.pk:
        reject("MENU_NOT_OWNED", "这份菜单不属于本人。")
    source = menu.batch_slot
    if source is None and menu.listed_world_minute is not None:
        reject("BATCH_MISSING", "这份营业菜品的实际托管批次缺失，不能凭菜单重建库存。")
    if source and not _escrow_valid(player, menu):
        reject("BATCH_MISMATCH", "营业托管批次与菜单不一致，请核实。")
    returned = InventorySlot(bag=bag, slot_index=_index(bag), item_id=menu.item_id, item_amount=menu.quantity)
    if source:
        copy_batch_attributes(source, returned)
        source.item_amount = 0
        source.save(update_fields=["item_amount"])
    else:
        for name in ("rated", "rating_price", "overall_score", "ingredient", "cook_time"):
            setattr(returned, name, getattr(menu, name))
        returned.quality, returned.freshness_unknown = menu.dish_quality, True
    returned.save()
    result = dict(menu_id=menu.pk, returned_instance_id=str(returned.instance_id), item_id=returned.item_id,
                  quantity=returned.item_amount, freshness_unknown=returned.freshness_unknown)
    menu.delete()
    return result


@transaction.atomic
def execute_stall_action(player, bag, action, params, rid, clock):
    GlobalGameTimeState.objects.select_for_update().get(pk=1)
    if not bag.pk or bag.player_id != player.pk or bag.character_id != player.username:
        reject("ITEM_NOT_OWNED", "请使用本人主背包。")
    state, _ = PlayerRestaurantState.objects.select_for_update().get_or_create(player=player)
    if action == "open_player_stall":
        if params:
            reject("INVALID_PARAMS", "开办摊位不需要额外参数。")
        if state.stall_opened_world_minute is None:
            state.stall_opened_world_minute = clock.world_minute
            state.save(update_fields=["stall_opened_world_minute"])
        result = dict(is_open=True, level=state.current_level)
    elif action == "withdraw_stall_dish":
        if set(params) != {"menu_id"} or type(params["menu_id"]) is not int:
            reject("INVALID_PARAMS", "菜单标识无效。")
        menu = MenuItem.objects.select_for_update().filter(player=player, pk=params["menu_id"]).first()
        if not menu:
            reject("MENU_NOT_OWNED", "本人菜单中没有这份菜品。")
        result = withdraw_menu_batch(player, bag, menu)
    else:
        if state.stall_opened_world_minute is None:
            reject("STALL_NOT_OPEN", "请先现场开办摊位。")
        if action == "list_stall_dish":
            if set(params) != {"instance_id", "quantity"} or type(params["quantity"]) is not int or params["quantity"] <= 0:
                reject("INVALID_PARAMS", "请选择实际菜品批次和正整数份数。")
            source = bag.slots.select_for_update().filter(instance_id=_uuid(params["instance_id"]), item_amount__gt=0).first()
            if not source:
                reject("ITEM_NOT_OWNED", "本人主背包中没有这批菜品。")
            item = Item.objects.filter(pk=source.item_id).first()
            cost, provenance = _purchase_cost(player, source)
            row = _row(source, item, clock.world_minute, state.current_level, cost, provenance)
            if not row["usable"]:
                reject(row["reason"], "该批次当前不能用于营业，请核对来源与鲜期。")
            quantity = params["quantity"]
            if quantity > source.item_amount:
                reject("INSUFFICIENT_ITEMS", "该批次实际份数不足。")
            occupied = sum(MenuItem.objects.filter(player=player).values_list("quantity", flat=True))
            if occupied + quantity > _capacity(_config(), state.current_level):
                reject("MENU_CAPACITY_REACHED", "菜单总份数超过设施容量。")
            escrow, _ = InventoryBag.objects.get_or_create(player=player, character_id=_escrow_code(player))
            target = InventorySlot(bag=escrow, slot_index=_index(escrow), item_id=source.item_id, item_amount=quantity)
            copy_batch_attributes(source, target); target.save()
            source.item_amount -= quantity; source.save(update_fields=["item_amount"])
            menu = MenuItem.objects.create(player=player, instance_id=str(target.instance_id), item_id=target.item_id,
                item_name=item.item_name, dish_quality=target.quality, rated=target.rated, rating_price=target.rating_price,
                overall_score=target.overall_score, ingredient=target.ingredient, cook_time=target.cook_time, quantity=quantity,
                batch_slot=target, listed_world_minute=clock.world_minute, purchase_unit_cost=cost, cost_provenance=provenance)
            result = dict(menu_id=menu.pk, instance_id=str(target.instance_id), item_id=target.item_id,
                          quantity=quantity, unit_price=row["unit_price"], purchase_unit_cost=cost)
        elif action == "upgrade_player_stall":
            if set(params) != {"expected_quote"}:
                reject("INVALID_PARAMS", "请确认设施改良报价。")
            quote, inputs = _upgrade_quote(player, bag, state, clock.world_minute)
            if not isinstance(params["expected_quote"], str) or params["expected_quote"] != quote["quote_hash"]:
                reject("QUOTE_CHANGED", "设施条件或材料资金已变化，请重新确认。")
            if not quote["available"]:
                reject(quote["reason"], "当前尚不满足设施改良条件。")
            consumed = []
            for source, quantity in inputs:
                source = InventorySlot.objects.select_for_update().get(pk=source.pk)
                if source.item_amount < quantity:
                    reject("INSUFFICIENT_MATERIALS", "设施材料数量已变化。")
                source.item_amount -= quantity; source.save(update_fields=["item_amount"])
                consumed.append(dict(item_id=source.item_id, quantity=quantity))
            bag.money -= quote["gold_cost"]; bag._money_change_source = "player_stall_upgrade"
            bag.save(update_fields=["money"])
            rewards = []
            for reward in quote["reward_materials"]:
                item = Item.objects.get(pk=reward["item_id"])
                uses = item.max_use_count if item.item_type in {ItemType.USABLE, ItemType.COLLECT_TOOL,
                    ItemType.HOE_TOOL, ItemType.CHOP_TOOL, ItemType.BREAK_TOOL, ItemType.REAP_TOOL, ItemType.WATER_TOOL} else None
                slot = InventorySlot.objects.create(bag=bag, slot_index=_index(bag), item_id=item.pk,
                    item_amount=reward["quantity"], remaining_uses=uses, source_request_id=rid,
                    produced_world_minute=clock.world_minute, freshness_unknown=item.item_type == ItemType.FOOD)
                rewards.append(dict(instance_id=str(slot.instance_id), item_id=item.pk, quantity=slot.item_amount))
            ps, _ = PlayerState.objects.get_or_create(player_id=str(player.pk))
            for recipe in quote["reward_recipes"]:
                PlayerRecipe.objects.get_or_create(player=ps, recipe_name=recipe["recipe_name"],
                    defaults=dict(catalog_recipe_id=recipe["recipe_id"], show_ingredient_count=False))
            state.current_level = quote["target_level"]; state.save(update_fields=["current_level"])
            result = dict(new_level=state.current_level, spent_gold=quote["gold_cost"], materials_consumed=consumed,
                          reward_materials=rewards, reward_recipes=quote["reward_recipes"], money=bag.money)
        else:
            reject("INVALID_ACTION", "未开放的摊位动作。")
    EconomicLedgerEntry.objects.create(request_id=uuid.uuid5(uuid.NAMESPACE_URL, f"stall:{player.pk}:{rid}"),
        entry_type=action.upper(), world_minute=clock.world_minute,
        external_delta=-result.get("spent_gold", 0), changes=dict(player_id=str(player.pk), result=deepcopy(result)))
    return result


@transaction.atomic
def process_stall_tick(clock):
    """Current authoritative hour only; no HTTP, LLM or wall-clock catch-up."""
    GlobalGameTimeState.objects.select_for_update().get(pk=1)
    if not VillageSimulationConfig.objects.filter(config_key="default", village_economy_enabled=True).exists():
        return []
    config, changed = _config(), []
    hour = (clock.world_minute % 1440) // 60
    if not config.opening_hour <= hour < config.closing_hour:
        return changed
    hour_start = clock.world_minute - clock.world_minute % 60
    day = date.fromordinal(clock.world_minute // 1440 + 1)
    for state in PlayerRestaurantState.objects.select_for_update().filter(stall_opened_world_minute__isnull=False).select_related("player"):
        if state.last_stall_tick_world_minute is not None and clock.world_minute <= state.last_stall_tick_world_minute:
            continue
        player = state.player
        bag = InventoryBag.objects.select_for_update().filter(player=player, character_id=player.username).first()
        if not bag:
            continue
        hourly, created = RestaurantHourlySlot.objects.select_for_update().get_or_create(player=player,
            game_year=day.year, game_month=day.month, game_day=day.day, game_hour=hour,
            defaults=dict(quota=_quota(config, state.current_level), restaurant_level=state.current_level))
        if created:
            rng = deterministic_rng(uuid.uuid5(uuid.NAMESPACE_URL, f"stall-hour:{player.pk}:{hour_start}"), "STALL_EVENTS_V1", 0)
            RestaurantSaleEvent.objects.bulk_create([RestaurantSaleEvent(slot=hourly, scheduled_minute=rng.randint(0, 59)) for _ in range(hourly.quota)])
        sold_any = False
        for event in hourly.sale_events.select_for_update().filter(processed=False, scheduled_minute__lte=clock.world_minute % 60).order_by("scheduled_minute", "pk"):
            scheduled = hour_start + event.scheduled_minute
            menu = list(MenuItem.objects.select_for_update().filter(player=player, quantity__gt=0,
                batch_slot__isnull=False, listed_world_minute__lte=scheduled).select_related("batch_slot"))
            eligible = []
            if not (created and scheduled < clock.world_minute) and scheduled >= state.stall_opened_world_minute and hourly.sold_count < hourly.quota:
                for row in menu:
                    item = Item.objects.filter(pk=row.item_id).first()
                    if _escrow_valid(player, row) and not _reason(row.batch_slot, item, clock.world_minute):
                        price = _price(row.batch_slot, item, state.current_level)
                        if price is not None and price > 0:
                            eligible.append((row, price))
            if not eligible:
                event.processed, event.skipped = True, True
                event.save(update_fields=["processed", "skipped"])
                continue
            rng = deterministic_rng(uuid.uuid5(uuid.NAMESPACE_URL, f"stall-sale:{event.pk}"), "STALL_SALE_V1", 0)
            menu, price = rng.choices(eligible, weights=[row.quantity for row, _ in eligible], k=1)[0]
            source = InventorySlot.objects.select_for_update().get(pk=menu.batch_slot_id)
            snapshot = _batch_snapshot(source)
            rating = min(5, max(1, source.quality))
            comment = "普通顾客规则反馈：" + ("这份餐食口味不错。" if rating >= 4 else "这份餐食符合日常用餐需要。")
            log, _ = DailyBusinessLog.objects.get_or_create(player=player, season_id=state.season_id,
                game_year=day.year, game_month=day.month, game_day_of_month=day.day, defaults=dict(game_day=clock.world_minute // 1440))
            tx = CustomerTransaction.objects.create(player=player, daily_log=log, season_id=state.season_id,
                game_day=clock.world_minute // 1440, world_minute=clock.world_minute,
                customer_name=rng.choice(config.customer_name_pool or ["普通顾客"]), item_id=source.item_id,
                item_name=menu.item_name, dish_quality=source.quality, amount_paid=price, rating=rating,
                review_comment=comment, feedback_source="RULE_CUSTOMER", batch_snapshot=snapshot, unit_cost=menu.purchase_unit_cost)
            source.item_amount -= 1; source.save(update_fields=["item_amount"])
            menu.quantity -= 1
            if menu.quantity:
                menu.save(update_fields=["quantity"])
            else:
                menu.delete()
            hourly.sold_count += 1; hourly.save(update_fields=["sold_count"])
            event.processed, event.transaction = True, tx
            event.save(update_fields=["processed", "transaction"])
            bag.money += price; bag._money_change_source = "player_stall_sale"; bag.save(update_fields=["money"])
            state.total_dishes_sold += 1; state.total_gold_earned += price
            state.total_ratings_received += int(rating >= 4)
            if source.item_id not in state.dishes_sold_set:
                state.dishes_sold_set.append(source.item_id)
            log.customer_count += 1; log.total_earned += price
            log.save(update_fields=["customer_count", "total_earned"])
            award_skill(player, source_type="stall_sale", source_id=event.pk, skill_code="business", xp=5,
                milestone=dict(code="first_stall_sale", title="完成首次真实摊位销售", xp=20),
                facts=dict(sale_event_id=event.pk, transaction_id=tx.pk, item_id=source.item_id,
                           quantity=1, amount_paid=price, source_instance_id=str(source.instance_id)))
            EconomicLedgerEntry.objects.create(request_id=uuid.uuid5(uuid.NAMESPACE_URL, f"stall-sale:{event.pk}"),
                entry_type="PLAYER_STALL_SALE", world_minute=clock.world_minute, external_delta=price,
                changes={f"PLAYER:{player.pk}": {"money": price, str(source.item_id): -1},
                    "sale_event_id": event.pk, "transaction_id": tx.pk, "consumption": "EXTERNAL_CUSTOMER", "batch": snapshot})
            sold_any = True
        state.last_stall_tick_world_minute = clock.world_minute
        state.save(update_fields=["last_stall_tick_world_minute", "total_dishes_sold", "total_gold_earned", "total_ratings_received", "dishes_sold_set"])
        if sold_any:
            changed.append(str(player.pk))
    return changed
