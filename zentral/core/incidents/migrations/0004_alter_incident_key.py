# Generated by Django 3.2.12 on 2022-03-29 06:37

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('incidents', '0003_incident_name'),
    ]

    operations = [
        migrations.AlterField(
            model_name='incident',
            name='key',
            field=models.JSONField(),
        ),
    ]
