from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("village", "0003_seed_npc_runtime_states")]

    operations = [
        migrations.AddField(
            model_name="npccommandstate",
            name="received_at_ms",
            field=models.BigIntegerField(blank=True, null=True, verbose_name="服务端接收时间戳(毫秒)"),
        ),
    ]
