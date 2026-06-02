from django.core.management.base import BaseCommand
from leaderboard.models import ReportEvent
from player.models import PlayerState


class Command(BaseCommand):
    help = "Backfill game_day on existing ReportEvents that have NULL game_day"

    def handle(self, *args, **options):
        null_events = ReportEvent.objects.filter(game_day__isnull=True)
        count = null_events.count()
        if count == 0:
            self.stdout.write(self.style.SUCCESS("No events to backfill."))
            return

        player_days = dict(
            PlayerState.objects.values_list("player_id", "total_game_days")
        )

        updated = 0
        batch = []
        batch_size = 500
        for event in null_events.iterator(chunk_size=batch_size):
            day = player_days.get(event.player_id)
            if day is not None:
                event.game_day = day
                batch.append(event)
            if len(batch) >= batch_size:
                ReportEvent.objects.bulk_update(batch, ["game_day"])
                updated += len(batch)
                batch = []
        if batch:
            ReportEvent.objects.bulk_update(batch, ["game_day"])
            updated += len(batch)

        self.stdout.write(self.style.SUCCESS(
            f"Backfilled {updated}/{count} events."
        ))
