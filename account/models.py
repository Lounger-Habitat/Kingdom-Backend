import uuid
from django.db import models


class GameAccount(models.Model):
    """游戏玩家账号，与 Django admin 的 User 完全独立。"""
    username = models.CharField(max_length=30, unique=True)
    password = models.CharField(max_length=128)
    is_guest = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    # Django admin 需要的字段（不会用到，但避免框架报错）
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        tag = " [guest]" if self.is_guest else ""
        return f"{self.username}{tag}"

    # ---- DRF 兼容：让 IsAuthenticated 等权限类正常工作 ----

    @property
    def is_authenticated(self):
        return True

    @property
    def is_anonymous(self):
        return False

    # ---- 密码相关（简易实现，不依赖 django.contrib.auth）----

    def set_password(self, raw_password):
        from django.contrib.auth.hashers import make_password
        self.password = make_password(raw_password)

    def check_password(self, raw_password):
        from django.contrib.auth.hashers import check_password
        return check_password(raw_password, self.password)

    @classmethod
    def create_user(cls, username, password, is_guest=False):
        account = cls(username=username, is_guest=is_guest)
        account.set_password(password)
        account.save()
        return account
