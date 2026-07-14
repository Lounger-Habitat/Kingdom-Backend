"""配置采集点 5 级物品池和升级金币消耗。"""

from django.db import migrations


# ── 肉类权重配置 ──
# item_id: (lv1_weight, lv2_weight, lv3_weight, lv4_weight, lv5_weight)
MEAT_WEIGHTS = {
    1056: (100, 100,  80,  60,  50),   # 鸡肉
    1057: (100, 100,  90,  70,  60),   # 五花肉
    1034: (100, 100, 100, 100,  90),   # 排骨
    1009: (100, 100, 120, 140, 150),   # 里脊肉
    1077: (100, 100, 120, 150, 180),   # 牛肉
}

# ── 食材权重配置 ──
VEG_WEIGHTS = {
    1059: (100, 100,  80,  60,  40),   # 生菜
    1073: (100, 100,  80,  60,  40),   # 葱
    1033: (100, 100,  90,  70,  50),   # 大蒜
    1023: (100, 100,  90,  70,  50),   # 卷心菜
    1042: (100, 100, 100,  80,  60),   # 土豆
    1025: (100, 100, 100,  80,  60),   # 胡萝卜
    1031: (100, 100, 100,  90,  70),   # 茄子
    1047: (100, 100, 100, 100,  80),   # 西红柿
    1001: (100, 100, 110, 110, 100),   # 南瓜
    1014: (100, 100, 110, 120, 120),   # 海带
    1024: (100, 100, 120, 130, 140),   # 西兰花
    1032: (100, 100, 120, 140, 160),   # 鸡蛋
    1040: (100, 100, 130, 150, 180),   # 红辣椒
    1043: (100, 100, 130, 160, 200),   # 青萝卜
}

# ── 每级数量范围 ──
# level: (min_amount, max_amount)
AMOUNT_RANGES = {
    1: (3, 5),
    2: (4, 9),
    3: (7, 14),
    4: (12, 19),
    5: (15, 22),
}

# ── 升级金币消耗（从该等级升到下一级）──
UPGRADE_COST_MONEY = {
    1: 50,
    2: 100,
    3: 150,
    4: 200,
    5: 0,   # 满级无需消耗
}

# ── 升级菜品消耗（从该等级升到下一级）──
# key: (采集点collect_type, 当前level) -> [(item_id, amount), ...]
UPGRADE_DISH_COSTS = {
    # 肉类采集点：需要与肉类相关的菜品，数量按5递增
    ("采集肉类", 1): [(1048, 5), (1053, 5)],              # 西红柿炒鸡蛋x5, 辣子鸡丁x5
    ("采集肉类", 2): [(1054, 10), (1062, 10)],             # 红烧肉x10, 鱼香肉丝x10
    ("采集肉类", 3): [(1061, 15), (1071, 15)],             # 红烧排骨x15, 糖醋里脊x15
    ("采集肉类", 4): [(1063, 20), (1064, 20), (1112, 20)], # 菠萝咕噜肉x20, 农家小炒肉x20, 水煮牛肉x20
    # 食材采集点：需要与蔬菜相关的菜品，数量按5递增
    ("采集食材", 1): [(1055, 5), (1060, 5)],              # 蔬菜沙拉x5, 地三鲜x5
    ("采集食材", 2): [(1068, 10), (1066, 10)],             # 蒜蓉西兰花x10, 凉拌海带丝x10
    ("采集食材", 3): [(1065, 15), (1070, 15)],             # 海岛炒饭x15, 蘑菇焖鸡x15
    ("采集食材", 4): [(1103, 20), (1111, 20), (1072, 20)], # 西红柿炖牛腩x20, 海带牛肉汤x20, 酸辣土豆丝x20
}


def seed_5_levels(apps, schema_editor):
    CollectionPoint = apps.get_model("inventory", "CollectionPoint")
    CollectionPointLevel = apps.get_model("inventory", "CollectionPointLevel")
    CollectionPointLevelItem = apps.get_model("inventory", "CollectionPointLevelItem")
    CollectionPointUpgradeItemCost = apps.get_model("inventory", "CollectionPointUpgradeItemCost")
    Item = apps.get_model("items", "Item")

    meat_cp = CollectionPoint.objects.get(collect_type="采集肉类")
    veg_cp = CollectionPoint.objects.get(collect_type="采集食材")

    # 清除旧的等级数据（包括 0011 迁移创建的 level 1）
    CollectionPointLevel.objects.filter(
        collection_point__in=[meat_cp, veg_cp]
    ).delete()

    # 为每个采集点创建 5 个等级
    for cp, weight_map in [(meat_cp, MEAT_WEIGHTS), (veg_cp, VEG_WEIGHTS)]:
        for level in range(1, 6):
            lv = CollectionPointLevel.objects.create(
                collection_point=cp,
                level=level,
                description=f"等级 {level}",
                upgrade_cost_money=UPGRADE_COST_MONEY[level],
            )

            # 创建升级菜品消耗
            dish_costs = UPGRADE_DISH_COSTS.get((cp.collect_type, level), [])
            for item_id, amount in dish_costs:
                try:
                    item = Item.objects.get(item_id=item_id)
                except Item.DoesNotExist:
                    continue
                CollectionPointUpgradeItemCost.objects.create(
                    level=lv,
                    item=item,
                    amount=amount,
                )

            min_amt, max_amt = AMOUNT_RANGES[level]
            for item_id, weights in weight_map.items():
                try:
                    item = Item.objects.get(item_id=item_id)
                except Item.DoesNotExist:
                    continue
                CollectionPointLevelItem.objects.create(
                    level=lv,
                    item=item,
                    collect_weight=weights[level - 1],
                    collect_min_amount=min_amt,
                    collect_max_amount=max_amt,
                )


def reverse_seed(apps, schema_editor):
    CollectionPoint = apps.get_model("inventory", "CollectionPoint")
    CollectionPointLevel = apps.get_model("inventory", "CollectionPointLevel")
    CollectionPointUpgradeItemCost = apps.get_model("inventory", "CollectionPointUpgradeItemCost")
    for cp in CollectionPoint.objects.filter(collect_type__in=["采集肉类", "采集食材"]):
        levels = CollectionPointLevel.objects.filter(collection_point=cp)
        CollectionPointUpgradeItemCost.objects.filter(level__in=levels).delete()
        levels.filter(level__gte=2).delete()
        # 恢复 level 1 的默认值
        lv1 = levels.filter(level=1).first()
        if lv1:
            lv1.upgrade_cost_money = 0
            lv1.save()


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0011_seed_collection_points"),
    ]

    operations = [
        migrations.RunPython(seed_5_levels, reverse_seed),
    ]
