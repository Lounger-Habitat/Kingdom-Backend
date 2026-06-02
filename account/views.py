import uuid
import secrets

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from .models import GameAccount
from .serializers import RegisterSerializer, LoginSerializer, ConvertGuestSerializer
from inventory.services import init_player_bags
from player.models import PlayerState


def _serializer_errors(ser):
    """将 DRF 序列化器错误转为统一的 message 字符串。"""
    messages = []
    for field, errs in ser.errors.items():
        for e in errs:
            messages.append(str(e))
    return "; ".join(messages) if messages else "参数错误"


def _jwt_response(account):
    refresh = RefreshToken.for_user(account)
    return {
        "code": 0,
        "message": "success",
        "data": {
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "user_id": account.id,
            "username": account.username,
            "is_guest": account.is_guest,
        },
    }


@api_view(["POST"])
@permission_classes([AllowAny])
def auth_register(request):
    ser = RegisterSerializer(data=request.data)
    if not ser.is_valid():
        return Response(
            {"code": 400, "message": _serializer_errors(ser)},
            status=status.HTTP_400_BAD_REQUEST,
        )

    account = GameAccount.create_user(
        username=ser.validated_data["username"],
        password=ser.validated_data["password"],
        is_guest=False,
    )
    PlayerState.objects.create(player_id=str(account.id))
    init_player_bags(account)
    return Response(_jwt_response(account), status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([AllowAny])
def auth_login(request):
    ser = LoginSerializer(data=request.data)
    if not ser.is_valid():
        return Response(
            {"code": 400, "message": _serializer_errors(ser)},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        account = GameAccount.objects.get(username=ser.validated_data["username"])
    except GameAccount.DoesNotExist:
        return Response(
            {"code": 401, "message": "用户名不存在"},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    if not account.check_password(ser.validated_data["password"]):
        return Response(
            {"code": 401, "message": "密码错误"},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    return Response(_jwt_response(account))


@api_view(["POST"])
@permission_classes([AllowAny])
def auth_guest(request):
    guest_name = f"guest_{uuid.uuid4().hex[:8]}"
    password = secrets.token_hex(16)

    account = GameAccount.create_user(username=guest_name, password=password, is_guest=True)
    PlayerState.objects.create(player_id=str(account.id))
    init_player_bags(account)
    return Response(_jwt_response(account), status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def auth_convert(request):
    account = request.user  # GameAccount via GameJWTAuthentication

    if not account.is_guest:
        return Response({"code": 400, "message": "当前账号不是游客"}, status=400)

    ser = ConvertGuestSerializer(data=request.data)
    if not ser.is_valid():
        return Response(
            {"code": 400, "message": _serializer_errors(ser)},
            status=status.HTTP_400_BAD_REQUEST,
        )

    account.username = ser.validated_data["new_username"]
    account.set_password(ser.validated_data["new_password"])
    account.is_guest = False
    account.save()

    return Response(_jwt_response(account))


@api_view(["POST"])
@permission_classes([AllowAny])
def auth_refresh(request):
    refresh_token = request.data.get("refresh")
    if not refresh_token:
        return Response({"code": 400, "message": "refresh token required"}, status=400)

    try:
        refresh = RefreshToken(refresh_token)
        account_id = refresh["user_id"]
        account = GameAccount.objects.get(id=account_id, is_active=True)
        return Response({
            "code": 0,
            "message": "success",
            "data": {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "user_id": account.id,
                "username": account.username,
                "is_guest": account.is_guest,
            },
        })
    except Exception:
        return Response(
            {"code": 401, "message": "refresh token 无效或已过期"},
            status=status.HTTP_401_UNAUTHORIZED,
        )
