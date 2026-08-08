from django.core.management.base import BaseCommand

from leaderboard.services import settle_due_periods


class Command(BaseCommand):
    help = "结算到期的北京时间周期增量榜；应由系统每分钟执行一次。"

    def handle(self, *args, **options):
        settle_due_periods()
        self.stdout.write(self.style.SUCCESS("periodic leaderboard settlement checked"))
