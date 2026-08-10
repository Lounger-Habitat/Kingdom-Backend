"""Seed the phase-two base ration without changing existing NPC assets."""

import uuid

from django.db import migrations


FORMAL_NPC_IDS = ("10011", "10012", "10013", "10014", "10015")
BASE_RATION_ITEM_ID = 29001
BASE_RATION_ITEM_TYPE = "食物"
BASE_RATION_INSTANCE_NAMESPACE = uuid.UUID("c1f4c9ec-b93a-4df3-8b0f-021c3c0f159f")


def ration_instance_id(agent_id, ordinal):
    return uuid.uuid5(
        BASE_RATION_INSTANCE_NAMESPACE,
        f"village.phase2.base-ration:{agent_id}:{ordinal}",
    )


def _get_or_create_base_ration(Item):
    item = Item.objects.filter(item_id=BASE_RATION_ITEM_ID).first()
    if item is None:
        return Item.objects.create(
            item_id=BASE_RATION_ITEM_ID,
            item_name="村庄基础口粮",
            item_type=BASE_RATION_ITEM_TYPE,
            item_description="阶段二基础生活动作口粮",
            item_price=0,
            collectable=False,
        )

    # This ID is an explicit phase-two contract.  Never coerce an existing
    # different item into food: failing the migration is safer than silently
    # making another asset consumable.
    if item.item_type != BASE_RATION_ITEM_TYPE or item.is_deleted:
        raise RuntimeError(
            "phase-two base ration item 29001 already exists with an incompatible definition"
        )
    return item


def seed_phase2_food(apps, schema_editor):
    Item = apps.get_model("items", "Item")
    NpcDefinition = apps.get_model("inventory", "NpcDefinition")
    NpcGlobalInventory = apps.get_model("inventory", "NpcGlobalInventory")
    NpcGlobalInventorySlot = apps.get_model("inventory", "NpcGlobalInventorySlot")

    _get_or_create_base_ration(Item)

    formal_npcs = NpcDefinition.objects.filter(
        agent_id__in=FORMAL_NPC_IDS,
        is_test_only=False,
    ).order_by("agent_id")
    for npc in formal_npcs:
        bag, _ = NpcGlobalInventory.objects.get_or_create(npc=npc)
        next_slot_index = (
            NpcGlobalInventorySlot.objects.filter(bag=bag)
            .order_by("-slot_index")
            .values_list("slot_index", flat=True)
            .first()
        )
        next_slot_index = 0 if next_slot_index is None else next_slot_index + 1

        for ordinal in range(4):
            instance_id = ration_instance_id(npc.agent_id, ordinal)
            matching_slots = NpcGlobalInventorySlot.objects.filter(instance_id=instance_id)
            existing = matching_slots.first()
            if existing is not None:
                if existing.bag_id != bag.id:
                    raise RuntimeError(
                        "phase-two base ration deterministic instance belongs to a different inventory"
                    )
                continue
            NpcGlobalInventorySlot.objects.create(
                bag=bag,
                slot_index=next_slot_index,
                instance_id=instance_id,
                item_id=BASE_RATION_ITEM_ID,
                item_amount=1,
                item_bag_name="村庄基础口粮",
                hide_in_shop=True,
            )
            next_slot_index += 1


def reverse_phase2_food(apps, schema_editor):
    """Only remove unchanged, deterministic seeds; consumed assets survive."""
    NpcGlobalInventorySlot = apps.get_model("inventory", "NpcGlobalInventorySlot")

    instance_ids = [
        ration_instance_id(agent_id, ordinal)
        for agent_id in FORMAL_NPC_IDS
        for ordinal in range(4)
    ]
    NpcGlobalInventorySlot.objects.filter(
        instance_id__in=instance_ids,
        item_id=BASE_RATION_ITEM_ID,
        item_amount=1,
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("items", "0005_alter_item_options"),
        ("village", "0006_phase2_needs_models"),
    ]

    operations = [
        migrations.RunPython(seed_phase2_food, reverse_phase2_food),
    ]
