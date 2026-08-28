import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("village", "0018_phase7_presentation")]

    operations = [
        migrations.CreateModel(
            name="NpcSubjectiveGoalState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("goal_id", models.UUIDField(blank=True, null=True, verbose_name="目标ID")),
                ("summary", models.CharField(blank=True, default="", max_length=120, verbose_name="目标摘要")),
                ("status", models.CharField(choices=[("ACTIVE", "进行中"), ("COMPLETED", "已完成"), ("ABANDONED", "已放弃")], default="ABANDONED", max_length=16, verbose_name="状态")),
                ("visibility", models.CharField(default="PUBLIC", max_length=16, verbose_name="可见性")),
                ("goal_version", models.BigIntegerField(default=0, verbose_name="目标版本")),
                ("updated_world_minute", models.BigIntegerField(default=0, verbose_name="更新世界分钟")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("npc", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="subjective_goal_state", to="inventory.npcdefinition")),
            ],
            options={"verbose_name": "NPC 主观目标", "verbose_name_plural": "NPC 主观目标"},
        ),
        migrations.AddConstraint(
            model_name="npcsubjectivegoalstate",
            constraint=models.CheckConstraint(
                condition=models.Q(("goal_version__gte", 0), ("updated_world_minute__gte", 0)),
                name="npc_subjective_goal_versions_non_negative",
            ),
        ),
    ]
