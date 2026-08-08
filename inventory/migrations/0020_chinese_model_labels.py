from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("inventory", "0019_alter_bagtemplate_character_id")]
    operations = [
        migrations.AlterModelOptions(name="bagtemplate", options={"verbose_name": "背包模板", "verbose_name_plural": "背包模板"}),
        migrations.AlterModelOptions(name="inventorybag", options={"verbose_name": "玩家背包", "verbose_name_plural": "玩家背包"}),
        migrations.AlterModelOptions(name="npcglobalinventory", options={"verbose_name": "NPC 全局库存", "verbose_name_plural": "NPC 全局库存"}),
    ]
