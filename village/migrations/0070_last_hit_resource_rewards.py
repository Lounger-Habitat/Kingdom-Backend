from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("village", "0069_npc_give_items")]

    operations = [
        migrations.RemoveConstraint("villagehuntattempt", "wildlife_one_active_actor"),
        migrations.RemoveConstraint("villagechopattempt", "forestry_one_active_actor"),
    ]
