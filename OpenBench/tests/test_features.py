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

# Error limits, checks of new Tests, ETAs, SPSA convergence, action Nodes and Ladders.
#
# python manage.py test OpenBench

import datetime

from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

import OpenBench.eta
import OpenBench.utils

from OpenBench import error_watch, github, ladder, spsa_history
from OpenBench.config import OPENBENCH_CONFIG
from OpenBench.models import Book, Engine, EngineConfig, Ladder, LadderPoint, Machine, Profile, Result
from OpenBench.models import ServerState, SPSASnapshot, Test, WorkflowNode
from OpenBench.tests import test_workflows as TW
from OpenBench.workflows import actions, notifications, runner
from OpenBench.workflows import eta as WorkflowEta
from OpenBench.workflows import graph as Graph
from OpenBench.workloads import checks

node, edge = TW.node, TW.edge

def counts(**fields):
    return Test(**{ 'games' : 0, 'crashes' : 0, 'timelosses' : 0, 'illegals' : 0, 'finished' : False, **fields })

class ErrorWatchTests(SimpleTestCase):

    def test_first_error_is_announced_once(self):

        test = counts(games=100)
        self.assertEqual(error_watch.record_errors(test, 0, 1, 0), (None, None)) # Time losses alone are not
        alert, stop = error_watch.record_errors(test, 1, 0, 0)
        self.assertIn('1 crash, 1 time loss, 0 illegal moves', alert)
        self.assertIsNone(stop)
        self.assertEqual(error_watch.record_errors(test, 1, 0, 0), (None, None))

    def test_limits_stop_the_test(self):

        test = counts(games=100)
        error_watch.record_errors(test, 2, 0, 0)
        alert, stop = error_watch.record_errors(test, 1, 0, 0)
        self.assertEqual(stop, 'Stopped by OpenBench: 3 crashes or stalls')
        self.assertTrue(test.finished)
        self.assertEqual(test.stop_reason, stop)

        test = counts(games=10)
        self.assertEqual(error_watch.record_errors(test, 0, 0, 1)[1], 'Stopped by OpenBench: 1 illegal move')

    def test_time_losses_need_enough_games(self):

        test = counts(games=100)
        self.assertIsNone(error_watch.record_errors(test, 0, 10, 0)[1]) # 10%, but only 100 games
        test.games = 500
        self.assertIn('2.0% of the games lost on time (10 of 500)', error_watch.record_errors(test, 0, 0, 0)[1])

    def test_limits_count_from_the_restart(self):

        test = counts(games=100, crashes=3, finished=True, stop_reason='x')
        error_watch.reset_baseline(test)
        test.finished = False
        self.assertEqual(test.stop_reason, '')

        self.assertEqual(error_watch.record_errors(test, 0, 0, 0), (None, None))
        alert, stop = error_watch.record_errors(test, 1, 0, 0)
        self.assertIn('1 crash', alert) # The first one since the restart
        self.assertIsNone(stop)

    def test_finished_tests_are_left_alone(self):
        test = counts(games=100, finished=True)
        self.assertIsNone(error_watch.record_errors(test, 5, 0, 5)[1])
        self.assertEqual(test.stop_reason, '')

    def test_configured_limits(self):
        with mock.patch.dict(OPENBENCH_CONFIG, { 'error_limits' : { 'crashes' : 0, 'illegals' : 2 } }):
            test = counts(games=100)
            self.assertIsNone(error_watch.record_errors(test, 50, 0, 1)[1])
            self.assertIn('2 illegal moves', error_watch.record_errors(test, 0, 0, 1)[1])

class ChecksTests(SimpleTestCase):

    def engines(self, dev_bench=100, base_bench=100, **fields):
        test = Test(**{ 'test_mode' : 'SPRT', 'dev_engine' : 'Fispur', 'base_engine' : 'Fispur', 'dev_options' : 'Hash=8',
                        'base_options' : 'Hash=8', 'dev_network' : '', 'base_network' : '',
                        'dev_repo' : 'https://github.com/x/y', 'base_repo' : 'https://github.com/x/y/', **fields })
        test.dev  = Engine(name='feature', sha='a' * 40, bench=dev_bench)
        test.base = Engine(name='main',    sha='b' * 40, bench=base_bench)
        return test

    def test_same_bench(self):
        self.assertTrue(checks.same_bench(self.engines()))
        self.assertFalse(checks.same_bench(self.engines(base_bench=101)))
        self.assertFalse(checks.same_bench(self.engines(base_options='Hash=16')))

    def test_github_checks(self):

        compares = { ('b' * 40, 'a' * 40) : { 'ahead_by' : 1, 'behind_by' : 2, 'status' : 'diverged' },
                     ('b' * 40, 'c' * 40) : { 'ahead_by' : 4, 'behind_by' : 0, 'status' : 'ahead' } }

        with mock.patch.object(checks.github, 'compare', lambda repo, engine, base, head: compares[(base, head)]), \
             mock.patch.object(checks.github, 'commit', lambda repo, engine, ref: { 'sha' : 'c' * 40, 'message' : '', 'date' : '' }):
            found = checks.run_checks(self.engines(), check_base=True)
            self.assertEqual(checks.run_checks(self.engines()), { 'same_bench' : True, 'behind' : 2 }) # The form's Base is current

        self.assertEqual(found, { 'same_bench' : True, 'behind' : 2, 'base_moved' : 4, 'base_branch' : 'main' })
        self.assertEqual(checks.describe(found), [
            'Dev and Base have the same Bench, so the change is probably not functional',
            'Dev is 2 commits behind Base, consider rebasing',
            'main has 4 newer commits than the tested Base'])

    def test_github_failures_only_drop_the_check(self):

        def broken(*args):
            raise github.GithubError('down')

        with mock.patch.object(checks.github, 'compare', broken), mock.patch.object(checks.github, 'commit', broken), \
             mock.patch.object(checks.traceback, 'print_exc', lambda: None):
            self.assertEqual(checks.run_checks(self.engines(base_bench=5), check_base=True), {})

    def test_other_engines_and_modes_are_not_compared(self):
        with mock.patch.object(checks.github, 'compare', lambda *args: self.fail('Must not compare')):
            self.assertEqual(checks.run_checks(self.engines(base_engine='Other')), {})
            self.assertEqual(checks.run_checks(self.engines(test_mode='SPSA')), {})

class EtaTests(SimpleTestCase):

    def sprt(self, **fields):
        return Test(**{ 'test_mode' : 'SPRT', 'games' : 4000, 'currentllr' : 1.5, 'lowerllr' : -2.94, 'upperllr' : 2.94, **fields })

    def test_sprt_estimates(self):
        self.assertEqual(OpenBench.eta.remaining_games(self.sprt()), 3840)
        self.assertEqual(OpenBench.eta.remaining_games(self.sprt(currentllr=-1.47)), 4000)
        self.assertIsNone(OpenBench.eta.remaining_games(self.sprt(games=100)))       # Too early
        self.assertIsNone(OpenBench.eta.remaining_games(self.sprt(currentllr=0.05))) # Too close to 0
        self.assertEqual(OpenBench.eta.remaining_games(self.sprt(finished=True)), 0)

    def test_fixed_games(self):
        self.assertEqual(OpenBench.eta.remaining_games(Test(test_mode='GAMES', games=300, max_games=1000)), 700)

    def test_seconds_and_text(self):

        now  = timezone.now()
        test = self.sprt(rate_games=600.0, rate_time=3600.0, rate_updated=now) # 10 games per minute
        self.assertAlmostEqual(OpenBench.eta.remaining_seconds(test, now), 3840 * 6.0)

        self.assertEqual(OpenBench.eta.describe(None), '-')
        self.assertEqual(OpenBench.eta.describe(10), '<1m')
        self.assertEqual(OpenBench.eta.describe(60), '~1m')
        self.assertEqual(OpenBench.eta.describe(3600 * 2 + 600), '~2h 10m')
        self.assertEqual(OpenBench.eta.describe(3600 * 76), '~3d 4h')

class GraphActionTests(SimpleTestCase):

    def graph(self, **config):
        return {
            'defaults' : {},
            'nodes' : {
                'stc'   : node('SPRT', dev='x', base='main'),
                'tune'  : node('SPSA', dev='x', spsa_inputs='A, int, 10, 0, 20, 2, 0.002'),
                'merge' : node('MERGE', **config.get('merge', {})),
                'commit': node('COMMIT', **config.get('commit', { 'file' : 'src/Search.cs', 'source' : 'tune' })),
            },
            'edges' : [ edge('stc', 'passed', 'merge'), edge('tune', 'done', 'commit') ],
        }

    def test_valid_actions(self):
        self.assertEqual(Graph.validate(self.graph()), [])
        self.assertEqual(Graph.validate(self.graph(merge={ 'source' : '@stc.base', 'mode' : 'ff', 'target' : 'dev' })), [])
        self.assertEqual(Graph.validate(self.graph(merge={ 'source' : '$branch' })), [])
        self.assertEqual(Graph.validate(self.graph(commit={ 'file' : 'a/b.cs' })), []) # The trigger is the SPSA

    def test_invalid_actions(self):

        cases = [
            ({ 'merge' : { 'mode' : 'rebase' } }, 'merge: Mode must be one of pr, merge, ff'),
            ({ 'merge' : { 'source' : '@tune.tuned' } }, 'merge: Tuned values are options'),
            ({ 'merge' : { 'source' : '@nope.dev' } }, 'merge: Unknown Node "nope"'),
            ({ 'merge' : { 'target' : 'a b' } }, 'merge: Target must be a branch'),
            ({ 'commit' : { 'file' : '../etc/passwd', 'source' : 'tune' } }, 'commit: File must be a path'),
            ({ 'commit' : { 'file' : 'a.cs', 'source' : 'stc' } }, 'commit: Source must be the key of an SPSA'),
            ({ 'commit' : { 'file' : 'a.cs', 'source' : 'tune', 'pattern' : 'no name (\\d+)' } }, 'commit: Pattern must contain {name}'),
            ({ 'commit' : { 'file' : 'a.cs', 'source' : 'tune', 'pattern' : '{name} = \\d+' } }, 'commit: Pattern must contain {name}'),
            ({ 'commit' : { 'file' : 'a.cs', 'source' : 'tune', 'mode' : 'ff' } }, 'commit: Mode must be one of pr, commit'),
        ]

        for config, expected in cases:
            errors = Graph.validate(self.graph(**config))
            self.assertTrue(any(error.startswith(expected) for error in errors), (config, errors))

    def test_actions_need_an_incoming_edge(self):
        graph = self.graph()
        graph['edges'] = [edge('tune', 'done', 'commit')]
        self.assertIn('merge must have an incoming edge', Graph.validate(graph))

    def test_trigger_after_an_action(self):
        graph = self.graph()
        graph['nodes']['after'] = node('SPRT', dev='@trigger.dev', base='main')
        graph['edges'].append(edge('merge', 'done', 'after'))
        self.assertIn('after: Not every incoming Node provides @trigger.dev', Graph.validate(graph))

    def test_same_bench_bounds(self):
        graph = self.graph()
        graph['nodes']['stc']['config']['same_bench_bounds'] = '[-3.00, 1.00]'
        self.assertEqual(Graph.validate(graph), [])
        graph['nodes']['stc']['config']['same_bench_bounds'] = '[1.00, -3.00]'
        self.assertIn('stc: Same Bench Bounds must be formatted as [float1, float2]', Graph.validate(graph))

    def test_apply_values(self):

        text = 'int Futility = 100;\nint LmrBase=7;\nfloat Scale = 1.5; // Futility = 3'
        new, missing = actions.apply_values(text, [('Futility', '120'), ('LmrBase', '8'), ('Scale', '1.25'), ('Gone', '1')], Graph.DEFAULT_PATTERNS)
        self.assertEqual(new, 'int Futility = 120;\nint LmrBase=8;\nfloat Scale = 1.25; // Futility = 3')
        self.assertEqual(missing, ['Gone'])

        # Fispur's Tunables, whose aligned columns stay aligned
        fispur = '\n'.join([
            '            [Tunable(   72,     0,   400)]',
            '            public static readonly int LmrBase = Tuning.resolve();',
            '',
            '            [Tunable(-1165, -4096,     0)]',
            '            public static readonly int PVNodeReduction = Tuning.resolve();',
            '            [Tunable(    9,     1,    16, poisoned = true)]',
            '            public static readonly int RfpMaxDepth = Tuning.resolve();',
        ])
        new, missing = actions.apply_values(fispur, [('LmrBase', '8'), ('PVNodeReduction', '-980'), ('RfpMaxDepth', '10'), ('Rfp', '1')], Graph.DEFAULT_PATTERNS)
        self.assertEqual(missing, ['Rfp'])
        self.assertIn('[Tunable(    8,     0,   400)]', new)
        self.assertIn('[Tunable( -980, -4096,     0)]', new)
        self.assertIn('[Tunable(   10,     1,    16, poisoned = true)]', new)
        self.assertEqual(new.count('Tuning.resolve()'), 3)

        # Names are taken literally, never as a pattern
        self.assertEqual(actions.apply_values('a.b = 1; axb = 2', [('a.b', '5')], Graph.DEFAULT_PATTERNS)[0], 'a.b = 5; axb = 2')

class FakeGithub:

    # Records the writes of the action Nodes, and answers their reads
    def __init__(self, heads=None, files=None):
        self.heads, self.files, self.calls = dict(heads or {}), dict(files or {}), []

    def install(self, test):
        for name in ['commit', 'branch_exists', 'create_branch', 'fast_forward', 'merge', 'open_pull_request', 'read_file', 'write_file']:
            patcher = mock.patch.object(github, name, getattr(self, name))
            patcher.start()
            test.addCleanup(patcher.stop)

    def commit(self, repo, engine, ref):
        sha = self.heads.get(ref, TW.fake_sha(ref))
        return { 'sha' : sha, 'message' : 'Commit\n\nBench: 4242', 'date' : '2026-11-01T00:00:00Z' }

    def branch_exists(self, repo, engine, branch):
        return branch in self.heads

    def create_branch(self, repo, engine, branch, sha):
        self.calls.append(('create_branch', branch, sha))
        self.heads[branch] = sha

    def fast_forward(self, repo, engine, branch, sha):
        self.calls.append(('fast_forward', branch, sha))

    def merge(self, repo, engine, branch, sha, message):
        self.calls.append(('merge', branch, sha, message))
        return { 'sha' : 'f' * 40, 'status' : 201 }

    def open_pull_request(self, repo, engine, head, base, title, body):
        self.calls.append(('pr', head, base, title, body))
        return 'https://github.com/Xenymor/Fispur/pull/1'

    def read_file(self, repo, engine, path, ref):
        return self.files[path], 'blob'

    def write_file(self, repo, engine, path, branch, text, blob, message):
        self.calls.append(('write', path, branch, message))
        self.files[path] = text
        return 'e' * 40

@mock.patch('OpenBench.workloads.verify_workload.collect_github_info', TW.fake_github)
@mock.patch('OpenBench.workloads.create_workload.run_checks', lambda workload, check_base=False: {})
@mock.patch('OpenBench.workflows.runner.lookup_commit', TW.fake_commit)
class FeatureRunnerTests(TestCase):

    setUp       = TW.RunnerTests.setUp
    defaults    = TW.RunnerTests.defaults
    create      = TW.RunnerTests.create
    nodes       = TW.RunnerTests.nodes
    finish_sprt = TW.RunnerTests.finish_sprt

    def step(self, workflow):
        runner.step(workflow.id)
        return self.nodes(workflow)

    # # # Action Nodes

    def merge_graph(self, **config):
        return { 'defaults' : {}, 'edges' : [ edge('stc', 'passed', 'merge') ], 'nodes' : {
            'stc' : node('SPRT', dev='feature', base='main'), 'merge' : node('MERGE', **config) } }

    def test_merge_as_pull_request(self):

        fake = FakeGithub(); fake.install(self)
        workflow = self.create(self.merge_graph())
        nodes    = self.nodes(workflow)
        sha      = nodes['stc'].test.dev.sha

        self.finish_sprt(nodes['stc'].test, passed=True)
        nodes = self.step(workflow)

        self.assertEqual((nodes['merge'].state, nodes['merge'].outcome), ('FINISHED', 'done'))
        self.assertEqual(nodes['merge'].detail, 'https://github.com/Xenymor/Fispur/pull/1')

        # The branch still points at the tested commit, so the pull request uses it
        kind, head, base, title, body = fake.calls[-1]
        self.assertEqual((kind, head, base, title), ('pr', 'feature', 'main', 'feature (Workflow #%d)' % (workflow.id)))
        self.assertIn('stc: passed, Elo', body)
        self.assertTrue(body.endswith('Bench: %d' % (nodes['stc'].test.dev.bench)))
        self.assertEqual(sha, TW.fake_sha('feature'))

    def test_pull_request_of_a_branch_that_moved_on(self):

        fake = FakeGithub(heads={ 'feature' : 'd' * 40 }); fake.install(self)
        workflow = self.create(self.merge_graph())
        nodes    = self.nodes(workflow)
        self.finish_sprt(nodes['stc'].test, passed=True)
        self.step(workflow)

        branch = 'openbench/wf%d-merge' % (workflow.id)
        self.assertEqual(fake.calls[0], ('create_branch', branch, nodes['stc'].test.dev.sha))
        self.assertEqual(fake.calls[1][:3], ('pr', branch, 'main'))

    def test_merge_and_fast_forward_poke_the_ladders(self):

        for mode, expected in [('merge', 'Merged into dev as ffffffff'), ('ff', 'Fast-forwarded dev to ')]:

            fake = FakeGithub(); fake.install(self)
            workflow = self.create(self.merge_graph(mode=mode, target='dev'))
            nodes    = self.nodes(workflow)
            self.finish_sprt(nodes['stc'].test, passed=True)

            with mock.patch.object(ladder, 'request_poll') as poll:
                nodes = self.step(workflow)

            self.assertTrue(nodes['merge'].detail.startswith(expected), nodes['merge'].detail)
            poll.assert_called_once_with('https://github.com/Xenymor/Fispur', 'dev')

    def test_failed_actions_can_be_retried(self):

        fake = FakeGithub(); fake.install(self)
        workflow = self.create(self.merge_graph(mode='ff'))
        nodes    = self.nodes(workflow)
        self.finish_sprt(nodes['stc'].test, passed=True)

        with mock.patch.object(github, 'fast_forward', mock.Mock(side_effect=github.GithubError('Github answered 422: Update is not a fast forward'))):
            nodes = self.step(workflow)

        self.assertEqual(nodes['merge'].state, 'ERROR')
        self.assertIn('not a fast forward', nodes['merge'].error)
        self.assertEqual(workflow.__class__.objects.get(id=workflow.id).status, 'ATTENTION')

        runner.retry(workflow.id, 'merge')
        self.assertEqual(self.nodes(workflow)['merge'].state, 'FINISHED')

    def test_commit_tuned_values(self):

        source = 'class Search {\n    public static int A = 10;\n    public static int B = 3;\n}\n'
        fake   = FakeGithub(files={ 'src/Search.cs' : source }); fake.install(self)

        graph = { 'defaults' : {}, 'edges' : [ edge('tune', 'done', 'commit') ], 'nodes' : {
            'tune'   : node('SPSA', dev='feature', spsa_inputs='A, int, 10, 0, 20, 2, 0.002\nB, int, 3, 0, 9, 1, 0.002'),
            'commit' : node('COMMIT', file='src/Search.cs', mode='pr') } }

        workflow = self.create(graph)
        tune     = self.nodes(workflow)['tune'].test
        tune.spsa_run.parameters.filter(name='A').update(value=12.4)
        tune.games, tune.finished = 2 * 2 * 10, True
        tune.save()

        nodes  = self.step(workflow)
        branch = 'openbench/tuned-wf%d-commit' % (workflow.id)

        self.assertEqual(nodes['commit'].state, 'FINISHED', nodes['commit'].error)
        self.assertEqual(fake.files['src/Search.cs'], source.replace('A = 10', 'A = 12'))
        self.assertEqual(fake.calls[0], ('create_branch', branch, tune.dev.sha))
        self.assertEqual(fake.calls[1][:3], ('write', 'src/Search.cs', branch))
        self.assertEqual(fake.calls[2][:3], ('pr', branch, 'main'))

        # Running again changes nothing in the file, which already holds the values
        self.assertEqual(actions.apply_values(fake.files['src/Search.cs'], [('A', '12'), ('B', '3')], Graph.DEFAULT_PATTERNS)[0], fake.files['src/Search.cs'])

    def test_commit_with_missing_names_fails(self):

        fake = FakeGithub(files={ 'a.cs' : 'int B = 1;' }); fake.install(self)
        graph = { 'defaults' : {}, 'edges' : [ edge('tune', 'done', 'commit') ], 'nodes' : {
            'tune'   : node('SPSA', dev='feature', spsa_inputs='A, int, 10, 0, 20, 2, 0.002'),
            'commit' : node('COMMIT', file='a.cs', mode='commit') } }

        workflow = self.create(graph)
        tune     = self.nodes(workflow)['tune'].test
        tune.games, tune.finished = 40, True
        tune.save()

        nodes = self.step(workflow)
        self.assertEqual(nodes['commit'].state, 'ERROR')
        self.assertEqual(nodes['commit'].error, 'Not found in a.cs: A')

    # # # Same Bench

    def test_same_bench_switches_to_non_regression_bounds(self):

        graph = { 'defaults' : {}, 'edges' : [], 'nodes' : {
            'same'  : node('SPRT', dev='abc', base='xyz', same_bench_bounds='[-3.00, 1.00]'), # Equal length, equal Bench
            'other' : node('SPRT', dev='abcd', base='xyz', same_bench_bounds='[-3.00, 1.00]') } }

        nodes = self.nodes(self.create(graph))
        self.assertEqual((nodes['same'].test.elolower, nodes['same'].test.eloupper), (-3.0, 1.0))
        self.assertIn('[Same Bench, so bounds [-3.00, 1.00]]', nodes['same'].test.info)
        self.assertEqual((nodes['other'].test.elolower, nodes['other'].test.eloupper), (0.0, 3.0))

    def test_pinned_bases_are_checked(self):

        calls = []
        graph = { 'defaults' : {}, 'edges' : [ edge('a', 'passed', 'b') ], 'nodes' : {
            'a' : node('SPRT', dev='x', base='main'), 'b' : node('SPRT', dev='@a.dev', base='@a.base') } }

        with mock.patch('OpenBench.workloads.create_workload.run_checks', lambda test, check_base=False: calls.append(check_base) or {}):
            workflow = self.create(graph)
            self.finish_sprt(self.nodes(workflow)['a'].test, passed=True)
            self.step(workflow)

        self.assertEqual(calls, [True, False]) # Only a Base pinned from a branch can have moved on

    # # # Errors while playing

    def report(self, test, machine, crashes=0, timelosses=0, illegals=0, penta='0 1 2 1 0'):
        result = Result.objects.get_or_create(test=test, machine=machine)[0]
        post   = { 'crashes' : str(crashes), 'timelosses' : str(timelosses), 'illegals' : str(illegals),
                   'machine_id' : str(machine.id), 'result_id' : str(result.id), 'test_id' : str(test.id),
                   'trinomial' : '0 8 0', 'pentanomial' : penta }
        sent = []
        with mock.patch.object(notifications, 'send', lambda build: sent.extend(build(''))), \
             self.captureOnCommitCallbacks(execute=True):
            answer = OpenBench.utils.update_test(SimpleNamespace(POST=post), machine)
        return answer, [message['embeds'][0]['description'] for message in sent]

    def machine(self):
        info = { 'client_ver' : OPENBENCH_CONFIG['client_version'], 'OPENBENCH_CONFIG_CHECKSUM' : ServerState.checksum() }
        return Machine.objects.create(user=self.user, secret='s', info=info)

    def test_crashes_stop_a_workflow_test(self):

        workflow = self.create({ 'defaults' : {}, 'edges' : [], 'nodes' : { 'a' : node('SPRT', dev='a', base='main') } })
        test     = self.nodes(workflow)['a'].test
        machine  = self.machine()

        answer, sent = self.report(test, machine, crashes=1)
        self.assertEqual(answer, {})
        self.assertEqual(sent, ['First error since the start: 1 crash, 0 time losses, 0 illegal moves'])

        answer, sent = self.report(test, machine, crashes=2)
        self.assertEqual((answer, sent), ({ 'stop' : True }, [])) # The Workflow announces the stop itself

        test = Test.objects.get(id=test.id)
        self.assertEqual((test.crashes, test.finished, test.stop_reason), (3, True, 'Stopped by OpenBench: 3 crashes or stalls'))

        nodes = self.step(workflow)
        self.assertEqual((nodes['a'].state, nodes['a'].error), ('STOPPED', 'Stopped by OpenBench: 3 crashes or stalls'))

    def test_restart_resets_the_limits(self):

        workflow = self.create({ 'defaults' : {}, 'edges' : [], 'nodes' : { 'a' : node('SPRT', dev='a', base='main') } })
        test     = self.nodes(workflow)['a'].test
        machine  = self.machine()
        self.report(test, machine, illegals=1)

        self.client.force_login(self.user)
        self.client.get('/test/%d/RESTART/' % (test.id))
        test = Test.objects.get(id=test.id)
        self.assertFalse(test.finished)
        self.assertEqual(test.stop_reason, '')

        answer, sent = self.report(test, machine)
        self.assertEqual(answer, {})

    # # # SPSA convergence

    def tune(self, auto_stop):
        graph = { 'defaults' : {}, 'edges' : [], 'nodes' : {
            'tune' : node('SPSA', dev='feature', spsa_inputs='A, int, 10, 0, 100, 2, 0.002', spsa_auto_stop=str(auto_stop)) } }
        workflow = self.create(graph)
        return workflow, self.nodes(workflow)['tune'].test

    def play(self, test, values):

        # One snapshot for each value, as update_test() records them
        run    = test.spsa_run
        params = list(run.parameters.all())
        step   = spsa_history.interval(run)

        for value in values:
            params[0].value = value
            params[0].save() # As update_test() saves them before
            test.games += step
            if spsa_history.record(test, params):
                return True
        return False

    def test_convergence_stops_the_tune(self):

        workflow, test = self.tune(auto_stop=0.5)
        test.spsa_run.iterations = 1000 # 4000 games, a snapshot every 20
        test.spsa_run.save()

        # Still moving: never converged, however long
        self.assertFalse(self.play(test, range(10, 100)))

        # Settled: converged once the window holds only settled values
        self.assertTrue(self.play(test, [99.5, 100.0] * 40))
        self.assertTrue(test.finished)
        self.assertIsNotNone(test.spsa_run.converged_at)
        test.save()

        nodes = self.step(workflow)
        self.assertEqual((nodes['tune'].state, nodes['tune'].outcome), ('FINISHED', 'done'))
        self.assertEqual(nodes['tune'].tuned, 'A=100')

    def test_no_auto_stop_without_threshold(self):

        workflow, test = self.tune(auto_stop=0)
        test.spsa_run.iterations = 1000
        test.spsa_run.save()

        self.assertFalse(self.play(test, [50] * 150))
        self.assertGreater(SPSASnapshot.objects.filter(spsa_run=test.spsa_run).count(), 100)

        history = spsa_history.history(test)
        self.assertEqual(history['params'][0]['values'][0], 10.0) # Starts at the start
        self.assertEqual(history['params'][0]['values'][-1], 50)
        self.assertEqual(history['drift'], 0.0)

    def test_snapshots_are_spaced(self):

        workflow, test = self.tune(auto_stop=0)
        run, params = test.spsa_run, list(test.spsa_run.parameters.all())

        test.games = 1
        self.assertFalse(spsa_history.record(test, params))
        self.assertEqual(SPSASnapshot.objects.count(), 0) # Too soon

        test.games = spsa_history.interval(run)
        spsa_history.record(test, params)
        self.assertEqual(SPSASnapshot.objects.count(), 1)

    # # # Workflow ETA

    def test_workflow_eta(self):

        graph = { 'defaults' : {}, 'nodes' : {
            'stc' : node('SPRT', dev='x', base='main'),
            'ltc' : node('SPRT', dev='@trigger.dev', base='main', time_control='60+0.6'),
            'merge' : node('MERGE'),
        }, 'edges' : [ edge('stc', 'passed', 'ltc'), edge('ltc', 'passed', 'merge') ] }

        workflow = self.create(graph)
        now      = timezone.now()
        Test.objects.filter(id=self.nodes(workflow)['stc'].test_id).update(
            games=4000, currentllr=1.47, rate_games=600.0, rate_time=3600.0, rate_updated=now) # 4000 more games at 10 per minute

        with mock.patch.object(OpenBench.eta, 'typical_seconds', lambda mode, control, games=None: 3600.0 if control == '60.0+0.60' else None):
            times = WorkflowEta.estimate(workflow.graph, self.nodes(workflow), now)

        stc = Test.objects.get(id=self.nodes(workflow)['stc'].test_id)
        self.assertAlmostEqual((times['stc']['finish'] - now).total_seconds(), 4000 * (stc.upperllr / 1.47 - 1) * 6.0, delta=6)
        self.assertEqual(times['ltc']['start'], times['stc']['finish'])
        self.assertAlmostEqual((times['ltc']['finish'] - times['ltc']['start']).total_seconds(), 3600.0)
        self.assertEqual(times['merge']['start'], times['ltc']['finish'])
        self.assertEqual(times['merge']['finish'], times['merge']['start'])

        # The state of the page carries them, and the end of the Workflow at the earliest
        self.client.force_login(self.user)
        with mock.patch.object(OpenBench.eta, 'typical_seconds', lambda mode, control, games=None: 3600.0):
            state = self.client.get('/workflow/%d/state/' % (workflow.id)).json()
        self.assertIsNotNone(state['nodes']['ltc']['eta']['start'])
        self.assertIsNotNone(state['finish'])

    def test_unknown_eta_propagates(self):

        graph = { 'defaults' : {}, 'nodes' : { 'stc' : node('SPRT', dev='x', base='main'), 'ltc' : node('SPRT', dev='@trigger.dev', base='main') },
                  'edges' : [ edge('stc', 'passed', 'ltc') ] }
        workflow = self.create(graph)
        times    = WorkflowEta.estimate(workflow.graph, self.nodes(workflow), timezone.now())
        self.assertEqual(times['ltc'], { 'start' : None, 'finish' : None })

    # # # Ladders

    def make_ladder(self, fake, **fields):
        fields = { 'name' : 'Main progress', 'engine' : 'Fispur', 'games' : '1000', **fields }
        created, errors = ladder.create_ladder(self.user, fields)
        self.assertIsNone(errors)
        return created

    def test_ladder_measures_new_commits(self):

        fake = FakeGithub(heads={ 'main' : TW.fake_sha('anchor') }); fake.install(self)
        created = self.make_ladder(fake)

        self.assertEqual((created.anchor_sha, created.anchor_bench, created.branch), (TW.fake_sha('anchor'), 4242, 'main'))
        self.assertEqual(created.repo, 'https://github.com/Xenymor/Fispur')

        # Nothing to measure while main is the anchor
        self.assertIsNone(ladder.measure(created))

        fake.heads['main'] = TW.fake_sha('newer')
        point = ladder.measure(created)
        test  = point.test

        self.assertEqual(point.error, '')
        self.assertEqual((test.test_mode, test.max_games), ('GAMES', 1000))
        self.assertEqual((test.dev.sha, test.base.sha, test.base.bench), (TW.fake_sha('newer'), TW.fake_sha('anchor'), 4242))
        self.assertEqual(test.dev.name, 'main@%s' % (TW.fake_sha('newer')[:8]))
        self.assertTrue(test.info.startswith('[Ladder #%d: Main progress]' % (created.id)))

        # The same commit is measured once
        self.assertIsNone(ladder.measure(created))

    def test_ladder_polling_waits_for_running_tests(self):

        fake = FakeGithub(heads={ 'main' : TW.fake_sha('anchor') }); fake.install(self)
        created = self.make_ladder(fake, poll_hours='6')

        fake.heads['main'] = TW.fake_sha('one')
        ladder.poll_all()
        self.assertEqual(created.points.count(), 1)

        # Not due again for 6 hours, unless asked to look
        fake.heads['main'] = TW.fake_sha('two')
        ladder.poll_all()
        self.assertEqual(created.points.count(), 1)

        ladder.request_poll('https://github.com/Xenymor/Fispur/', 'main')
        ladder.poll_all()
        self.assertEqual(created.points.count(), 1) # The first Test still runs

        Test.objects.filter(id=created.points.first().test_id).update(finished=True)
        ladder.poll_all()
        self.assertEqual(created.points.count(), 2)

        rows = ladder.points(created)
        self.assertEqual([row['sha'] for row in rows], [TW.fake_sha('one'), TW.fake_sha('two')])
        self.assertIn('elo', rows[0])

    def test_ladder_creation_errors(self):

        fake = FakeGithub(); fake.install(self)
        self.assertIn('Unknown Engine', ladder.create_ladder(self.user, { 'name' : 'x', 'engine' : 'Nope' })[1])
        self.assertIn('A Ladder needs a name of at most 128 characters', ladder.create_ladder(self.user, { 'name' : '', 'engine' : 'Fispur' })[1])

        with mock.patch.object(github, 'commit', lambda repo, engine, ref: { 'sha' : 'a' * 40, 'message' : 'no bench', 'date' : '' }):
            errors = ladder.create_ladder(self.user, { 'name' : 'x', 'engine' : 'Fispur' })[1]
            self.assertTrue(errors[0].startswith('Unable to find a Bench for the anchor'))
            self.assertIsNone(ladder.create_ladder(self.user, { 'name' : 'x', 'engine' : 'Fispur', 'anchor_bench' : '123' })[1])

    def test_failed_ladder_points_are_kept(self):

        fake = FakeGithub(heads={ 'main' : TW.fake_sha('anchor') }); fake.install(self)
        created = self.make_ladder(fake)
        fake.heads['main'] = 'missing'

        with mock.patch.object(notifications, 'send', lambda build: None):
            point = ladder.measure(created)

        self.assertIn('could not be found', point.error)
        self.assertIsNone(point.test)

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                          PUSH-TO-TEST AND THE PAGES                         #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

import hashlib
import hmac
import json

from OpenBench import automation
from OpenBench.models import PushEvent, Workflow, WorkflowTemplate

SECRET = 'top-secret'

def signed(body, secret=SECRET):
    return 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

def push_payload(branch='feature', message='Try LMR [ob]', repo='https://github.com/Xenymor/Fispur', sha='a' * 40):
    return json.dumps({ 'ref' : 'refs/heads/%s' % (branch), 'after' : sha, 'deleted' : False,
                        'repository' : { 'html_url' : repo }, 'head_commit' : { 'message' : message } }).encode()

class AutomationUnitTests(SimpleTestCase):

    def test_markers(self):
        self.assertEqual(automation.markers('Fix [ob] and [OB:ltc] [ob:ltc] [ob:smp-1.0]'), ['default', 'ltc', 'smp-1.0'])
        self.assertEqual(automation.markers('No marker [ob:] [obx]'), [])

    def test_substitute(self):
        graph = { 'a' : { 'config' : { 'dev' : '$branch', 'title' : 'Merge $branch', 'n' : 3 }, 'label' : 'STC $branch' } }
        self.assertEqual(automation.substitute(graph, 'lmr'),
                         { 'a' : { 'config' : { 'dev' : 'lmr', 'title' : 'Merge lmr', 'n' : 3 }, 'label' : 'STC lmr' } })

    def test_signatures(self):
        body = b'{}'
        self.assertTrue(automation.verify_signature(SECRET, body, signed(body)))
        self.assertFalse(automation.verify_signature(SECRET, body, signed(body, 'other')))
        self.assertFalse(automation.verify_signature(SECRET, body, ''))

@mock.patch('OpenBench.workloads.verify_workload.collect_github_info', TW.fake_github)
@mock.patch('OpenBench.workloads.create_workload.run_checks', lambda workload, check_base=False: {})
@mock.patch('OpenBench.workflows.runner.lookup_commit', TW.fake_commit)
class AutomationTests(TestCase):

    setUp    = TW.RunnerTests.setUp
    defaults = TW.RunnerTests.defaults

    def template(self, name='default', **extra):
        graph = { 'defaults' : self.defaults(), 'edges' : [ edge('stc', 'passed', 'merge') ], 'nodes' : {
            'stc'   : node('SPRT', dev='$branch', base='main'),
            'merge' : node('MERGE', source='@stc.dev') } }
        graph['nodes']['stc']['label'] = 'STC $branch'
        return WorkflowTemplate.objects.create(name=name, author=self.user.username, graph=graph, **extra)

    def post_push(self, body, event='push', secret=SECRET, delivery='d1'):
        with mock.patch.object(automation, 'read_secret', lambda: SECRET):
            return self.client.post('/api/github/push/', body, content_type='application/json',
                HTTP_X_HUB_SIGNATURE_256=signed(body, secret), HTTP_X_GITHUB_EVENT=event, HTTP_X_GITHUB_DELIVERY=delivery)

    def process(self):
        sent = []
        with mock.patch.object(notifications, 'send', lambda build: sent.extend(build(''))), \
             mock.patch.object(automation.ladder, 'request_poll') as poll:
            automation.process_pushes()
        return [message['embeds'][0]['title'] for message in sent], poll

    def test_webhook_queues_pushes(self):

        body = push_payload()
        self.assertEqual(self.post_push(body).status_code, 202)
        self.assertEqual(self.post_push(body).json(), { 'queued' : False }) # Redelivered
        self.assertEqual(self.post_push(body, secret='wrong', delivery='d2').status_code, 403)
        self.assertEqual(self.post_push(b'{}', event='ping', delivery='d3').json(), { 'pong' : True })
        self.assertEqual(self.post_push(push_payload().replace(b'refs/heads/', b'refs/tags/'), delivery='d4').status_code, 200)

        event = PushEvent.objects.get()
        self.assertEqual((event.branch, event.repo, event.sha, event.processed), ('feature', 'https://github.com/Xenymor/Fispur', 'a' * 40, False))

        with mock.patch.object(automation, 'read_secret', lambda: None):
            self.assertEqual(self.client.post('/api/github/push/', body, content_type='application/json').status_code, 404)

    def test_push_starts_the_template(self):

        self.template()
        self.post_push(push_payload())
        sent, poll = self.process()

        workflow = Workflow.objects.get()
        self.assertEqual(workflow.name, 'default: feature')
        self.assertEqual(workflow.graph['nodes']['stc']['config']['dev'], 'feature')
        self.assertEqual(workflow.graph['nodes']['stc']['label'], 'STC feature')
        self.assertEqual(workflow.nodes.get(key='stc').test.dev.name, 'feature')

        event = PushEvent.objects.get()
        self.assertEqual((event.processed, event.result), (True, 'Started Workflow #%d' % (workflow.id)))
        self.assertEqual(sent, ['Push of feature started Workflow #%d' % (workflow.id)])
        poll.assert_called_once_with('https://github.com/Xenymor/Fispur', 'feature')

        # Processed once only
        self.assertEqual(self.process()[0], [])

    def test_pushes_that_start_nothing(self):

        self.template(name='ltc', enabled=False)
        other = self.template(name='other')
        other.graph['defaults']['dev_repo'] = 'https://github.com/someone/else'
        other.save()

        for number, message in enumerate(['No marker', 'Disabled [ob:ltc]', 'Other repo [ob:other]', 'Unknown [ob:nope]']):
            self.post_push(push_payload(message=message), delivery='d%d' % (number))

        sent, poll = self.process()
        results = list(PushEvent.objects.order_by('id').values_list('result', flat=True))
        self.assertEqual(results, ['No markers', 'No enabled Template "ltc"', 'Template other is for another repository', 'No enabled Template "nope"'])
        self.assertEqual(Workflow.objects.count(), 0)
        self.assertEqual(len(sent), 2) # The unknown Templates need attention

    def test_failing_templates_are_announced(self):

        self.template()
        self.post_push(push_payload(branch='missing'))
        sent, poll = self.process()

        self.assertEqual(Workflow.objects.count(), 0)
        self.assertTrue(PushEvent.objects.get().result.startswith('Template default failed: STC missing'), PushEvent.objects.get().result)
        self.assertEqual(sent, ['Push of missing could not start Template default'])

    def test_verify_template(self):

        graph = self.template().graph
        self.assertEqual(automation.verify_template('ltc', graph), [])
        self.assertIn('Template names are', automation.verify_template('a b', graph)[0])

        attached = json.loads(json.dumps(graph))
        attached['nodes']['stc']['config']['existing_test'] = '5'
        attached['edges'] = []
        attached['nodes'].pop('merge')
        self.assertIn('Templates cannot attach existing Tests', automation.verify_template('x', attached)[0])

        unknown = json.loads(json.dumps(graph))
        unknown['defaults']['dev_repo'] = ''
        self.assertIn('Dev Source', automation.verify_template('x', unknown)[0])

    def test_template_pages(self):

        self.client.force_login(self.user)
        template = self.template()

        self.assertContains(self.client.get('/workflow/templates/'), '[ob]')
        self.assertContains(self.client.get('/workflow/templates/default/edit/'), 'Save Template')
        self.assertContains(self.client.get('/workflow/templates/new/?from=default'), 'copy-of-default')

        # Saving from the editor, which posts JSON
        graph  = dict(template.graph)
        answer = self.client.post('/workflow/templates/new/', json.dumps({ 'name' : 'ltc', 'graph' : graph }), content_type='application/json')
        self.assertEqual(answer.json(), { 'redirect' : '/workflow/templates/' })
        answer = self.client.post('/workflow/templates/new/', json.dumps({ 'name' : 'LTC', 'graph' : graph }), content_type='application/json')
        self.assertEqual(answer.json(), { 'errors' : ['A Template named LTC already exists'] })

        # Started by hand, disabled, and deleted
        answer   = self.client.post('/workflow/templates/ltc/start/', { 'branch' : 'lmr' })
        workflow = Workflow.objects.get(name='ltc: lmr')
        self.assertRedirects(answer, '/workflow/%d/' % (workflow.id), fetch_redirect_response=False)

        self.client.post('/workflow/templates/ltc/toggle/')
        self.assertFalse(WorkflowTemplate.objects.get(name='ltc').enabled)
        self.client.post('/workflow/templates/ltc/delete/')
        self.assertFalse(WorkflowTemplate.objects.filter(name='ltc').exists())

        # A Workflow can be saved as a Template, without its attached Tests
        self.assertContains(self.client.get('/workflow/%d/' % (workflow.id)), 'Save as Template')
        self.assertContains(self.client.get('/workflow/templates/new/?from=%d' % (workflow.id)), 'Create Template')

    def test_other_users_cannot_change_templates(self):
        other = User.objects.create_user('other', password='x')
        Profile.objects.create(user=other, enabled=True)
        self.template()
        self.client.force_login(other)
        self.client.post('/workflow/templates/default/delete/')
        self.assertTrue(WorkflowTemplate.objects.filter(name='default').exists())

@mock.patch('OpenBench.workloads.verify_workload.collect_github_info', TW.fake_github)
@mock.patch('OpenBench.workloads.create_workload.run_checks', lambda workload, check_base=False: { 'same_bench' : True, 'behind' : 3 })
@mock.patch('OpenBench.workflows.runner.lookup_commit', TW.fake_commit)
class PageTests(TestCase):

    setUp    = TW.RunnerTests.setUp
    defaults = TW.RunnerTests.defaults
    create   = TW.RunnerTests.create
    nodes    = TW.RunnerTests.nodes

    def sprt(self, dev='lmr'):
        workflow = self.create({ 'defaults' : {}, 'edges' : [], 'nodes' : { 'a' : node('SPRT', dev=dev, base='main') } })
        return self.nodes(workflow)['a'].test

    def test_workload_page(self):

        test  = self.sprt()
        older = self.sprt()
        Test.objects.filter(id=test.id).update(crashes=2, tags='search lmr', notes='Reduce more', stop_reason='Stopped by OpenBench: 3 crashes', finished=True)

        self.client.force_login(self.user)
        page = self.client.get('/test/%d/' % (test.id))
        self.assertContains(page, 'Dev and Base have the same Bench')
        self.assertContains(page, 'Dev is 3 commits behind Base')
        self.assertContains(page, '2 crashes, 0 time losses, 0 illegal moves')
        self.assertContains(page, 'Stopped by OpenBench: 3 crashes')
        self.assertContains(page, '/search/?tags=search')
        self.assertContains(page, 'Earlier Workloads of lmr')
        self.assertContains(page, 'href="/test/%d/"' % (older.id))

    def test_tags_notes_and_search(self):

        test = self.sprt()
        self.client.force_login(self.user)
        self.client.post('/test/%d/MODIFY/' % (test.id), { 'tags' : 'lmr, search lmr', 'notes' : 'First try', 'priority' : '0', 'throughput' : '1000' })

        test = Test.objects.get(id=test.id)
        self.assertEqual((test.tags, test.notes), ('lmr search', 'First try'))

        self.assertContains(self.client.get('/search/?tags=search+LMR'), 'href="/test/%d/"' % (test.id))
        self.assertNotContains(self.client.get('/search/?tags=searc'), 'href="/test/%d/"' % (test.id))
        self.assertContains(self.client.get('/search/?notes-contains=first'), 'href="/test/%d/"' % (test.id))

        # Tags are matched whole, whatever characters they hold
        Test.objects.filter(id=test.id).update(tags='c++ a.b')
        self.assertContains(self.client.get('/search/?tags=C%2B%2B'), 'href="/test/%d/"' % (test.id))
        self.assertContains(self.client.get('/search/?tags=a.b'), 'href="/test/%d/"' % (test.id))
        self.assertNotContains(self.client.get('/search/?tags=a'), 'href="/test/%d/"' % (test.id))

    def test_changelog(self):

        passed = self.sprt('lmr')
        simple = self.sprt('simplify')
        failed = self.sprt('worse')
        Test.objects.filter(id=passed.id).update(passed=True, finished=True, notes='Reduce more\nDetails')
        Test.objects.filter(id=simple.id).update(passed=True, finished=True, elolower=-3.0, eloupper=1.0)
        Test.objects.filter(id=failed.id).update(failed=True, finished=True)

        self.client.force_login(self.user)
        page = self.client.get('/changelog/?engine=Fispur&since=2000-01-01')
        self.assertContains(page, '1 gainer, 1 simplification')
        self.assertContains(page, '- lmr: ')
        self.assertContains(page, '(Test #%d) - Reduce more' % (passed.id))
        self.assertContains(page, '## Simplifications')
        self.assertNotContains(page, 'worse')

        # A tag or branch is resolved to the date of its commit
        with mock.patch.object(github, 'commit', lambda repo, engine, ref: { 'sha' : 'a' * 40, 'message' : '', 'date' : '2999-01-01T00:00:00Z' }):
            self.assertContains(self.client.get('/changelog/?engine=Fispur&since=v1.0'), 'No SPRT passed in that time')

    def test_ladder_pages(self):

        fake = FakeGithub(heads={ 'main' : TW.fake_sha('anchor') }); fake.install(self)
        self.client.force_login(self.user)

        self.assertContains(self.client.get('/ladders/'), 'Create a Ladder')
        answer  = self.client.post('/ladders/', { 'name' : 'Main', 'engine' : 'Fispur', 'branch' : 'main', 'games' : '500', 'poll_hours' : '6' })
        created = Ladder.objects.get()
        self.assertRedirects(answer, '/ladder/%d/' % (created.id), fetch_redirect_response=False)

        fake.heads['main'] = TW.fake_sha('newer')
        self.client.post('/ladder/%d/measure/' % (created.id))
        point = created.points.get()

        page = self.client.get('/ladder/%d/' % (created.id))
        self.assertContains(page, 'Elo against the anchor')
        self.assertContains(page, TW.fake_sha('newer')[:8])
        self.assertContains(self.client.get('/test/%d/' % (point.test_id)), '#%d Main' % (created.id))
        self.assertContains(self.client.get('/ladders/'), 'Main')

        self.client.post('/ladder/%d/toggle/' % (created.id))
        self.assertFalse(Ladder.objects.get().enabled)
        self.client.post('/ladder/%d/delete/' % (created.id))
        self.assertFalse(Ladder.objects.exists())
        self.assertTrue(Test.objects.filter(id=point.test_id).exists())

    def test_spsa_history_api(self):

        graph = { 'defaults' : {}, 'edges' : [], 'nodes' : { 'tune' : node('SPSA', dev='x', spsa_inputs='A, int, 10, 0, 100, 2, 0.002') } }
        test  = self.nodes(self.create(graph))['tune'].test
        SPSASnapshot.objects.create(spsa_run=test.spsa_run, games=40, values=[12.0])

        self.client.force_login(self.user)
        data = self.client.get('/api/spsa/%d/history/' % (test.id)).json()
        self.assertEqual(data['games'], [0, 40])
        self.assertEqual(data['params'][0]['values'], [10.0, 12.0])
        self.assertContains(self.client.get('/tune/%d/' % (test.id)), 'Show SPSA History')

    def test_sidebar_links(self):
        self.client.force_login(self.user)
        page = self.client.get('/index/')
        for link in ['/workflow/templates/', '/ladders/', '/changelog/']:
            self.assertContains(page, 'href="%s"' % (link))

@mock.patch('OpenBench.workloads.verify_workload.collect_github_info', TW.fake_github)
@mock.patch('OpenBench.workloads.create_workload.run_checks', lambda workload, check_base=False: {})
@mock.patch('OpenBench.workflows.runner.lookup_commit', TW.fake_commit)
class FollowUpTests(TestCase):

    setUp    = TW.RunnerTests.setUp
    defaults = TW.RunnerTests.defaults
    create   = TW.RunnerTests.create
    nodes    = TW.RunnerTests.nodes

    def test_ladder_anchor_defaults_to_the_head_and_can_change(self):

        fake = FakeGithub(heads={ 'main' : TW.fake_sha('head') }); fake.install(self)
        created, errors = ladder.create_ladder(self.user, { 'name' : 'Main', 'engine' : 'Fispur', 'games' : '100' })
        self.assertEqual(created.anchor_sha, TW.fake_sha('head')) # The current main

        fake.heads['main'] = TW.fake_sha('newer')
        first = ladder.measure(created)
        self.assertEqual((first.anchor, ladder.measure(created)), (TW.fake_sha('head'), None))

        # A new anchor by its sha: the head is measured again against it, and the old point leaves the line
        self.client.force_login(self.user)
        self.client.post('/ladder/%d/anchor/' % (created.id), { 'anchor' : TW.fake_sha('older'), 'anchor_bench' : '777' })
        created = Ladder.objects.get(id=created.id)
        self.assertEqual((created.anchor_sha, created.anchor_bench, created.last_polled), (TW.fake_sha('older'), 777, None))

        second = ladder.measure(created)
        self.assertEqual((second.anchor, second.test.base.sha, second.test.base.bench), (TW.fake_sha('older'), TW.fake_sha('older'), 777))
        self.assertEqual([row['current'] for row in ladder.points(created)], [False, True])

        self.assertEqual(ladder.set_anchor(created, ''), ['Give the sha, branch or tag of the new anchor'])
        self.assertEqual(ladder.set_anchor(created, 'x', 'abc'), ['The anchor Bench must be a positive number, or empty to autofill it'])

    def test_auto_stop_of_running_tunes(self):

        graph = { 'defaults' : {}, 'edges' : [], 'nodes' : { 'tune' : node('SPSA', dev='x', spsa_inputs='A, int, 10, 0, 100, 2, 0.002') } }
        test  = self.nodes(self.create(graph))['tune'].test
        self.assertEqual(test.spsa_run.auto_stop, 0.0)

        self.client.force_login(self.user)
        self.assertContains(self.client.get('/tune/%d/' % (test.id)), 'name="auto_stop"')
        self.client.post('/tune/%d/MODIFY/' % (test.id), { 'auto_stop' : '0.75', 'priority' : '0', 'throughput' : '1000' })
        self.assertEqual(Test.objects.get(id=test.id).spsa_run.auto_stop, 0.75)

    def test_history_must_cover_the_window(self):

        graph = { 'defaults' : {}, 'edges' : [], 'nodes' : { 'tune' : node('SPSA', dev='x', spsa_inputs='A, int, 10, 0, 100, 2, 0.002') } }
        run   = self.nodes(self.create(graph))['tune'].test.spsa_run
        run.iterations = 1000 # 4000 games, so the window is 800 games
        params = list(run.parameters.all())

        # A tune whose history only started late, at 3400 of 4000 games: settled, but not long enough known
        late = [(games, [50.0]) for games in range(3400, 3801, 20)]
        self.assertIsNone(spsa_history.window_drift(run, params, late))

        full = [(games, [50.0]) for games in range(2000, 3801, 20)]
        self.assertEqual(spsa_history.window_drift(run, params, full), 0.0)
