from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("account", "0003_alter_gameaccount_options")]
    operations = [
        migrations.AddField(model_name="gameaccount", name="appearance",
                            field=models.JSONField(null=True, blank=True, default=None)),
        migrations.AddField(model_name="gameaccount", name="appearance_revision",
                            field=models.PositiveIntegerField(default=0)),
    ]
