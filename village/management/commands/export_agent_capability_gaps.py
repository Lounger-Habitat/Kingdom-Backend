from django.core.management.base import BaseCommand, CommandError

from village.capability_gaps import rebuild_log


class Command(BaseCommand):
    help = "Rebuild the Agent capability-gap JSONL file from saved Action tasks."

    def handle(self, *args, **options):
        try:
            count, path = rebuild_log()
        except (OSError, ValueError) as error:
            raise CommandError(str(error)) from error
        self.stdout.write(self.style.SUCCESS(f"Exported {count} capability gaps to {path}"))
