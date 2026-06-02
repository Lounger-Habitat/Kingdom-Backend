import json
import time
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from .models import Item
from .serializers import ItemSerializer


def _items_response(items, request=None, status_code=200):
    timestamp = int(time.time() * 1000)
    serialized = ItemSerializer(items, many=True, context={"request": request}).data
    return Response({
        "code": status_code,
        "message": "success",
        "data": {
            "timestamp": timestamp,
            "items": serialized,
        },
    })


@api_view(["GET"])
@permission_classes([AllowAny])
def item_list(request):
    items = Item.objects.filter(is_deleted=False)
    return _items_response(items, request=request)


@api_view(["POST"])
def item_upload(request):
    """批量上传/更新物品，按 item_id 做 upsert。支持 JSON 和 multipart/form-data（含图片）"""
    from rest_framework.parsers import MultiPartParser, JSONParser

    items_data = request.data
    if isinstance(items_data, dict):
        items_data = items_data.get("items", [])
    if isinstance(items_data, str):
        try:
            items_data = json.loads(items_data)
        except json.JSONDecodeError:
            return Response({"code": 400, "message": "items 字段不是合法的 JSON"}, status=400)
    if not isinstance(items_data, list):
        return Response({"code": 400, "message": "需要传入物品列表"}, status=400)

    field_map = {
        "itemID": "item_id",
        "itemName": "item_name",
        "itemType": "item_type",
        "itemDescription": "item_description",
        "itemUseRadius": "item_use_radius",
        "canPickedup": "can_picked_up",
        "canDropped": "can_dropped",
        "canCarried": "can_carried",
        "itemPrice": "item_price",
        "sellPercentage": "sell_percentage",
        "iconUrl": "icon_url",
        "isDeleted": "is_deleted",
    }

    created, updated, errors = 0, 0, []

    for i, raw in enumerate(items_data):
        normalized = {}
        for src, dst in field_map.items():
            if src in raw:
                normalized[dst] = raw[src]
        for key in field_map.values():
            if key in raw and key not in normalized:
                normalized[key] = raw[key]

        # 处理图片文件：iconImage_0, iconImage_1, ... 或 icon_image_0
        icon_file = (
            request.FILES.get(f"iconImage_{i}")
            or request.FILES.get(f"icon_image_{i}")
        )
        if icon_file:
            normalized["icon_image"] = icon_file

        item_id = normalized.get("item_id")
        if item_id is None:
            errors.append(f"第 {i + 1} 条缺少 item_id")
            continue

        obj, was_created = Item.objects.update_or_create(
            item_id=item_id,
            defaults=normalized,
        )
        if was_created:
            created += 1
        else:
            updated += 1

    return Response({
        "code": 200,
        "message": "success",
        "data": {"created": created, "updated": updated, "errors": errors},
    })


@api_view(["POST"])
def item_upload_image(request, item_id):
    """为单个物品上传图片，multipart/form-data，字段名 icon_image"""
    try:
        item = Item.objects.get(item_id=item_id)
    except Item.DoesNotExist:
        return Response({"code": 404, "message": f"物品 {item_id} 不存在"}, status=404)

    icon_file = request.FILES.get("icon_image")
    if not icon_file:
        return Response({"code": 400, "message": "请上传 icon_image 文件"}, status=400)

    item.icon_image = icon_file
    item.save()

    return Response({
        "code": 200,
        "message": "success",
        "data": {"item_id": item_id, "icon_image": request.build_absolute_uri(item.icon_image.url)},
    })


@api_view(["GET"])
@permission_classes([AllowAny])
def item_list_since(request):
    ts = request.query_params.get("timestamp")
    if not ts:
        return Response({"code": 400, "message": "timestamp parameter required"}, status=400)

    try:
        ts_ms = int(ts)
    except ValueError:
        return Response({"code": 400, "message": "invalid timestamp"}, status=400)

    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    items = Item.objects.filter(updated_at__gt=dt)
    return _items_response(items, request=request)
