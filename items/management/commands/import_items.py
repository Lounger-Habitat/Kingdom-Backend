"""从 Unity ScriptableObject .asset 文件导入物品数据到数据库。

用法:
    python manage.py import_items [asset_path]

默认读取 ../Assets/GameData/ItemDataList_SO.asset
"""

import re
import yaml
from pathlib import Path
from django.core.management.base import BaseCommand
from items.models import Item, ItemType


# Unity itemType (int) -> Django ItemType (string)
ITEM_TYPE_MAP = {
    0: ItemType.SEED,
    1: ItemType.COMMODITY,
    2: ItemType.FURNITURE,
    3: ItemType.HOE_TOOL,
    4: ItemType.CHOP_TOOL,
    5: ItemType.BREAK_TOOL,
    6: ItemType.REAP_TOOL,
    7: ItemType.WATER_TOOL,
    8: ItemType.COLLECT_TOOL,
    9: ItemType.REAPABLE_SCENERY,
    10: ItemType.FOOD,
    100: ItemType.INGREDIENT,
    101: ItemType.MEAT,
    102: ItemType.VEGETABLE,
    103: ItemType.FRUIT,
    104: ItemType.SEASONING,
}


def parse_asset_file(file_path):
    """解析 Unity .asset YAML 文件，提取物品列表。"""
    with open(file_path, encoding="utf-8") as f:
        content = f.read()

    # Unity YAML 有自定义 tag，pyyaml 无法直接解析，先移除
    content = re.sub(r'%TAG.*\n', '', content)
    content = re.sub(r'!u!\d+ ', '', content)
    content = re.sub(r'\{fileID: \d+(, guid: [a-f0-9]+, type: \d+)?\}', 'null', content)

    data = yaml.safe_load(content)
    items = data.get("MonoBehaviour", {}).get("itemDetailsList", [])
    return items


class Command(BaseCommand):
    help = "从 Unity ScriptableObject .asset 文件导入物品数据"

    def add_arguments(self, parser):
        parser.add_argument(
            "asset_path",
            nargs="?",
            default=None,
            help=".asset 文件路径（默认: ../Assets/GameData/ItemDataList_SO.asset）",
        )
        parser.add_argument(
            "--clear",
            action="store_true",
            help="导入前清空现有物品数据",
        )

    def handle(self, *args, **options):
        if options["asset_path"]:
            asset_path = Path(options["asset_path"])
        else:
            asset_path = Path(__file__).resolve().parent.parent.parent.parent.parent / "Assets" / "GameData" / "ItemDataList_SO.asset"

        if not asset_path.exists():
            self.stderr.write(self.style.ERROR(f"文件不存在: {asset_path}"))
            return

        if options["clear"]:
            count = Item.objects.all().count()
            Item.objects.all().delete()
            self.stdout.write(self.style.WARNING(f"已清空 {count} 条物品数据"))

        items = parse_asset_file(asset_path)
        created = 0
        updated = 0
        skipped = 0

        for entry in items:
            item_id = entry.get("itemID")
            if not item_id:
                skipped += 1
                continue

            item_type_int = entry.get("itemType", 1)
            item_type = ITEM_TYPE_MAP.get(item_type_int, ItemType.COMMODITY)

            defaults = {
                "item_name": entry.get("itemName", ""),
                "item_type": item_type,
                "item_description": entry.get("itemDescription") or "",
                "item_use_radius": entry.get("itemUseRadius", 0),
                "can_picked_up": bool(entry.get("canPickedup", 0)),
                "can_dropped": bool(entry.get("canDropped", 0)),
                "can_carried": bool(entry.get("canCarried", 0)),
                "item_price": entry.get("itemPrice", 0),
                "sell_percentage": entry.get("sellPercentage", 0.5),
            }

            obj, is_created = Item.objects.update_or_create(
                item_id=item_id,
                defaults=defaults,
            )
            if is_created:
                created += 1
            else:
                updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"导入完成: 新增 {created}, 更新 {updated}, 跳过 {skipped}"
        ))
