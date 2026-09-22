from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Q
from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view
from rest_framework.response import Response

from inventory.serializers import serialize_bag
from items.models import Item

from .models import MarketListing, MarketListingSource, MarketListingStatus, MarketTransaction
from .serializers import serialize_listing, serialize_listings, serialize_transaction, success_response, error_response
from . import services


def _env_required():
    # No client-provided header/body field can relax this deployment policy.
    if settings.VILLAGE_REQUIRE_ENV_MARKET_WRITES:
        return Response(dict(success=False, result_code="ENV_REQUIRED",
            message="请在玩家模式前往集市，通过现场交互完成交易。"), status=403)
    return None


@extend_schema(
    request={
        "application/json": {
            "type": "object",
            "properties": {
                "itemId": {"type": "integer"},
                "instanceId": {"type": "string", "format": "uuid"},
                "amount": {"type": "integer"},
                "price": {"type": "integer"},
            },
            "required": ["instanceId", "amount", "price"],
        }
    }
)
@api_view(["POST"])
def marketplace_list(request):
    denied = _env_required()
    if denied is not None:
        return denied
    instance_id = request.data.get("instanceId")
    amount = request.data.get("amount", 1)
    price = request.data.get("price", 0)

    if not instance_id:
        return Response(error_response("缺少 instanceId"), status=400)

    listing, err = services.create_listing(request.user, instance_id, amount, price)
    if err:
        return Response(error_response(err), status=400)

    bag, _ = services.get_player_main_bag(request.user)
    return Response(success_response({
        "listing": serialize_listing(listing),
        "bag": serialize_bag(bag),
    }, "上架成功"))


@api_view(["POST"])
def marketplace_delist(request):
    denied = _env_required()
    if denied is not None:
        return denied
    listing_id = request.data.get("listingId")
    if not listing_id:
        return Response(error_response("缺少 listingId"), status=400)

    result, err = services.delist_listing(request.user, listing_id)
    if err:
        return Response(error_response(err), status=400)

    return Response(success_response({
        "bag": serialize_bag(result),
    }, "下架成功"))


@api_view(["GET"])
def marketplace_browse(request):
    # Expire stale listings first
    services.expire_due_listings()

    keyword = request.GET.get("keyword", "").strip()
    item_type = request.GET.get("itemType", "").strip()
    item_id = request.GET.get("itemId", "").strip()
    min_price = request.GET.get("minPrice", "")
    max_price = request.GET.get("maxPrice", "")
    sort_by = request.GET.get("sortBy", "newest")
    page = int(request.GET.get("page", 1))
    page_size = int(request.GET.get("pageSize", 20))

    qs = MarketListing.objects.filter(status=MarketListingStatus.ACTIVE)

    if keyword:
        qs = qs.filter(
            Q(item_name_snapshot__icontains=keyword) |
            Q(seller__username__icontains=keyword) |
            Q(seller_npc__name__icontains=keyword)
        )
    if item_type:
        qs = qs.filter(item_type_snapshot=item_type)
    if item_id:
        qs = qs.filter(item_id=int(item_id))
    if min_price:
        qs = qs.filter(price__gte=int(min_price))
    if max_price:
        qs = qs.filter(price__lte=int(max_price))

    sort_map = {
        "price_asc": "price",
        "price_desc": "-price",
        "newest": "-created_at",
        "oldest": "created_at",
        "amount_desc": "-remaining_amount",
    }
    qs = qs.order_by(sort_map.get(sort_by, "-created_at"))

    paginator = Paginator(qs, page_size)
    page_obj = paginator.get_page(page)

    listings = serialize_listings(page_obj, request.user)

    return Response(success_response({
        "listings": listings,
        "totalCount": paginator.count,
        "page": page,
        "pageSize": page_size,
    }))


@api_view(["POST"])
def marketplace_buy(request):
    denied = _env_required()
    if denied is not None:
        return denied
    listing_id = request.data.get("listingId")
    amount = request.data.get("amount", 1)

    if not listing_id:
        return Response(error_response("缺少 listingId"), status=400)

    # Expire stale listings first
    services.expire_due_listings()

    result, err = services.buy_listing(request.user, listing_id, amount)
    if err:
        return Response(error_response(err), status=400)

    return Response(success_response({
        "transaction": serialize_transaction(result["transaction"]),
        "listing": serialize_listing(result["listing"]),
        "bag": serialize_bag(result["bag"]),
    }, "购买成功"))


@api_view(["GET"])
def marketplace_my_listings(request):
    # Expire stale listings for this player
    services.expire_due_listings()

    status_filter = request.GET.get("status", "").strip()
    page = int(request.GET.get("page", 1))
    page_size = int(request.GET.get("pageSize", 20))

    qs = MarketListing.objects.filter(seller=request.user)
    if status_filter:
        qs = qs.filter(status=status_filter)

    qs = qs.order_by("-created_at")

    paginator = Paginator(qs, page_size)
    page_obj = paginator.get_page(page)

    listings = serialize_listings(page_obj, request.user)

    return Response(success_response({
        "listings": listings,
        "totalCount": paginator.count,
        "page": page,
        "pageSize": page_size,
    }))


@api_view(["GET"])
def marketplace_history(request):
    role = request.GET.get("role", "all").strip()
    page = int(request.GET.get("page", 1))
    page_size = int(request.GET.get("pageSize", 20))

    from .models import MarketTransaction as MT

    if role == "buyer":
        qs = MT.objects.filter(buyer=request.user)
    elif role == "seller":
        qs = MT.objects.filter(seller=request.user)
    else:
        qs = MT.objects.filter(Q(buyer=request.user) | Q(seller=request.user))

    qs = qs.select_related("seller", "buyer").order_by("-created_at")

    paginator = Paginator(qs, page_size)
    page_obj = paginator.get_page(page)

    transactions = []
    for tx in page_obj:
        d = serialize_transaction(tx)
        # Add current player's role
        if tx.buyer_id == request.user.id:
            d["currentRole"] = "buyer"
        elif tx.seller_id == request.user.id:
            d["currentRole"] = "seller"
        else:
            d["currentRole"] = "unknown"
        transactions.append(d)

    return Response(success_response({
        "transactions": transactions,
        "totalCount": paginator.count,
        "page": page,
        "pageSize": page_size,
    }))


@api_view(["GET"])
def marketplace_sale_notifications(request):
    since = request.GET.get("since", "").strip()
    if not since:
        return Response(error_response("缺少 since 参数"), status=400)

    since_dt = parse_datetime(since)
    if since_dt is None:
        return Response(error_response("since 参数格式无效"), status=400)

    transactions = MarketTransaction.objects.filter(
        seller=request.user,
        created_at__gt=since_dt,
    ).select_related("seller", "buyer").order_by("-created_at")

    tx_list = [serialize_transaction(tx) for tx in transactions]

    data = {"transactions": tx_list}
    if tx_list:
        bag, _ = services.get_player_main_bag(request.user)
        data["bag"] = serialize_bag(bag)

    return Response(success_response(data))


# ─── Admin: Item Autocomplete (pinyin search) ─────────────────
from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from pypinyin import lazy_pinyin, Style


@staff_member_required
def item_autocomplete(request):
    """Admin autocomplete endpoint for Item with pinyin initial search."""
    term = request.GET.get("term", "").strip()
    page = int(request.GET.get("page", 1))
    page_size = 20

    qs = Item.objects.filter(is_deleted=False)

    if term:
        # First try direct name/id match in DB
        qs_name = qs.filter(Q(item_name__icontains=term) | Q(item_id__icontains=term))

        # Then filter by pinyin initials in Python (122 items is fast enough)
        term_lower = term.lower()
        pinyin_matches = []
        for item in qs.exclude(pk__in=qs_name.values_list("pk", flat=True)):
            initials = "".join(lazy_pinyin(item.item_name, style=Style.FIRST_LETTER))
            if term_lower in initials.lower():
                pinyin_matches.append(item.pk)

        qs = qs.filter(Q(pk__in=qs_name.values_list("pk", flat=True)) | Q(pk__in=pinyin_matches))

    total = qs.count()
    qs = qs.order_by("item_id")[(page - 1) * page_size : page * page_size]

    results = [
        {"id": item.item_id, "text": f"[{item.item_id}] {item.item_name}"}
        for item in qs
    ]

    return JsonResponse({
        "results": results,
        "pagination": {"more": (page * page_size) < total},
    })
