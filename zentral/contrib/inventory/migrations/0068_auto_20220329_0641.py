# Generated by Django 3.2.12 on 2022-03-29 06:41

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0067_auto_20220311_1417'),
    ]

    operations = [
        migrations.AlterField(
            model_name='machinesnapshot',
            name='extra_facts',
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='principalusersource',
            name='properties',
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='puppetnode',
            name='extra_facts',
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='puppettrustedfacts',
            name='extensions',
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='source',
            name='config',
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='teamviewer',
            name='unattended',
            field=models.BooleanField(blank=True, null=True),
        ),
    ]
