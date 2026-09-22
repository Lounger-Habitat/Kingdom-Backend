import uuid
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("village", "0067_npc_daily_records")]
    operations = [
        migrations.AddField(model_name="villagesimulationconfig", name="action_epoch",
                            field=models.UUIDField(default=uuid.uuid4, editable=False)),
        migrations.AddField(model_name="npccommandstate", name="agent_execution",
                            field=models.JSONField(default=dict, blank=True)),
    ]
