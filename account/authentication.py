"""Custom JWT authentication that uses GameAccount instead of Django User."""

from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed
from .models import GameAccount


class GameJWTAuthentication(JWTAuthentication):
    """JWT 认证：从 token 的 user_id 字段查找 GameAccount，而非 Django User。"""

    def get_user(self, validated_token):
        user_id = validated_token.get("user_id")
        if user_id is None:
            raise AuthenticationFailed("Token 中缺少 user_id")

        try:
            account = GameAccount.objects.get(id=user_id, is_active=True)
        except GameAccount.DoesNotExist:
            raise AuthenticationFailed("玩家账号不存在或已禁用")

        return account
