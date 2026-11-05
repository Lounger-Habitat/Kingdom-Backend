import json

from django.core.management.base import BaseCommand, CommandError

from inventory.normalization import normalize_inventory


class Command(BaseCommand):
    help = "Merge material stock and split individual dishes/equipment; dry-run unless --apply is supplied."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit changes (default rolls back every write).")

    def handle(self, *args, **options):
        try:
            result = normalize_inventory(apply=options["apply"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(dict(mode="apply" if options["apply"] else "dry-run", **result), sort_keys=True))
