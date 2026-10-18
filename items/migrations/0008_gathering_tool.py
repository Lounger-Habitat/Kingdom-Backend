"""Frozen G1 item definition only; store stock requires a trusted live action."""
from django.db import migrations


def install_tool(apps, schema_editor):
    alias = schema_editor.connection.alias
    Item = apps.get_model("items", "Item")
    Definition = apps.get_model("player", "ItemGameplayDefinition")
    item, _ = Item.objects.using(alias).get_or_create(item_id=4101, defaults=dict(
        item_name="采集工具包", item_type="CollectTool", item_price=50, max_use_count=20,
        stackable=False, collectable=False,
        item_description="T1采集工具包。采集2级后按食材类别寻找资源，每次成功消耗1耐久；基础采集不需要工具。"))
    if item.item_name != "采集工具包" or item.item_type != "CollectTool":
        raise ValueError("G1 gathering tool item ID collision: 4101")
    Definition.objects.using(alias).get_or_create(item_id=4101, defaults=dict(
        capability_code="targeted_gathering", tier=1, required_skill="gathering", required_level=2,
        effects=[dict(code="category_selection", description="按食材类别采集", uses_per_success=1)]))


class Migration(migrations.Migration):
    dependencies = [("items", "0007_consumable_stacking"), ("player", "0014_player_growth")]
    operations = [migrations.RunPython(install_tool, migrations.RunPython.noop)]
