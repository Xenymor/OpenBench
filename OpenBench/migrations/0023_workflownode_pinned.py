from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('OpenBench', '0022_engine_sha_index'),
    ]

    operations = [
        migrations.AddField(
            model_name='workflownode',
            name='pinned',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
