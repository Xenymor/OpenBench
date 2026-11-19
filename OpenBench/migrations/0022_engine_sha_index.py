from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('OpenBench', '0021_logevent_test_id_index'),
    ]

    operations = [
        migrations.AlterField(
            model_name='engine',
            name='sha',
            field=models.CharField(db_index=True, max_length=64),
        ),
    ]
