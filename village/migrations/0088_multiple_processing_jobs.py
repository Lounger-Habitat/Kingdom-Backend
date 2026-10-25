from django.db import migrations, models
from django.utils import timezone


class Migration(migrations.Migration):
    dependencies = [("village", "0087_legacy_ingredient_quality")]
    operations = [
        migrations.RemoveConstraint(
            model_name="villageplayerprocessingjob", name="processing_one_unfinished_player"),
        migrations.AddField(
            model_name="villageplayerprocessingjob", name="submitted_at",
            field=models.DateTimeField(auto_now_add=True, default=timezone.now), preserve_default=False),
    ]
