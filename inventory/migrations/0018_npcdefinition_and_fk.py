from django.db import migrations, models
import django.db.models.deletion


def create_npc_definitions_and_link(apps, schema_editor):
    """创建 NpcDefinition 并关联现有 BagTemplate / NpcGlobalInventory。"""
    NpcDefinition = apps.get_model("inventory", "NpcDefinition")
    BagTemplate = apps.get_model("inventory", "BagTemplate")
    NpcGlobalInventory = apps.get_model("inventory", "NpcGlobalInventory")

    # NPC 映射：agent_id -> name（已知 NPC）
    known_npcs = {
        "10012": "jack",
        "10013": "mark",
        "10014": "lucy",
    }

    # 1. 为已知 NPC 创建 NpcDefinition
    for agent_id, name in known_npcs.items():
        npc, _ = NpcDefinition.objects.get_or_create(agent_id=agent_id, defaults={"name": name})
        BagTemplate.objects.filter(character_id=agent_id).update(npc=npc)
        NpcGlobalInventory.objects.filter(character_id=agent_id).update(npc=npc)

    # 2. 为剩余未关联的 BagTemplate（NPC 模板，非 player）创建 NpcDefinition
    for tmpl in BagTemplate.objects.filter(npc__isnull=True).exclude(character_id="player"):
        agent_id = tmpl.character_id
        npc, _ = NpcDefinition.objects.get_or_create(agent_id=agent_id, defaults={"name": agent_id})
        tmpl.npc = npc
        tmpl.save(update_fields=["npc"])

    # 3. 为剩余未关联的 NpcGlobalInventory 创建 NpcDefinition
    for inv in NpcGlobalInventory.objects.filter(npc__isnull=True):
        agent_id = inv.character_id
        npc, _ = NpcDefinition.objects.get_or_create(agent_id=agent_id, defaults={"name": agent_id})
        inv.npc = npc
        inv.save(update_fields=["npc"])


def reverse(apps, schema_editor):
    """回滚：清除 FK 关联。"""
    NpcDefinition = apps.get_model("inventory", "NpcDefinition")
    BagTemplate = apps.get_model("inventory", "BagTemplate")
    NpcGlobalInventory = apps.get_model("inventory", "NpcGlobalInventory")

    BagTemplate.objects.filter(npc__isnull=False).update(npc=None)
    NpcGlobalInventory.objects.filter(npc__isnull=False).update(npc=None)
    NpcDefinition.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0017_envactionreceipt"),
    ]

    operations = [
        # 1. 创建 NpcDefinition 表
        migrations.CreateModel(
            name="NpcDefinition",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("agent_id", models.CharField(db_index=True, help_text="Unity AgentController.agentID，如 10012", max_length=100, unique=True)),
                ("name", models.CharField(help_text="展示名，如 jack", max_length=100)),
            ],
            options={
                "verbose_name": "NPC 定义",
                "verbose_name_plural": "NPC 定义",
            },
        ),
        # 2. BagTemplate 加 npc FK（nullable）
        migrations.AddField(
            model_name="bagtemplate",
            name="npc",
            field=models.ForeignKey(
                blank=True,
                help_text="绑定NPC，玩家模板留空",
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="templates",
                to="inventory.npcdefinition",
            ),
        ),
        # 3. BagTemplate 加 unique_together（npc）
        migrations.AlterUniqueTogether(
            name="bagtemplate",
            unique_together={("npc",)},
        ),
        # 4. NpcGlobalInventory 加 npc 字段（先 nullable，数据迁移后改必填）
        migrations.AddField(
            model_name="npcglobalinventory",
            name="npc",
            field=models.OneToOneField(
                help_text="关联NPC",
                null=True,
                blank=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="global_inventory",
                to="inventory.npcdefinition",
            ),
        ),
        # 5. 数据迁移：创建 NpcDefinition 并关联所有现有记录
        migrations.RunPython(create_npc_definitions_and_link, reverse),
        # 6. NpcGlobalInventory.npc 改为必填（移除 null/blank）
        migrations.AlterField(
            model_name="npcglobalinventory",
            name="npc",
            field=models.OneToOneField(
                help_text="关联NPC",
                on_delete=django.db.models.deletion.CASCADE,
                related_name="global_inventory",
                to="inventory.npcdefinition",
            ),
        ),
        # 7. 移除 NpcGlobalInventory.character_id（已由 npc FK 替代）
        migrations.RemoveField(
            model_name="npcglobalinventory",
            name="character_id",
        ),
    ]
