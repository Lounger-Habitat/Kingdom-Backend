import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('account', '0004_gameaccount_appearance'),
        ('inventory', '0028_inventoryslotdish_inventoryslotreview_and_more'),
        ('village', '0080_player_processing'),
    ]

    operations = [
        migrations.CreateModel(
            name='VillagePlayerNpcRelationship',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('score', models.PositiveSmallIntegerField(default=0)),
                ('revision', models.PositiveBigIntegerField(default=0)),
                ('last_changed_world_minute', models.BigIntegerField(default=0)),
                ('npc', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='inventory.npcdefinition')),
                ('player', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='account.gameaccount')),
            ],
        ),
        migrations.CreateModel(
            name='VillagePlayerNpcRelationshipChange',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('requested_delta', models.PositiveSmallIntegerField(default=2)),
                ('applied_delta', models.PositiveSmallIntegerField(default=0)),
                ('before_score', models.PositiveSmallIntegerField()),
                ('after_score', models.PositiveSmallIntegerField()),
                ('world_minute', models.BigIntegerField()),
                ('world_day', models.BigIntegerField()),
                ('commission', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, to='village.villagepersonalcommission')),
                ('relationship', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='changes', to='village.villageplayernpcrelationship')),
            ],
        ),
        migrations.CreateModel(
            name='VillageProfessionalContractLink',
            fields=[
                ('commission', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, primary_key=True, related_name='professional_contract', serialize=False, to='village.villagepersonalcommission')),
                ('template_code', models.CharField(max_length=32)),
                ('required_business_level', models.PositiveSmallIntegerField(default=7)),
                ('required_cooperation_score', models.PositiveSmallIntegerField(default=0)),
                ('visit', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='village.villagecaravanvisit')),
            ],
        ),
        migrations.AddConstraint(
            model_name='villageplayernpcrelationship',
            constraint=models.UniqueConstraint(fields=('player', 'npc'), name='player_npc_cooperation_unique'),
        ),
        migrations.AddConstraint(
            model_name='villageplayernpcrelationship',
            constraint=models.CheckConstraint(condition=models.Q(('score__lte', 100)), name='player_npc_cooperation_bounded'),
        ),
        migrations.AddConstraint(
            model_name='villageprofessionalcontractlink',
            constraint=models.UniqueConstraint(fields=('visit', 'template_code'), name='professional_template_once_visit'),
        ),
    ]
