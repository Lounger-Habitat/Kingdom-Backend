from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("village", "0026_npcruntimestate_world_position")]

    operations = [
        migrations.AddField(
            model_name="villagestoreinventoryslot",
            name="remaining_uses",
            field=models.IntegerField(blank=True, null=True, verbose_name="剩余使用次数"),
        ),
    ]
