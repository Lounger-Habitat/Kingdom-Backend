from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("inventory", "0022_phase0_village_inventory_guards"),
    ]

    operations = [
        migrations.CreateModel(
            name="VillageLocationDefinition",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("location_code", models.CharField(db_index=True, max_length=64, unique=True)),
                ("display_name", models.CharField(max_length=100)),
                ("scene_name", models.CharField(default="WuxiVillage", max_length=100)),
                ("center_anchor_code", models.CharField(max_length=100, unique=True)),
                ("location_type", models.CharField(max_length=50)),
                ("bounds", models.JSONField(blank=True, default=dict)),
                ("location_tags", models.JSONField(blank=True, default=list)),
                ("opening_hours", models.JSONField(blank=True, default=dict)),
                ("capacity", models.PositiveIntegerField(default=1)),
                ("supported_action_types", models.JSONField(blank=True, default=list)),
                ("allowed_occupations", models.JSONField(blank=True, default=list)),
                ("required_story_states", models.JSONField(blank=True, default=list)),
                ("is_public", models.BooleanField(default=True)),
                ("is_active", models.BooleanField(default=True)),
            ],
            options={
                "verbose_name": "村庄地点定义",
                "verbose_name_plural": "村庄地点定义",
                "ordering": ["location_code"],
            },
        ),
        migrations.CreateModel(
            name="VillageSimulationConfig",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("config_key", models.CharField(default="default", max_length=32, unique=True)),
                ("tick_interval_seconds", models.PositiveIntegerField(default=5)),
                ("hub_command_timeout_seconds", models.PositiveIntegerField(default=30)),
                ("command_queue_limit", models.PositiveIntegerField(default=8)),
                ("day_start_minute", models.PositiveIntegerField(default=360)),
                ("day_close_minute", models.PositiveIntegerField(default=1320)),
                ("max_catchup_days", models.PositiveIntegerField(default=3)),
                ("village_simulation_enabled", models.BooleanField(default=False)),
                ("village_needs_enabled", models.BooleanField(default=False)),
                ("village_economy_enabled", models.BooleanField(default=False)),
                ("village_social_enabled", models.BooleanField(default=False)),
                ("village_story_enabled", models.BooleanField(default=False)),
                ("village_hub_command_adapter_enabled", models.BooleanField(default=False)),
                ("village_offline_catchup_enabled", models.BooleanField(default=False)),
                ("village_debug_overlay_enabled", models.BooleanField(default=False)),
                ("debug_logs_enabled", models.BooleanField(default=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"verbose_name": "村庄模拟配置", "verbose_name_plural": "村庄模拟配置"},
        ),
        migrations.CreateModel(
            name="NpcProfile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("display_name", models.CharField(max_length=100)),
                ("occupation", models.CharField(max_length=100)),
                ("home_location_code", models.CharField(max_length=64)),
                ("work_location_code", models.CharField(max_length=64)),
                ("social_location_code", models.CharField(max_length=64)),
                ("personality_traits", models.JSONField(blank=True, default=list)),
                ("personal_goal", models.TextField(blank=True, default="")),
                ("capabilities", models.JSONField(blank=True, default=list)),
                ("need_rates", models.JSONField(blank=True, default=dict)),
                ("is_active", models.BooleanField(default=True)),
                ("npc", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="village_profile", to="inventory.npcdefinition")),
            ],
            options={
                "verbose_name": "NPC 村庄档案",
                "verbose_name_plural": "NPC 村庄档案",
                "ordering": ["npc__agent_id"],
            },
        ),
    ]
