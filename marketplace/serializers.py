from rest_framework import serializers
from .models import MarketListing, MarketTransaction


def serialize_listing(listing, viewer=None):
    """Serialize a MarketListing to a camelCase dict."""
    seller_name = ""
    if listing.source_type == "system":
        seller_name = "系统"
    elif listing.seller:
        seller_name = listing.seller.username
    elif listing.seller_npc:
        seller_name = listing.seller_npc.name

    is_own = (
        viewer is not None
        and (
            (hasattr(viewer, "id") and listing.seller_id is not None and listing.seller_id == viewer.id)
            or (hasattr(viewer, "agent_id") and listing.seller_npc_id is not None and listing.seller_npc_id == viewer.id)
        )
    )

    data = {
        "listingId": listing.pk,
        "sourceType": listing.source_type,
        "sellerName": seller_name,
        "isOwnListing": is_own,
        "itemId": listing.item_id,
        "itemName": listing.item_name_snapshot,
        "itemType": listing.item_type_snapshot,
        "iconUrl": listing.icon_url_snapshot,
        "amount": listing.total_amount,
        "remainingAmount": listing.remaining_amount,
        "price": listing.price,
        "status": listing.status,
        "rated": listing.rated,
        "ratingPrice": listing.rating_price,
        "overallScore": listing.overall_score,
        "ingredient": listing.ingredient,
        "dishQuality": listing.dish_quality,
        "cookTime": listing.cook_time,
        "remainingUses": listing.remaining_uses,
        "reviewComment": listing.review_comment,
        "itemBagName": listing.item_bag_name,
        "createdAt": listing.created_at.isoformat() if listing.created_at else None,
        "expiresAt": listing.expires_at.isoformat() if listing.expires_at else None,
    }
    return data


def serialize_transaction(tx):
    """Serialize a MarketTransaction to a camelCase dict."""
    seller_name = ""
    if tx.source_type == "system":
        seller_name = "系统"
    elif tx.seller:
        seller_name = tx.seller.username
    elif tx.seller_npc:
        seller_name = tx.seller_npc.name

    buyer_name = tx.buyer.username if tx.buyer else (tx.buyer_npc.name if tx.buyer_npc else "")

    return {
        "transactionId": tx.pk,
        "listingId": tx.listing_id,
        "sourceType": tx.source_type,
        "sellerName": seller_name,
        "buyerName": buyer_name,
        "itemId": tx.item_id,
        "itemName": tx.item_name_snapshot,
        "amount": tx.amount,
        "unitPrice": tx.unit_price,
        "totalPrice": tx.total_price,
        "commission": tx.commission,
        "sellerEarnings": tx.seller_earnings,
        "systemIncome": tx.system_income,
        "createdAt": tx.created_at.isoformat() if tx.created_at else None,
    }


def success_response(data=None, message="操作成功"):
    resp = {"success": True, "message": message}
    if data is not None:
        resp["data"] = data
    return resp


def error_response(message):
    return {"success": False, "message": message}
