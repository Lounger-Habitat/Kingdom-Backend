from django.core.management.base import BaseCommand
from django.db import transaction

from village.management.commands.seed_village_v1 import Command as VillageSeedCommand
from village.models import VillageSimulationConfig


class Command(BaseCommand):
    help = "仅增量配置Bill的36格农田与三种新种子，不改写已有地块或物品"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="校验后回滚所有新增")

    def handle(self, *args, **options):
        summary = {"farm_items": 0, "farm_plots": 0, "caravan_configs": 0}
        with transaction.atomic():
            VillageSeedCommand._seed_farm_items(summary)
            VillageSeedCommand._seed_farm_plots(summary)
            for config in VillageSimulationConfig.objects.exclude(caravan_item_ids=[]):
                expanded = list(config.caravan_item_ids or [])
                for item_id in (29301, 29302, 29303):
                    if item_id not in expanded:
                        expanded.append(item_id)
                if expanded != config.caravan_item_ids:
                    config.caravan_item_ids = expanded
                    config.save(update_fields=["caravan_item_ids"])
                summary["caravan_configs"] += 1
            if options["dry_run"]:
                transaction.set_rollback(True)

        mode = "DRY-RUN（已回滚）" if options["dry_run"] else "APPLIED"
        self.stdout.write(
            self.style.SUCCESS(
                f"configure_bill_farm {mode}: "
                f"farm_items={summary['farm_items']}, farm_plots={summary['farm_plots']}, "
                f"caravan_configs={summary['caravan_configs']}"
            )
        )
