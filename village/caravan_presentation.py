"""Read-only, player-visible caravan goods and store import history."""
from collections import defaultdict

from django.db.models import F, Q, Subquery
from django.db.models.functions import Cast
from django.db.models import BigIntegerField
from game_time.models import GlobalGameTimeState
from items.models import Item
from . import caravan
from .models import EconomicLedgerEntry, VillageCaravanVisit, VillageStoreInventorySlot


HISTORY_PAGE_SIZE = 30


def public_caravan(route, history_page=1):
    route = str(route or "").upper()
    if route not in ("NORMAL", "EMERGENCY"):
        return None
    clock = GlobalGameTimeState.objects.filter(pk=1).first()
    minute = clock.world_minute if clock else None
    result = {"route": route, "world_minute": minute,
              "location_code": "TRADE_ROAD" if route == "NORMAL" else caravan.EMERGENCY_LOCATION,
              "status": "UNKNOWN", "items": [], "history": [],
              "history_page": 1, "history_pages": 1, "history_total": 0}
    if minute is None:
        return result
    data = caravan.projection(minute)  # Never sync_visit: viewing must not roll stock.
    matching = data["route"] == route
    ready = data["route_readiness"]["route"] == route
    current = matching and data["route_frozen"]
    if not matching:
        status = "NEXT_VISIT" if ready else "LOCKED" if data["route_readiness"]["route"] == "NONE" else "OTHER_ROUTE"
    elif minute < data["arrival_world_minute"] or not current:
        status = "WAITING"
    elif minute >= data["departure_world_minute"]:
        status = "DEPARTED"
    elif data["reason"]:
        status = "UNAVAILABLE"
    else:
        status = "OPEN" if data["can_import"] else "SOLD_OUT"
    result.update(status=status, route_open=ready or current,
                  arrival_world_minute=data["arrival_world_minute"],
                  departure_world_minute=data["departure_world_minute"],
                  next_arrival_world_minute=data["next_arrival_world_minute"],
                  demand_registered=bool(data["next_requested_items"]))

    # Associate historic imports with the frozen visit on the settlement day.
    days = VillageCaravanVisit.objects.filter(route=route).values("day_index")
    entries = (EconomicLedgerEntry.objects.filter(
        Q(changes__payer="STORE:GENERAL_STORE")
        | Q(changes__payer="10015", actor_npc__agent_id="10015"),
        entry_type="IMPORT_GOODS",
    ).annotate(visit_day=Cast(F("world_minute") / 1440, BigIntegerField()))
        .filter(visit_day__in=Subquery(days)).order_by("-world_minute", "-id"))
    total = entries.count()
    pages = max(1, (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
    page = max(1, min(history_page, pages))
    result.update(history_page=page, history_pages=pages, history_total=total)
    entries = entries[(page - 1) * HISTORY_PAGE_SIZE:page * HISTORY_PAGE_SIZE]
    stock = data["stock"] if matching else {}
    imports = []
    for entry in entries:
        personal = entry.changes.get("payer") == "10015"
        for row in entry.changes.get("imports", []):
            if isinstance(row, dict) and type(row.get("item_id")) is int:
                imports.append({"ledger_id": entry.pk, "world_minute": entry.world_minute,
                                "item_id": row["item_id"], "amount": row.get("amount"),
                                "unit_price": row.get("unit_price"), "total_cost": row.get("total_cost"),
                                "operator_display_name": "Nora",
                                "payment_source": "PERSONAL" if personal else "STORE",
                                "stock_destination": "PERSONAL_INVENTORY" if personal else "STORE_UNLISTED"})
    item_ids = {int(key) for key in stock} | {row["item_id"] for row in imports}
    names = dict(Item.objects.filter(item_id__in=item_ids).values_list("item_id", "item_name"))
    counts = defaultdict(int)
    from .lucy_restaurant import store_available
    for slot in VillageStoreInventorySlot.objects.filter(
        store_id="GENERAL_STORE", item_id__in=item_ids, item_amount__gt=0,
    ):
        counts[slot.item_id] += store_available(slot, minute)
    for key, amount in stock.items():
        item_id = int(key)
        result["items"].append({"item_id": item_id, "item_name": names.get(item_id, f"物品#{item_id}"),
                                "available_amount": amount, "unit_price": data["import_prices"].get(key),
                                "store_amount": counts[item_id]})
    result["items"].sort(key=lambda row: (row["available_amount"] <= 0, row["item_id"]))
    for row in imports:
        row["item_name"] = names.get(row["item_id"], f"物品#{row['item_id']}")
    result["history"] = imports
    return result
