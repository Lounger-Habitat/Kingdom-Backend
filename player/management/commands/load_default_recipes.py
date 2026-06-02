import json
from pathlib import Path
from django.core.management.base import BaseCommand
from player.models import DefaultRecipe


class Command(BaseCommand):
    help = "从 StreamingAssets/Recipe.json 导入默认菜谱名到 DefaultRecipe 表（只存菜名，食材从 recipes.Recipe 目录查询）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            type=str,
            default=None,
            help="指定 JSON 文件路径（默认使用 StreamingAssets/Recipe.json）",
        )

    def handle(self, *args, **options):
        if options["file"]:
            json_path = Path(options["file"])
        else:
            json_path = Path(__file__).resolve().parents[4] / "Assets" / "StreamingAssets" / "Recipe.json"

        if not json_path.exists():
            self.stderr.write(self.style.ERROR(f"文件不存在: {json_path}"))
            return

        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        created, skipped = 0, 0
        for recipe_name in data.keys():
            _, was_created = DefaultRecipe.objects.get_or_create(recipe_name=recipe_name)
            if was_created:
                created += 1
            else:
                skipped += 1

        self.stdout.write(self.style.SUCCESS(
            f"导入完成: 新增 {created}, 已存在跳过 {skipped}, 共 {len(data)} 条"
        ))
