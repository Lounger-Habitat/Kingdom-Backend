from django.core.management.base import BaseCommand

from village.runtime_services import OutboxService


class Command(BaseCommand):
    help = "Remove one bounded batch of old acknowledged village deliveries."

    def add_arguments(self, parser):
        parser.add_argument("--retention-days", type=int, default=7)
        parser.add_argument("--batch-size", type=int, default=1000)

    def handle(self, *args, **options):
        result = OutboxService.cleanup(options["retention_days"], options["batch_size"])
        self.stdout.write(self.style.SUCCESS(str(result)))
