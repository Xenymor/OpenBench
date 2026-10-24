# Workflows replace the automatic follow-ups of 0018_test_followup. Every Test
# whose follow-up is still pending becomes a Workflow that attaches the Test,
# and continues just as the follow-up would have. Self-contained on purpose,
# so that later changes to OpenBench/workflows/ never alter this migration.

from django.db import migrations, models
import django.db.models.deletion


def number(value):
    return ('%.6f' % (value)).rstrip('0').rstrip('.')

def sprt_settings(followup):
    return {
        'time_control'    : followup['time_control'],
        'test_bounds'     : '[%s, %s]' % (number(followup['elolower']), number(followup['eloupper'])),
        'test_confidence' : '[%s, %s]' % (number(followup['beta']), number(followup['alpha'])),
    }

def defaults_of(test):
    return {
        'dev_engine'   : test.dev_engine,  'dev_repo'  : test.dev_repo,
        'dev_network'  : test.dev_network, 'dev_options' : test.dev_options,
        'base_engine'  : test.base_engine, 'base_repo' : test.base_repo,
        'base_network' : test.base_network, 'base_options' : test.base_options,
        'time_control' : test.dev_time_control,
        'book_name'    : test.book_name,    'upload_pgns' : test.upload_pgns,
        'priority'     : str(test.priority), 'throughput' : str(test.throughput),
        'workload_size': str(test.workload_size if test.test_mode == 'SPRT' else 32),
        'syzygy_wdl'   : test.syzygy_wdl,   'syzygy_adj'  : test.syzygy_adj,
        'win_adj'      : test.win_adj,      'draw_adj'    : test.draw_adj,
        'scale_method' : test.scale_method, 'scale_nps'   : str(test.scale_nps),
    }

def ltc_node(parent, ltc, x):
    return {
        'kind' : 'SPRT', 'label' : 'LTC', 'x' : x, 'y' : 80,
        'config' : {
            'dev'          : '@%s.dev'  % (parent),
            'base'         : '@%s.base' % (parent),
            'dev_options'  : ltc['options'],
            'base_options' : ltc['options'],
            **sprt_settings(ltc),
        },
    }

def sprt_graph(test):

    followup = test.followup
    nodes    = { 'stc' : { 'kind' : 'SPRT', 'label' : 'STC', 'x' : 40, 'y' : 80, 'config' : { 'existing_test' : test.id } } }
    nodes['ltc'] = ltc_node('stc', followup, 340)

    edges = [{ 'from' : 'stc', 'port' : 'passed', 'to' : 'ltc' }]
    return { 'defaults' : defaults_of(test), 'nodes' : nodes, 'edges' : edges }

def spsa_graph(test):

    followup = test.followup
    nodes    = {
        'tune' : {
            'kind' : 'SPSA', 'label' : 'SPSA', 'x' : 40, 'y' : 80,
            'config' : { 'existing_test' : test.id, 'at_percent' : number(followup['at_percent']) },
        },
        'sprt' : {
            'kind' : 'SPRT', 'label' : 'Tuned', 'x' : 340, 'y' : 80,
            'config' : {
                'dev'        : '@tune.tuned',
                'base'       : followup['base_sha'],   # Pinned when the tune was created
                'base_bench' : str(followup['base_bench']),
                'base_name'  : followup['base_name'],
                **sprt_settings(followup),
            },
        },
    }

    edges = [{ 'from' : 'tune', 'port' : 'done', 'to' : 'sprt' }]

    if followup.get('ltc'):
        nodes['ltc'] = ltc_node('sprt', followup['ltc'], 640)
        edges.append({ 'from' : 'sprt', 'port' : 'passed', 'to' : 'ltc' })

    return { 'defaults' : defaults_of(test), 'nodes' : nodes, 'edges' : edges }

def convert_followups(apps, schema_editor):

    Test         = apps.get_model('OpenBench', 'Test')
    Workflow     = apps.get_model('OpenBench', 'Workflow')
    WorkflowNode = apps.get_model('OpenBench', 'WorkflowNode')

    pending = Test.objects.filter(followup__isnull=False, followup_test=0, deleted=False)

    for test in pending:

        # Neither a failed or stopped SPRT, nor a finished or stopped tune, could still start a follow-up
        if test.test_mode == 'SPRT' and test.finished and not test.passed:
            continue

        if test.test_mode == 'SPSA' and test.finished:
            continue

        if test.test_mode not in ['SPRT', 'SPSA'] or not test.followup:
            continue

        graph    = sprt_graph(test) if test.test_mode == 'SPRT' else spsa_graph(test)
        workflow = Workflow.objects.create(author=test.author, name='Follow-ups of #%d' % (test.id), graph=graph)

        for key, node in graph['nodes'].items():
            attached = node['config'].get('existing_test') == test.id
            WorkflowNode.objects.create(
                workflow = workflow,
                key      = key,
                kind     = node['kind'],
                state    = 'RUNNING' if attached else 'WAITING',
                test     = test if attached else None,
                started  = test.creation if attached else None,
            )


class Migration(migrations.Migration):

    dependencies = [
        ('OpenBench', '0018_test_followup'),
    ]

    operations = [
        migrations.CreateModel(
            name='Workflow',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('author', models.CharField(max_length=64)),
                ('name', models.CharField(max_length=128)),
                ('status', models.CharField(choices=[('RUNNING', 'RUNNING'), ('ATTENTION', 'ATTENTION'), ('PAUSED', 'PAUSED'), ('FINISHED', 'FINISHED'), ('ABORTED', 'ABORTED')], db_index=True, default='RUNNING', max_length=16)),
                ('paused', models.BooleanField(default=False)),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('updated', models.DateTimeField(auto_now=True)),
                ('graph', models.JSONField(default=dict)),
            ],
        ),
        migrations.CreateModel(
            name='WorkflowNode',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key', models.CharField(max_length=32)),
                ('kind', models.CharField(max_length=16)),
                ('state', models.CharField(choices=[('WAITING', 'WAITING'), ('RUNNING', 'RUNNING'), ('FINISHED', 'FINISHED'), ('SKIPPED', 'SKIPPED'), ('STOPPED', 'STOPPED'), ('ERROR', 'ERROR')], default='WAITING', max_length=16)),
                ('outcome', models.CharField(blank=True, max_length=16)),
                ('trigger', models.CharField(blank=True, max_length=32)),
                ('tuned', models.CharField(blank=True, max_length=2048)),
                ('detail', models.CharField(blank=True, max_length=256)),
                ('error', models.CharField(blank=True, max_length=1024)),
                ('started', models.DateTimeField(blank=True, null=True)),
                ('finished', models.DateTimeField(blank=True, null=True)),
                ('test', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='workflow_node', to='OpenBench.test')),
                ('workflow', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='nodes', to='OpenBench.workflow')),
            ],
            options={
                'unique_together': {('workflow', 'key')},
            },
        ),
        migrations.RunPython(convert_followups, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='test',
            name='followup',
        ),
        migrations.RemoveField(
            model_name='test',
            name='followup_test',
        ),
    ]
