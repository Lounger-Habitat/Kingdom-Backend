from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("player", "0012_global_time_and_action_point_regeneration")]
    operations = [
        migrations.AlterModelOptions(name="defaultrecipe", options={"ordering": ["recipe_name"], "verbose_name": "新玩家默认菜谱", "verbose_name_plural": "新玩家默认菜谱"}),
        migrations.AlterModelOptions(name="playerrecipe", options={"verbose_name": "玩家菜谱", "verbose_name_plural": "玩家菜谱"}),
        migrations.AlterModelOptions(name="playerstate", options={"ordering": ["player_id"], "verbose_name": "玩家状态", "verbose_name_plural": "玩家状态"}),
        migrations.AlterModelOptions(name="playertitle", options={"verbose_name": "玩家称号", "verbose_name_plural": "玩家称号"}),
    ]
