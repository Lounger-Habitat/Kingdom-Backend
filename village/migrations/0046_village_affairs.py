from django.db import migrations, models
import django.db.models.deletion


def migrate_affairs(apps, schema_editor):
    Affair = apps.get_model("village", "VillageAffair")
    Fact = apps.get_model("village", "VillageStoryFact")
    Config = apps.get_model("village", "VillageSimulationConfig")
    alias = schema_editor.connection.alias
    Config.objects.using(alias).all().update(story_repair_wood_target=100, story_repair_labor_target=20,
                                            story_repair_meal_target=20, story_repair_fund_target=500)
    for fact in Fact.objects.using(alias).filter(arc_id="BROKEN_TRADE_ROUTE_V1").select_related("arc"):
        repaired = fact.bridge_condition == "REPAIRED"
        # Old repair_fund was the entire PUBLIC_FUND balance. Old donations were
        # also spendable public money, not reserved bridge funds. Preserve that
        # account and its ledger; do not invent a second allocation of its cash.
        Affair.objects.using(alias).get_or_create(code="BRIDGE_REPAIR", defaults=dict(
            kind="RESOURCE_PROJECT", title="修复旧桥", status="COMPLETED" if repaired else "PREPARING",
            requirements={"materials": {"1002": 100}, "meals": 20, "gold": 500, "labor": 20},
            progress={"materials": {"1002": fact.repair_wood}, "meals": fact.community_meals,
                      "gold": 0, "labor": fact.repair_labor_hours},
            locations=["REPAIR_YARD", "OLD_BRIDGE"], opened_world_minute=fact.arc.start_day * 1440,
            completed_world_minute=fact.arc.phase_entered_day * 1440 if repaired else None,
        ))
        fact.repair_fund = 0
        fact.save(using=alias, update_fields=["repair_fund"])


class Migration(migrations.Migration):
    dependencies = [("village", "0045_villageplayercontract_villagecontractclaim_and_more")]
    operations = [
        migrations.AlterField(model_name="villagesimulationconfig", name="story_repair_wood_target", field=models.PositiveIntegerField(default=100)),
        migrations.AlterField(model_name="villagesimulationconfig", name="story_repair_labor_target", field=models.PositiveIntegerField(default=20)),
        migrations.AddField(model_name="villagesimulationconfig", name="story_repair_meal_target", field=models.PositiveIntegerField(default=20)),
        migrations.CreateModel(name="VillageAffair", fields=[
            ("code", models.CharField(max_length=64, primary_key=True, serialize=False)),
            ("kind", models.CharField(default="RESOURCE_PROJECT", max_length=32)),
            ("title", models.CharField(max_length=100)),
            ("status", models.CharField(default="PREPARING", max_length=16)),
            ("requirements", models.JSONField(default=dict)),
            ("progress", models.JSONField(default=dict)),
            ("locations", models.JSONField(default=list)),
            ("opened_world_minute", models.BigIntegerField()),
            ("completed_world_minute", models.BigIntegerField(blank=True, null=True)),
            ("version", models.PositiveBigIntegerField(default=0)),
        ]),
        migrations.CreateModel(name="VillageAffairContribution", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("request_id", models.UUIDField()), ("actor_type", models.CharField(max_length=16)),
            ("actor_id", models.CharField(max_length=100)), ("kind", models.CharField(max_length=16)),
            ("item_id", models.PositiveIntegerField(default=0)), ("quantity", models.PositiveIntegerField()),
            ("world_minute", models.BigIntegerField()),
            ("affair", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="contributions", to="village.villageaffair")),
        ], options={"constraints": [models.UniqueConstraint(fields=("affair", "request_id", "kind", "item_id"), name="village_affair_contribution_once")]}),
        migrations.RunPython(migrate_affairs, migrations.RunPython.noop),
    ]
