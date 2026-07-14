from django.core.management.base import BaseCommand
from player.models import ActionPointCostConfig


class Command(BaseCommand):
    help = "初始化行动力消耗配置种子数据"

    def handle(self, *args, **options):
        self.stdout.write("开始初始化行动力消耗配置...")

        configs = [
            {"cost_type": "collect", "cost_amount": 15, "description": "采集"},
            {"cost_type": "talk",    "cost_amount": 1,  "description": "对话"},
            {"cost_type": "cook",    "cost_amount": 0,  "description": "烹饪"},
            {"cost_type": "trade",   "cost_amount": 0,  "description": "交易"},
            {"cost_type": "judge",   "cost_amount": 2,  "description": "点评菜品"},
        ]

        for data in configs:
            obj, created = ActionPointCostConfig.objects.update_or_create(
                cost_type=data["cost_type"],
                defaults=data,
            )
            status = "创建" if created else "更新"
            self.stdout.write(f"  {status}: {obj}")

        self.stdout.write(self.style.SUCCESS("行动力消耗配置初始化完成！"))
