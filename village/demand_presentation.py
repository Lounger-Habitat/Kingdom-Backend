"""Public demand register, grouped by submission day rather than arrival day."""
from django.db.models import BigIntegerField, F
from django.db.models.functions import Cast
from game_time.models import GlobalGameTimeState
from items.models import Item
from .caravan import ARRIVAL
from .models import VillageActionReceipt, VillageCaravanRequest


def public_demands(day_index=None, page=1):
    clock = GlobalGameTimeState.objects.filter(pk=1).first()
    minute = clock.world_minute if clock else None
    receipts = VillageActionReceipt.objects.filter(
        action_kind="ECONOMY_CONTACT_CARAVAN", success=True,
        response_data__data__demand_registered=True,
    )
    requests = VillageCaravanRequest.objects.all()
    days = set(receipts.annotate(day=Cast(F("world_minute") / 1440, BigIntegerField()))
               .values_list("day", flat=True).distinct())
    days.update(requests.annotate(day=Cast(F("submitted_world_minute") / 1440, BigIntegerField()))
                .values_list("day", flat=True).distinct())
    selected = day_index if day_index is not None else minute // 1440 if minute is not None else max(days, default=0)
    start, end = selected * 1440, (selected + 1) * 1440
    rows = []
    signatures = set()
    for receipt in receipts.filter(world_minute__gte=start, world_minute__lt=end).select_related("npc").order_by("-world_minute", "-created_at", "-request_id"):
        data = receipt.response_data["data"]
        ids = sorted({i for i in data.get("item_ids", []) if type(i) is int})
        target = data.get("target_day_index", receipt.world_minute // 1440 + (receipt.world_minute % 1440 >= ARRIVAL))
        signatures.add((target, receipt.world_minute, tuple(ids)))
        rows.append({"registration_id": str(receipt.request_id), "world_minute": receipt.world_minute,
                     "target_day_index": target, "arrival_world_minute": target * 1440 + ARRIVAL,
                     "operator_display_name": receipt.npc.name if receipt.npc else "Nora",
                     "item_ids": ids, "record_source": "SUBMISSION"})
    # Legacy/latest snapshots remain visible when no corresponding receipt exists.
    for request in requests.filter(submitted_world_minute__gte=start, submitted_world_minute__lt=end):
        ids = sorted({i for i in request.item_ids if type(i) is int})
        if (request.day_index, request.submitted_world_minute, tuple(ids)) in signatures:
            continue
        rows.append({"registration_id": f"legacy-{request.day_index}", "world_minute": request.submitted_world_minute,
                     "target_day_index": request.day_index, "arrival_world_minute": request.day_index * 1440 + ARRIVAL,
                     "operator_display_name": "Nora", "item_ids": ids, "record_source": "SAVED_REQUEST"})
    rows.sort(key=lambda row: row["world_minute"], reverse=True)
    total = len(rows)
    page_count = max(1, (total + 19) // 20)
    page = min(max(1, page), page_count)
    rows = rows[(page - 1) * 20:page * 20]
    ids = {i for row in rows for i in row["item_ids"]}
    names = dict(Item.objects.filter(item_id__in=ids).values_list("item_id", "item_name"))
    for row in rows:
        row["items"] = [{"item_id": i, "item_name": names.get(i, f"物品#{i}")} for i in row.pop("item_ids")]
    return {"world_minute": minute, "day_index": selected, "available_days": sorted(days, reverse=True),
            "records": rows, "total_count": total, "page": page, "page_count": page_count}
