# Generated by Django 3.2.12 on 2022-03-29 06:56

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('filebeat', '0005_auto_20190918_1514'),
    ]

    operations = [
        migrations.AlterField(
            model_name='configuration',
            name='inputs',
            field=models.JSONField(editable=False),
        ),
    ]
