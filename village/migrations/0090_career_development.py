import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("village", "0089_farm_expansion_and_caravan_orders")]

    operations = [
        migrations.AddField(model_name="villagesimulationconfig", name="career_development_enabled",
            field=models.BooleanField(default=False, verbose_name="开放职业发展与新品试销")),
        migrations.AddField(model_name="villagecaravanorder", name="procurement_snapshot",
            field=models.JSONField(default=dict)),
        migrations.CreateModel(name="VillageCareerDevelopment", fields=[
            ("code", models.CharField(max_length=48, primary_key=True, serialize=False)),
            ("owner_npc_id", models.CharField(max_length=32)),
            ("owner_name", models.CharField(max_length=40)),
            ("title", models.CharField(max_length=120)),
            ("phase", models.PositiveIntegerField(default=1)),
            ("location_code", models.CharField(max_length=64)),
            ("status", models.CharField(default="LOCKED", max_length=16)),
            ("progress", models.JSONField(default=dict)),
            ("version", models.PositiveIntegerField(default=1)),
            ("opened_world_minute", models.BigIntegerField(blank=True, null=True)),
            ("trial_world_minute", models.BigIntegerField(blank=True, null=True)),
            ("completed_world_minute", models.BigIntegerField(blank=True, null=True)),
            ("preparation_affair", models.OneToOneField(blank=True, null=True,
                on_delete=django.db.models.deletion.PROTECT, related_name="career_development", to="village.villageaffair")),
        ], options={"ordering": ["owner_npc_id", "phase", "code"]}),
        migrations.CreateModel(name="VillageCareerEvidence", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("kind", models.CharField(max_length=16)),
            ("request_id", models.CharField(max_length=128)),
            ("actor_type", models.CharField(max_length=8)),
            ("actor_id", models.CharField(max_length=100)),
            ("items", models.JSONField(default=dict)),
            ("metadata", models.JSONField(default=dict)),
            ("payload_hash", models.CharField(max_length=64)),
            ("world_minute", models.BigIntegerField()),
        ], options={"constraints": [models.UniqueConstraint(fields=("kind", "request_id", "actor_type", "actor_id"), name="career_evidence_once")]}),
        migrations.CreateModel(name="VillageCareerProductionReservation", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("request_id", models.CharField(max_length=128)),
            ("item_id", models.PositiveIntegerField()),
            ("quantity", models.PositiveIntegerField()),
            ("status", models.CharField(default="RESERVED", max_length=16)),
            ("world_minute", models.BigIntegerField()),
            ("settled_world_minute", models.BigIntegerField(blank=True, null=True)),
        ], options={"constraints": [models.UniqueConstraint(fields=("request_id", "item_id"), name="career_trial_reservation_once")]}),
    ]
