from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0021_align_npc_shop_visibility_with_templates"),
    ]

    operations = [
        migrations.AddField(
            model_name="npcdefinition",
            name="is_test_only",
            field=models.BooleanField(
                default=False,
                help_text="仅供隔离回归场景使用；正式村庄 NPC 必须为 False",
            ),
        ),
        migrations.AddField(
            model_name="bagtemplate",
            name="seed_only",
            field=models.BooleanField(
                default=False,
                help_text="只在首次建档时复制，禁止作为每日补货目标",
            ),
        ),
        migrations.AddField(
            model_name="bagtemplate",
            name="is_test_only",
            field=models.BooleanField(default=False, help_text="仅供隔离测试 NPC 使用"),
        ),
    ]
