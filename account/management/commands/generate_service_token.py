"""生成 Env Server 专用的、可轮换的 service token。

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

        # service token 必须带受限 scope；过期后由部署系统重新签发。
        token = AccessToken.for_user(account)
        token["scope"] = "env_server"
        token["aud"] = "kingdom-env"
        token.set_exp(lifetime=timedelta(days=1))

        token_str = str(token)
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=" * 60))
        self.stdout.write(self.style.SUCCESS("Service Token (Env Server scope，有效期24小时):"))
        self.stdout.write(self.style.SUCCESS("=" * 60))
        self.stdout.write(token_str)
        self.stdout.write(self.style.SUCCESS("=" * 60))
        self.stdout.write("")
        self.stdout.write("通过服务器宿主机的 ENV_SERVER_TOKEN 注入，勿写入客户端构建物或 Resources 资源。")
