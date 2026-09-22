"""Player store settlements under the player action savepoint."""
import copy
import uuid

from django.db.models import Max

from inventory.components import COMPONENT_FIELDS
from inventory.models import InventorySlot
from inventory.serializers import serialize_bag
from items.models import Item
from .models import (EconomicLedgerEntry, VillageAccount, VillageStore,
                     VillageStoreAccount, VillageStoreInventorySlot, VillageStoreListing,
                     VillageWorldEvent)
from .phase3_direct_trade import _compute_fee, _consume_store_fefo_available, _TRADABLE_STORE_STATUSES
from .phase5_story import get_store_purchase_limit


def store_sale_quote(slot, item, minute):
    from .phase3_direct_trade import general_store_tool_sale_reason
    reason = None
    if item is None or item.is_deleted or item.item_price <= 0:
        reason = "该物品暂无有效收购价格"
    elif slot.item_amount <= 0:
        reason = "该批次已用完"
    elif slot.freshness_unknown:
        reason = "保鲜状态待核实，暂不能出售"
    elif slot.expires_world_minute is not None and slot.expires_world_minute <= minute:
        reason = "该批次已过期"
    else:
        tool_reason = general_store_tool_sale_reason(item.item_type, item.max_use_count, slot.remaining_uses)
        if tool_reason:
            reason = "仅收购满耐久工具" if tool_reason == "TOOL_NOT_FULL_DURABILITY" else "工具耐久无效或未核实，暂不能收购"
    return dict(allowed=reason is None, reason=reason,
                unit_price=item.item_price if item is not None and not item.is_deleted and item.item_price > 0 else None,
                available_quantity=slot.item_amount if reason is None else 0)


def sell_to_store(player, bag, params, request_id, location, clock):
    from .player_actions import fail, key, positive
    quantity = positive(params.get("quantity"))
    store = VillageStore.objects.select_for_update().filter(pk="GENERAL_STORE").first()
    if store is None or location != "GENERAL_STORE" or store.location_code != location:
        fail("LOCATION_INVALID", "请前往 Nora 杂货铺。")
    if store.status != "OPEN":
        fail("TRADE_STORE_CLOSED", "杂货铺当前未营业。")
    account = VillageStoreAccount.objects.select_for_update().filter(store=store).first()
    slot = InventorySlot.objects.select_for_update().filter(bag=bag, instance_id=key(params.get("instance_id"))).first()
    if slot is None:
        fail("INVALID_ITEM", "本人背包中已找不到这个物品批次。")
    item = Item.objects.filter(pk=slot.item_id, is_deleted=False).first()
    quote = store_sale_quote(slot, item, clock.world_minute)
    if not quote["allowed"]:
        fail("TRADE_ITEM_NOT_FOR_SALE", quote["reason"])
    if type(params.get("expected_unit_price")) is not int or params["expected_unit_price"] != quote["unit_price"]:
        fail("PRICE_CHANGED", "收购价格已变化，请核对后重新确认。")
    if quantity > slot.item_amount:
        fail("TRADE_INSUFFICIENT_ITEM", "该批次可出售数量不足。")
    total = quote["unit_price"] * quantity
    if account is None or account.balance < total:
        fail("TRADE_INSUFFICIENT_MONEY", "商店金币不足，无法收购。")
    target = VillageStoreInventorySlot(store=store, item_id=slot.item_id, item_amount=quantity,
        quality=slot.quality, produced_world_minute=slot.produced_world_minute,
        expires_world_minute=slot.expires_world_minute, source_slot_instance_id=slot.instance_id,
        common_attributes=dict(freshness_unknown=slot.freshness_unknown, item_bag_name=slot.item_bag_name,
            source_player_id=str(player.pk), source_request_id=str(slot.source_request_id) if slot.source_request_id else None))
    for name in COMPONENT_FIELDS:
        setattr(target, name, copy.deepcopy(getattr(slot, name)))
    target.save()
    slot.item_amount -= quantity
    slot.save(update_fields=["item_amount"])
    bag.money += total
    bag._money_change_source = "village_store_sell"
    bag.save(update_fields=["money"])
    account.balance -= total
    account.account_version += 1
    account.save(update_fields=["balance", "account_version", "updated_at"])
    store.inventory_version += 1
    store.save(update_fields=["inventory_version", "updated_at"])
    result = dict(store_code=store.pk, instance_id=str(slot.instance_id), item_id=item.pk,
        item_name=item.item_name, amount=quantity, quantity=quantity, unit_price=quote["unit_price"],
        gold_amount=total, sale_mode="IMMEDIATE_STORE", player_id=str(player.pk),
        player_display_name=player.username, affected_npc_ids=[store.operator_npc.agent_id] if store.operator_npc_id else [])
    summary = f"{player.username}向{store.display_name}出售{item.item_name}×{quantity}，获得{total}金币"
    event = VillageWorldEvent.objects.create(dedupe_key=f"player:{player.pk}:{request_id}",
        request_id=request_id, event_type="ECONOMY_SELL_TO_STORE", world_minute=clock.world_minute,
        time_revision=clock.time_revision, player_ids=[str(player.pk)], location_code=location,
        participant_npc_ids=result["affected_npc_ids"], visibility="PARTICIPANTS", player_summary=summary,
        hub_summary=summary, private_data=result)
    EconomicLedgerEntry.objects.create(request_id=uuid.uuid5(uuid.NAMESPACE_URL, f"player:{player.pk}:{request_id}"),
        entry_type="SELL_TO_STORE", actor_npc=None, world_minute=clock.world_minute, event=event,
        changes={**result, f"PLAYER:{player.pk}": {"money": total, str(item.pk): -quantity},
                 f"STORE:{store.pk}": {"money": -total, str(item.pk): quantity}})
    from .phase4_visibility import enqueue_event_deliveries
    enqueue_event_deliveries(event)
    result["bag"] = serialize_bag(bag)
    return result


def buy_from_store(player, bag, params, request_id, location, clock):
    from .player_actions import fail, key, positive
    quantity = positive(params.get("quantity"))
    reference = VillageStoreListing.objects.filter(pk=key(params.get("listing_id"))).first()
    if reference is None:
        fail("LISTING_NOT_FOUND", "商品已不存在。")
    store = VillageStore.objects.select_for_update().get(pk=reference.store_id)
    listing = VillageStoreListing.objects.select_for_update().get(pk=reference.pk)
    if store.location_code != location:
        fail("LOCATION_INVALID", "请前往这件商品所属的商店。")
    if listing.status != "ACTIVE":
        fail("TRADE_LISTING_NOT_ACTIVE", "商品已售罄或下架。")
    if store.status not in _TRADABLE_STORE_STATUSES:
        fail("TRADE_STORE_CLOSED", "商店当前未营业。")
    public_meal = bool(listing.public_meal_code)
    if public_meal and quantity != 1:
        fail("PUBLIC_MEAL_ONE_PORTION_ONLY", "公共餐每人每日只能领取一份。")
    if "expected_unit_price" in params and (type(params["expected_unit_price"]) is not int
            or params["expected_unit_price"] != listing.unit_price):
        fail("PRICE_CHANGED", "价格已变化，请刷新后确认。")
    item = Item.objects.filter(pk=listing.item_id, is_deleted=False).first()
    if item is None:
        fail("INVALID_ITEM", "商品不存在或已停用。")
    if not public_meal:
        remaining = get_store_purchase_limit(None, listing.item_id, clock.world_minute,
                                            store.pk, player_id=player.pk)
        if remaining is not None and quantity > remaining:
            fail("STORY_RATION_LIMIT_REACHED", "购买数量超过今日剩余限额。")
        account = VillageStoreAccount.objects.select_for_update().filter(store=store).first()
        fund = VillageAccount.objects.select_for_update().filter(account_code="PUBLIC_FUND").first()
        if account is None or fund is None:
            fail("STORE_UNAVAILABLE", "商店结算账户尚未就绪。")
    if store.pk == "RESTAURANT_STORE" and not public_meal:
        from .lucy_cooking import fixed_price
        try:
            for batch in VillageStoreInventorySlot.objects.filter(listing=listing, item_amount__gt=0):
                if fixed_price(listing.item_id, batch.item_attributes) != listing.unit_price:
                    fail("RESTAURANT_PRICE_STALE", "餐品价格已变化，请等待货架更新。")
        except ValueError as exc:
            fail(str(exc), "这份餐品暂时无法出售。")
    total = listing.unit_price * quantity
    claim = None
    subsidy = 0
    if public_meal:
        from .lucy_restaurant import take_public_meal
        claim, error = take_public_meal(store, listing, clock.world_minute, request_id, player_id=player.pk)
        if error:
            messages = {"PUBLIC_MEAL_DAILY_LIMIT_REACHED": "今日个人或全村公共餐额度已用完，每人一份，全村共十份。",
                        "PUBLIC_MEAL_FUND_INSUFFICIENT": "公共资金不足，暂时无法领取公共餐。",
                        "TRADE_INSUFFICIENT_ITEM": "公共餐新鲜库存不足。"}
            fail(error, messages.get(error, "这份公共餐暂时无法领取。"))
        subsidy = claim["subsidy_amount"]
        fee, net = 0, subsidy
        consumed = [(claim["slot"], 1)]
    else:
        try:
            fee, net = _compute_fee(total, store.transaction_fee_bps, store.minimum_fee)
        except ValueError:
            fail("TRADE_INVALID_FEE_CONFIG", "商店交易费用配置无效。")
        if bag.money < total:
            fail("TRADE_INSUFFICIENT_MONEY", "金币不足。")
        try:
            consumed = _consume_store_fefo_available(store, listing, quantity, clock.world_minute)
        except ValueError:
            fail("TRADE_INSUFFICIENT_ITEM", "商品可售库存不足。")
    next_index = (bag.slots.aggregate(n=Max("slot_index"))["n"] or 0) + 1
    purchased = []
    for source, amount in consumed:
        attributes = source.item_attributes or {}
        target = InventorySlot(bag=bag, slot_index=next_index, item_id=source.item_id,
            item_amount=amount, quality=source.quality, produced_world_minute=source.produced_world_minute,
            expires_world_minute=source.expires_world_minute, source_request_id=request_id,
            freshness_unknown=bool(attributes.get("freshness_unknown")),
            item_bag_name=attributes.get("item_bag_name", ""))
        for name in COMPONENT_FIELDS:
            setattr(target, name, getattr(source, name))
        if claim:
            target.dish_metadata = claim["attributes"]["dish_metadata"]
        target.save()
        next_index += 1
        purchased.append(dict(instance_id=str(target.instance_id), item_id=target.item_id,
            quantity=amount, quality=target.quality, expires_world_minute=target.expires_world_minute))
    bag.money -= total
    bag._money_change_source = "village_store_buy"
    bag.save(update_fields=["money"])
    if not public_meal:
        account.balance += net
        account.account_version += 1
        account.save(update_fields=["balance", "account_version", "updated_at"])
        fund.balance += fee
        fund.asset_version += 1
        fund.save(update_fields=["balance", "asset_version", "updated_at"])
    store.inventory_version += 1
    store.save(update_fields=["inventory_version", "updated_at"])
    from .lucy_restaurant import store_available
    if not any(store_available(s, clock.world_minute) for s in listing.inventory_slots.all()):
        listing.status = "SOLD_OUT"
        listing.save(update_fields=["status", "updated_at"])
    result = dict(listing_id=str(listing.pk), store_code=store.pk, item_id=item.pk,
        quantity=quantity, amount=quantity, unit_price=listing.unit_price, total_price=total,
        spent_gold=total, gross_amount=total, fee_amount=fee, store_net_amount=net,
        purchased_batches=purchased, affected_npc_ids=[store.operator_npc.agent_id] if store.operator_npc_id else [])
    if claim:
        result.update(public_meal_code=listing.public_meal_code, subsidy_amount=subsidy,
            remaining_personal_today=0, remaining_village_today=claim["remaining_village_today"])
    verb = "领取公共餐" if public_meal else "购买"
    summary = f"{player.username}在{store.display_name}{verb}{item.item_name}×{quantity}"
    event = VillageWorldEvent.objects.create(dedupe_key=f"player:{player.pk}:{request_id}",
        request_id=request_id, event_type="ECONOMY_BUY_FROM_STORE", world_minute=clock.world_minute,
        time_revision=clock.time_revision, player_ids=[str(player.pk)], location_code=location,
        participant_npc_ids=result["affected_npc_ids"], visibility="PARTICIPANTS", player_summary=summary,
        hub_summary=summary, private_data={**result, "player_id": str(player.pk)})
    # NPC ledger ids are globally unique; derive a player-namespaced id because
    # player action request ids are intentionally unique per player only.
    EconomicLedgerEntry.objects.create(request_id=uuid.uuid5(uuid.NAMESPACE_URL, f"player:{player.pk}:{request_id}"),
        entry_type="BUY_FROM_STORE", actor_npc=None, world_minute=clock.world_minute, event=event,
        changes={"listing_id": str(listing.pk), "amount": quantity, "gross_amount": total,
                 "fee_amount": fee, "store_net_amount": net, "player_id": str(player.pk),
                 "subsidy_amount": subsidy,
                 "player_display_name": player.username,
                 f"PLAYER:{player.pk}": {"money": -total, str(item.pk): quantity},
                 f"STORE:{store.pk}": {"money": net, str(item.pk): -quantity}, "PUBLIC_FUND": {"money": fee - subsidy}})
    from .phase4_visibility import enqueue_event_deliveries
    enqueue_event_deliveries(event)
    result["bag"] = serialize_bag(bag)
    return result
