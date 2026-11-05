"""One runtime projection for admin-configured equipment. Never initializes data on reads."""
from copy import copy
from .models import EquipmentDefinition, EquipmentLevel, ItemGameplayDefinition, WeaponMove

WEAPONS = {"spear": 0, "sword": 1, "bow": 2, "axe": 3}
CAPABILITIES = {"spear": "hunting_spear", "sword": "hunting_sword", "bow": "hunting_bow", "axe": "forestry",
                "gathering": "targeted_gathering", "farming": "farming", "cooking": "cooking", "processing": "processing"}


def compatibility_tier(level):
    return 3 if level >= 5 else 2 if level >= 3 else 1


def stats_for(slot, *, level=None, catalog=None):
    level = slot.equipment_level if level is None else level
    if type(level) is not int or not 1 <= level <= 5:
        return None
    if catalog is not None:
        return catalog.get((slot.item_id, level))
    row = EquipmentLevel.objects.select_related("equipment__item").filter(
        equipment__item_id=slot.item_id, equipment__enabled=True,
        equipment__item__is_deleted=False, level=level).first()
    if row is None:
        return None
    return _stats(row)


def _stats(row):
    result = {f.name: getattr(row, f.name) for f in row._meta.fields if f.name not in {"id", "equipment"}}
    result.update(item_id=row.equipment.item_id, item_name=row.equipment.item.item_name, category=row.equipment.category,
        advanced=row.equipment.advanced, required_skill=row.equipment.required_skill,
        capability=CAPABILITIES[row.equipment.category], tier=compatibility_tier(row.level))
    return result


def equipment_catalog():
    return {(row.equipment.item_id, row.level): _stats(row) for row in EquipmentLevel.objects.select_related("equipment__item")
            .filter(equipment__enabled=True, equipment__item__is_deleted=False)}


def definition_for(slot):
    """Adapt legacy content gates to levels without mutating persistent item definitions."""
    definition = ItemGameplayDefinition.objects.select_related("item").filter(item_id=slot.item_id, item__is_deleted=False).first()
    stats = stats_for(slot)
    if stats is None:
        return None
    if definition is None:
        definition = ItemGameplayDefinition(item=EquipmentDefinition.objects.select_related("item").get(item_id=slot.item_id).item)
    definition = copy(definition)
    definition.item = copy(definition.item)
    definition.capability_code = stats["capability"]
    definition.tier = stats["tier"]
    definition.required_skill = stats["required_skill"]
    definition.required_level = stats["required_skill_level"]
    definition.item.max_use_count = stats["max_durability"]
    definition.equipment_stats = stats
    return definition


def weapon_moves(category, *, moves=None):
    return {row.code: dict(name=row.name, multiplier=row.multiplier, minimum_multiplier=row.minimum_multiplier)
            for row in (WeaponMove.objects.filter(category=category) if moves is None else moves) if row.category == category}


def equipment_attributes(stats):
    if not stats:
        return []
    rows = [dict(code="equipment_level", label="装备等级", value=f"Lv.{stats['level']}"),
            dict(code="durability", label="最大耐久", value=str(stats["max_durability"]))]
    if stats["category"] in WEAPONS:
        rows.append(dict(code="base_attack", label="基础攻击", value=f"{stats['attack_min']}～{stats['attack_max']}"))
    category = stats["category"]
    for key, label in {"gathering": [("gathering_time_reduction", "采摘减时"), ("quality_bonus_chance", "精采概率")],
        "farming": [("harvest_bonus_chance", "额外一份收获概率")],
        "cooking": [("quality_bonus_chance", "提升一星概率")],
        "processing": [("processing_time_reduction", "加工减时")]}.get(category, []):
        rows.append(dict(code=key, label=label, value=f"{stats[key]:.0%}"))
    if category == "axe":
        rows.append(dict(code="chop_power", label="每击伐木进度", value=f"×{stats['chop_power']:g}"))
    if category == "cooking":
        rows.append(dict(code="batches", label="单次批数上限", value=f"{stats['cooking_batches']} 批"))
    return rows
