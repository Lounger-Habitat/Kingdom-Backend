"""Mark reviews dishes; public-finance commands and new salary accrual are retired."""
from django.db import migrations


def update_mark_role(apps, schema_editor):
    retired = {"PUBLISH_PRICE_INDEX", "AUDIT_PUBLIC_FUND", "INSPECT_PUBLIC_ECONOMY_STATE", "CLAIM_PUBLIC_SALARY"}
    profiles = apps.get_model("village", "NpcProfile").objects.using(schema_editor.connection.alias)
    for profile in profiles.select_related("npc").all():
        actions = [action for action in profile.capabilities or [] if str(action).strip().upper() not in retired]
        fields = []
        if str(profile.npc.agent_id) == "10013" and not profile.npc.is_test_only:
            profile.occupation = "dish_reviewer"
            profile.personal_goal = "品鉴真实菜品，给出具体、公正的评分和评语"
            fields.extend(["occupation", "personal_goal"])
            for action in ("RATE_DISH", "INSPECT_RATEABLE_DISHES"):
                if action not in {str(value).strip().upper() for value in actions}:
                    actions.append(action)
        if actions != profile.capabilities:
            profile.capabilities = actions
            fields.append("capabilities")
        if fields:
            profile.save(using=schema_editor.connection.alias, update_fields=fields)


class Migration(migrations.Migration):
    dependencies = [("village", "0058_retire_commitment_commands")]
    operations = [migrations.RunPython(update_mark_role, migrations.RunPython.noop)]
