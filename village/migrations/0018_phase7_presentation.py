from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("village", "0017_alter_npccommandstate_status")]

    operations = [
        migrations.AddField(
            model_name="villagesimulationconfig",
            name="village_presentation_enabled",
            field=models.BooleanField(default=False),
        ),
    ]
