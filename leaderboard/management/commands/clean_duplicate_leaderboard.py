"""Management command to clean duplicate leaderboard entries and refresh."""
from django.core.management.base import BaseCommand
from leaderboard.models import LeaderboardEntry, BoardType
from leaderboard.services import refresh_leaderboard


class Command(BaseCommand):
    help = "清理排行榜重复数据并重新生成"

    def add_arguments(self, parser):
        parser.add_argument(
            '--board-type',
            type=str,
            default='total_freshness',
            help='要清理的榜单类型 (默认: total_freshness)',
        )
        parser.add_argument(
            '--season-id',
            type=str,
            default=None,
            help='赛季ID (默认: 使用当前活跃赛季)',
        )

    def handle(self, *args, **options):
        board_type = options['board_type']
        season_id = options['season_id']

        # 验证 board_type
        if board_type not in BoardType.values:
            self.stdout.write(self.style.ERROR(f'无效的榜单类型: {board_type}'))
            self.stdout.write(f'有效类型: {", ".join(BoardType.values)}')
            return

        # 如果没有指定 season_id，获取当前活跃赛季
        if season_id is None:
            from leaderboard.season_service import get_active_season
            season = get_active_season()
            season_id = season.season_id
            self.stdout.write(f'使用当前赛季: {season_id}')

        # 统计删除前的记录数
        count_before = LeaderboardEntry.objects.filter(
            board_type=board_type,
            season_id=season_id,
        ).count()

        # 删除所有匹配的记录
        deleted = LeaderboardEntry.objects.filter(
            board_type=board_type,
            season_id=season_id,
        ).delete()[0]

        self.stdout.write(f'删除 {board_type} 记录: {deleted} 条')

        # 重新生成排行榜
        self.stdout.write(f'正在重新生成 {board_type}...')
        refresh_leaderboard(board_type, season_id)

        # 统计删除后的记录数
        count_after = LeaderboardEntry.objects.filter(
            board_type=board_type,
            season_id=season_id,
        ).count()

        self.stdout.write(self.style.SUCCESS(
            f'清理完成: {board_type} ({season_id}) '
            f'删除 {deleted} 条, 重新生成 {count_after} 条'
        ))