import logging

from django.db import transaction

from items.models import Item, ItemType
from marketplace.models import MarketListing, MarketListingSource, MarketListingStatus, MarketTransaction
from marketplace.services import (
    calculate_settlement,
    get_player_main_bag,
    _add_snapshot_item_to_bag,
)

logger = logging.getLogger(__name__)


def _get_quality_coeff(dish_quality):
    if dish_quality <= 1:
        return 0.3
    if dish_quality == 2:
        return 0.7
    return 1.0


def _get_fair_price(listing):
    if listing.item_type_snapshot == ItemType.FOOD:
        if listing.rated and listing.rating_price > 0:
            return listing.rating_price
        try:
            item = Item.objects.get(item_id=listing.item_id)
            return int(item.item_price * _get_quality_coeff(listing.dish_quality))
        except Item.DoesNotExist:
            return 0
    else:
        try:
            item = Item.objects.get(item_id=listing.item_id)
            return item.item_price
        except Item.DoesNotExist:
            return 0


@transaction.atomic
def _execute_auto_buy(config, buyer, listing, amount):
    from .models import AutoBuyConfig, AutoBuyPurchase

    listing = MarketListing.objects.select_for_update().get(pk=listing.pk)

    if listing.status != MarketListingStatus.ACTIVE:
        return None, "挂单不在出售中状态"
    if amount > listing.remaining_amount:
        return None, "购买数量超过剩余数量"

    settlement = calculate_settlement(listing, amount)
    total_price = settlement["total_price"]

    config = AutoBuyConfig.objects.select_for_update().get(pk=config.pk)
    if config.fund < total_price:
        return None, "资金池不足"

    buyer_bag, _ = get_player_main_bag(buyer)

    # Deduct from public fund
    config.fund -= total_price
    config.save(update_fields=["fund"])

    # Pay seller for player listings
    if listing.source_type == MarketListingSource.PLAYER and listing.seller:
        seller_bag, _ = get_player_main_bag(listing.seller)
        seller_bag.money += settlement["seller_earnings"]
        seller_bag._money_change_source = "autobuy_sell"
        seller_bag.save()

    # Add items to buyer bag
    _add_snapshot_item_to_bag(buyer_bag, listing.item_snapshot, amount)

    # Update listing
    listing.remaining_amount -= amount
    if listing.remaining_amount <= 0:
        listing.status = MarketListingStatus.SOLD_OUT
    listing.save()

    # Create marketplace transaction record
    MarketTransaction.objects.create(
        listing=listing,
        source_type=listing.source_type,
        seller=listing.seller,
        buyer=buyer,
        item_id=listing.item_id,
        item_name_snapshot=listing.item_name_snapshot,
        amount=amount,
        unit_price=listing.price,
        total_price=total_price,
        commission=settlement["commission"],
        seller_earnings=settlement["seller_earnings"],
        system_income=settlement["system_income"],
    )

    # Create autobuy purchase record
    purchase = AutoBuyPurchase.objects.create(
        config=config,
        buyer=buyer,
        listing_id=listing.pk,
        item_id=listing.item_id,
        item_name=listing.item_name_snapshot,
        amount=amount,
        unit_price=listing.price,
        total_price=total_price,
        commission=settlement["commission"],
        seller_earnings=settlement["seller_earnings"],
        fund_balance=config.fund,
    )

    return purchase, None


def run_auto_buy():
    from .models import AutoBuyConfig, AutoBuyRule

    config = AutoBuyConfig.get_instance()
    if not config.enabled or config.fund <= 0:
        return

    rules = AutoBuyRule.objects.filter(is_active=True).select_related("buyer")
    if not rules.exists():
        return

    rules_by_type = {}
    for rule in rules:
        for t in rule.item_types:
            rules_by_type.setdefault(t, []).append(rule)

    listings = MarketListing.objects.filter(
        status=MarketListingStatus.ACTIVE,
        remaining_amount__gt=0,
    ).order_by("price")

    bought_count = 0
    for listing in listings:
        matching_rules = rules_by_type.get(listing.item_type_snapshot, [])
        if not matching_rules:
            continue

        fair_price = _get_fair_price(listing)
        if fair_price <= 0:
            continue

        for rule in matching_rules:
            max_price = int(fair_price * rule.max_price_multiplier)
            if listing.price > max_price:
                continue

            if listing.seller_id == rule.buyer_id:
                continue

            result, err = _execute_auto_buy(
                config, rule.buyer, listing, listing.remaining_amount,
            )
            if err:
                logger.debug(
                    "[autobuy] Failed: buyer=%s listing=%d err=%s",
                    rule.buyer.username, listing.pk, err,
                )
                continue

            bought_count += 1
            logger.info(
                "[autobuy] Bought: %s bought %d x %s @ %d (listing #%d), fund=%d",
                rule.buyer.username,
                result.amount,
                listing.item_name_snapshot,
                listing.price,
                listing.pk,
                config.fund,
            )
            break

    if bought_count > 0:
        logger.info("[autobuy] Run complete: %d purchases made", bought_count)
