from django.db import migrations


def install(apps, schema_editor):
    weapons = {
        4110: ("基础长矛", 0, "hunting_spear", 1, 60, 1),
        4111: ("改良长矛", 0, "hunting_spear", 2, 120, 4),
        4112: ("基础弓", 2, "hunting_bow", 1, 60, 1),
        4113: ("改良弓", 2, "hunting_bow", 2, 120, 4),
        4114: ("基础剑", 1, "hunting_sword", 1, 60, 1),
    }
    Item = apps.get_model("items", "Item")
    Definition = apps.get_model("player", "ItemGameplayDefinition")
    alias = schema_editor.connection.alias
    for item_id, (name, weapon, capability, tier, uses, level) in weapons.items():
        item, _ = Item.objects.using(alias).get_or_create(pk=item_id, defaults=dict(item_name=name,
            item_type="CollectTool", max_use_count=uses, collectable=False, stackable=False, item_price=50 if tier == 1 else 150))
        if item.item_name != name or item.item_type != "CollectTool" or item.max_use_count != uses:
            raise ValueError(f"G5 weapon item ID collision: {item_id}")
        definition, _ = Definition.objects.using(alias).get_or_create(item_id=item_id, defaults=dict(
            capability_code=capability, tier=tier, required_skill="hunting", required_level=level,
            effects=[dict(code="weapon", weapon=weapon, damage_multiplier=1.2 if tier == 2 else 1)]))
        if (definition.capability_code != capability or definition.tier != tier
                or definition.required_skill != "hunting" or definition.required_level != level):
            raise ValueError(f"G5 weapon capability collision: {item_id}")
    for item_id, name, price, kind in [(29106, "鹿（待处理猎物）", 50, "Commodity"), (29107, "鹿肉", 12, "肉类")]:
        item, _ = Item.objects.using(alias).get_or_create(pk=item_id, defaults=dict(item_name=name, item_price=price,
            item_type=kind, collectable=False))
        if item.item_name != name or item.item_type != kind:
            raise ValueError(f"G5 deer item ID collision: {item_id}")


class Migration(migrations.Migration):
    dependencies = [("items", "0010_player_cooking_tools"), ("player", "0016_player_hunting_state")]
    operations = [migrations.RunPython(install, migrations.RunPython.noop)]
