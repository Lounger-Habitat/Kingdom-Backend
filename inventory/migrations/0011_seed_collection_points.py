"""初始化采集点及等级1物品池（从现有 Item.collectable 字段迁移）。"""

from django.db import migrations


def seed_collection_points(apps, schema_editor):
    CollectionPoint = apps.get_model("inventory", "CollectionPoint")
    CollectionPointLevel = apps.get_model("inventory", "CollectionPointLevel")
    CollectionPointLevelItem = apps.get_model("inventory", "CollectionPointLevelItem")
    Item = apps.get_model("items", "Item")

    # 1. 创建两个采集点
    meat_cp = CollectionPoint.objects.create(
        name="肉类采集点",
        collect_type="采集肉类",
        item_type="肉类",
        action_cost=15,
        collect_count=3,
        default_level=1,
    )
    veg_cp = CollectionPoint.objects.create(
        name="食材采集点",
        collect_type="采集食材",
        item_type="蔬菜",
        action_cost=15,
        collect_count=3,
        default_level=1,
    )

    # 2. 为每个采集点创建 level=1
    meat_lv1 = CollectionPointLevel.objects.create(
        collection_point=meat_cp, level=1, description="初始等级", upgrade_cost_money=0,
    )
    veg_lv1 = CollectionPointLevel.objects.create(
        collection_point=veg_cp, level=1, description="初始等级", upgrade_cost_money=0,
    )

    # 3. 从现有 collectable Item 初始化物品池
    for item in Item.objects.filter(collectable=True, is_deleted=False, item_type="肉类"):
        CollectionPointLevelItem.objects.create(
            level=meat_lv1,
            item=item,
            collect_weight=item.collect_weight,
            collect_min_amount=item.collect_min_amount,
            collect_max_amount=item.collect_max_amount,
        )

    for item in Item.objects.filter(collectable=True, is_deleted=False, item_type="蔬菜"):
        CollectionPointLevelItem.objects.create(
            level=veg_lv1,
            item=item,
            collect_weight=item.collect_weight,
            collect_min_amount=item.collect_min_amount,
            collect_max_amount=item.collect_max_amount,
        )


def reverse_seed(apps, schema_editor):
    CollectionPoint = apps.get_model("inventory", "CollectionPoint")
    CollectionPoint.objects.filter(collect_type__in=["采集肉类", "采集食材"]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0010_collectionpoint_collectionpointlevel_and_more"),
        ("items", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_collection_points, reverse_seed),
    ]
