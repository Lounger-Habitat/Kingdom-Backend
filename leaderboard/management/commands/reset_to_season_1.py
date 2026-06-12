"""Management command: full reset all data back to season_1."""
from django.core.management.base import BaseCommand
from leaderboard.season_service import full_reset_to_season_1


class Command(BaseCommand):
    help = "全量重置回第一赛季：清除所有赛季/排行榜/玩家数据，回到初始状态"

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="确认执行重置（必须传入才会真正执行）",
        )

    def handle(self, *args, **options):
        if not options["confirm"]:
            self.stdout.write(self.style.WARNING(
                "⚠️  此操作将删除所有赛季、排行榜、玩家背包/食谱/称号/保底数据并重置回第一赛季！\n"
                "请传入 --confirm 参数确认执行。"
            ))
            return

        self.stdout.write("正在执行全量重置...")
        stats = full_reset_to_season_1()

        self.stdout.write(self.style.SUCCESS("✅ 全量重置完成！"))
        for key, value in stats.items():
            self.stdout.write(f"  {key}: {value}")
