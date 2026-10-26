# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                                                                             #
#   OpenBench is a chess engine testing framework authored by Andrew Grant.   #
#   <https://github.com/AndyGrant/OpenBench>           <andrew@grantnet.us>   #
#                                                                             #
#   OpenBench is free software: you can redistribute it and/or modify         #
#   it under the terms of the GNU General Public License as published by      #
#   the Free Software Foundation, either version 3 of the License, or         #
#   (at your option) any later version.                                       #
#                                                                             #
#   OpenBench is distributed in the hope that it will be useful,              #
#   but WITHOUT ANY WARRANTY; without even the implied warranty of            #
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the             #
#   GNU General Public License for more details.                              #
#                                                                             #
#   You should have received a copy of the GNU General Public License         #
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.     #
#                                                                             #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

# python manage.py test OpenBench

import copy
import hashlib
import json
import os
import re
import tempfile

from unittest import mock

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

from OpenBench.models import Book, EngineConfig, LogEvent, Network, Profile, Test, Workflow, WorkflowNode
from OpenBench.workflows import conditions
from OpenBench.workflows import notifications
from OpenBench.workflows import graph as Graph
from OpenBench.workflows import runner
from OpenBench.workflows.options import merge_options

def node(kind, **config):
    return { 'kind' : kind, 'label' : '', 'config' : config, 'x' : 0, 'y' : 0 }

def edge(source, port, target):
    return { 'from' : source, 'port' : port, 'to' : target }

def example_graph():

    # SPRT x vs y. If x wins, x vs main, otherwise y vs main. Whichever fails is tuned.
    return {
        'defaults' : {},
        'nodes' : {
            'ab'     : node('SPRT', dev='x', base='y'),
            'x_main' : node('SPRT', dev='@ab.dev', base='main'),
            'y_main' : node('SPRT', dev='@ab.base', base='main'),
            'tune'   : node('SPSA', dev='@trigger.dev', spsa_inputs='A, int, 10, 0, 20, 2, 0.002'),
        },
        'edges' : [
            edge('ab', 'passed', 'x_main'), edge('ab', 'failed', 'y_main'),
            edge('x_main', 'failed', 'tune'), edge('y_main', 'failed', 'tune'),
        ],
    }

def condition_graph(condition='failed && elo > 3', source=''):

    # An SPRT whose whole result goes into an IF, which picks the next step
    return {
        'defaults' : {},
        'nodes' : {
            'ab'   : node('SPRT', dev='x', base='main'),
            'if'   : node('IF', condition=condition, source=source),
            'yes'  : node('SPRT', dev='@trigger.dev', base='main', time_control='60+0.6'),
            'no'   : node('SPRT', dev='other', base='main'),
        },
        'edges' : [ edge('ab', 'done', 'if'), edge('if', 'true', 'yes'), edge('if', 'false', 'no') ],
    }

class ConditionTests(SimpleTestCase):

    RESULT = { 'passed' : False, 'failed' : True, 'elo' : 3.4, 'elo_lower' : -1.0, 'elo_upper' : 7.8, 'llr' : -2.95, 'games' : 9000.0 }

    def test_evaluation(self):
        cases = {
            'failed && elo > 3'                          : True,
            'failed and elo > 3.5'                       : False,
            'passed || elo_lower > -2'                   : True,
            'not passed and (elo >= 3 or games > 20000)' : True,
            '!(failed) || llr < -2.9'                    : True,
            'elo > -1 && elo_upper <= 7.8'               : True,
            'games == 9000 && elo != 3.4'                : False,
        }
        for text, expected in cases.items():
            self.assertEqual(conditions.evaluate(text, { None : self.RESULT }), expected, text)

    def test_qualified_variables(self):

        results = { None : self.RESULT, 'x' : dict(self.RESULT, passed=True, failed=False, elo=1.5),
                    'y' : self.RESULT, 'my-node' : self.RESULT, '1' : self.RESULT }

        cases = {
            'x.passed && y.failed'                : True,
            'x.passed && y.elo > 2'               : True,
            'x.elo > 2 || my-node.games >= 9000'  : True,
            'failed && x.failed'                  : False,
            '1.failed && not x.failed'            : True,
        }
        for text, expected in cases.items():
            self.assertEqual(conditions.evaluate(text, results), expected, text)

        self.assertEqual(conditions.references('x.passed && y.elo > 2 || failed'), { None, 'x', 'y' })
        self.assertEqual(conditions.references('x.passed'), { 'x' })

        with self.assertRaises(conditions.ConditionError):
            conditions.parse('x.bogus')

    def test_rejected_conditions(self):
        for text in ['', 'elo', 'failed > 3', 'x > 1', 'elo >', '((((elo > 1', 'failed && && elo',
                     '__import__("os")', 'elo > 1; passed', '(' * 40 + 'passed' + ')' * 40, 'a' * 300]:
            with self.assertRaises(conditions.ConditionError, msg=text):
                conditions.parse(text)

class NotificationTests(SimpleTestCase):

    WEBHOOK = 'https://discord.com/api/webhooks/123456/abc_DEF-ghi'

    def config_from(self, text):
        with tempfile.NamedTemporaryFile('w', suffix='.discord', delete=False) as fout:
            fout.write(text)
        try: return notifications.read_config(fout.name)
        finally: os.unlink(fout.name)

    def test_reading_the_credentials(self):

        config = self.config_from('webhook=%s\nsite=https://bench.example.com/\n' % (self.WEBHOOK))
        self.assertEqual(config, { 'webhook' : self.WEBHOOK, 'site' : 'https://bench.example.com' })

        # Only Discord's webhooks, and a site that is a web address
        self.assertIsNone(self.config_from('webhook=https://evil.example.com/api/webhooks/1/x'))
        self.assertIsNone(self.config_from('webhook=http://discord.com/api/webhooks/1/x'))
        self.assertEqual(self.config_from('webhook=%s\nsite=javascript:alert(1)' % (self.WEBHOOK))['site'], '')
        self.assertIsNone(notifications.read_config(os.path.join(tempfile.gettempdir(), 'does-not-exist.discord')))

    def test_messages_never_ping_anyone(self):

        workflow = Workflow(id=12, name='@everyone LMR')
        message  = notifications.build_message(workflow, [('attention', 'ab: Stopped'), ('finished', 'ab: passed')], 'https://bench.example.com')

        self.assertEqual(message['allowed_mentions'], { 'parse' : [] })
        self.assertEqual([embed['title'] for embed in message['embeds']], ['#12 @everyone LMR needs attention', '#12 @everyone LMR finished'])
        self.assertEqual(message['embeds'][0]['url'], 'https://bench.example.com/workflow/12/')

    def test_delivery(self):

        sent = []
        with mock.patch.object(notifications, 'read_config', lambda: { 'webhook' : self.WEBHOOK, 'site' : '' }), \
             mock.patch.object(notifications.requests, 'post', lambda url, json, timeout: sent.append((url, json)) or mock.Mock(status_code=204)), \
             mock.patch.object(notifications.threading, 'Thread', lambda target, args, daemon: mock.Mock(start=lambda: target(*args))):
            notifications.notify(Workflow(id=1, name='A'), [('attention', 'x: broken')])
            notifications.notify(Workflow(id=1, name='A'), [])

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], self.WEBHOOK)
        self.assertEqual(sent[0][1]['embeds'][0]['description'], 'x: broken')

        # Not set up: nothing is sent at all
        with mock.patch.object(notifications, 'read_config', lambda: None), \
             mock.patch.object(notifications.requests, 'post', lambda *args, **kwargs: self.fail('Must not post')):
            notifications.notify(Workflow(id=1, name='A'), [('attention', 'x: broken')])

class GraphTests(SimpleTestCase):

    def propagate(self, graph, **states):
        full = { key : ('WAITING', '') for key in graph['nodes'] }
        full.update(states)
        return Graph.propagate(graph, full)

    def test_example_is_valid(self):
        self.assertEqual(Graph.validate(example_graph()), [])

    def test_rejects_cycles(self):
        graph = example_graph()
        graph['edges'].append(edge('tune', 'done', 'ab'))
        self.assertEqual(Graph.validate(graph), ['Workflows may not contain cycles'])

    def test_rejects_unknown_ports(self):
        graph = example_graph()
        graph['edges'].append(edge('tune', 'passed', 'ab'))
        self.assertIn('has no output', Graph.validate(graph)[0])

    def test_rejects_trigger_after_join(self):
        graph = example_graph()
        graph['nodes']['join'] = node('JOIN')
        graph['nodes']['after'] = node('SPRT', dev='@trigger.dev', base='main')
        graph['edges'] += [edge('x_main', 'passed', 'join'), edge('y_main', 'passed', 'join'), edge('join', 'all', 'after')]
        self.assertTrue(any('ambiguous after a JOIN' in error for error in Graph.validate(graph)))

    def test_rejects_references_off_the_path(self):

        # x_main is skipped whenever ab fails, so the tune cannot rely on it
        graph = example_graph()
        graph['nodes']['tune']['config']['dev'] = '@x_main.dev'
        self.assertTrue(any('every path' in error for error in Graph.validate(graph)))

    def test_rejects_wrong_reference_fields(self):
        graph = example_graph()
        graph['nodes']['x_main']['config']['dev'] = '@ab.tuned'
        self.assertTrue(any('provides no "tuned"' in error for error in Graph.validate(graph)))

    def test_join_needs_two_inputs(self):
        graph = example_graph()
        graph['nodes']['join'] = node('JOIN')
        graph['edges'].append(edge('ab', 'passed', 'join'))
        self.assertTrue(any('at least two incoming' in error for error in Graph.validate(graph)))

    def test_roots_start_first(self):
        self.assertEqual(self.propagate(example_graph()), { 'ab' : ('READY', '', '') })

    def test_passed_branch(self):
        changes = self.propagate(example_graph(), ab=('FINISHED', 'passed'))
        self.assertEqual(changes, { 'x_main' : ('READY', '', 'ab'), 'y_main' : ('SKIPPED', '', '') })

    def test_failed_branch_merges_into_tune(self):
        changes = self.propagate(example_graph(),
            ab=('FINISHED', 'failed'), x_main=('SKIPPED', ''), y_main=('FINISHED', 'failed'))
        self.assertEqual(changes, { 'tune' : ('READY', '', 'y_main') })

    def test_skips_cascade(self):
        changes = self.propagate(example_graph(),
            ab=('FINISHED', 'passed'), x_main=('FINISHED', 'passed'), y_main=('SKIPPED', ''))
        self.assertEqual(changes, { 'tune' : ('SKIPPED', '', '') })

    def test_parallel_and_joins(self):

        graph = {
            'defaults' : {},
            'nodes' : {
                'a' : node('SPRT', dev='a', base='main'), 'b' : node('SPRT', dev='b', base='main'),
                'join' : node('JOIN'), 'all' : node('SPRT', dev='c', base='main'),
                'some' : node('SPRT', dev='d', base='main'), 'none' : node('SPRT', dev='e', base='main'),
            },
            'edges' : [
                edge('a', 'passed', 'join'), edge('b', 'passed', 'join'),
                edge('join', 'all', 'all'), edge('join', 'some', 'some'), edge('join', 'none', 'none'),
            ],
        }

        self.assertEqual(Graph.validate(graph), [])
        self.assertEqual(set(self.propagate(graph)), { 'a', 'b' }) # Both roots start in parallel

        # The JOIN waits for b, then fires 'some', and nothing else runs
        self.assertEqual(self.propagate(graph, a=('FINISHED', 'passed'), b=('RUNNING', '')), {})
        changes = self.propagate(graph, a=('FINISHED', 'passed'), b=('FINISHED', 'failed'))
        self.assertEqual(changes['join'], ('FINISHED', 'some', ''))
        self.assertEqual(changes['some'], ('READY', '', 'join'))
        self.assertEqual(changes['all'][0], 'SKIPPED')
        self.assertEqual(changes['none'][0], 'SKIPPED')

        changes = self.propagate(graph, a=('FINISHED', 'passed'), b=('FINISHED', 'passed'))
        self.assertEqual(changes['join'], ('FINISHED', 'all', ''))

        changes = self.propagate(graph, a=('FINISHED', 'failed'), b=('FINISHED', 'failed'))
        self.assertEqual(changes['join'], ('FINISHED', 'none', ''))

        # Skipped inputs never ran, so they do not count against 'all'
        changes = self.propagate(graph, a=('FINISHED', 'passed'), b=('SKIPPED', ''))
        self.assertEqual(changes['join'], ('FINISHED', 'all', ''))

        changes = self.propagate(graph, a=('SKIPPED', ''), b=('SKIPPED', ''))
        self.assertEqual(changes['join'][0], 'SKIPPED')

    def test_stopped_blocks_only_its_branch(self):
        graph = example_graph()
        graph['nodes']['other'] = node('SPRT', dev='z', base='main')
        changes = self.propagate(graph, ab=('STOPPED', ''))
        self.assertEqual(changes, { 'other' : ('READY', '', '') })

    def test_done_fires_with_either_outcome(self):

        graph = condition_graph()
        self.assertEqual(Graph.validate(graph), [])

        for outcome in ['passed', 'failed']:
            self.assertEqual(self.propagate(graph, ab=('FINISHED', outcome)), { 'if' : ('READY', '', 'ab') })

    def test_condition_validation(self):

        graph = condition_graph(condition='elo >> 3')
        self.assertTrue(any('if: ' in error for error in Graph.validate(graph)))

        # Without a source, the condition needs an SPRT behind every incoming edge
        graph = condition_graph()
        graph['nodes']['tune'] = node('SPSA', dev='t', spsa_inputs='A, int, 1, 0, 2, 1, 0.1')
        graph['edges'].append(edge('tune', 'done', 'if'))
        self.assertTrue(any('must come from an SPRT' in error for error in Graph.validate(graph)))

        # A named source must be an SPRT that has certainly finished
        self.assertEqual(Graph.validate(condition_graph(source='ab')), [])
        self.assertTrue(any('key of an SPRT' in error for error in Graph.validate(condition_graph(source='nope'))))
        self.assertTrue(any('every path' in error for error in Graph.validate(condition_graph(source='no'))))

        # @trigger after an IF refers to what started the IF, which has to provide it
        graph = condition_graph()
        graph['nodes']['yes']['config']['dev'] = '@trigger.tuned'
        self.assertTrue(any('provides @trigger.tuned' in error for error in Graph.validate(graph)))

    def two_sprts(self, condition='x.passed && y.failed'):

        # Two SPRTs in parallel, and an IF that is started by x but also needs y
        return {
            'defaults' : {},
            'nodes' : {
                'x'    : node('SPRT', dev='x', base='main'),
                'y'    : node('SPRT', dev='y', base='main'),
                'if'   : node('IF', condition=condition),
                'next' : node('SPRT', dev='@x.dev', base='main'),
            },
            'edges' : [ edge('x', 'done', 'if'), edge('if', 'true', 'next') ],
        }

    def test_conditions_wait_for_named_sprts(self):

        graph = self.two_sprts()
        self.assertEqual(Graph.validate(graph), [])

        # x is done, but the IF waits for y as well
        self.assertEqual(self.propagate(graph, x=('FINISHED', 'passed'), y=('RUNNING', '')), {})
        changes = self.propagate(graph, x=('FINISHED', 'passed'), y=('FINISHED', 'failed'))
        self.assertEqual(changes, { 'if' : ('READY', '', 'x') })

        # A skipped SPRT is decided as well, which the runner then reports
        self.assertEqual(self.propagate(graph, x=('FINISHED', 'passed'), y=('SKIPPED', ''))['if'][0], 'READY')

    def test_named_sprts_are_validated(self):

        errors = Graph.validate(self.two_sprts('x.passed && nope.failed'))
        self.assertTrue(any('"nope" in the condition is not the key of an SPRT' in error for error in errors))

        # Waiting for a Node that only runs after the IF would never end
        errors = Graph.validate(self.two_sprts('x.passed && next.failed'))
        self.assertTrue(any('runs after this IF' in error for error in errors))

        # Only qualified variables, so the IF does not need an SPRT behind its incoming edges
        graph = self.two_sprts('x.passed && y.failed')
        graph['nodes']['tune'] = node('SPSA', dev='t', spsa_inputs='A, int, 1, 0, 2, 1, 0.1')
        graph['edges'] = [edge('tune', 'done', 'if'), edge('if', 'true', 'next')]
        graph['nodes']['next']['config']['dev'] = 'next'
        self.assertEqual(Graph.validate(graph), [])

    def test_conditions_cannot_wait_in_a_cycle(self):

        # if1 waits for y, which runs after if2, which waits for z, which runs after if1
        graph = {
            'defaults' : {},
            'nodes' : {
                'a'   : node('SPRT', dev='a', base='main'), 'b' : node('SPRT', dev='b', base='main'),
                'if1' : node('IF', condition='y.passed'),   'if2' : node('IF', condition='z.passed'),
                'y'   : node('SPRT', dev='y', base='main'), 'z' : node('SPRT', dev='z', base='main'),
            },
            'edges' : [ edge('a', 'done', 'if1'), edge('b', 'done', 'if2'), edge('if2', 'true', 'y'), edge('if1', 'true', 'z') ],
        }
        self.assertIn('wait for each other in a cycle', Graph.validate(graph)[0])

        # Without the cross-wait it is fine
        graph['nodes']['if2']['config']['condition'] = 'b.passed'
        self.assertEqual(Graph.validate(graph), [])

    def test_merge_options(self):
        self.assertEqual(merge_options('Threads=1 Hash=8', 'hash=16 A=3'), 'Threads=1 hash=16 A=3')
        self.assertEqual(merge_options('Threads=1 Hash=8', ''), 'Threads=1 Hash=8')

def fake_github(errors, request, field):

    # Stands in for collect_github_info(): every branch exists, its sha is derived
    # from its name, and its Bench is taken from the form or from the branch name
    branch = request.POST['%s_branch' % (field)]

    if branch == 'missing':
        errors.append('%s could not be found' % (branch))
        return

    sha = branch if re.match('^[0-9a-f]{40}$', branch) else hashlib.sha1(branch.encode()).hexdigest()

    try: bench = int(request.POST['%s_bench' % (field)])
    except: bench = None if branch == 'nobench' else 1000 + len(branch)

    if not bench:
        errors.append('Unable to parse a Bench for %s' % (branch))
        return

    return ('https://api.github.com/repos/x/y/zipball/' + sha, branch, sha, bench, 'Commit of ' + branch)

@mock.patch('OpenBench.workloads.verify_workload.collect_github_info', fake_github)
@mock.patch('OpenBench.workloads.create_workload.branch_is_out_of_date', lambda workload: False)
class RunnerTests(TestCase):

    def setUp(self):

        self.user = User.objects.create_user('Xenymor', password='x')
        Profile.objects.create(user=self.user, enabled=True, approver=True)
        EngineConfig.objects.create(name='Fispur', source='https://github.com/Xenymor/Fispur', nps=1000000)
        Book.objects.create(name='UHO.epd', source='https://example.com/UHO.zip', sha='0')

    def defaults(self):
        return {
            'dev_engine'  : 'Fispur', 'dev_repo'  : 'https://github.com/Xenymor/Fispur', 'dev_network'  : '',
            'base_engine' : 'Fispur', 'base_repo' : 'https://github.com/Xenymor/Fispur', 'base_network' : '',
            'dev_options' : 'Threads=1 Hash=8', 'base_options' : 'Threads=1 Hash=8',
            'dev_time_control' : '10.0+0.1', 'base_time_control' : '10.0+0.1',
            'test_bounds' : '[0.00, 3.00]', 'test_confidence' : '[0.05, 0.05]',
            'book_name' : 'UHO.epd', 'upload_pgns' : 'FALSE', 'priority' : '0', 'throughput' : '1000',
            'workload_size' : '32', 'syzygy_wdl' : 'DISABLED', 'syzygy_adj' : 'OPTIONAL',
            'win_adj' : 'movecount=3 score=400', 'draw_adj' : 'movenumber=40 movecount=8 score=10',
            'scale_method' : 'BASE', 'scale_nps' : '1000000',
        }

    def create(self, graph):
        graph = copy.deepcopy(graph)
        graph['defaults'] = dict(self.defaults(), **graph.get('defaults', {}))
        for item in graph['nodes'].values():
            if item['kind'] == 'SPSA':
                item['config'].update(spsa_iterations='10', spsa_pairs_per='2', spsa_alpha='0.602',
                    spsa_gamma='0.101', spsa_A_ratio='0.1', spsa_reporting_type='BATCHED', spsa_distribution_type='SINGLE')
        workflow, errors = runner.create_workflow(self.user, 'Example', graph)
        self.assertIsNone(errors)
        return workflow

    def nodes(self, workflow):
        return { item.key : item for item in WorkflowNode.objects.filter(workflow=workflow).select_related('test') }

    def finish_sprt(self, test, passed):
        test.passed, test.failed, test.finished = passed, not passed, True
        test.save()

    def test_example_failed_then_tune(self):

        workflow = self.create(example_graph())
        nodes    = self.nodes(workflow)

        self.assertEqual(nodes['ab'].state, 'RUNNING')
        self.assertEqual(nodes['ab'].test.dev.name, 'x')
        self.assertEqual(nodes['ab'].test.base.name, 'y')
        self.assertTrue(nodes['ab'].test.approved)
        self.assertTrue(nodes['ab'].test.info.startswith('[Workflow #%d: ab]' % (workflow.id)))
        self.assertEqual(nodes['x_main'].state, 'WAITING')

        self.finish_sprt(nodes['ab'].test, passed=False)
        runner.step(workflow.id)
        nodes = self.nodes(workflow)

        self.assertEqual(nodes['x_main'].state, 'SKIPPED')
        self.assertEqual(nodes['y_main'].state, 'RUNNING')

        # y is pinned to the exact commit and Bench of the A/B test, but keeps its name
        y_main = nodes['y_main'].test
        self.assertEqual(y_main.dev.sha, nodes['ab'].test.base.sha)
        self.assertEqual(y_main.dev.bench, nodes['ab'].test.base.bench)
        self.assertEqual(y_main.dev.name, 'y')
        self.assertEqual(y_main.base.name, 'main')

        self.finish_sprt(y_main, passed=False)
        runner.step(workflow.id)
        nodes = self.nodes(workflow)

        tune = nodes['tune']
        self.assertEqual(tune.state, 'RUNNING')
        self.assertEqual(tune.trigger, 'y_main')
        self.assertEqual(tune.test.test_mode, 'SPSA')
        self.assertEqual(tune.test.dev.sha, y_main.dev.sha)
        self.assertEqual(tune.test.spsa_run.iterations, 10)

        tune.test.games = 2 * 2 * 10
        tune.test.finished = True
        tune.test.save()
        workflow = runner.step(workflow.id)
        nodes    = self.nodes(workflow)

        self.assertEqual(nodes['tune'].state, 'FINISHED')
        self.assertEqual(nodes['tune'].outcome, 'done')
        self.assertEqual(nodes['tune'].tuned, 'A=10')
        self.assertEqual(workflow.status, 'FINISHED')

    def test_tuned_values_and_option_overrides(self):

        graph = {
            'defaults' : {},
            'nodes' : {
                'tune' : node('SPSA', dev='feature', spsa_inputs='A, int, 10, 0, 20, 2, 0.002', at_percent='50'),
                'sprt' : node('SPRT', dev='@tune.tuned', base='main', dev_options='Hash=16', time_control='60+0.6'),
            },
            'edges' : [edge('tune', 'done', 'sprt')],
        }

        workflow = self.create(graph)
        tune     = self.nodes(workflow)['tune'].test

        # Done at 50%, while the tune keeps running
        tune.games = 20
        tune.save()
        tune.spsa_run.parameters.update(value=13.4)
        runner.step(workflow.id)

        nodes = self.nodes(workflow)
        sprt  = nodes['sprt'].test
        self.assertEqual(nodes['tune'].tuned, 'A=13')
        self.assertEqual(sprt.dev_options, 'Threads=1 Hash=16 A=13')
        self.assertEqual(sprt.base_options, 'Threads=1 Hash=8')
        self.assertEqual(sprt.dev_time_control, '60.0+0.60')
        self.assertEqual(sprt.dev.sha, tune.dev.sha)
        self.assertEqual(sprt.dev.name, 'feature-tuned')

    def test_errors_can_be_retried(self):

        graph = { 'defaults' : {}, 'nodes' : { 'a' : node('SPRT', dev='nobench', base='main') }, 'edges' : [] }
        workflow = self.create(graph)
        nodes    = self.nodes(workflow)

        self.assertEqual(nodes['a'].state, 'ERROR')
        self.assertIn('Unable to parse a Bench', nodes['a'].error)
        self.assertEqual(Workflow.objects.get(id=workflow.id).status, 'ATTENTION')

        # Fixed by giving a Bench, then retried
        workflow.graph['nodes']['a']['config']['dev_bench'] = '777'
        workflow.save()
        workflow = runner.retry(workflow.id, 'a')

        self.assertEqual(self.nodes(workflow)['a'].state, 'RUNNING')
        self.assertEqual(self.nodes(workflow)['a'].test.dev.bench, 777)
        self.assertEqual(workflow.status, 'RUNNING')

    def test_stopped_restarted_and_decided(self):

        workflow = self.create(example_graph())
        test     = self.nodes(workflow)['ab'].test

        test.finished = True # Stopped by hand
        test.save()
        workflow = runner.step(workflow.id)
        self.assertEqual(self.nodes(workflow)['ab'].state, 'STOPPED')
        self.assertEqual(workflow.status, 'ATTENTION')

        test.finished = False # Restarted by hand
        test.save()
        workflow = runner.step(workflow.id)
        self.assertEqual(self.nodes(workflow)['ab'].state, 'RUNNING')
        self.assertEqual(workflow.status, 'RUNNING')

        test.deleted = True
        test.save()
        runner.step(workflow.id)
        workflow = runner.resolve(workflow.id, 'ab', 'passed')
        nodes    = self.nodes(workflow)

        self.assertEqual(nodes['ab'].outcome, 'passed')
        self.assertEqual(nodes['x_main'].state, 'RUNNING')
        self.assertEqual(nodes['y_main'].state, 'SKIPPED')

    def test_idle_steps_write_nothing(self):

        # The watcher steps every Workflow every few seconds, which must not cause writes
        workflow = self.create(example_graph())
        updated  = Workflow.objects.get(id=workflow.id).updated

        with self.assertNumQueries(4): # Savepoint, lock, Nodes, release
            runner.step(workflow.id)

        self.assertEqual(Workflow.objects.get(id=workflow.id).updated, updated)

    def test_pause_and_abort(self):

        workflow = self.create(example_graph())
        runner.set_paused(workflow.id, True)

        self.finish_sprt(self.nodes(workflow)['ab'].test, passed=True)
        workflow = runner.step(workflow.id)
        self.assertEqual(self.nodes(workflow)['x_main'].state, 'WAITING')
        self.assertEqual(workflow.status, 'PAUSED')

        workflow = runner.set_paused(workflow.id, False)
        x_main   = self.nodes(workflow)['x_main']
        self.assertEqual(x_main.state, 'RUNNING')

        runner.abort(workflow.id, 'Xenymor')
        self.assertTrue(Test.objects.get(id=x_main.test.id).finished)
        self.assertEqual(runner.step(workflow.id).status, 'ABORTED')

    def test_existing_tests_are_attached(self):

        first  = self.create({ 'defaults' : {}, 'nodes' : { 'a' : node('SPRT', dev='a', base='main') }, 'edges' : [] })
        test   = self.nodes(first)['a'].test
        graph  = {
            'defaults' : self.defaults(),
            'nodes' : { 'stc' : node('SPRT', existing_test=str(test.id)), 'ltc' : node('SPRT', dev='@stc.dev', base='@stc.base') },
            'edges' : [edge('stc', 'passed', 'ltc')],
        }

        # A Test belongs to at most one Workflow
        workflow, errors = runner.create_workflow(self.user, 'LTC', graph)
        self.assertIn('already belongs to a Workflow', errors[0])

        WorkflowNode.objects.filter(test=test).update(test=None)
        workflow, errors = runner.create_workflow(self.user, 'LTC', graph)
        self.assertIsNone(errors)

        self.finish_sprt(test, passed=True)
        nodes = self.nodes(runner.step(workflow.id))
        self.assertEqual(nodes['ltc'].test.dev.sha, test.dev.sha)
        self.assertEqual(nodes['ltc'].test.base.sha, test.base.sha)

    def test_foreign_tests_cannot_be_attached(self):

        # Aborting would stop the attached Test, so it must be a Test the user may stop
        first = self.create({ 'defaults' : {}, 'nodes' : { 'a' : node('SPRT', dev='a', base='main') }, 'edges' : [] })
        test  = self.nodes(first)['a'].test
        WorkflowNode.objects.filter(test=test).update(test=None)

        other = User.objects.create_user('Other', password='x')
        Profile.objects.create(user=other, enabled=True, approver=False)
        graph = { 'defaults' : self.defaults(), 'nodes' : { 'stc' : node('SPRT', existing_test=str(test.id)) }, 'edges' : [] }

        workflow, errors = runner.create_workflow(other, 'Foreign', graph)
        self.assertIn('belongs to another user', errors[0])

        # Huge ids and positions are rejected before they reach the database or the browser
        graph['nodes']['stc']['config']['existing_test'] = str(10 ** 30)
        self.assertIn('invalid Test', runner.create_workflow(self.user, 'Huge', graph)[1][0])

        graph['nodes']['stc'] = node('SPRT', dev='a', base='main')
        graph['nodes']['stc']['x'] = 10 ** 9
        self.assertIn('Malformed position', runner.create_workflow(self.user, 'Far', graph)[1][0])

    def edit(self, workflow, change, user=None):
        graph = copy.deepcopy(Workflow.objects.get(id=workflow.id).graph)
        change(graph)
        return runner.update_workflow(workflow.id, user or self.user, 'Edited', graph)

    def test_edit_rules_for_started_nodes(self):

        workflow = self.create(example_graph())

        def change_ab(graph):
            graph['nodes']['ab']['config']['base'] = 'z'

        def remove_ab(graph):
            del graph['nodes']['ab']
            graph['edges'] = [e for e in graph['edges'] if 'ab' not in (e['from'], e['to'])]
            graph['nodes']['x_main']['config']['dev'] = 'x'
            graph['nodes']['y_main']['config']['dev'] = 'y'

        def into_ab(graph):
            graph['nodes']['new'] = node('SPRT', dev='n', base='main')
            graph['edges'].append(edge('new', 'passed', 'ab'))

        self.assertIn('settings cannot change', self.edit(workflow, change_ab)[1][0])
        self.assertIn('cannot be removed', self.edit(workflow, remove_ab)[1][0])
        self.assertIn('incoming edges cannot change', self.edit(workflow, into_ab)[1][0])

        # Labels and positions of started Nodes may change, and Nodes that have not started are free
        def allowed(graph):
            graph['nodes']['ab']['label'] = 'Renamed'
            graph['nodes']['ab']['x'] = 500
            graph['nodes']['x_main']['config']['time_control'] = '60+0.6'
            graph['nodes']['extra'] = node('SPRT', dev='extra', base='main') # A new root starts right away

        test_id = self.nodes(workflow)['ab'].test.id
        workflow, errors = self.edit(workflow, allowed)
        self.assertIsNone(errors)
        self.assertEqual(workflow.name, 'Edited')

        nodes = self.nodes(workflow)
        self.assertEqual(nodes['extra'].state, 'RUNNING')
        self.assertEqual(nodes['ab'].state, 'RUNNING')
        self.assertEqual(nodes['ab'].test.id, test_id)

        self.finish_sprt(nodes['ab'].test, passed=True)
        x_main = self.nodes(runner.step(workflow.id))['x_main'].test
        self.assertEqual(x_main.dev_time_control, '60.0+0.60')

    def test_edit_revisits_skipped_and_fired_nodes(self):

        workflow = self.create(example_graph())
        self.finish_sprt(self.nodes(workflow)['ab'].test, passed=True)
        workflow = runner.step(workflow.id)
        self.assertEqual(self.nodes(workflow)['y_main'].state, 'SKIPPED')

        def extend(graph):
            # y_main now also runs once x_main fails, and a new Node hangs off the port that already fired
            graph['edges'].append(edge('x_main', 'failed', 'y_main'))
            graph['nodes']['late'] = node('SPRT', dev='@trigger.dev', base='main')
            graph['edges'].append(edge('ab', 'passed', 'late'))

        workflow, errors = self.edit(workflow, extend)
        self.assertIsNone(errors)

        nodes = self.nodes(workflow)
        self.assertEqual(nodes['y_main'].state, 'WAITING')
        self.assertEqual(nodes['late'].state, 'RUNNING')
        self.assertEqual(nodes['late'].trigger, 'ab')
        self.assertEqual(nodes['late'].test.dev.sha, nodes['ab'].test.dev.sha)

        self.finish_sprt(nodes['x_main'].test, passed=False)
        self.assertEqual(self.nodes(runner.step(workflow.id))['y_main'].state, 'RUNNING')

    def test_edit_fixes_errors_and_extends_finished_workflows(self):

        graph    = { 'defaults' : {}, 'nodes' : { 'a' : node('SPRT', dev='nobench', base='main') }, 'edges' : [] }
        workflow = self.create(graph)
        self.assertEqual(self.nodes(workflow)['a'].state, 'ERROR')

        def give_bench(graph):
            graph['nodes']['a']['config']['dev_bench'] = '777'

        workflow, errors = self.edit(workflow, give_bench)
        self.assertIsNone(errors)
        self.assertEqual(self.nodes(workflow)['a'].test.dev.bench, 777)

        self.finish_sprt(self.nodes(workflow)['a'].test, passed=True)
        self.assertEqual(runner.step(workflow.id).status, 'FINISHED')

        def ltc(graph):
            graph['nodes']['ltc'] = node('SPRT', dev='@a.dev', base='@a.base', time_control='60+0.6')
            graph['edges'].append(edge('a', 'passed', 'ltc'))

        workflow, errors = self.edit(workflow, ltc)
        self.assertIsNone(errors)
        self.assertEqual(workflow.status, 'RUNNING')
        self.assertEqual(self.nodes(workflow)['ltc'].state, 'RUNNING')

        runner.abort(workflow.id, 'Xenymor')
        self.assertIn('Aborted', self.edit(workflow, give_bench)[1][0])

    def set_penta(self, test, results, passed):
        test.LL, test.LD, test.DD, test.DW, test.WW = results
        test.games = 2 * sum(results)
        test.passed, test.failed, test.finished = passed, not passed, True
        test.save()

    def test_conditions_on_elo(self):

        # failed && elo > 3: a failed SPRT that still gained enough goes on at a longer time control
        workflow = self.create(condition_graph())
        ab       = self.nodes(workflow)['ab'].test
        self.set_penta(ab, (100, 300, 500, 300, 120), passed=False) # Elo +5.26

        nodes = self.nodes(runner.step(workflow.id))
        self.assertEqual((nodes['if'].state, nodes['if'].outcome), ('FINISHED', 'true'))
        self.assertIn('failed, Elo +5.26', nodes['if'].detail)
        self.assertEqual(nodes['no'].state, 'SKIPPED')

        # The Node after the IF is decided in the very same step, and @trigger looks through the IF
        self.assertEqual(nodes['yes'].state, 'RUNNING')
        self.assertEqual(nodes['yes'].test.dev.sha, ab.dev.sha)
        self.assertEqual(nodes['yes'].test.dev.name, 'x')

        # Too little Elo takes the other path
        workflow = self.create(condition_graph())
        self.set_penta(self.nodes(workflow)['ab'].test, (120, 300, 500, 300, 100), passed=False)
        nodes = self.nodes(runner.step(workflow.id))
        self.assertEqual(nodes['if'].outcome, 'false')
        self.assertEqual(nodes['no'].state, 'RUNNING')
        self.assertEqual(nodes['yes'].state, 'SKIPPED')

    def test_conditions_follow_decisions_by_hand(self):

        # A stopped SPRT decided as failed by hand counts as failed, though its Test is neither
        workflow = self.create(condition_graph())
        test     = self.nodes(workflow)['ab'].test
        test.LL, test.LD, test.DD, test.DW, test.WW = (100, 300, 500, 300, 120)
        test.finished = True
        test.save()

        runner.step(workflow.id)
        nodes = self.nodes(runner.resolve(workflow.id, 'ab', 'failed'))

        self.assertEqual(nodes['if'].outcome, 'true')
        self.assertIn('failed, Elo +5.26', nodes['if'].detail)

    def test_condition_on_a_named_source(self):

        # The IF hangs off a later Node, but looks at the result of the first SPRT
        graph = condition_graph(condition='passed && elo_lower > 0', source='ab')
        graph['nodes']['mid'] = node('SPRT', dev='mid', base='main')
        graph['edges'] = [edge('ab', 'done', 'mid'), edge('mid', 'done', 'if'), edge('if', 'true', 'yes'), edge('if', 'false', 'no')]

        workflow = self.create(graph)
        self.set_penta(self.nodes(workflow)['ab'].test, (10, 100, 500, 300, 200), passed=True)
        runner.step(workflow.id)
        self.set_penta(self.nodes(workflow)['mid'].test, (300, 300, 300, 300, 300), passed=False)

        nodes = self.nodes(runner.step(workflow.id))
        self.assertEqual(nodes['if'].outcome, 'true')
        self.assertTrue(nodes['if'].detail.startswith('ab: passed'))
        self.assertEqual(nodes['yes'].test.dev.name, 'mid') # @trigger is still what started the IF

    def test_conditions_on_several_sprts(self):

        graph = {
            'defaults' : {},
            'nodes' : {
                'x'    : node('SPRT', dev='x', base='main'),
                'y'    : node('SPRT', dev='y', base='main'),
                'if'   : node('IF', condition='x.passed && y.failed && x.elo > 2'),
                'next' : node('SPRT', dev='@trigger.dev', base='@y.dev'),
            },
            'edges' : [ edge('x', 'done', 'if'), edge('y', 'done', 'if'), edge('if', 'true', 'next') ],
        }

        workflow = self.create(graph)
        nodes    = self.nodes(workflow)
        self.set_penta(nodes['x'].test, (10, 100, 500, 300, 200), passed=True)

        # x alone is not enough, y has not finished yet
        nodes = self.nodes(runner.step(workflow.id))
        self.assertEqual(nodes['if'].state, 'WAITING')

        self.set_penta(nodes['y'].test, (300, 300, 300, 300, 300), passed=False)
        nodes = self.nodes(runner.step(workflow.id))
        self.assertEqual((nodes['if'].state, nodes['if'].outcome), ('FINISHED', 'true'))
        self.assertIn('x: passed', nodes['if'].detail)
        self.assertIn('y: failed', nodes['if'].detail)
        self.assertEqual(nodes['next'].test.dev.name, 'x')
        self.assertEqual(nodes['next'].test.base.name, 'y')

    def test_networks_per_node(self):

        Network.objects.create(engine='Fispur', sha256='aaaa1111', name='net-a', author='Xenymor')
        Network.objects.create(engine='Fispur', sha256='bbbb2222', name='net-b', author='Xenymor')

        graph = {
            'defaults' : {},
            'nodes' : {
                # Each engine its own net, and later the same commits with other nets
                'a'    : node('SPRT', dev='x', base='main', dev_network='aaaa1111', base_network='bbbb2222', dev_bench='1001', base_bench='2002'),
                'same' : node('SPRT', dev='@a.dev', base='@a.base'),
                'swap' : node('SPRT', dev='@a.dev', base='@a.dev', base_network='none', base_bench='111111'),
                'tune' : node('SPSA', dev='@a.dev', dev_network='bbbb2222', dev_bench='222222', spsa_inputs='A, int, 10, 0, 20, 2, 0.002'),
            },
            'edges' : [ edge('a', 'done', 'same'), edge('a', 'done', 'swap'), edge('a', 'done', 'tune') ],
        }

        workflow = self.create(graph)
        a = self.nodes(workflow)['a'].test
        self.assertEqual((a.dev_network, a.dev_netname), ('aaaa1111', 'net-a'))
        self.assertEqual((a.base_network, a.base_netname), ('bbbb2222', 'net-b'))
        self.assertEqual((a.dev.bench, a.base.bench), (1001, 2002))

        self.finish_sprt(a, passed=True)
        nodes = self.nodes(runner.step(workflow.id))

        # A reference keeps the net it was tested with, unless the Node picks another
        self.assertEqual((nodes['same'].test.dev_network, nodes['same'].test.base_network), ('aaaa1111', 'bbbb2222'))
        self.assertEqual((nodes['swap'].test.dev_network, nodes['swap'].test.base_network), ('aaaa1111', ''))
        self.assertEqual(nodes['tune'].test.dev_network, 'bbbb2222')
        self.assertEqual(nodes['tune'].test.dev_netname, 'net-b')
        self.assertEqual((nodes['swap'].test.base.bench, nodes['tune'].test.dev.bench), (111111, 222222))

        # A net of another engine, or one that does not exist, is rejected on save
        graph['nodes']['a']['config']['dev_network'] = 'cccc3333'
        graph['defaults'] = self.defaults()
        workflow, errors = runner.create_workflow(self.user, 'Bad net', graph)
        self.assertIsNone(workflow)
        self.assertTrue(any(error.startswith('a: Unknown Network') for error in errors), errors)

    def test_benches_of_references(self):

        Network.objects.create(engine='Fispur', sha256='aaaa1111', name='net-a', author='Xenymor')

        graph = {
            'defaults' : {},
            'nodes' : {
                'a'     : node('SPRT', dev='x', base='main'),
                'carry' : node('SPRT', dev='@a.dev', base='@a.base'),                         # Same commits, same nets
                'given' : node('SPRT', dev='@a.dev', base='@a.base', base_bench='424242'),   # Corrected by hand
                'net'   : node('SPRT', dev='@a.dev', base='@a.base', dev_network='aaaa1111', dev_bench='777777'),
            },
            'edges' : [ edge('a', 'done', 'carry'), edge('a', 'done', 'given'), edge('a', 'done', 'net') ],
        }

        # Another net without its Bench would fail on every worker, so it fails right here
        broken = copy.deepcopy(graph)
        broken['nodes']['net']['config']['dev_bench'] = ''
        broken['defaults'] = self.defaults()
        workflow, errors = runner.create_workflow(self.user, 'No bench', broken)
        self.assertTrue(any('picks its own Network, so its Bench' in error for error in errors), errors)

        broken['nodes']['net']['config']['dev_bench'] = 'lots'
        workflow, errors = runner.create_workflow(self.user, 'Bad bench', broken)
        self.assertTrue(any('Bench must be a positive number' in error for error in errors), errors)

        workflow = self.create(graph)
        a = self.nodes(workflow)['a'].test
        self.finish_sprt(a, passed=True)
        nodes = self.nodes(runner.step(workflow.id))

        self.assertEqual((nodes['carry'].test.dev.bench, nodes['carry'].test.base.bench), (a.dev.bench, a.base.bench))
        self.assertEqual((nodes['given'].test.dev.bench, nodes['given'].test.base.bench), (a.dev.bench, 424242))
        self.assertEqual((nodes['net'].test.dev.bench, nodes['net'].test.dev_network), (777777, 'aaaa1111'))
        self.assertEqual(nodes['net'].test.dev.sha, a.dev.sha)

    def test_benches_of_branches_with_other_nets(self):

        Network.objects.create(engine='Fispur', sha256='aaaa1111', name='net-a', author='Xenymor')
        Network.objects.create(engine='Fispur', sha256='bbbb2222', name='net-b', author='Xenymor')

        def graph_with(**config):
            defaults = dict(self.defaults(), dev_network='aaaa1111', base_network='aaaa1111')
            return { 'defaults' : defaults, 'nodes' : { 'a' : node('SPRT', dev='feature', base='main', **config) }, 'edges' : [] }

        # The Bench in the commit message belongs to the default net, not to another one
        workflow, errors = runner.create_workflow(self.user, 'Other net', graph_with(dev_network='bbbb2222'))
        self.assertTrue(any('picks its own Network, so its Bench' in error for error in errors), errors)

        # Picking the default net again changes nothing, so the Bench may still be autofilled
        workflow, errors = runner.create_workflow(self.user, 'Same net', graph_with(dev_network='aaaa1111'))
        self.assertIsNone(errors)

        # Thousands separators are fine, as in commit messages
        workflow, errors = runner.create_workflow(self.user, 'Commas', graph_with(dev_network='bbbb2222', dev_bench='1,600,000'))
        self.assertIsNone(errors)
        self.assertEqual(self.nodes(workflow)['a'].test.dev.bench, 1600000)

    def wrong_bench(self, test, side, measured, network='', exe=False):

        # What views.client_bench_error() records when a worker measures another Bench
        engine = getattr(test, side)
        binary = 'Fispur-%s' % (engine.sha.upper()[:8]) + ('-%s' % (network) if network else '') + ('.exe' if exe else '')
        LogEvent.objects.create(author='Xenymor', summary='[%s] Wrong Bench: %d' % (binary, measured),
                                log_file='', machine_id=7, test_id=test.id)
        test.finished = True
        test.save()

    def test_measured_bench_restarts_the_node(self):

        workflow = self.create(condition_graph())
        old      = self.nodes(workflow)['ab'].test
        self.wrong_bench(old, 'dev', 1234567, exe=True)

        node = self.nodes(runner.step(workflow.id))['ab']
        self.assertEqual(node.state, 'STOPPED')
        self.assertIn('Stopped by a worker:', node.error)
        self.assertIn('Wrong Bench: 1234567', node.error)

        # The view offers the Bench that was measured
        self.client.force_login(self.user)
        state = self.client.get('/workflow/%d/state/' % (workflow.id)).json()['nodes']['ab']['bench']
        self.assertEqual(state, { 'dev' : { 'given' : old.dev.bench, 'measured' : [1234567] } })

        # One click: a new Test with that Bench, the stopped one stays as it is
        self.client.post('/workflow/%d/fixbench/' % (workflow.id), { 'key' : 'ab' })
        node = self.nodes(Workflow.objects.get(id=workflow.id))['ab']
        self.assertEqual(node.state, 'RUNNING')
        self.assertNotEqual(node.test.id, old.id)
        self.assertEqual((node.test.dev.bench, node.test.base.bench), (1234567, old.base.bench))
        self.assertEqual(Workflow.objects.get(id=workflow.id).graph['nodes']['ab']['config']['dev_bench'], '1234567')
        self.assertTrue(Test.objects.get(id=old.id).finished)

    def test_measured_bench_of_base_and_with_nets(self):

        Network.objects.create(engine='Fispur', sha256='aaaa1111', name='net-a', author='Xenymor')
        graph = { 'defaults' : {}, 'edges' : [],
                  'nodes' : { 'a' : node('SPRT', dev='x', base='main', base_network='aaaa1111', base_bench='555') } }

        workflow = self.create(graph)
        old      = self.nodes(workflow)['a'].test
        self.wrong_bench(old, 'base', 2222222, network='aaaa1111')
        runner.step(workflow.id)

        restarted = self.nodes(runner.use_measured_bench(workflow.id, 'a'))['a']
        self.assertEqual((restarted.test.base.bench, restarted.test.base_network), (2222222, 'aaaa1111'))
        self.assertEqual(restarted.test.dev.bench, old.dev.bench)

        # The new Test has no reports of its own yet
        self.assertEqual(runner.measured_benches(restarted.test, ['dev', 'base']), {})

        # A binary without the net is a different build, and is not taken for Base
        LogEvent.objects.create(author='Xenymor', summary='[Fispur-%s] Wrong Bench: 3' % (old.base.sha.upper()[:8]),
                                log_file='', machine_id=7, test_id=restarted.test.id)
        self.assertEqual(runner.measured_benches(restarted.test, ['base']), {})

    def test_disagreeing_workers_and_other_stops(self):

        workflow = self.create(condition_graph())
        test     = self.nodes(workflow)['ab'].test
        self.wrong_bench(test, 'dev', 1111111)
        self.wrong_bench(test, 'dev', 1111112)
        runner.step(workflow.id)

        with self.assertRaisesRegex(runner.WorkflowError, 'different Benches'):
            runner.use_measured_bench(workflow.id, 'ab')

        # Stopped by hand: no suggestion, and nothing to restart with
        workflow = self.create(condition_graph())
        test     = self.nodes(workflow)['ab'].test
        test.finished = True
        test.save()
        node = self.nodes(runner.step(workflow.id))['ab']
        self.assertEqual(node.error, 'The Test was stopped')

        with self.assertRaisesRegex(runner.WorkflowError, 'No worker reported'):
            runner.use_measured_bench(workflow.id, 'ab')

        # 'done' always fires alongside the outcome, so it is not an outcome of its own
        with self.assertRaisesRegex(runner.WorkflowError, 'cannot be decided as "done"'):
            runner.resolve(workflow.id, 'ab', 'done')
        self.assertEqual(Graph.outcomes('SPRT'), ['passed', 'failed'])
        self.assertEqual(Graph.outcomes('SPSA'), ['done'])

    def capture_notifications(self, action):

        # Runs action, and returns the events that would have been sent, once committed
        sent = []
        with mock.patch.object(notifications, 'notify', lambda workflow, events: sent.append(events)):
            with self.captureOnCommitCallbacks(execute=True):
                result = action()
        return result, sent

    def test_notifications_for_problems_and_the_end(self):

        # A Node that cannot start is announced once, not on every later step
        graph = { 'defaults' : {}, 'nodes' : { 'a' : node('SPRT', dev='nobench', base='main') }, 'edges' : [] }
        workflow, sent = self.capture_notifications(lambda: self.create(graph))
        self.assertEqual([kind for events in sent for kind, line in events], ['attention'])
        self.assertIn('Unable to parse a Bench', sent[0][0][1])

        _, sent = self.capture_notifications(lambda: runner.step(workflow.id))
        self.assertEqual(sent, [])

        # The end of a Workflow lists every Node
        workflow, sent = self.capture_notifications(lambda: self.create(condition_graph()))
        self.assertEqual(sent, [])

        self.set_penta(self.nodes(workflow)['ab'].test, (120, 300, 500, 300, 100), passed=False)
        workflow, sent = self.capture_notifications(lambda: runner.step(workflow.id))
        self.assertEqual(sent, []) # Only attention and the end, by default

        self.finish_sprt(self.nodes(workflow)['no'].test, passed=True)
        workflow, sent = self.capture_notifications(lambda: runner.step(workflow.id))
        lines = [line for kind, line in sent[0] if kind == 'finished']
        self.assertEqual(workflow.status, 'FINISHED')
        self.assertTrue(lines[0].startswith('ab: failed, Elo -5.26'))
        self.assertIn('yes: skipped', lines)

    def test_notification_levels(self):

        graph = condition_graph()
        graph['nodes']['ab']['config']['notify'] = 'ignored' # Only the defaults count

        for notify, expected in [('all', ['failed']), ('off', [])]:
            graph['defaults'] = { 'notify' : notify }
            workflow = self.create(graph)
            self.set_penta(self.nodes(workflow)['ab'].test, (120, 300, 500, 300, 100), passed=False)
            _, sent = self.capture_notifications(lambda: runner.step(workflow.id))
            self.assertEqual([kind for events in sent for kind, line in events], expected, notify)

    def test_settings_are_verified_at_creation(self):

        graph = example_graph()
        graph['nodes']['ab']['config']['time_control'] = 'garbage'
        graph['nodes']['x_main']['config']['dev_options'] = 'not options'
        graph['defaults'] = self.defaults()

        workflow, errors = runner.create_workflow(self.user, 'Broken', graph)
        self.assertIsNone(workflow)
        self.assertTrue(any(error.startswith('ab: Dev Time Control') for error in errors))
        self.assertTrue(any(error.startswith('x_main: Dev Options must be') for error in errors))

    def test_views(self):

        self.client.force_login(self.user)

        graph = example_graph()
        graph['defaults'] = self.defaults()
        graph['nodes']['tune']['config'].update(spsa_iterations='10', spsa_pairs_per='2', spsa_alpha='0.602',
            spsa_gamma='0.101', spsa_A_ratio='0.1', spsa_reporting_type='BATCHED', spsa_distribution_type='SINGLE')

        response = self.client.post('/workflow/new/', json.dumps({ 'name' : 'Web', 'graph' : graph }), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        workflow_id = response.json()['id']

        for url in ['/workflows/', '/workflow/new/', '/workflow/new/?clone=%d' % (workflow_id), '/workflow/%d/' % (workflow_id)]:
            self.assertEqual(self.client.get(url).status_code, 200, url)

        self.assertContains(self.client.get('/workflows/'), '0 / 4')

        state = self.client.get('/workflow/%d/state/' % (workflow_id)).json()
        self.assertEqual(state['nodes']['ab']['state'], 'RUNNING')
        self.assertEqual(state['nodes']['ab']['test']['elo'], 'Elo +0.00 ± 0.00')

        test_id = state['nodes']['ab']['test']['id']
        self.assertContains(self.client.get('/test/%d/' % (test_id)), '/workflow/%d/' % (workflow_id))

        self.client.post('/workflow/%d/pause/' % (workflow_id))
        self.assertTrue(Workflow.objects.get(id=workflow_id).paused)

        response = self.client.post('/workflow/new/', json.dumps({ 'name' : 'Bad', 'graph' : { 'nodes' : {} } }), content_type='application/json')
        self.assertEqual(response.status_code, 400)

        self.assertEqual(self.client.get('/workflow/new/?clone=abc').status_code, 200)

        # The polled JSON honours require_login_to_view, as the rendered pages do
        self.client.logout()
        with mock.patch.dict('OpenBench.workflows.views.OPENBENCH_CONFIG', { 'require_login_to_view' : True }):
            self.assertEqual(self.client.get('/workflow/%d/state/' % (workflow_id)).status_code, 403)
        self.assertEqual(self.client.get('/workflow/%d/state/' % (workflow_id)).status_code, 200)

        # Actions need a login, and the right to modify the Workflow
        self.client.post('/workflow/%d/resume/' % (workflow_id))
        self.assertTrue(Workflow.objects.get(id=workflow_id).paused)

        # The editor of a running Workflow lists the Nodes that have started
        self.client.force_login(self.user)
        response = self.client.get('/workflow/%d/edit/' % (workflow_id))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['edit']['locked'], ['ab'])

        graph['nodes']['ab']['label'] = 'Renamed'
        response = self.client.post('/workflow/%d/edit/' % (workflow_id), json.dumps({ 'name' : 'Web', 'graph' : graph }), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Workflow.objects.get(id=workflow_id).graph['nodes']['ab']['label'], 'Renamed')

        other = User.objects.create_user('Other', password='x')
        Profile.objects.create(user=other, enabled=True, approver=True)
        self.client.force_login(other)

        # Even approvers may not edit, since the Tests would be created in the author's name
        response = self.client.post('/workflow/%d/edit/' % (workflow_id), json.dumps({ 'name' : 'Evil', 'graph' : graph }), content_type='application/json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get('/workflow/%d/edit/' % (workflow_id)).status_code, 302)
        self.assertEqual(Workflow.objects.get(id=workflow_id).name, 'Web')

        Profile.objects.filter(user=other).update(approver=False)
        self.client.post('/workflow/%d/abort/' % (workflow_id))
        self.assertNotEqual(Workflow.objects.get(id=workflow_id).status, 'ABORTED')
