"""生成 Env Server 永不过期的 service token。

用法: python manage.py generate_service_token
"""
from datetime import datetime, timedelta
from django.core.management.base import BaseCommand
from rest_framework_simplejwt.tokens import AccessToken
from account.models import GameAccount


class Command(BaseCommand):
    help = "生成 Env Server 专用的永不过期 JWT token"

    def handle(self, *args, **options):
        # 获取或创建 env_server 账号
        account, created = GameAccount.objects.get_or_create(
            username="env_server",
            defaults={"is_ai": True},
        )
        if created:
            account.set_password("env_server_internal_token")
            account.save()
            self.stdout.write(self.style.SUCCESS(f"已创建服务账号: env_server (id={account.id})"))
        else:
            self.stdout.write(f"服务账号已存在: env_server (id={account.id})")

        # 生成 token，过期时间设为 10 年
        token = AccessToken.for_user(account)
        token.set_exp(lifetime=timedelta(days=3650))

        token_str = str(token)
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=" * 60))
        self.stdout.write(self.style.SUCCESS("Service Token (永不过期，10年有效):"))
        self.stdout.write(self.style.SUCCESS("=" * 60))
        self.stdout.write(token_str)
        self.stdout.write(self.style.SUCCESS("=" * 60))
        self.stdout.write("")
        self.stdout.write("将此 token 填入 Unity 的 EnvServerConfig.asset → serviceToken 字段")
