"""Management command to remove 100-score entries from the freshness leaderboard and refresh."""
from django.core.management.base import BaseCommand
from leaderboard.models import ReportEvent, LeaderboardEntry, BoardType
from leaderboard.services import refresh_leaderboard


class Command(BaseCommand):
    help = "删除极鲜榜中100分的记录并刷新榜单"

    def handle(self, *args, **options):
        # 1. 找到并删除 judge_score=100 的 ReportEvent
        events = ReportEvent.objects.filter(event_type="judge_confirmed")
        deleted_events = 0
        for ev in events:
            payload = ev.payload if isinstance(ev.payload, dict) else {}
            if payload.get("judge_score") == 100:
                self.stdout.write(f"删除事件: player={ev.player_id}, payload={ev.payload}")
                ev.delete()
                deleted_events += 1

        # 2. 删除极鲜榜中 score=100 的 LeaderboardEntry
        deleted_entries_total = LeaderboardEntry.objects.filter(
            board_type=BoardType.TOTAL_FRESHNESS, score=100
        ).delete()[0]

        self.stdout.write(f"删除 ReportEvent: {deleted_events} 条")
        self.stdout.write(f"删除 TOTAL_FRESHNESS LeaderboardEntry: {deleted_entries_total} 条")

        # 3. 刷新极鲜榜
        refresh_leaderboard(BoardType.TOTAL_FRESHNESS)
        self.stdout.write(self.style.SUCCESS("极鲜榜已刷新完成"))
