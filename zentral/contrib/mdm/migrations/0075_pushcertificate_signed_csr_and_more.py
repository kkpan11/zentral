# Generated by Django 4.2.10 on 2024-04-03 12:38

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mdm', '0074_depenrollment_ios_max_version_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='pushcertificate',
            name='signed_csr',
            field=models.BinaryField(null=True),
        ),
        migrations.AddField(
            model_name='pushcertificate',
            name='signed_csr_updated_at',
            field=models.DateTimeField(null=True),
        ),
        migrations.AlterField(
            model_name='pushcertificate',
            name='certificate',
            field=models.BinaryField(null=True),
        ),
        migrations.AlterField(
            model_name='pushcertificate',
            name='not_after',
            field=models.DateTimeField(null=True),
        ),
        migrations.AlterField(
            model_name='pushcertificate',
            name='not_before',
            field=models.DateTimeField(null=True),
        ),
        migrations.AlterField(
            model_name='pushcertificate',
            name='topic',
            field=models.CharField(max_length=256, null=True, unique=True),
        ),
    ]
