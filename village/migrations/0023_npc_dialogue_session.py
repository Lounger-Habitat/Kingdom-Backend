from django.db import migrations, models
import django.db.models.deletion
import uuid


def replace_socialize_capability(apps, schema_editor):
    NpcProfile = apps.get_model("village", "NpcProfile")
    for profile in NpcProfile.objects.filter(npc__is_test_only=False, is_active=True):
        capabilities = [
            action for action in (profile.capabilities or []) if action != "SOCIALIZE"
        ]
        for action in ("START_DIALOGUE", "SPEAK"):
            if action not in capabilities:
                capabilities.append(action)
        profile.capabilities = capabilities
        profile.save(update_fields=["capabilities"])


class Migration(migrations.Migration):
    dependencies = [("village", "0022_add_story_query_capabilities")]
    operations = [
        migrations.CreateModel(
            name="NpcDialogueSession",
            fields=[
                ("conversation_id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("status", models.CharField(choices=[("ACTIVE", "进行中"), ("COMPLETED", "已完成"), ("EXPIRED", "已过期")], default="ACTIVE", max_length=16)),
                ("topic", models.CharField(blank=True, default="", max_length=120)),
                ("messages", models.JSONField(blank=True, default=list)),
                ("end_requests", models.JSONField(blank=True, default=list)),
                ("started_world_minute", models.BigIntegerField()),
                ("expires_world_minute", models.BigIntegerField()),
                ("ended_world_minute", models.BigIntegerField(blank=True, null=True)),
                ("end_reason", models.CharField(blank=True, default="", max_length=64)),
                ("valid_for_social_needs", models.BooleanField(blank=True, null=True)),
                ("social_multiplier_percent", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("initiator", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="initiated_dialogues", to="inventory.npcdefinition")),
                ("reservation", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="dialogue_session", to="village.npcactionreservation")),
                ("target", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="received_dialogues", to="inventory.npcdefinition")),
            ],
            options={"verbose_name": "NPC 对话会话", "verbose_name_plural": "NPC 对话会话"},
        ),
        migrations.RunPython(replace_socialize_capability, migrations.RunPython.noop),
    ]
