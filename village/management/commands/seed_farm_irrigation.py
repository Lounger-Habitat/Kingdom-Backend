from django.core.management.base import BaseCommand

from game_time.models import GlobalGameTimeState
from village.farm_irrigation import ensure_irrigation_affair


class Command(BaseCommand):
    help = "添加疏通农田水渠事务；重复执行保留现有进度。"

    def handle(self, *args, **options):
        row = ensure_irrigation_affair(GlobalGameTimeState.get_instance().world_minute)
        self.stdout.write(f"{row.code}: {row.status}, version={row.version}")
