"""Bounded read-only projections for Phase 7 player and operator views."""

from __future__ import annotations

import uuid
from datetime import date

from django.db import connection
from django.db.models import Count, Max, Min, Q, Sum
from django.utils import timezone

from game_time.models import GlobalGameTimeState
from inventory.models import NpcGlobalInventory, NpcGlobalInventorySlot
from items.models import Item
from marketplace.models import MarketListing
from recipes.models import Recipe

from .command_catalog import AGENT_INSTRUCTION_ACTIONS, COMMAND_CATALOG
from .models import (
    DailyPriceSnapshot,
    EconomicLedgerEntry,
    NpcActionReservation,
    NpcCommandState,
    NpcCommitment,
    NpcDialogueSession,
    NpcProfile,
    NpcRelationship,
    NpcRuntimeState,
    NpcSubjectiveGoalState,
    NpcStoryFactAccess,
    NpcTradeDelivery,
    NpcTradeOrder,
    VillageAccount,
    VillageActionReceipt,
    VillageDeliveryCursor,
    VillageDeliveryOutbox,
    VillageLocationDefinition,
    VillageStoryArc,
    VillageStoryFact,
    VillageStoryTransitionReceipt,
    VillageStoreListing,
    VillageTradeOffer,
    VillageWorldEvent,
)
from .phase3_catalog import TOOL_ITEM_IDS
from .phase4_contract import relationship_label, relationship_snapshot
from .runtime_services import _OUTCOME_REASONS


FORMAL_NPC_IDS = ("10011", "10012", "10013", "10014", "10015")
PRICE_ITEM_IDS = (1030, 1056, 1053, 1067)
FOOD_ITEM_IDS = (*PRICE_ITEM_IDS, 29001)
SCAN_BATCH_SIZE = 200
SCAN_EVENT_BUDGET = 1000


def _display_code(value: object) -> str:
    return str(value or "").replace("_", " ")


def _public_command_summary(action: str, params: dict, duration: int | None,
                            npc_names: dict[str, str],
                            location_names: dict[str, str], item_names: dict[int, str],
                            marketplace_item_names: dict[str, str],
                            home_location_code: str = "", details: dict | None = None) -> str:
    """Build a bounded public description without exposing raw command parameters."""
    action = str(action or "").upper()
    label = COMMAND_CATALOG.get(action, {}).get("label", {
        "WAKE_UP": "唤醒",
        "UPDATE_SUBJECTIVE_GOAL": "更新主观目标",
    }.get(action, action or "未知指令"))
    params = params if isinstance(params, dict) else {}
    details = details or {}

    def location(*keys: str) -> str:
        for key in keys:
            code = params.get(key)
            if code:
                return location_names.get(str(code), _display_code(code))
        return ""

    def npc(*keys: str) -> str:
        for key in keys:
            npc_id = params.get(key)
            if npc_id:
                return npc_names.get(str(npc_id), f"NPC {npc_id}")
        return ""

    def item(*keys: str) -> str:
        for key in keys:
            item_id = params.get(key)
            try:
                if item_id is not None:
                    return item_names.get(int(item_id), f"物品#{item_id}")
            except (TypeError, ValueError):
                return f"物品#{item_id}"
        return ""

    def resolved(kind: str, key: str) -> dict:
        return details.get(kind, {}).get(str(params.get(key)), {})

    def named_item(item_id: object) -> str:
        try:
            return item_names.get(int(item_id), f"物品#{item_id}")
        except (TypeError, ValueError):
            return ""

    duration = params.get("duration_minutes", duration)
    if action == "MOVE":
        destination = location("location_code")
        target = npc("target_npc_id")
        return f"移动至{destination}" if destination else f"移动至{target}身边" if target else label
    if action == "GO_HOME":
        home = location_names.get(home_location_code, _display_code(home_location_code))
        return f"回家（{home}）" if home else label
    if action == "EAT":
        source = {"COMMUNITY_MEAL": "公共餐食", "PERSONAL_INVENTORY": "个人背包"}.get(
            str(params.get("source", "")).upper(), "")
        goods = item("item_id")
        if not goods:
            goods = named_item(resolved("instances", "food_instance_id").get("item_id"))
        return f"进食{goods}（{source}）" if goods and source else f"进食（{source}）" if source else label
    if action in {"REST", "SLEEP", "WAIT", "WORK_PLACEHOLDER"}:
        return f"{label} {duration} 分钟" if duration is not None else label
    if action == "SOCIALIZE":
        target = npc("target_npc_id")
        detail = f"与{target}交流" if target else label
        return f"{detail} {duration} 分钟" if duration is not None else detail
    if action == "START_DIALOGUE":
        target = npc("target_npc_id")
        topic = str(params.get("topic", "")).strip()
        detail = f"与{target}开始对话" if target else label
        return f"{detail}：{topic}" if topic else detail
    if action == "SPEAK":
        speech = str(params.get("text", "")).strip()
        return f"对话发言：“{speech}”" if speech else label
    if action == "PLANT":
        plot, crop = _display_code(params.get("plot_code")), _display_code(params.get("crop_code"))
        return f"在{plot}播种{crop}" if plot and crop else label
    if action == "HARVEST":
        plot = _display_code(params.get("plot_code"))
        return f"收获{plot}" if plot else label
    if action == "HUNT":
        hunt = {"HUNT_CHICKEN": "肉类"}.get(str(params.get("hunt_code", "")).upper(), "")
        return f"狩猎{hunt}" if hunt else label
    if action == "COOK":
        recipe = params.get("recipe_id")
        recipe_name = details.get("recipes", {}).get(str(recipe), "")
        return f"烹饪{recipe_name}" if recipe_name else f"烹饪菜谱#{recipe}" if recipe is not None else label
    if action == "CREATE_ORDER":
        side = {"BUY": "收购", "SELL": "出售"}.get(str(params.get("side", "")).upper(), label)
        goods = item("item_id")
        amount, price = params.get("amount"), params.get("unit_price")
        detail = f"{side}{goods}" if goods else side
        if amount is not None:
            detail += f" ×{amount}"
        if price is not None:
            detail += f"（单价 {price} 金币）"
        destination = location("delivery_point_code")
        return f"{detail}，交付至{destination}" if destination else detail
    if action == "CANCEL_ORDER":
        order = resolved("orders", "order_id")
        goods = named_item(order.get("item_id"))
        side = {"BUY": "收购", "SELL": "出售"}.get(order.get("side"), "订单")
        return f"取消{side}{goods} ×{order.get('amount')}" if goods else label
    if action == "START_DELIVERY":
        delivery = resolved("deliveries", "delivery_id")
        goods = named_item(delivery.get("item_id"))
        destination = location_names.get(
            str(delivery.get("delivery_point_code", "")), _display_code(delivery.get("delivery_point_code")))
        detail = f"配送{goods} ×{delivery.get('amount')}" if goods else label
        return f"{detail}至{destination}" if destination else detail
    if action in {"IMPORT_GOODS", "EXPORT_GOODS"}:
        goods, amount = item("item_id"), params.get("amount")
        return f"{label}{goods} ×{amount}" if goods and amount is not None else label
    if action == "RATE_DISH":
        owner, score = npc("owner_npc_id"), params.get("score")
        goods = named_item(resolved("instances", "food_instance_id").get("item_id"))
        dish = goods or "菜品"
        return f"为{owner}的{dish}评分 {score} 星" if owner and score is not None else label
    if action == "PROPOSE_TRADE":
        target, goods = npc("target_npc_id"), item("item_id")
        amount, gold = params.get("amount"), params.get("gold_amount")
        detail = f"向{target}发起交易" if target else label
        if goods and amount is not None:
            detail += f"：{goods} ×{amount}"
        if gold is not None:
            detail += f"，{gold} 金币"
        return detail
    if action == "RESPOND_TRADE":
        decision = {"ACCEPT": "接受", "REJECT": "拒绝"}.get(
            str(params.get("decision", "")).upper(), "响应")
        offer = resolved("offers", "offer_id")
        goods = named_item(offer.get("item_id"))
        return f"{decision}{goods} ×{offer.get('amount')}的交易报价" if goods else f"{decision}交易报价"
    if action == "CANCEL_TRADE_OFFER":
        offer = resolved("offers", "offer_id")
        goods = named_item(offer.get("item_id"))
        return f"取消{goods} ×{offer.get('amount')}的交易报价" if goods else label
    if action == "LIST_STORE_ITEM":
        goods, amount = item("item_id"), params.get("amount")
        detail = f"{label}{goods}" if goods else label
        if amount is not None:
            detail += f" ×{amount}"
        price = params.get("custom_unit_price", params.get("price"))
        return f"{detail}（单价 {price} 金币）" if price is not None else detail
    if action == "SELL_ON_MARKETPLACE":
        goods = named_item(resolved("instances", "instance_id").get("item_id"))
        amount, price = params.get("amount"), params.get("price")
        detail = f"上架全服商品{goods} ×{amount}" if goods else label
        return f"{detail}（单价 {price} 金币）" if price is not None else detail
    if action == "BUY_FROM_MARKETPLACE":
        goods = marketplace_item_names.get(str(params.get("listing_id")), "未知物品")
        amount = params.get("amount")
        return f"购买{goods} ×{amount}" if amount is not None else f"购买{goods}"
    if action == "BUY_FROM_STORE":
        listing = resolved("store_listings", "listing_id")
        goods = named_item(listing.get("item_id"))
        amount = params.get("amount")
        return f"购买{goods} ×{amount}" if goods and amount is not None else f"{label} ×{amount}" if amount is not None else label
    if action in {"UPDATE_STORE_PRICE", "DELIST_STORE_ITEM"}:
        listing = resolved("store_listings", "listing_id")
        goods = named_item(listing.get("item_id"))
        if action == "UPDATE_STORE_PRICE":
            price = params.get("custom_unit_price")
            return f"修改{goods}价格为 {price} 金币" if goods and price is not None else label
        return f"下架{goods}" if goods else label
    if action == "DELIST_FROM_MARKETPLACE":
        goods = marketplace_item_names.get(str(params.get("listing_id")), "")
        return f"下架全服商品{goods}" if goods else label
    if action == "BROWSE_MARKETPLACE":
        goods = item("item_id")
        return f"浏览{goods}市场" if goods else label
    if action == "INSPECT_STORE_INVENTORY":
        store = {"GENERAL_STORE": "杂货铺", "RESTAURANT_STORE": "饭店"}.get(
            str(params.get("store_code", "")).upper(), "")
        return f"查看{store}库存" if store else label
    if action == "CREATE_COMMITMENT":
        target = npc("beneficiary_npc_id")
        kind = {
            "DELIVERY": "交货", "REPAYMENT": "还款", "DONATION": "捐赠",
            "JOINT_WORK": "共同工作", "MEETING": "见面",
        }.get(str(params.get("commitment_type", "")).upper(), "事项")
        return f"向{target}承诺{kind}" if target else label
    if action in {"CANCEL_COMMITMENT", "PERFORM_COMMITMENT"}:
        commitment = resolved("commitments", "commitment_id")
        target = npc_names.get(str(commitment.get("beneficiary_npc_id")), "")
        kind = {
            "DELIVERY": "交货", "REPAYMENT": "还款", "DONATION": "捐赠",
            "JOINT_WORK": "共同工作", "MEETING": "见面",
        }.get(commitment.get("commitment_type"), "承诺")
        verb = "取消" if action == "CANCEL_COMMITMENT" else "履行"
        return f"{verb}对{target}的{kind}承诺" if target else label
    if action == "STORY_VOTE":
        plan = _display_code(params.get("plan_code"))
        return f"剧情投票：{plan}" if plan else label
    if action == "STORY_ACT":
        story_action = _display_code(params.get("action_code"))
        goods = item("item_id", "supply_item_id", "limit_item_id", "meal_item_id")
        amount = params.get("amount", params.get("supply_amount", params.get("meal_amount")))
        detail = f"剧情行动：{story_action}" if story_action else label
        return f"{detail}，使用{goods} ×{amount}" if goods and amount is not None else detail
    if action == "UPDATE_SUBJECTIVE_GOAL":
        goal = str(params.get("summary", "")).strip()
        return f"更新主观目标：{goal}" if goal else label
    return label


def _public_command_detail(action: str, params: dict, duration: int | None,
                           status: str, result_code: str, response_data: dict,
                           received: int | None, started: int | None,
                           completed: int | None, now: int,
                           npc_names: dict[str, str], location_names: dict[str, str],
                           item_names: dict[int, str], reference_data: dict) -> str:
    """Return one concrete, player-facing result line; never expose machine codes."""
    action, params = str(action or "").upper(), params if isinstance(params, dict) else {}
    response = response_data if isinstance(response_data, dict) else {}
    data = response.get("data") if isinstance(response.get("data"), dict) else response
    effects = response.get("effects") if isinstance(response.get("effects"), dict) else {}
    finished_at = completed if completed is not None else now

    def bounded(value: str) -> str:
        value = str(value or "").strip().replace("\n", " ")
        return value if len(value) <= 40 else value[:39] + "…"

    def elapsed(end: int) -> int | None:
        begin = started if started is not None else received
        return max(0, end - begin) if begin is not None else duration

    def world_time(value: object) -> str:
        try:
            minute = max(0, int(value))
            day = date.fromordinal(minute // 1440 + 1)
            return f"{day:%m月%d日} {minute % 1440 // 60:02d}:{minute % 60:02d}"
        except (TypeError, ValueError, OverflowError):
            return "时间未知"

    def number(source: dict, *keys: str):
        for key in keys:
            value = source.get(key) if isinstance(source, dict) else None
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value
        return None

    def location_name() -> str:
        code = params.get("location_code") or data.get("target_location_code")
        if action == "GO_HOME" and not code:
            code = data.get("current_location_code")
        return location_names.get(str(code), "目标地点") if code else ""

    def npc_name(*keys: str) -> str:
        for key in keys:
            value = params.get(key) or data.get(key)
            if value:
                return npc_names.get(str(value), "对方")
        return "对方"

    def named_item(value: object) -> str:
        try:
            return item_names.get(int(value), "物品")
        except (TypeError, ValueError):
            return "物品"

    def item_change(kind: str) -> str:
        rows = effects.get(kind)
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            return ""
        row = rows[0]
        amount = row.get("amount", row.get("quantity", ""))
        return f"{named_item(row.get('item_id'))}×{amount}" if amount != "" else named_item(row.get("item_id"))

    if status == "WAITING":
        waited = max(0, now - received) if received is not None else 0
        return bounded(f"已等待{waited}分钟，前方指令完成后开始。")
    if status == "RUNNING":
        spent = max(0, now - started) if started is not None else 0
        total = duration or data.get("duration_minutes")
        return bounded(f"已执行{spent}/{total}分钟。" if total else f"已执行{spent}分钟。")
    if status in {"CANCELLED", "EXPIRED"}:
        spent = elapsed(completed if completed is not None else now) or 0
        verb = "取消" if status == "CANCELLED" else "失效"
        return bounded(f"等待或执行{spent}分钟后{verb}。")
    if status != "SUCCEEDED":
        reason = _OUTCOME_REASONS.get(result_code, "执行结果未分类").replace(
            "Backend", "服务端").replace("Hub", "服务").replace("NPC", "村民")
        spent = elapsed(completed if completed is not None else now)
        current = data.get("current_location_code")
        required = data.get("required_location_code") or params.get("location_code")
        if current and required:
            fact = f"；当前{location_names.get(str(current), '现地点')}，要求{location_names.get(str(required), '目标地点')}"
        elif params.get("amount") is not None:
            fact = f"；请求数量{params['amount']}"
        elif spent:
            fact = f"；耗时{spent}分钟"
        else:
            fact = f"；发生于{world_time(completed if completed is not None else now)}"
        return bounded(reason + fact + "。")

    spent = elapsed(completed if completed is not None else now)
    if action in {"MOVE", "GO_HOME"}:
        destination = location_name()
        return bounded(f"用时{spent or 0}分钟，到达{destination or '目标地点'}。")
    if action in {"WAIT", "REST", "SLEEP", "WORK_PLACEHOLDER"}:
        energy = number(effects.get("needs", {}), "energy")
        extra = f"，精力变化{energy:+g}" if energy is not None else ""
        return bounded(f"实际用时{spent if spent is not None else duration or 0}分钟{extra}。")
    if action == "WAKE_UP":
        slept = number(data, "slept_minutes", "actual_duration_minutes")
        return bounded(f"实际睡眠{slept if slept is not None else spent or 0}分钟。")
    if action == "EAT":
        hunger = number(effects.get("needs", {}), "hunger")
        current = number(data.get("public_needs", {}), "hunger")
        return bounded(f"饥饿变化{hunger:+g}，当前{current:g}。" if hunger is not None and current is not None
                       else f"用时{spent or 0}分钟，饥饿状态已结算。")
    if action == "SOCIALIZE":
        return bounded(f"与{npc_name('target_npc_id')}交流{spent if spent is not None else duration or 0}分钟。")
    if action == "START_DIALOGUE":
        return bounded(f"与{npc_name('target_npc_id')}建立对话，话题{len(str(params.get('topic', '')))}字。")
    if action == "SPEAK":
        ended = result_code == "DIALOGUE_COMPLETED" or bool(params.get("end_turn"))
        return bounded(f"发送{len(str(params.get('text', '')))}字，对话{'结束' if ended else '继续'}。")

    gained, consumed = item_change("items_added"), item_change("items_consumed")
    if action in {"HARVEST", "HUNT", "COOK"} and gained:
        return bounded(f"获得{gained}，用时{spent or 0}分钟。")
    if action in {"PLANT", "COOK"} and consumed:
        return bounded(f"消耗{consumed}，用时{spent or 0}分钟。")

    if action == "CREATE_ORDER":
        return bounded(f"订单开放：{named_item(params.get('item_id'))}×{params.get('amount', 0)}，单价{params.get('unit_price', 0)}。")
    if action == "CANCEL_ORDER":
        order = reference_data.get("orders", {}).get(str(params.get("order_id")), {})
        return bounded(f"返还{named_item(order.get('item_id'))}×{order.get('amount', 0)}。")
    if action == "START_DELIVERY":
        delivery = reference_data.get("deliveries", {}).get(str(params.get("delivery_id")), {})
        return bounded(f"配送{named_item(delivery.get('item_id'))}×{delivery.get('amount', 0)}，用时{spent or 0}分钟。")
    if action in {"IMPORT_GOODS", "EXPORT_GOODS"}:
        money = number(effects, "money_delta", "gold_delta")
        verb = "增加" if action == "IMPORT_GOODS" else "减少"
        suffix = f"，金币变化{money:+g}" if money is not None else ""
        return bounded(f"库存{verb}{named_item(params.get('item_id'))}×{params.get('amount', 0)}{suffix}。")
    if action == "RATE_DISH":
        return bounded(f"提交{params.get('score', 0)}星评分，菜品属于{npc_name('owner_npc_id')}。")
    if action == "PUBLISH_PRICE_INDEX":
        count = len(data.get("prices", [])) if isinstance(data.get("prices"), list) else 0
        return bounded(f"发布{count}种商品价格，时间{world_time(finished_at)}。")
    if action == "AUDIT_PUBLIC_FUND":
        balance = number(data, "balance", "fund_balance", "public_fund_balance")
        return bounded(f"审计后公款余额{balance:g}金币。" if balance is not None else f"审计用时{spent or 0}分钟。")
    if action in {"PROPOSE_TRADE", "RESPOND_TRADE", "CANCEL_TRADE_OFFER"}:
        decision = {"ACCEPT": "接受", "REJECT": "拒绝"}.get(str(params.get("decision", "")).upper(), "处理")
        return bounded(f"{decision}报价，涉及{params.get('amount', 0)}件物品/{params.get('gold_amount', 0)}金币。")
    if action in {"LIST_STORE_ITEM", "UPDATE_STORE_PRICE", "DELIST_STORE_ITEM", "BUY_FROM_STORE",
                  "SELL_ON_MARKETPLACE", "BUY_FROM_MARKETPLACE", "DELIST_FROM_MARKETPLACE"}:
        amount, price = params.get("amount"), params.get("custom_unit_price", params.get("price"))
        facts = [f"数量{amount}" if amount is not None else "", f"单价{price}金币" if price is not None else "",
                 f"用时{spent}分钟" if spent is not None else ""]
        return bounded("，".join(value for value in facts if value) + "。")
    if action in {"CREATE_COMMITMENT", "CANCEL_COMMITMENT", "PERFORM_COMMITMENT"}:
        due = params.get("due_world_minute") or data.get("due_world_minute")
        return bounded(f"涉及{npc_name('beneficiary_npc_id')}，截止{world_time(due)}。" if due else f"用时{spent or 0}分钟，承诺状态已变更。")
    if action == "STORY_VOTE":
        votes = number(data, "vote_count", "votes")
        plan = {"BRIDGE_REPAIR": "修桥", "FOREST_ROUTE": "森林路线", "RATION_AND_WAIT": "配给等待"}.get(
            str(params.get("plan_code", "")).upper(), "当前方案")
        return bounded(f"选择{plan}，当前{votes:g}票。" if votes is not None else f"选择{plan}，时间{world_time(finished_at)}。")
    if action == "STORY_ACT":
        progress = number(data, "progress", "story_progress")
        amount = params.get("amount", params.get("supply_amount", params.get("meal_amount")))
        return bounded(f"剧情进度更新为{progress:g}。" if progress is not None else f"投入资源{amount or 0}，用时{spent or 0}分钟。")
    if action == "UPDATE_SUBJECTIVE_GOAL":
        return bounded(f"新目标：{params.get('summary', '')}")

    query_arrays = {
        "INSPECT_NEARBY": (("nearby_npcs", "nearby_locations"), "发现{0}人、{1}处地点。"),
        "INSPECT_TRADE_OFFERS": (("offers",), "待处理报价{0}份。"),
        "INSPECT_RELEVANT_STORIES": (("stories",), "找到{0}条相关剧情。"),
        "INSPECT_ORDERS": (("orders", "deliveries"), "订单{0}笔，配送{1}笔。"),
        "INSPECT_RATEABLE_DISHES": (("dishes",), "可评分菜品{0}道。"),
        "BROWSE_MARKETPLACE": (("listings",), "找到{0}条在售挂单。"),
        "CHECK_MY_MARKETPLACE_LISTINGS": (("listings",), "自己的在售挂单{0}条。"),
    }
    if action in query_arrays:
        keys, template = query_arrays[action]
        counts = [len(data.get(key, [])) if isinstance(data.get(key), list) else 0 for key in keys]
        return bounded(template.format(*counts))
    if action == "CHECK_MARKETPLACE_HISTORY":
        rows = data.get("history") if isinstance(data.get("history"), list) else data.get("trades", [])
        return bounded(f"成交记录{len(rows) if isinstance(rows, list) else 0}笔。")
    if action == "INSPECT_INVENTORY":
        rows = data.get("items", []) if isinstance(data.get("items"), list) else []
        total = sum(int(row.get("quantity", row.get("available_quantity", 0)) or 0) for row in rows if isinstance(row, dict))
        money = number(data, "money", "gold") or 0
        return bounded(f"金币{money:g}，物品{len(rows)}种/{total}件。")
    if action == "INSPECT_FARM_STATE":
        rows = data.get("farm_plots", data.get("plots", []))
        rows = rows if isinstance(rows, list) else []
        mature = sum(str(row.get("status", "")).upper() == "MATURE" for row in rows if isinstance(row, dict))
        return bounded(f"地块{len(rows)}块，其中成熟{mature}块。")
    if action == "INSPECT_SOCIAL_STATE":
        relationships = data.get("relationships", [])
        commitments = data.get("commitments", [])
        return bounded(f"关系{len(relationships) if isinstance(relationships, list) else 0}人，承诺{len(commitments) if isinstance(commitments, list) else 0}项。")
    if action in {"INSPECT_STORY_STATE", "INSPECT_STORY_DETAIL"}:
        phase = _display_code(data.get("phase", data.get("current_phase")))
        actions = data.get("available_actions", [])
        return bounded(f"当前{phase or '未知阶段'}，可执行{len(actions) if isinstance(actions, list) else 0}项。")
    if action == "INSPECT_RESTAURANT_STATE":
        recipes = data.get("recipes", [])
        return bounded(f"可用菜谱{len(recipes) if isinstance(recipes, list) else 0}道。")
    if action == "INSPECT_STORE_INVENTORY":
        rows = data.get("listings", data.get("items", []))
        return bounded(f"在售商品{len(rows) if isinstance(rows, list) else 0}种。")
    if action == "INSPECT_MARKET_STATE":
        rows = data.get("prices", data.get("items", []))
        return bounded(f"可交易商品{len(rows) if isinstance(rows, list) else 0}种。")
    if action == "INSPECT_PUBLIC_ECONOMY_STATE":
        balance = number(data, "balance", "public_fund_balance") or 0
        return bounded(f"公款余额{balance:g}金币，时间{world_time(finished_at)}。")
    if action == "OBSERVE":
        needs = data.get("public_needs", {})
        hunger, energy = number(needs, "hunger"), number(needs, "energy")
        return bounded(f"饥饿{hunger:g}，精力{energy:g}。" if hunger is not None and energy is not None else f"时间{world_time(finished_at)}。")
    return bounded(f"用时{spent or 0}分钟，完成于{world_time(finished_at)}。")


def presentation_enabled() -> bool:
    from .models import VillageSimulationConfig

    return VillageSimulationConfig.objects.filter(
        config_key="default", village_presentation_enabled=True
    ).exists()


def _category(event: VillageWorldEvent) -> str:
    explicit = str((event.public_data or {}).get("category", "")).upper()
    if explicit:
        return explicit
    event_type = event.event_type.upper()
    if event_type.startswith("STORY_"):
        return "STORY"
    if any(word in event_type for word in (
        "TRADE", "ORDER", "STORE", "PRICE", "FUND", "IMPORT", "EXPORT",
        "COOK", "HARVEST", "HUNT", "PLANT",
    )):
        return "ECONOMY"
    return "NPC"


def _visible(event: VillageWorldEvent, player_id: str) -> bool:
    return event.visibility == "PUBLIC" or player_id in {
        str(value) for value in (event.player_ids or [])
    }


def _journal_item(event: VillageWorldEvent) -> dict:
    return {
        "event_id": str(event.event_id),
        "event_sequence": event.event_sequence,
        "event_type": event.event_type,
        "world_minute": event.world_minute,
        "actor_npc_id": str(event.actor_npc.agent_id) if event.actor_npc else None,
        "participant_npc_ids": [str(value) for value in (event.participant_npc_ids or [])],
        "location_code": event.location_code,
        "player_summary": event.player_summary,
        "category": _category(event),
    }


def public_journal(player_id: str, filters: dict) -> dict:
    """Return a private-safe reverse cursor page without unbounded materialization."""
    limit = filters["limit"]
    scan_before = filters.get("before_sequence")
    events = []
    scanned = 0
    reached_end = False
    while len(events) < limit and scanned < SCAN_EVENT_BUDGET:
        queryset = VillageWorldEvent.objects.select_related("actor_npc")
        if connection.features.supports_json_field_contains:
            visible = Q(visibility="PUBLIC") | Q(player_ids__contains=[player_id])
            if player_id.isdigit():
                visible |= Q(player_ids__contains=[int(player_id)])
            queryset = queryset.filter(visible)
        if scan_before is not None:
            queryset = queryset.filter(event_sequence__lt=scan_before)
        if filters.get("day_index") is not None:
            start = filters["day_index"] * 1440
            queryset = queryset.filter(world_minute__gte=start, world_minute__lt=start + 1440)
        if filters.get("location_code"):
            queryset = queryset.filter(location_code=filters["location_code"])
        batch_limit = min(SCAN_BATCH_SIZE, SCAN_EVENT_BUDGET - scanned)
        batch = list(queryset.order_by("-event_sequence")[:batch_limit])
        if not batch:
            reached_end = True
            break
        scanned += len(batch)
        for event in batch:
            scan_before = event.event_sequence
            if not _visible(event, player_id):
                continue
            npc_id = filters.get("npc_id")
            if npc_id and npc_id not in {
                str(event.actor_npc.agent_id) if event.actor_npc else "",
                *(str(value) for value in (event.participant_npc_ids or [])),
            }:
                continue
            if filters.get("category") and _category(event) != filters["category"]:
                continue
            events.append(_journal_item(event))
            if len(events) == limit:
                break
        if len(batch) < batch_limit:
            reached_end = True
            break
    next_sequence = None
    if len(events) == limit:
        next_sequence = events[-1]["event_sequence"]
    elif not reached_end and scanned >= SCAN_EVENT_BUDGET:
        next_sequence = scan_before
    return {
        "events": events,
        "next_before_sequence": next_sequence,
    }


def _coverage_days() -> tuple[float, float]:
    slots = NpcGlobalInventorySlot.objects.filter(
        bag__npc__agent_id__in=FORMAL_NPC_IDS, item_amount__gt=0
    )
    tool_uses = sum(
        row.remaining_uses if row.remaining_uses is not None else row.item_amount
        for row in slots.filter(item_id__in=TOOL_ITEM_IDS)
    )
    food_amount = slots.filter(item_id__in=FOOD_ITEM_IDS).aggregate(total=Sum("item_amount"))["total"] or 0
    return round(food_amount / 5, 2), round(tool_uses / 2, 2)


def _fund_delta(entry: EconomicLedgerEntry) -> int:
    value = 0
    public_fund = (entry.changes or {}).get("PUBLIC_FUND")
    if isinstance(public_fund, dict):
        try:
            value += int(public_fund.get("money", 0) or 0)
        except (TypeError, ValueError):
            pass
    if entry.entry_type in {"IMPORT_GOODS", "EXPORT_GOODS"}:
        value += int(entry.external_delta or 0)
    return value


def _public_economy(world_minute: int) -> dict:
    day_index = world_minute // 1440
    food_days, tool_days = _coverage_days()
    prices = [
        {
            "day_index": row.day_index,
            "prices": {
                str(item_id): (row.prices or {}).get(str(item_id))
                for item_id in PRICE_ITEM_IDS
                if str(item_id) in (row.prices or {})
            },
        }
        for row in DailyPriceSnapshot.objects.filter(status="PUBLISHED")
        .order_by("-day_index")[:7]
    ]
    today = EconomicLedgerEntry.objects.filter(
        world_minute__gte=day_index * 1440,
        world_minute__lt=(day_index + 1) * 1440,
    )
    import_total = abs(today.filter(entry_type="IMPORT_GOODS").aggregate(total=Sum("external_delta"))["total"] or 0)
    export_total = today.filter(entry_type="EXPORT_GOODS").aggregate(total=Sum("external_delta"))["total"] or 0
    uses = []
    for entry in EconomicLedgerEntry.objects.order_by("-world_minute", "-id")[:100]:
        delta = _fund_delta(entry)
        if delta < 0:
            uses.append({
                "entry_type": entry.entry_type,
                "world_minute": entry.world_minute,
                "amount": abs(delta),
            })
        if len(uses) == 10:
            break
    orders = [
        {
            "order_id": str(row.order_id),
            "item_id": row.item_id,
            "amount": row.amount,
            "unit_price": row.unit_price,
            "delivery_point_code": row.delivery_point_code,
            "expires_world_minute": row.expires_world_minute,
        }
        for row in NpcTradeOrder.objects.filter(side="BUY", status="OPEN")
        .order_by("created_at", "order_id")[:10]
    ]
    fund = VillageAccount.objects.filter(account_code="PUBLIC_FUND").values_list("balance", flat=True).first() or 0
    return {
        "food_stock_coverage_days": food_days,
        "tool_stock_coverage_days": tool_days,
        "price_history": prices,
        "today_import_total": import_total,
        "today_export_total": export_total,
        "public_fund_balance": fund,
        "recent_public_fund_uses": uses,
        "public_orders": orders,
    }


def _public_story(player_id: str) -> dict:
    arc = VillageStoryArc.objects.order_by("arc_code").first()
    if not arc:
        return {}
    fact = getattr(arc, "facts", None)
    progress = {}
    if fact is not None and arc.current_plan == "BRIDGE_REPAIR":
        progress = {
            "bridge_condition": fact.bridge_condition,
            "repair_fund": fact.repair_fund,
            "repair_wood": fact.repair_wood,
            "repair_metal": fact.repair_metal,
            "repair_labor_hours": fact.repair_labor_hours,
            "community_meals": fact.community_meals,
        }
    elif fact is not None and arc.current_plan == "FOREST_ROUTE":
        progress = {
            "forest_route_scouted": fact.forest_route_scouted,
            "forest_route_supply": fact.forest_route_supply,
        }
        if fact.emergency_caravan_contacted:
            progress["emergency_caravan_status"] = (
                "SUCCEEDED" if fact.emergency_caravan_succeeded else "FAILED"
            )
    elif fact is not None and arc.current_plan == "RATION_AND_WAIT":
        progress = {
            "ration_rules_published": fact.ration_rules_published,
            "store_limits_enabled": fact.store_limits_enabled,
            "community_meals": fact.community_meals,
        }
        if fact.store_limits_enabled:
            progress["ration_daily_limits"] = dict(fact.ration_daily_limits or {})
    transition = VillageStoryTransitionReceipt.objects.filter(
        arc=arc, applied=True
    ).order_by("-created_at").first()
    contribution_events = []
    for event in VillageWorldEvent.objects.filter(event_type__startswith="STORY_").order_by("-event_sequence")[:100]:
        if player_id in {str(value) for value in (event.player_ids or [])}:
            contribution_events.append(str(event.event_id))
            if len(contribution_events) == 10:
                break
    result = {
        "arc_code": arc.arc_code,
        "public_problem": arc.current_phase,
        "public_plan": arc.current_plan or None,
        "progress": progress,
        "player_contribution": {
            "event_count": len(contribution_events),
            "event_ids": contribution_events,
        },
        "recent_transition_reason": (
            transition.world_event.player_summary if transition else ""
        ),
    }
    if arc.is_completed:
        result["outcome"] = arc.outcome
    return result


def _location_is_open(opening_hours: dict, world_minute: int) -> bool:
    hours = opening_hours if isinstance(opening_hours, dict) else {}
    if hours.get("alwaysOpen") is True or not hours:
        return True
    if hours.get("alwaysOpen") is not False:
        return False
    open_minute = hours.get("openMinute")
    close_minute = hours.get("closeMinute")
    if (
        isinstance(open_minute, bool) or not isinstance(open_minute, int)
        or isinstance(close_minute, bool) or not isinstance(close_minute, int)
        or not 0 <= open_minute < 1440
        or not 0 <= close_minute < 1440
        or open_minute == close_minute
    ):
        return False
    minute_of_day = world_minute % 1440
    if open_minute < close_minute:
        return open_minute <= minute_of_day < close_minute
    return minute_of_day >= open_minute or minute_of_day < close_minute


def public_overview(player_id: str) -> dict:
    time_state = GlobalGameTimeState.objects.filter(pk=1).first()
    world_minute = time_state.world_minute if time_state else 0
    yesterday = max(0, world_minute // 1440 - 1)
    yesterday_events = public_journal(player_id, {"limit": 100, "day_index": yesterday})["events"]
    per_npc = {}
    for event in yesterday_events:
        npc_id = event["actor_npc_id"]
        if npc_id and npc_id not in per_npc:
            per_npc[npc_id] = {
                "npc_id": npc_id,
                "event_type": event["event_type"],
                "player_summary": event["player_summary"],
            }
    locations = [
        {
            "location_code": row.location_code,
            "display_name": row.display_name,
            "location_type": row.location_type,
            "is_open": _location_is_open(row.opening_hours, world_minute),
        }
        for row in VillageLocationDefinition.objects.filter(is_active=True, is_public=True)
        .order_by("location_code")[:100]
    ]
    return {
        "yesterday_summary": {
            "day_index": yesterday,
            "npc_actions": [
                per_npc.get(npc_id, {
                    "npc_id": npc_id,
                    "event_type": None,
                    "player_summary": "",
                })
                for npc_id in FORMAL_NPC_IDS
            ],
            "event_count": len(yesterday_events),
        },
        "economy": _public_economy(world_minute),
        "story": _public_story(player_id),
        "locations": locations,
    }


def public_npc_observation(npc_id: str) -> dict | None:
    profile = NpcProfile.objects.select_related("npc").filter(
        npc__agent_id=npc_id, npc__is_test_only=False, is_active=True
    ).first()
    if profile is None:
        return None
    runtime = NpcRuntimeState.objects.filter(npc=profile.npc).first()
    if runtime is None:
        return None
    goal = NpcSubjectiveGoalState.objects.filter(
        npc=profile.npc, status="ACTIVE", visibility="PUBLIC"
    ).first()
    names = {
        str(row.npc.agent_id): row.display_name
        for row in NpcProfile.objects.select_related("npc").filter(is_active=True)
    }
    location_names = dict(VillageLocationDefinition.objects.filter(
        is_active=True, is_public=True
    ).values_list("location_code", "display_name"))
    open_commitments = NpcCommitment.objects.filter(status="OPEN").filter(
        Q(promisor_npc=profile.npc) | Q(beneficiary_npc=profile.npc)
    ).select_related("promisor_npc", "beneficiary_npc").order_by("due_world_minute")[:20]
    commitment_rows = list(open_commitments)
    relationships = []
    for row in NpcRelationship.objects.filter(from_npc=profile.npc).select_related("to_npc"):
        other_id = str(row.to_npc.agent_id)
        has_open = any(
            {item.promisor_npc_id, item.beneficiary_npc_id} == {profile.npc_id, row.to_npc_id}
            for item in commitment_rows
        )
        relationships.append({
            "other_npc_id": other_id,
            "other_display_name": names.get(other_id, row.to_npc.name),
            "label": relationship_label(relationship_snapshot(row), has_open),
            "has_open_commitment": has_open,
        })
    recent_receipts = list(
        VillageActionReceipt.objects.filter(npc=profile.npc).filter(
            Q(action_kind__in=AGENT_INSTRUCTION_ACTIONS)
            | Q(command__kind__in=AGENT_INSTRUCTION_ACTIONS)
        ).select_related("command").order_by("-world_minute", "-created_at")
    )
    waiting_commands = list(NpcCommandState.objects.filter(
        npc=profile.npc, kind__in=AGENT_INSTRUCTION_ACTIONS,
        status__in=["RECEIVED", "QUEUED"],
    ).order_by("-received_world_minute", "-created_at"))
    history_rows = []
    for receipt in recent_receipts:
        command = receipt.command
        action_kind = (
            command.kind if command and command.kind in AGENT_INSTRUCTION_ACTIONS
            else receipt.action_kind
        )
        raw_parameters = (command.sanitized_parameters if command else
                          (receipt.response_data or {}).get("parameters", {}))
        parameters = dict(raw_parameters) if isinstance(raw_parameters, dict) else {}
        response_data = receipt.response_data or {}
        if action_kind == "EAT" and parameters.get("item_id") is None:
            effects = response_data.get("effects", {})
            eaten = effects.get("eat", {}).get("items_consumed", []) if isinstance(effects, dict) else []
            eaten = eaten or (effects.get("items", []) if isinstance(effects, dict) else [])
            if eaten and isinstance(eaten[0], dict):
                parameters["item_id"] = eaten[0].get("item_id")
        if action_kind == "UPDATE_SUBJECTIVE_GOAL":
            goal_data = response_data.get("subjective_goal")
            if isinstance(goal_data, dict) and goal_data.get("summary"):
                parameters["summary"] = goal_data["summary"]
        history_rows.append((receipt, action_kind, parameters, command.duration_minutes if command else None))
    active_command = NpcCommandState.objects.filter(
        npc=profile.npc, command_id=runtime.active_command_id
    ).first() if runtime.active_command_id else None
    item_ids = set()
    marketplace_listing_ids = set()
    instance_ids, store_listing_ids = set(), set()
    recipe_ids, order_ids, delivery_ids = set(), set(), set()
    offer_ids, commitment_ids = set(), set()

    def add_uuid(target: set, value: object) -> None:
        try:
            if value:
                target.add(uuid.UUID(str(value)))
        except (TypeError, ValueError, AttributeError):
            pass

    parameter_rows = [(parameters, action_kind) for _, action_kind, parameters, _ in history_rows]
    parameter_rows.extend((command.sanitized_parameters, command.kind) for command in waiting_commands)
    if active_command:
        parameter_rows.append((active_command.sanitized_parameters, active_command.kind))
    for params, action in parameter_rows:
        params = params if isinstance(params, dict) else {}
        for key in ("item_id", "supply_item_id", "limit_item_id", "meal_item_id"):
            try:
                if params.get(key) is not None:
                    item_ids.add(int(params[key]))
            except (TypeError, ValueError):
                pass
        if action == "BUY_FROM_MARKETPLACE":
            try:
                marketplace_listing_ids.add(int(params.get("listing_id")))
            except (TypeError, ValueError):
                pass
        if action == "DELIST_FROM_MARKETPLACE":
            try:
                marketplace_listing_ids.add(int(params.get("listing_id")))
            except (TypeError, ValueError):
                pass
        add_uuid(instance_ids, params.get("food_instance_id"))
        add_uuid(instance_ids, params.get("instance_id"))
        if action in {"UPDATE_STORE_PRICE", "DELIST_STORE_ITEM", "BUY_FROM_STORE"}:
            add_uuid(store_listing_ids, params.get("listing_id"))
        if action == "COOK":
            try:
                recipe_ids.add(int(params.get("recipe_id")))
            except (TypeError, ValueError):
                pass
        add_uuid(order_ids, params.get("order_id"))
        add_uuid(delivery_ids, params.get("delivery_id"))
        add_uuid(offer_ids, params.get("offer_id"))
        add_uuid(commitment_ids, params.get("commitment_id"))

    instance_rows = list(NpcGlobalInventorySlot.objects.filter(
        instance_id__in=instance_ids
    ).values("instance_id", "item_id"))
    store_listing_rows = list(VillageStoreListing.objects.filter(
        listing_id__in=store_listing_ids
    ).values("listing_id", "item_id"))
    recipe_rows = list(Recipe.objects.filter(pk__in=recipe_ids).values("id", "recipe_name", "item_id"))
    order_rows = list(NpcTradeOrder.objects.filter(order_id__in=order_ids).values(
        "order_id", "side", "item_id", "amount"))
    delivery_rows = list(NpcTradeDelivery.objects.filter(delivery_id__in=delivery_ids).values(
        "delivery_id", "buyer_order__item_id", "reserved_amount", "delivery_point_code"))
    offer_rows = list(VillageTradeOffer.objects.filter(offer_id__in=offer_ids).values(
        "offer_id", "item_id", "amount"))
    commitment_detail_rows = list(NpcCommitment.objects.filter(
        commitment_id__in=commitment_ids
    ).values("commitment_id", "commitment_type", "beneficiary_npc__agent_id"))
    item_ids.update(row["item_id"] for row in instance_rows)
    item_ids.update(row["item_id"] for row in store_listing_rows)
    item_ids.update(row["item_id"] for row in order_rows)
    item_ids.update(row["buyer_order__item_id"] for row in delivery_rows)
    item_ids.update(row["item_id"] for row in offer_rows)
    marketplace_listings = list(MarketListing.objects.filter(
        pk__in=marketplace_listing_ids
    ).values("id", "item_id", "item_name_snapshot"))
    item_ids.update(row["item_id"] for row in marketplace_listings)
    item_names = dict(Item.objects.filter(item_id__in=item_ids).values_list("item_id", "item_name"))
    marketplace_item_names = {
        str(row["id"]): row["item_name_snapshot"] or item_names.get(row["item_id"], f"物品#{row['item_id']}")
        for row in marketplace_listings
    }
    details = {
        "instances": {str(row["instance_id"]): row for row in instance_rows},
        "store_listings": {str(row["listing_id"]): row for row in store_listing_rows},
        "recipes": {str(row["id"]): row["recipe_name"] for row in recipe_rows},
        "orders": {str(row["order_id"]): row for row in order_rows},
        "deliveries": {
            str(row["delivery_id"]): {
                "item_id": row["buyer_order__item_id"], "amount": row["reserved_amount"],
                "delivery_point_code": row["delivery_point_code"],
            }
            for row in delivery_rows
        },
        "offers": {str(row["offer_id"]): row for row in offer_rows},
        "commitments": {
            str(row["commitment_id"]): {
                "commitment_type": row["commitment_type"],
                "beneficiary_npc_id": row["beneficiary_npc__agent_id"],
            }
            for row in commitment_detail_rows
        },
    }
    time_state = GlobalGameTimeState.objects.filter(pk=1).first()
    current_world_minute = time_state.world_minute if time_state else 0
    ordered_commands = []
    for receipt, action_kind, parameters, duration in history_rows:
        command = receipt.command
        status = (
            command.status if command and command.status in {"CANCELLED", "EXPIRED"}
            else "SUCCEEDED" if receipt.success else "FAILED"
        )
        row = {
            "name": action_kind,
            "summary": _public_command_summary(
                action_kind, parameters, duration,
                names, location_names, item_names, marketplace_item_names,
                profile.home_location_code, details
            ),
            "status": status,
            "result_code": receipt.result_code,
            "received_world_minute": command.received_world_minute if command else None,
            "started_world_minute": command.started_world_minute if command else None,
            "completed_world_minute": receipt.world_minute,
        }
        row["detail"] = _public_command_detail(
            action_kind, parameters, duration, status, receipt.result_code,
            receipt.response_data, row["received_world_minute"], row["started_world_minute"],
            row["completed_world_minute"], current_world_minute,
            names, location_names, item_names, details,
        )
        order_minute = (
            command.received_world_minute if command and command.received_world_minute is not None
            else command.started_world_minute if command and command.started_world_minute is not None
            else receipt.world_minute
        )
        ordered_commands.append((order_minute, command.created_at if command else receipt.created_at, row))
    for command in waiting_commands:
        row = {
            "name": command.kind,
            "summary": _public_command_summary(
                command.kind, command.sanitized_parameters, command.duration_minutes,
                names, location_names, item_names, marketplace_item_names,
                profile.home_location_code, details
            ),
            "status": "WAITING",
            "result_code": "",
            "received_world_minute": command.received_world_minute,
            "started_world_minute": None,
            "completed_world_minute": None,
        }
        row["detail"] = _public_command_detail(
            command.kind, command.sanitized_parameters, command.duration_minutes,
            "WAITING", "", {}, row["received_world_minute"], None, None,
            current_world_minute, names, location_names, item_names, details,
        )
        ordered_commands.append((command.received_world_minute or -1, command.created_at, row))
    if active_command:
        row = {
            "name": active_command.kind,
            "summary": _public_command_summary(
                active_command.kind, active_command.sanitized_parameters, active_command.duration_minutes,
                names, location_names, item_names, marketplace_item_names,
                profile.home_location_code, details
            ),
            "status": "RUNNING",
            "result_code": "",
            "received_world_minute": active_command.received_world_minute,
            "started_world_minute": active_command.started_world_minute,
            "completed_world_minute": None,
        }
        row["detail"] = _public_command_detail(
            active_command.kind, active_command.sanitized_parameters, active_command.duration_minutes,
            "RUNNING", "", {}, row["received_world_minute"], row["started_world_minute"], None,
            current_world_minute, names, location_names, item_names, details,
        )
        order_minute = (
            active_command.received_world_minute
            if active_command.received_world_minute is not None
            else active_command.started_world_minute or -1
        )
        ordered_commands.append((order_minute, active_command.created_at, row))
    ordered_commands.sort(key=lambda item: (item[0], item[1]), reverse=True)
    recent_commands = [item[2] for item in ordered_commands]
    dialogue = NpcDialogueSession.objects.filter(
        Q(initiator=profile.npc) | Q(target=profile.npc), status="ACTIVE"
    ).select_related("initiator", "target").first()
    if dialogue:
        other = dialogue.target if dialogue.initiator_id == profile.npc_id else dialogue.initiator
        action_summary = f"与{names.get(str(other.agent_id), other.name)}交流"
    elif active_command:
        action_summary = _public_command_summary(
            active_command.kind, active_command.sanitized_parameters, active_command.duration_minutes,
            names, location_names, item_names, marketplace_item_names,
            profile.home_location_code, details
        )
    else:
        action = runtime.current_action or "IDLE"
        action_summary = COMMAND_CATALOG.get(action, {}).get("label", "空闲")
    return {
        "npc_id": npc_id,
        "display_name": profile.display_name,
        "world_minute": current_world_minute,
        "public_needs": {
            "hunger": runtime.hunger_fp / 10,
            "energy": runtime.energy_fp / 10,
            "health": runtime.health_fp / 10,
            "social": runtime.social_fp / 10,
            "stress": runtime.stress_fp / 10,
        },
        "public_status": {
            "action": "SOCIALIZING" if dialogue else runtime.current_action or "IDLE",
            "action_status": "SOCIAL" if dialogue else runtime.action_status,
            "summary": action_summary,
        },
        "subjective_goal": ({
            "goal_id": str(goal.goal_id),
            "summary": goal.summary,
            "status": goal.status,
            "goal_version": goal.goal_version,
            "updated_world_minute": goal.updated_world_minute,
        } if goal else None),
        "recent_commands": recent_commands,
        "relationships": relationships,
        "commitments": [
            {
                "commitment_id": str(row.commitment_id),
                "commitment_type": row.commitment_type,
                "promisor_npc_id": str(row.promisor_npc.agent_id),
                "promisor_display_name": names.get(str(row.promisor_npc.agent_id), row.promisor_npc.name),
                "beneficiary_npc_id": str(row.beneficiary_npc.agent_id),
                "beneficiary_display_name": names.get(str(row.beneficiary_npc.agent_id), row.beneficiary_npc.name),
                "status": row.status,
                "due_world_minute": row.due_world_minute,
            }
            for row in commitment_rows
        ],
    }


def operations_snapshot() -> dict:
    now = timezone.now()
    time_state = GlobalGameTimeState.objects.filter(pk=1).first()
    runtimes = list(
        NpcRuntimeState.objects.select_related("npc").filter(
            npc__agent_id__in=FORMAL_NPC_IDS
        ).order_by("npc__agent_id")[:100]
    )
    dialogue_npc_ids = set()
    for dialogue in NpcDialogueSession.objects.filter(status="ACTIVE").values_list(
        "initiator__agent_id", "target__agent_id"
    ):
        dialogue_npc_ids.update(str(npc_id) for npc_id in dialogue)
    recent = {
        str(runtime.npc.agent_id): [
            {
                "request_id": str(row.request_id),
                "result_code": row.result_code,
                "success": row.success,
                "world_minute": row.world_minute,
            }
            for row in VillageActionReceipt.objects.filter(npc=runtime.npc)
            .order_by("-created_at")[:5]
        ]
        for runtime in runtimes
    }
    npcs = [{
        "npc_id": str(row.npc.agent_id),
        "location_code": row.current_location_code,
        "action_status": "DIALOGUING" if str(row.npc.agent_id) in dialogue_npc_ids else row.action_status,
        "current_goal": row.current_goal,
        "current_action": "DIALOGUE" if str(row.npc.agent_id) in dialogue_npc_ids else row.current_action,
        "action_started_world_minute": row.action_started_world_minute,
        "action_ends_world_minute": row.action_ends_world_minute,
        "active_command_id": str(row.active_command_id) if row.active_command_id else None,
        "state_version": row.state_version,
        "time_revision": row.time_revision,
        "needs_world_minute": row.needs_world_minute,
        "needs": {
            "hunger": row.hunger_fp / 10,
            "energy": row.energy_fp / 10,
            "health": row.health_fp / 10,
            "social": row.social_fp / 10,
            "stress": row.stress_fp / 10,
        },
        "recent_results": recent[str(row.npc.agent_id)],
    } for row in runtimes]
    inventories = [{
        "npc_id": str(row.npc.agent_id),
        "money": row.money,
        "asset_version": row.asset_version,
        "items": [
            {
                "instance_id": str(slot.instance_id),
                "item_id": slot.item_id,
                "amount": slot.item_amount,
                "remaining_uses": slot.remaining_uses,
            }
            for slot in row.slots.order_by("slot_index")[:100]
        ],
    } for row in NpcGlobalInventory.objects.select_related("npc").filter(
        npc__agent_id__in=FORMAL_NPC_IDS
    ).order_by("npc__agent_id")[:100]]
    accounts = [{
        "account_code": row.account_code,
        "balance": row.balance,
        "asset_version": row.asset_version,
        "last_caravan_day": row.last_caravan_day,
        "last_closed_day_index": row.last_closed_day_index,
    } for row in VillageAccount.objects.order_by("account_code")[:100]]
    ledger_entries = [{
        "request_id": str(row.request_id),
        "entry_type": row.entry_type,
        "actor_npc_id": str(row.actor_npc.agent_id) if row.actor_npc else None,
        "changes": dict(row.changes or {}),
        "external_delta": row.external_delta,
        "world_minute": row.world_minute,
        "event_id": str(row.event.event_id) if row.event else None,
    } for row in EconomicLedgerEntry.objects.select_related(
        "actor_npc", "event"
    ).order_by("-created_at")[:100]]
    orders = [{
        "order_id": str(row.order_id),
        "request_id": str(row.request_id),
        "npc_id": str(row.npc.agent_id),
        "side": row.side,
        "item_id": row.item_id,
        "amount": row.amount,
        "unit_price": row.unit_price,
        "delivery_point_code": row.delivery_point_code,
        "expires_world_minute": row.expires_world_minute,
        "status": row.status,
        "matched_order_id": str(row.matched_order_id) if row.matched_order_id else None,
    } for row in NpcTradeOrder.objects.select_related("npc").order_by("-created_at")[:100]]
    deliveries = [{
        "delivery_id": str(row.delivery_id),
        "buyer_order_id": str(row.buyer_order_id),
        "seller_order_id": str(row.seller_order_id),
        "seller_slot_instance_id": str(row.seller_slot_instance_id),
        "reserved_amount": row.reserved_amount,
        "reserved_money": row.reserved_money,
        "delivery_point_code": row.delivery_point_code,
        "expires_world_minute": row.expires_world_minute,
        "status": row.status,
    } for row in NpcTradeDelivery.objects.order_by("-created_at")[:100]]
    relationships = [{
        "from_npc_id": str(row.from_npc.agent_id),
        "to_npc_id": str(row.to_npc.agent_id),
        "affinity": row.affinity,
        "trust": row.trust,
        "obligation": row.obligation,
        "conflict": row.conflict,
        "version": row.version,
        "last_meaningful_event_id": (
            str(row.last_meaningful_event.event_id) if row.last_meaningful_event else None
        ),
    } for row in NpcRelationship.objects.select_related(
        "from_npc", "to_npc", "last_meaningful_event"
    ).order_by("from_npc__agent_id", "to_npc__agent_id")[:100]]
    commitments = [{
        "commitment_id": str(row.commitment_id),
        "commitment_type": row.commitment_type,
        "promisor_npc_id": str(row.promisor_npc.agent_id),
        "beneficiary_npc_id": str(row.beneficiary_npc.agent_id),
        "target": dict(row.target or {}),
        "due_world_minute": row.due_world_minute,
        "status": row.status,
        "created_event_id": str(row.created_event.event_id),
        "settled_event_id": str(row.settled_event.event_id) if row.settled_event else None,
    } for row in NpcCommitment.objects.select_related(
        "promisor_npc", "beneficiary_npc", "created_event", "settled_event"
    ).order_by("-created_at")[:100]]
    fact_access = [{
        "npc_id": str(row.npc.agent_id),
        "fact_code": row.fact_code,
        "unlocked_world_minute": row.unlocked_world_minute,
        "is_public": row.is_public,
        "unlocked_by_event_id": (
            str(row.unlocked_by_event.event_id) if row.unlocked_by_event else None
        ),
    } for row in NpcStoryFactAccess.objects.select_related(
        "npc", "unlocked_by_event"
    ).order_by("npc__agent_id", "fact_code")[:100]]
    arcs = [{
        "arc_code": row.arc_code,
        "current_phase": row.current_phase,
        "current_plan": row.current_plan,
        "story_version": row.story_version,
        "is_completed": row.is_completed,
        "outcome": row.outcome,
        "start_day": row.start_day,
        "phase_entered_day": row.phase_entered_day,
        "last_crisis_day": row.last_crisis_day,
        "ration_until_day": row.ration_until_day,
        "review_count": row.review_count,
    } for row in VillageStoryArc.objects.order_by("arc_code")[:100]]
    story_facts = [{
        "arc_code": row.arc_id,
        "bridge_condition": row.bridge_condition,
        "tool_stock_coverage_days": float(row.tool_stock_coverage_days),
        "food_stock_coverage_days": float(row.food_stock_coverage_days),
        "repair_fund": row.repair_fund,
        "repair_wood": row.repair_wood,
        "repair_metal": row.repair_metal,
        "repair_labor_hours": row.repair_labor_hours,
        "public_trust": row.public_trust,
        "forest_route_known": row.forest_route_known,
        "forest_route_scouted": row.forest_route_scouted,
        "forest_route_supply": row.forest_route_supply,
        "emergency_caravan_contacted": row.emergency_caravan_contacted,
        "emergency_caravan_succeeded": row.emergency_caravan_succeeded,
        "bill_warning_revealed": row.bill_warning_revealed,
        "mark_budget_delay_revealed": row.mark_budget_delay_revealed,
        "ration_rules_published": row.ration_rules_published,
        "store_limits_enabled": row.store_limits_enabled,
        "ration_daily_limits": dict(row.ration_daily_limits or {}),
        "community_meals": row.community_meals,
        "last_progress_day": row.last_progress_day,
        "stagnant_days": row.stagnant_days,
    } for row in VillageStoryFact.objects.order_by("arc_id")[:100]]
    transitions = [{
        "request_id": str(row.request_id),
        "arc_code": row.arc_id,
        "from_phase": row.from_phase,
        "to_phase": row.to_phase,
        "selected_plan": row.selected_plan,
        "source_day": row.source_day,
        "event_id": str(row.world_event.event_id),
        "applied": row.applied,
        "response_data": dict(row.response_data or {}),
    } for row in VillageStoryTransitionReceipt.objects.select_related(
        "world_event"
    ).order_by("-created_at")[:100]]
    commands = [{
        "command_id": str(row.command_id),
        "npc_id": str(row.npc.agent_id),
        "kind": row.kind,
        "target_id": row.target_id,
        "location_code": row.location_code,
        "parameters": dict(row.sanitized_parameters or {}),
        "status": row.status,
        "result_code": row.result_code,
        "received_world_minute": row.received_world_minute,
    } for row in NpcCommandState.objects.select_related("npc").order_by("-created_at")[:100]]
    locks = [{
        "reservation_id": str(row.reservation_id),
        "action_kind": row.action_kind,
        "participant_npc_ids": [str(npc.agent_id) for npc in row.participants.all()],
        "resource_key": row.resource_key,
        "status": row.status,
        "expires_world_minute": row.expires_world_minute,
    } for row in NpcActionReservation.objects.prefetch_related("participants").order_by("-created_at")[:100]]
    counts = {
        f"{row['message_kind'].lower()}_{row['status'].lower()}": row["count"]
        for row in VillageDeliveryOutbox.objects.values("message_kind", "status")
        .annotate(count=Count("delivery_id"))
    }
    oldest = VillageDeliveryOutbox.objects.exclude(status="ACKED").aggregate(oldest=Min("created_at"))["oldest"]
    attempts = VillageDeliveryOutbox.objects.aggregate(max_attempts=Max("attempt_count"))["max_attempts"] or 0
    outbox_rows = [{
        "delivery_id": str(row.delivery_id),
        "recipient": row.recipient,
        "message_kind": row.message_kind,
        "status": row.status,
        "attempt_count": row.attempt_count,
        "event_sequence": row.event.event_sequence if row.event else None,
        "last_error_code": row.last_error_code,
    } for row in VillageDeliveryOutbox.objects.select_related(
        "event"
    ).order_by("-created_at")[:100]]
    cursors = [{
        "recipient": row.recipient,
        "last_event_sequence": row.last_event_sequence,
        "updated_at": row.updated_at.isoformat(),
    } for row in VillageDeliveryCursor.objects.order_by("recipient")[:100]]
    receipt_metrics = VillageActionReceipt.objects.aggregate(
        total=Count("request_id"),
        failures=Count("request_id", filter=Q(success=False)),
    )
    receipt_total = receipt_metrics["total"] or 0
    command_status_counts = {
        row["status"].lower(): row["count"]
        for row in NpcCommandState.objects.values("status").annotate(count=Count("command_id"))
    }
    outbox = {
        "counts": counts,
        "oldest_unacked_age_seconds": max(0, int((now - oldest).total_seconds())) if oldest else 0,
        "max_attempt_count": attempts,
        "deliveries": outbox_rows,
        "cursors": cursors,
    }
    technical = {
        "world_time": {
            "world_minute": time_state.world_minute if time_state else None,
            "time_revision": time_state.time_revision if time_state else None,
        },
        "commands": commands,
        "command_status_counts": command_status_counts,
        "queue_length": sum(
            command_status_counts.get(status, 0) for status in ("received", "queued")
        ),
        "locks": locks,
        "outbox": outbox,
        "checkpoint": {
            "last_success_at": "unavailable",
            "latency_ms": "unavailable",
            "runtime_state_last_updated_at": max(
                (row.updated_at for row in runtimes), default=None
            ).isoformat() if runtimes else None,
        },
        "backend_settlement_failure_rate": (
            round(receipt_metrics["failures"] / receipt_total, 6) if receipt_total else 0
        ),
        "tick_latency_ms": {"p50": "unavailable", "p95": "unavailable", "p99": "unavailable"},
        "hub_failure_rate": "unavailable",
    }
    return {
        "npc": {"states": npcs},
        "economy": {
            "inventories": inventories,
            "accounts": accounts,
            "ledger_entries": ledger_entries,
            "orders": orders,
            "deliveries": deliveries,
        },
        "social": {
            "relationships": relationships,
            "commitments": commitments,
            "fact_access": fact_access,
        },
        "story": {
            "arcs": arcs,
            "facts": story_facts,
            "transition_receipts": transitions,
        },
        "technical": technical,
        # Keep the Phase 7 debug panel's existing read paths while it migrates
        # to the grouped operator DTO above.
        "npcs": npcs,
        "inventories": inventories,
        "commands": commands,
        "locks": locks,
        "outbox": outbox,
    }
