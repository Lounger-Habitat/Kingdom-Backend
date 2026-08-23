from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("village", "0016_phase6_delivery_cursor")]

    operations = [
        migrations.AlterField(
            model_name="npccommandstate",
            name="status",
            field=models.CharField(
                choices=[
                    ("RECEIVED", "已接收"),
                    ("QUEUED", "已排队"),
                    ("RUNNING", "执行中"),
                    ("SETTLING", "结算中"),
                    ("SUCCEEDED", "成功"),
                    ("FAILED", "失败"),
                    ("CANCELLED", "已取消"),
                    ("EXPIRED", "已过期"),
                ],
                default="RECEIVED",
                max_length=16,
                verbose_name="状态",
            ),
        ),
    ]
