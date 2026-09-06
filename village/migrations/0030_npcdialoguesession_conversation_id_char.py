# Generated manually: conversation_id becomes caller-supplied (Unity/agent runtime),
# no longer a server-generated UUID. Kept as opaque string PK (max 128).
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("village", "0029_needs_remainder_precision"),
    ]

    operations = [
        migrations.AlterField(
            model_name="npcdialoguesession",
            name="conversation_id",
            field=models.CharField(
                editable=False, max_length=128, primary_key=True, serialize=False, verbose_name="会话ID"
            ),
        ),
    ]
