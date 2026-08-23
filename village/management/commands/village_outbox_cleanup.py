from django.core.management.base import BaseCommand

from village.runtime_services import OutboxService


class Command(BaseCommand):
    help = "Summarize and remove old acknowledged village event deliveries."

    def add_arguments(self, parser):
        parser.add_argument("--retention-days", type=int, default=7)

    def handle(self, *args, **options):
        result = OutboxService.cleanup(options["retention_days"])
        self.stdout.write(self.style.SUCCESS(str(result)))
