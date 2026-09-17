"""Read-only public market listings; observing never expires or settles a listing."""
from django.db.models import Q
from django.core.paginator import Paginator
from django.utils import timezone
from game_time.models import GlobalGameTimeState
from marketplace.models import MarketListing
from marketplace.serializers import serialize_listings

FOOD_TYPES = ("食材", "肉类", "蔬菜", "水果", "调料类", "食物", "Food", "Ingredient")
TOOL_TYPES = ("Usable", "HoeTool", "ChopTool", "BreakTool", "ReapTool", "WaterTool", "CollectTool", "工具")
SORTS = {"price_asc": ("price", "pk"), "price_desc": ("-price", "pk"), "newest": ("-created_at", "-pk")}


def public_market(params):
    if set(params) - {"keyword", "category", "sort", "page"}:
        raise ValueError("Unknown parameter")
    keyword = params.get("keyword", "").strip()
    category = params.get("category", "all")
    sort = params.get("sort", "price_asc")
    page = params.get("page", "1")
    if len(keyword) > 80 or category not in ("all", "food", "tool", "other") or sort not in SORTS:
        raise ValueError("Invalid filter")
    if not str(page).isascii() or not str(page).isdigit() or not 1 <= int(page) <= 100000:
        raise ValueError("Invalid page")
    minute = GlobalGameTimeState.objects.filter(pk=1).values_list("world_minute", flat=True).first()
    qs = MarketListing.objects.filter(status="active", remaining_amount__gt=0).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now())
    )
    if minute is not None:
        qs = qs.filter(Q(item_snapshot__expiresWorldMinute__isnull=True)
                       | Q(item_snapshot__expiresWorldMinute=None)
                       | Q(item_snapshot__expiresWorldMinute__gt=minute))
    if keyword:
        qs = qs.filter(Q(item_name_snapshot__icontains=keyword) | Q(seller__username__icontains=keyword)
                       | Q(seller_npc__name__icontains=keyword))
    if category == "food":
        qs = qs.filter(item_type_snapshot__in=FOOD_TYPES)
    elif category == "tool":
        qs = qs.filter(item_type_snapshot__in=TOOL_TYPES)
    elif category == "other":
        qs = qs.exclude(item_type_snapshot__in=FOOD_TYPES + TOOL_TYPES)
    item_count = qs.values("item_id").distinct().count()
    qs = qs.select_related("seller", "seller_npc", "usage", "dish", "review").order_by(*SORTS[sort])
    paginator = Paginator(qs, 20)
    current = paginator.get_page(int(page))
    rows = []
    for item in serialize_listings(current):
        rows.append({
            "listing_id": item["listingId"], "item_id": item["itemId"],
            "item_name": item["itemName"], "seller_name": item["sellerName"],
            "available_amount": item["remainingAmount"], "unit_price": item["price"],
            "listed_at": item["createdAt"], "batch": item["batch"],
        })
    return {"listings": rows, "total_count": paginator.count, "item_count": item_count,
            "page": current.number, "page_count": paginator.num_pages, "world_minute": minute}
