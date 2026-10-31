from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("village", "0091_career_content")]

    operations = [
        migrations.RemoveConstraint("villageactionreceipt", "receipt_system_kind_wm_unique"),
        migrations.AddConstraint(
            "villageactionreceipt",
            models.UniqueConstraint(
                fields=["action_kind", "world_minute"], name="receipt_system_kind_wm_unique",
                condition=models.Q(action_kind="TIME_ADVANCE") | models.Q(action_kind="VILLAGE_TICK"),
            ),
        ),
    ]
