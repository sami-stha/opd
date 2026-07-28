from django.db import migrations, models


def set_followup_exempt_days(apps, schema_editor):
    FollowupRule = apps.get_model('core', 'FollowupRule')
    FollowupRule.objects.filter(exempt_within_days=3).update(exempt_within_days=7)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0015_slottypeconfig_assigned_doctor'),
    ]

    operations = [
        migrations.AlterField(
            model_name='followuprule',
            name='exempt_within_days',
            field=models.IntegerField(default=7),
        ),
        migrations.RunPython(set_followup_exempt_days, migrations.RunPython.noop),
    ]
