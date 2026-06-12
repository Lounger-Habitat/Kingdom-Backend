from django.core.management.base import BaseCommand
from leaderboard.season_service import get_active_season


class Command(BaseCommand):
    help = "初始化第一个赛季（如果赛季表为空）"

    def handle(self, *args, **options):
        season = get_active_season()
        self.stdout.write(self.style.SUCCESS(
            f"赛季初始化完成：{season.season_id} (#{season.sequence})，"
            f"时长 {season.duration_days} 天，状态 {season.status}"
        ))
