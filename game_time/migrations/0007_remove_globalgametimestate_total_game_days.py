from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("game_time", "0006_initial")]
    operations = [migrations.RemoveField(model_name="globalgametimestate", name="total_game_days")]
