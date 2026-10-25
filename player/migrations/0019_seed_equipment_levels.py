from django.db import migrations
from django.utils import timezone


SPECIES = [
    (4114, "基础剑", "sword", False, "hunting", "CollectTool", 15, 18),
    (4130, "精铁剑", "sword", True, "hunting", "CollectTool", 26, 30),
    (4110, "木柄长矛", "spear", False, "hunting", "CollectTool", 18, 22),
    (4111, "精铁长矛", "spear", True, "hunting", "CollectTool", 29, 34),
    (4112, "木弓", "bow", False, "hunting", "CollectTool", 20, 25),
    (4113, "硬木长弓", "bow", True, "hunting", "CollectTool", 31, 37),
    (4104, "基础斧", "axe", False, "forestry", "ChopTool", 22, 26),
    (4105, "精铁斧", "axe", True, "forestry", "ChopTool", 33, 38),
    (4101, "基础采集包", "gathering", False, "gathering", "CollectTool", 0, 0),
    (4102, "精制采集包", "gathering", True, "gathering", "CollectTool", 0, 0),
    (4001, "基础农具", "farming", False, "farming", "HoeTool", 0, 0),
    (4103, "精铁农具", "farming", True, "farming", "HoeTool", 0, 0),
    (4106, "简易炊具", "cooking", False, "cooking", "CollectTool", 0, 0),
    (4107, "精铁炊具", "cooking", True, "cooking", "CollectTool", 0, 0),
    (4002, "基础猎具", "processing", False, "", "CollectTool", 0, 0),
    (4131, "精制猎具", "processing", True, "", "CollectTool", 0, 0),
]


def install(apps, schema_editor):
    db = schema_editor.connection.alias
    Item = apps.get_model("items", "Item")
    Definition = apps.get_model("player", "EquipmentDefinition")
    Level = apps.get_model("player", "EquipmentLevel")
    Gameplay = apps.get_model("player", "ItemGameplayDefinition")
    Move = apps.get_model("player", "WeaponMove")
    capabilities = {"sword": "hunting_sword", "spear": "hunting_spear", "bow": "hunting_bow", "axe": "forestry",
                    "gathering": "targeted_gathering", "farming": "farming", "cooking": "cooking", "processing": "processing"}
    for item_id, name, category, advanced, skill, kind, attack_min, attack_max in SPECIES:
        weapon = category in {"sword", "spear", "bow"}
        durability = ([100, 120, 140, 160, 180] if advanced else [60, 75, 90, 105, 120]) if weapon else (
            [50, 65, 80, 95, 110] if advanced else [20, 30, 40, 50, 60])
        item, created = Item.objects.using(db).get_or_create(pk=item_id, defaults=dict(item_name=name,
            item_type=kind, collectable=False, stackable=False, max_use_count=durability[0], item_price=150 if advanced else 50))
        if not created and item_id in (4130, 4131):
            raise ValueError(f"装备新增物品ID冲突：{item_id}")
        Item.objects.using(db).filter(pk=item_id).update(item_name=name, max_use_count=durability[0],
            item_description=f"{name}；独立装备等级Lv.1～Lv.5，属性由装备配置管理。", stackable=False, updated_at=timezone.now())
        if created and item_id in (4130, 4131):
            original = Item.objects.using(db).filter(pk=4114 if item_id == 4130 else 4002).first()
            if original:
                Item.objects.using(db).filter(pk=item_id).update(icon_url=original.icon_url, icon_image=original.icon_image)
        profile = Definition.objects.using(db).create(item_id=item_id, category=category, advanced=advanced, required_skill=skill)
        Gameplay.objects.using(db).update_or_create(item_id=item_id, defaults=dict(capability_code=capabilities[category],
            tier=1, required_skill=skill, required_level=1, effects=[]))
        for index in range(5):
            quality = ([.08, .11, .14, .17, .20] if advanced else [0, .02, .04, .06, .08])[index]
            material = 1002 if not advanced or category in {"spear", "bow", "gathering"} else 4003
            upgrade_materials = [dict(item_id=material, quantity=index * (3 if advanced else 2))] if index else []
            if advanced and index:
                upgrade_materials.append(dict(item_id=29306, quantity=index))
            Level.objects.using(db).create(equipment=profile, level=index+1,
                required_skill_level=[1, 2, 4, 6, 7][index], max_durability=durability[index],
                attack_min=attack_min+index*3 if attack_min else 0, attack_max=attack_max+index*3 if attack_max else 0,
                gathering_time_reduction=([.25, .30, .35, .40, .45] if advanced else [.10, .15, .20, .25, .30])[index] if category == "gathering" else 0,
                quality_bonus_chance=quality if category in {"gathering", "cooking"} else 0,
                harvest_bonus_chance=([.15, .20, .25, .30, .35] if advanced else [0, .05, .10, .15, .20])[index] if category == "farming" else 0,
                chop_power=([1.3, 1.4, 1.5, 1.6, 1.7] if advanced else [1, 1.1, 1.2, 1.3, 1.4])[index] if category == "axe" else 1,
                cooking_batches=index+1 if category == "cooking" else 1,
                processing_time_reduction=([.20, .25, .30, .35, .40] if advanced else [0, .05, .10, .15, .20])[index] if category == "processing" else 0,
                upgrade_gold=([0, 50, 100, 175, 275] if advanced else [0, 20, 40, 70, 110])[index], upgrade_materials=upgrade_materials,
                repair_gold=0 if weapon and not advanced else 15 if advanced else 5,
                repair_materials=[] if weapon and not advanced else [dict(item_id=material, quantity=1)])
    for code, name, category, coefficient, minimum in [
        ("sword_light_0", "剑·首段轻击", "sword", 1.2, 0), ("sword_light_1", "剑·第二段轻击", "sword", 1.4, 0),
        ("sword_light_2", "剑·第三段轻击", "sword", 1.6, 0), ("sword_heavy", "剑·重击", "sword", 2, 0),
        ("spear_thrust", "矛·普通刺击", "spear", 1.4, 0), ("bow_shot", "弓·蓄力射击", "bow", 1.8, .6),
        ("axe_chop", "斧·防身劈砍", "axe", 1.3, 0)]:
        Move.objects.using(db).create(code=code, name=name, category=category, multiplier=coefficient, minimum_multiplier=minimum)
    # Migrate live instances only. Audit receipts and frozen production batches remain historical.
    aliases = {4120: 4102, 4121: 4103, 4122: 4105, 4123: 4107, 4124: 4111, 4125: 4113}
    old_t2 = {4102, 4103, 4105, 4107, 4111, 4113}
    for app, owner_name, usage_name in [
        ("inventory", "InventorySlot", "InventorySlotUsage"), ("inventory", "NpcGlobalInventorySlot", "NpcGlobalInventorySlotUsage"),
        ("village", "VillageStoreInventorySlot", "VillageStoreInventorySlotUsage"), ("marketplace", "MarketListing", "MarketListingUsage")]:
        Owner, Usage = apps.get_model(app, owner_name), apps.get_model(app, usage_name)
        for row in Owner.objects.using(db).filter(item_id__in=old_t2 | aliases.keys()).iterator():
            target_level = 5 if row.item_id in aliases else 3
            if row.item_id in aliases:
                row.item_id = aliases[row.item_id]
                row.save(using=db, update_fields=["item_id"])
            Usage.objects.using(db).update_or_create(owner_id=row.pk, defaults=dict(equipment_level=target_level))
    Listing = apps.get_model("village", "VillageStoreListing")
    for old, new in aliases.items():
        Listing.objects.using(db).filter(item_id=old).update(item_id=new)
    # Keep old IDs out of new purchases; existing IDs have been mapped above.
    Item.objects.using(db).filter(pk__in=aliases).update(is_deleted=True)


class Migration(migrations.Migration):
    dependencies = [("player", "0018_weaponmove_equipmentdefinition_equipmentlevel"),
        ("inventory", "0029_inventoryslotusage_equipment_level_and_more"),
        ("village", "0085_villagestoreinventoryslotusage_equipment_level"),
        ("marketplace", "0006_marketlistingusage_equipment_level")]
    operations = [migrations.RunPython(install, migrations.RunPython.noop)]
