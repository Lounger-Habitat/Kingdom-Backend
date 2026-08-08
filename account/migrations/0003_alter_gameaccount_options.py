from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("account", "0002_gameaccount_is_ai")]
    operations = [
        migrations.AlterModelOptions(
            name="gameaccount",
            options={"ordering": ["-created_at"], "verbose_name": "游戏账号", "verbose_name_plural": "游戏账号"},
        ),
    ]
