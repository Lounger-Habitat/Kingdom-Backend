from django.db import migrations


def install(apps, schema_editor):
    from village.career_catalog import install_catalog
    install_catalog(apps, alias=schema_editor.connection.alias)


class Migration(migrations.Migration):
    dependencies = [("village", "0090_career_development")]
    operations = [migrations.RunPython(install, migrations.RunPython.noop)]
