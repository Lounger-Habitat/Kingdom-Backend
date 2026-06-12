"""Backfill season_id on existing ReportEvent rows and current_season_id on PlayerState."""
from django.core.management.base import BaseCommand
from leaderboard.models import ReportEvent
from leaderboard.season_service import get_active_season
from player.models import PlayerState


class Command(BaseCommand):
    help = "回填 ReportEvent.season_id 和 PlayerState.current_season_id"

    def handle(self, *args, **options):
        season = get_active_season()
        sid = season.season_id

        # Backfill ReportEvent
        events_updated = ReportEvent.objects.filter(season_id="").update(season_id=sid)
        self.stdout.write(f"ReportEvent: {events_updated} rows backfilled to '{sid}'")

        # Backfill PlayerState
        players_updated = PlayerState.objects.filter(current_season_id="").update(current_season_id=sid)
        self.stdout.write(f"PlayerState: {players_updated} rows backfilled to '{sid}'")

        self.stdout.write(self.style.SUCCESS("Done."))
