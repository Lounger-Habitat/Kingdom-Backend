"""Phase6 live-actions-all 实跑前的一次性世界备货（幂等，只加不减）。

补的是"脚本无法自产"的资产缺口（安全库存/成本/依赖链所需），不覆盖现有资产：
  - Nora(10015) 玉米 1030 x20：配送消耗 5 + 出口 3（需高于安全库存 10）+ 上架 5；
  - Lucy(10014) 成品菜 1053 x2：EAT 消耗 1 + RATE_DISH 评分 1（即使 COOK 失败也有货）；
  - Lucy(10014) 菜谱2食材 1056 x4 / 1040 x2：确保 COOK 可做（已有则不动）；
  - Bill(10011) 金币 ->100：PROPOSE_TRADE 40 + BUY_FROM_STORE 12；
  - Nora(10015) 金币 ->100：安全余量。

用法：python manage.py seed_phase6_world [--dry-run]
"""
from __future__ import annotations

import uuid

from django.core.management.base import BaseCommand
from django.db import transaction

from inventory.models import NpcGlobalInventory, NpcGlobalInventorySlot
from items.models import Item

# 专用确定性实例命名空间（与 seed_village_v1 的 uuid5 模式一致，保证幂等）
PHASE6_NAMESPACE = uuid.UUID("c9f4a8e2-5b3d-4f0a-9e6c-2d8b1a7c4e3f")

# (npc_id, item_id, 目标数量)
ITEM_TARGETS = [
    ("10015", 1030, 20),   # Nora: 玉米（配送 5 + 出口 3 + 上架 5，安全库存 10）
    ("10014", 1053, 2),    # Lucy: 成品菜辣子鸡丁（EAT 1 + RATE_DISH 1）
    ("10014", 1056, 4),    # Lucy: 菜谱 2 食材 鸡肉
    ("10014", 1040, 2),    # Lucy: 菜谱 2 食材 红辣椒
]

# (npc_id, 目标金币；低于目标则抬升，绝不削减)
MONEY_TARGETS = [
    ("10011", 100),   # Bill: PROPOSE_TRADE 40 + BUY_FROM_STORE 12
    ("10015", 100),   # Nora: 安全余量（配送/上架收入会继续回补）
]


class Command(BaseCommand):
    help = "幂等补齐 Phase6 live-actions-all 实跑所需的物品/金币（只加不减）"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="执行完整校验但回滚所有写入")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        summary = {"items": [], "money": []}
        with transaction.atomic():
            for npc_id, item_id, target in ITEM_TARGETS:
                summary["items"].append(self._topup_item(npc_id, item_id, target))
            for npc_id, target in MONEY_TARGETS:
                summary["money"].append(self._topup_money(npc_id, target))
            if dry_run:
                transaction.set_rollback(True)
        mode = "DRY-RUN（已回滚）" if dry_run else "APPLIED"
        self.stdout.write(
            self.style.SUCCESS(
                f"seed_phase6_world {mode}:\n  items: {summary['items']}\n  money: {summary['money']}"
            )
        )

    def _topup_item(self, npc_id: str, item_id: int, target: int) -> str:
        bag = NpcGlobalInventory.objects.select_for_update().get(npc__agent_id=npc_id)
        current = sum(
            slot.item_amount
            for slot in NpcGlobalInventorySlot.objects.filter(bag=bag, item_id=item_id)
        )
        if current >= target:
            return f"{npc_id}:{item_id} ok({current})"
        need = target - current
        instance_id = uuid.uuid5(PHASE6_NAMESPACE, f"phase6:{npc_id}:{item_id}")
        name = Item.objects.filter(item_id=item_id).values_list("item_name", flat=True).first() or str(item_id)
        slot = NpcGlobalInventorySlot.objects.filter(instance_id=instance_id).first()
        if slot is None:
            next_index = bag.slots.order_by("-slot_index").values_list("slot_index", flat=True).first()
            next_index = 0 if next_index is None else next_index + 1
            NpcGlobalInventorySlot.objects.create(
                bag=bag,
                slot_index=next_index,
                instance_id=instance_id,
                item_id=item_id,
                item_amount=need,
                item_bag_name=f"{name}_phase6",
                hide_in_shop=False,
            )
        else:
            slot.item_amount = need
            slot.save(update_fields=["item_amount"])
        return f"{npc_id}:{item_id} +{need}({current}->{target})"

    def _topup_money(self, npc_id: str, target: int) -> str:
        bag = NpcGlobalInventory.objects.select_for_update().get(npc__agent_id=npc_id)
        if bag.money >= target:
            return f"{npc_id} ok({bag.money})"
        old = bag.money
        bag.money = target
        bag.save(update_fields=["money"])
        return f"{npc_id} {old}->{target}"
