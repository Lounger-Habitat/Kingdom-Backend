from django.db import migrations, models
import django.db.models.deletion


def exclude_existing_accounts(apps, schema_editor):
    Account = apps.get_model("account", "GameAccount")
    Arrival = apps.get_model("player", "PlayerArrivalState")
    alias = schema_editor.connection.alias
    # Historical model instances do not send the runtime account post_save hook.
    # Every existing account is explicitly excluded, including untouched tutorials.
    ids = Account.objects.using(alias).values_list("pk", flat=True).iterator(chunk_size=500)
    batch = []
    for player_id in ids:
        batch.append(Arrival(player_id=player_id, eligible=False))
        if len(batch) >= 500:
            Arrival.objects.using(alias).bulk_create(batch, ignore_conflicts=True)
            batch = []
    if batch:
        Arrival.objects.using(alias).bulk_create(batch, ignore_conflicts=True)


class Migration(migrations.Migration):
    dependencies = [("player", "0020_playertutorialstate"), ("account", "0004_gameaccount_appearance")]
    operations = [
        migrations.CreateModel(name="PlayerArrivalState", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("eligible", models.BooleanField(default=False)),
            ("revision", models.PositiveBigIntegerField(default=0)),
            ("started_at", models.DateTimeField(blank=True, null=True)),
            ("finished_at", models.DateTimeField(blank=True, null=True)),
            ("finish_reason", models.CharField(blank=True, default="", max_length=20)),
            ("player", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE,
                related_name="arrival_state", to="account.gameaccount")),
        ], options={"verbose_name": "玩家入村开场", "verbose_name_plural": "玩家入村开场"}),
        migrations.RunPython(exclude_existing_accounts, migrations.RunPython.noop),
    ]
