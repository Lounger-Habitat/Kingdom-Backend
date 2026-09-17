from django.db import migrations


def configure(apps, schema_editor):
    from village.lucy_configuration import configure_lucy
    configure_lucy(apps)


class Migration(migrations.Migration):
    dependencies = [
        ("village", "0038_lucy_metadata_and_review_actions"),
        ("inventory", "0027_lucy_metadata_and_review_actions"),
        ("recipes", "0005_lucy_meals_reviews"),
    ]
    operations = [migrations.RunPython(configure, migrations.RunPython.noop)]
