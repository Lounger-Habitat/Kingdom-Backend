from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("village", "0004_npccommandstate_received_at_ms")]

    operations = [
        migrations.AddField(
            model_name="npccommandstate",
            name="recipient_id",
            field=models.CharField(default="", max_length=128, verbose_name="接收者ID"),
            preserve_default=False,
        ),
    ]
