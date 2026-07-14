from rest_framework.decorators import api_view
from rest_framework.response import Response
from config.auth import get_player_id
from account.models import GameAccount
from .models import DailyBusinessLog
from .services import RestaurantService
from .serializers import (
    ExhibitDishSerializer, WithdrawDishSerializer
)


@api_view(["GET"])
def restaurant_state(request):
    """获取餐厅状态 + 菜单 + 升级信息。"""
    player_id = get_player_id(request)
    if not player_id:
        return Response({"success": False, "message": "缺少玩家ID"}, status=400)

    try:
        player = GameAccount.objects.get(id=player_id)
    except GameAccount.DoesNotExist:
        return Response({"success": False, "message": "玩家不存在"}, status=404)

    # 清理过期菜品
    expired_count = RestaurantService.cleanup_expired_menu_items(player)

    # 获取状态数据
    data = RestaurantService.get_restaurant_state_data(player)

    message = "获取成功"
    if expired_count > 0:
        message = f"昨日剩余{expired_count}道菜品已过期销毁"

    return Response({
        "success": True,
        "message": message,
        "data": data
    })


@api_view(["POST"])
def menu_exhibit(request):
    """上架菜品。"""
    player_id = get_player_id(request)
    if not player_id:
        return Response({"success": False, "message": "缺少玩家ID"}, status=400)

    try:
        player = GameAccount.objects.get(id=player_id)
    except GameAccount.DoesNotExist:
        return Response({"success": False, "message": "玩家不存在"}, status=404)

    serializer = ExhibitDishSerializer(data=request.data)
    if not serializer.is_valid():
        return Response({
            "success": False,
            "message": "参数错误",
            "errors": serializer.errors
        }, status=400)

    result, message = RestaurantService.exhibit_dish(player, serializer.validated_data)

    if result:
        return Response({
            "success": True,
            "message": "上架成功",
            "data": {
                "id": message.id,
                "instanceId": message.instance_id,
                "itemId": message.item_id,
                "itemName": message.item_name,
                "dishQuality": message.dish_quality,
                "quantity": message.quantity,
            }
        })
    else:
        return Response({"success": False, "message": message}, status=400)


@api_view(["POST"])
def menu_withdraw(request):
    """下架菜品。"""
    player_id = get_player_id(request)
    if not player_id:
        return Response({"success": False, "message": "缺少玩家ID"}, status=400)

    try:
        player = GameAccount.objects.get(id=player_id)
    except GameAccount.DoesNotExist:
        return Response({"success": False, "message": "玩家不存在"}, status=404)

    serializer = WithdrawDishSerializer(data=request.data)
    if not serializer.is_valid():
        return Response({
            "success": False,
            "message": "参数错误",
            "errors": serializer.errors
        }, status=400)

    result, message = RestaurantService.withdraw_dish(
        player, serializer.validated_data["menu_item_id"]
    )

    if result:
        return Response({"success": True, "message": message})
    else:
        return Response({"success": False, "message": message}, status=400)


@api_view(["POST"])
def daily_business(request):
    """触发每日营业。"""
    player_id = get_player_id(request)
    if not player_id:
        return Response({"success": False, "message": "缺少玩家ID"}, status=400)

    try:
        player = GameAccount.objects.get(id=player_id)
    except GameAccount.DoesNotExist:
        return Response({"success": False, "message": "玩家不存在"}, status=404)

    result, data = RestaurantService.process_daily_business(player)

    if result:
        return Response({
            "success": True,
            "message": f"营业完成，售出{data['customerCount']}道菜品，销毁{data['destroyedDishes']}道剩余菜品",
            "data": data
        })
    else:
        return Response({"success": False, "message": data}, status=400)


@api_view(["GET"])
def review_history(request):
    """获取评价历史（分页）。"""
    player_id = get_player_id(request)
    if not player_id:
        return Response({"success": False, "message": "缺少玩家ID"}, status=400)

    try:
        player = GameAccount.objects.get(id=player_id)
    except GameAccount.DoesNotExist:
        return Response({"success": False, "message": "玩家不存在"}, status=404)

    page = int(request.GET.get("page", 1))
    page_size = int(request.GET.get("pageSize", 20))

    data = RestaurantService.get_review_history(player, page, page_size)

    return Response({
        "success": True,
        "message": "获取成功",
        "data": data
    })


@api_view(["POST"])
def upgrade_restaurant(request):
    """升级餐厅。"""
    player_id = get_player_id(request)
    if not player_id:
        return Response({"success": False, "message": "缺少玩家ID"}, status=400)

    try:
        player = GameAccount.objects.get(id=player_id)
    except GameAccount.DoesNotExist:
        return Response({"success": False, "message": "玩家不存在"}, status=404)

    result, data = RestaurantService.upgrade_restaurant(player)

    if result:
        return Response({
            "success": True,
            "message": f"升级成功！当前等级：{data['newLevel']}",
            "data": data
        })
    else:
        return Response({"success": False, "message": data}, status=400)


@api_view(["GET"])
def business_history(request):
    """查询营业历史记录。支持分页和按天查询。"""
    player_id = get_player_id(request)
    if not player_id:
        return Response({"success": False, "message": "缺少玩家ID"}, status=400)

    try:
        player = GameAccount.objects.get(id=player_id)
    except GameAccount.DoesNotExist:
        return Response({"success": False, "message": "玩家不存在"}, status=404)

    qs = DailyBusinessLog.objects.filter(player=player)

    # 按指定天数查询
    game_day = request.GET.get("game_day")
    if game_day is not None:
        qs = qs.filter(game_day=int(game_day))

    qs = qs.order_by("-game_day")

    # 分页
    page = int(request.GET.get("page", 1))
    page_size = int(request.GET.get("pageSize", 20))
    total = qs.count()
    offset = (page - 1) * page_size
    records = qs[offset:offset + page_size]

    return Response({
        "success": True,
        "message": "获取成功",
        "data": {
            "total": total,
            "page": page,
            "pageSize": page_size,
            "totalPages": (total + page_size - 1) // page_size,
            "records": [
                {
                    "gameDay": r.game_day,
                    "seasonId": r.season_id,
                    "gameYear": r.game_year,
                    "gameMonth": r.game_month,
                    "gameDayOfMonth": r.game_day_of_month,
                    "customerCount": r.customer_count,
                    "totalEarned": r.total_earned,
                    "createdAt": r.created_at.isoformat() if r.created_at else "",
                }
                for r in records
            ],
        }
    })


@api_view(["GET"])
def upgrade_info(request):
    """获取升级条件详情。"""
    player_id = get_player_id(request)
    if not player_id:
        return Response({"success": False, "message": "缺少玩家ID"}, status=400)

    try:
        player = GameAccount.objects.get(id=player_id)
    except GameAccount.DoesNotExist:
        return Response({"success": False, "message": "玩家不存在"}, status=404)

    data = RestaurantService.get_restaurant_state_data(player)

    return Response({
        "success": True,
        "message": "获取成功",
        "data": {
            "upgradeInfo": data.get("upgradeInfo"),
            "currentState": data.get("currentState"),
        }
    })
