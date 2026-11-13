from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('OpenBench', '0020_test_notify'),
    ]

    operations = [
        migrations.AlterField(
            model_name='logevent',
            name='test_id',
            field=models.IntegerField(db_index=True, default=0),
        ),
    ]
