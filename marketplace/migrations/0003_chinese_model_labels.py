from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("marketplace", "0002_alter_marketlisting_overall_score")]
    operations = [
        migrations.AlterModelOptions(name="marketlisting", options={"ordering": ["-created_at"], "verbose_name": "市场挂单", "verbose_name_plural": "市场挂单"}),
        migrations.AlterModelOptions(name="markettransaction", options={"ordering": ["-created_at"], "verbose_name": "市场成交记录", "verbose_name_plural": "市场成交记录"}),
    ]
