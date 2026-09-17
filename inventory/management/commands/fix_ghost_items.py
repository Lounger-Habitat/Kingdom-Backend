"""
清理因 BulkPush bug 残留的「幽灵物品」。

Bug 描述：BulkPush 跳过 remainingUses=0 的字段，导致后端存 NULL，
inventory_use 无法识别为已耗尽，物品永远不被删除。

此命令找出所有 Usable 类型且 remaining_uses=NULL 的格位，直接删除。
"""
from django.core.management.base import BaseCommand
from inventory.models import InventorySlot
from items.models import Item, ItemType


class Command(BaseCommand):
    help = "清理 remaining_uses=NULL 的 Usable 类型幽灵物品"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="仅预览，不实际删除",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        # 找出所有 Usable 类型的 item_id
        usable_item_ids = set(
            Item.objects.filter(item_type=ItemType.USABLE).values_list("item_id", flat=True)
        )

        if not usable_item_ids:
            self.stdout.write(self.style.WARNING("没有找到 Usable 类型的物品定义"))
            return

        # 找出 remaining_uses=NULL 且属于 Usable 类型物品的格位
        ghosts = InventorySlot.objects.filter(
            item_id__in=usable_item_ids,
            usage__remaining__isnull=True,
            item_amount__gt=0,
        )

        count = ghosts.count()
        if count == 0:
            self.stdout.write(self.style.SUCCESS("没有发现幽灵物品，数据干净"))
            return

        self.stdout.write(f"发现 {count} 个幽灵物品：")
        for slot in ghosts[:50]:
            item = Item.objects.filter(item_id=slot.item_id).first()
            name = item.item_name if item else f"未知({slot.item_id})"
            bag_id = slot.bag.character_id if slot.bag else "?"
            self.stdout.write(
                f"  [{slot.instance_id}] bag={bag_id} item={name}(id={slot.item_id}) "
                f"amount={slot.item_amount} remaining_uses=NULL"
            )

        if dry_run:
            self.stdout.write(self.style.WARNING("--dry-run 模式，未执行删除"))
        else:
            deleted, _ = ghosts.delete()
            self.stdout.write(self.style.SUCCESS(f"已删除 {deleted} 个幽灵物品"))
