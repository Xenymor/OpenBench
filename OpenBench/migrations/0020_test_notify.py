from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('OpenBench', '0019_workflows'),
    ]

    operations = [
        migrations.AddField(
            model_name='test',
            name='notify',
            field=models.CharField(default='off', max_length=16),
        ),
    ]
