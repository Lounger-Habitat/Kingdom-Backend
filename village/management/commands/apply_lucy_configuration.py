import json
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from village.lucy_configuration import configure_lucy, normalize_existing_menu


class Command(BaseCommand):
    help = "增量更新Lucy目录/权限/储备与未售菜单，不发钱货、不改时钟。"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        result = configure_lucy()
        if not result["catalog_ready"]:
            raise CommandError(f"基础食材目录缺失：{result['missing_ingredient_ids']}")
        result["menu_changes"] = normalize_existing_menu()
        result["dry_run"] = options["dry_run"]
        if options["dry_run"]:
            transaction.set_rollback(True)
        self.stdout.write(json.dumps(result, ensure_ascii=False))
