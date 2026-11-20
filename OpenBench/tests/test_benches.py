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

from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase

import OpenBench.views # Before the workload modules, which only import cleanly once it is loaded

from OpenBench.models import Engine, EngineConfig, LogEvent, Network, Profile, Test
from OpenBench.workloads import verify_workload

SHA  = 'a' * 40
REPO = 'https://github.com/Xenymor/Fispur'
API  = 'https://api.github.com/repos/Xenymor/Fispur'

def fake_commit(message, sha=SHA):

    # Stands in for resolve_commit(): every branch resolves to sha, with this commit message
    def resolve(errors, post, field):
        if post['%s_branch' % (field)] == 'missing':
            errors.append('missing could not be found')
            return None
        return API, sha, message
    return resolve

class FakeGithub:

    # Stands in for requests.get(), answering from { url : (status, json) }, and recording each url
    def __init__(self, answers):
        self.answers, self.urls = answers, []

    def __call__(self, url, headers=None, timeout=None):
        self.urls.append(url)
        status, data = self.answers.get(url, (404, { 'message' : 'Not Found' }))
        return SimpleNamespace(status_code=status, json=lambda: data, text=str(data), headers={})

class BenchCarryOverTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user('Xenymor', password='x')
        Profile.objects.create(user=self.user, enabled=True)
        EngineConfig.objects.create(name='Fispur', source=REPO, nps=1000000)

    def workload(self, dev_bench, base_bench=1000, dev_network='', games=100, sha=SHA, tune=False):

        dev  = Engine.objects.create(name='dev', source='', sha=sha, bench=dev_bench)
        base = dev if tune else Engine.objects.create(name='base', source='', sha='b' * 40, bench=base_bench)

        return Test.objects.create(
            author='Xenymor', book_name='UHO.epd', games=games, test_mode='SPSA' if tune else 'SPRT',
            dev=dev,   dev_engine='Fispur',  dev_repo=REPO,  dev_network=dev_network,
            base=base, base_engine='Fispur', base_repo=REPO, base_network=dev_network if tune else '')

    def wrong_bench(self, test, side, measured):
        binary = 'Fispur-%s' % (getattr(test, side).sha.upper()[:8])
        LogEvent.objects.create(author='Xenymor', summary='[%s] Wrong Bench: %d' % (binary, measured),
                                log_file='', machine_id=7, test_id=test.id)

    def post(self, **fields):
        post = { 'dev_branch' : 'main', 'dev_repo' : REPO, 'dev_engine' : 'Fispur',
                 'dev_network' : '', 'dev_bench' : 'Autofill', 'info' : '' }
        return SimpleNamespace(POST=dict(post, **fields))

    def collect(self, message='Merge branch', sha=SHA, **fields):
        errors = []
        with mock.patch.object(verify_workload, 'resolve_commit', fake_commit(message, sha)):
            info = verify_workload.collect_github_info(errors, self.post(**fields), 'dev')
        return (info[3] if info else None), errors

    # Finding earlier Benches

    def test_known_bench_needs_the_same_commit_and_network(self):

        self.workload(111)
        self.workload(222, dev_network='aaaa1111')

        self.assertEqual(verify_workload.known_bench('Fispur', SHA, '')[0], 111)
        self.assertEqual(verify_workload.known_bench('Fispur', SHA, 'aaaa1111')[0], 222)
        self.assertIsNone(verify_workload.known_bench('Fispur', SHA, 'bbbb2222'))
        self.assertIsNone(verify_workload.known_bench('Other', SHA, ''))
        self.assertIsNone(verify_workload.known_bench('Fispur', 'c' * 40, ''))

        # The base of a Test counts as well
        self.assertEqual(verify_workload.known_bench('Fispur', 'b' * 40, '')[0], 1000)

    def test_known_bench_only_trusts_confirmed_benches(self):

        old = self.workload(111)
        self.workload(999, games=0)                                # Never played, so never confirmed
        Test.objects.filter(id=self.workload(888).id).update(deleted=True)

        disputed = self.workload(777)                              # Workers measured another Bench
        self.wrong_bench(disputed, 'dev', 111)

        bench, workload = verify_workload.known_bench('Fispur', SHA, '')
        self.assertEqual((bench, workload.id), (111, old.id))

        # Once REBENCHed to the measured Bench, the report is stale and the Test counts again
        Engine.objects.filter(id=disputed.dev_id).update(bench=111)
        self.assertEqual(verify_workload.known_bench('Fispur', SHA, '')[1].id, disputed.id)

        # The latest confirmed one wins, and tunes count with their one engine
        tune = self.workload(333, tune=True)
        self.assertEqual(verify_workload.known_bench('Fispur', SHA, '')[1].id, tune.id)

    def test_known_bench_reports_of_the_other_side_do_not_count(self):

        # The base was disputed, which says nothing about the dev engine of the same Test
        test = self.workload(111)
        self.wrong_bench(test, 'base', 5)
        self.assertEqual(verify_workload.known_bench('Fispur', SHA, '')[0], 111)
        self.assertIsNone(verify_workload.known_bench('Fispur', 'b' * 40, ''))

    def test_known_bench_takes_a_fixed_number_of_queries(self):

        # Engine ids, one query per side, and the reports of all candidates at once
        for index in range(20):
            self.wrong_bench(self.workload(100 + index), 'dev', 1)

        with self.assertNumQueries(4):
            self.assertIsNone(verify_workload.known_bench('Fispur', SHA, ''))

        # A commit that was never tested is a single indexed query
        with self.assertNumQueries(1):
            self.assertIsNone(verify_workload.known_bench('Fispur', 'c' * 40, ''))

    # Benches of the web form

    def test_parse_bench(self):
        cases = { '' : None, 'Autofill' : None, ' autofill ' : None, '1234' : 1234, '1,600,000' : 1600000,
                  ' 1 600 000 ' : 1600000, '1_000' : 1000, '0' : 0, '-5' : 0, '12.5' : 0, 'lots' : 0, '²' : 0,
                  '2147483647' : 2147483647, '2147483648' : 0, '9' * 19 : 0 }
        for value, expected in cases.items():
            self.assertEqual(verify_workload.parse_bench(value), expected, value)

    def test_form_bench_sources_in_order(self):

        # Nothing known yet: the commit message, as before
        self.assertEqual(self.collect('Bench: 1,234'), (1234, []))
        self.assertEqual(self.collect('Merge branch'), (None, ['Unable to parse a Bench for main']))

        # Benches that do not fit into the database are no Benches
        self.assertEqual(self.collect('Bench: 99,999,999,999'), (None, ['Unable to parse a Bench for main']))
        self.assertEqual(self.collect('Bench: 0'), (None, ['Unable to parse a Bench for main']))

        # An earlier Test of the commit and Network, even if the message has another Bench
        self.workload(555, dev_network='aaaa1111')
        self.assertEqual(self.collect('Bench: 1234', dev_network='aaaa1111'), (555, []))
        self.assertEqual(self.collect('Bench: 1234'), (1234, []))

        # A Bench typed in always wins, also with thousands separators
        self.assertEqual(self.collect('Bench: 1234', dev_network='aaaa1111', dev_bench='42'), (42, []))
        self.assertEqual(self.collect('Bench: 1234', dev_bench='1,600,000'), (1600000, []))

    def test_form_rejects_malformed_benches(self):
        for value in ['0', '-5', 'lots', '12.5']:
            self.assertEqual(self.collect('Bench: 1234', dev_bench=value),
                             (None, ['Dev Bench must be a positive number, or Autofill']), value)

    def test_form_ignores_a_bench_filled_in_for_an_older_commit(self):

        # create_workload.js filled in 4242 for SHA, but the branch moved on to another commit
        newer = 'c' * 40
        self.assertEqual(self.collect('Bench: 777', sha=newer, dev_bench='4242', dev_bench_sha=SHA), (777, []))

        # Still on that commit, or typed by hand (no sha): the Bench is used as it is
        self.assertEqual(self.collect('Bench: 777', dev_bench='4242', dev_bench_sha=SHA.upper()), (4242, []))
        self.assertEqual(self.collect('Bench: 777', sha=newer, dev_bench='4242', dev_bench_sha=''), (4242, []))

    def test_collect_github_info_strips_the_branch(self):
        errors = []
        with mock.patch.object(verify_workload, 'resolve_commit', fake_commit('Bench: 5')):
            info = verify_workload.collect_github_info(errors, self.post(dev_branch='  main \n'), 'dev')
        self.assertEqual((info[1], info[3], errors), ('main', 5, []))

    # Github

    def resolve(self, branch, answers):
        github, errors = FakeGithub(answers), []
        with mock.patch.object(verify_workload.requests, 'get', github):
            result = verify_workload.resolve_commit(errors, { 'x_engine' : 'Fispur', 'x_repo' : REPO, 'x_branch' : branch }, 'x')
        return result, errors, github.urls

    def test_resolve_commit(self):

        commit = { 'sha' : SHA, 'commit' : { 'message' : 'Bench: 5' }, 'files' : ['big diff'] }

        # A branch: one request, and only the sha and message are kept
        result, errors, urls = self.resolve(' main ', { API + '/branches/main' : (200, { 'commit' : commit }) })
        self.assertEqual((result, errors, urls), ((API, SHA, 'Bench: 5'), [], [API + '/branches/main']))

        # A full sha: the Git Database API, without the diff, lowercased
        sha = 'ABCDEF' + '0' * 34
        result, errors, urls = self.resolve(sha, { API + '/git/commits/' + sha.lower() : (200, { 'sha' : sha.lower(), 'message' : 'm' }) })
        self.assertEqual((result, urls), ((API, sha.lower(), 'm'), [API + '/git/commits/' + sha.lower()]))

        # A tag falls back to the commits API
        result, errors, urls = self.resolve('v1', { API + '/commits/v1' : (200, commit) })
        self.assertEqual((result[1], urls), (SHA, [API + '/branches/v1', API + '/commits/v1']))

        # Missing, and answers that are not a commit
        self.assertEqual(self.resolve('nope', {})[1], ['nope could not be found'])
        self.assertEqual(self.resolve('odd', { API + '/branches/odd' : (200, { 'commit' : { 'sha' : 'x', 'commit' : { 'message' : 'm' } } }) })[1],
                         ['odd could not be found'])

    def test_resolve_commit_tells_rate_limits_apart(self):

        limited = (403, { 'message' : 'API rate limit exceeded for 1.2.3.4.' })
        result, errors, urls = self.resolve('main', { API + '/branches/main' : limited })

        self.assertIsNone(result)
        self.assertEqual(urls, [API + '/branches/main']) # No second request for a tag
        self.assertIn('rate limit', errors[0])

    # The endpoint behind create_workload.js

    def test_api_bench(self):

        url    = '/api/bench/'
        params = { 'engine' : 'Fispur', 'repo' : REPO, 'branch' : 'main', 'network' : '' }

        # Only for enabled users
        self.assertIn('error', self.client.get(url, params).json())
        self.client.login(username='Xenymor', password='x')

        with mock.patch.object(verify_workload, 'resolve_commit', fake_commit('Something\n\nBench: 1,600,000')):

            self.assertEqual(self.client.get(url, params).json(), { 'sha' : SHA, 'known' : None, 'message_bench' : 1600000, 'message_applies' : True })

            test  = self.workload(555)
            known = { 'bench' : 555, 'workload' : { 'id' : test.id, 'type' : 'test' } }
            self.assertEqual(self.client.get(url, params).json(), { 'sha' : SHA, 'known' : known, 'message_bench' : 1600000, 'message_applies' : True })

            self.assertIn('error', self.client.get(url, dict(params, engine='Nope')).json())
            self.assertIn('error', self.client.get(url, dict(params, repo='https://example.com/x/y')).json())
            self.assertIn('error', self.client.get(url, dict(params, branch='  ')).json())
            self.assertEqual(self.client.get(url, dict(params, branch='missing')).json(), { 'error' : 'missing could not be found' })

        # With the sha of an earlier lookup, only the database is asked
        with mock.patch.object(verify_workload, 'resolve_commit', side_effect=AssertionError('Github was asked')):
            self.assertEqual(self.client.get(url, dict(params, sha=SHA.upper(), network='aaaa1111')).json(), { 'sha' : SHA, 'known' : None, 'message_applies' : False })
            self.assertEqual(self.client.get(url, dict(params, sha=SHA)).json(), { 'sha' : SHA, 'known' : known, 'message_applies' : True })
            self.assertIn('error', self.client.get(url, dict(params, sha='abc')).json())

class NetworkBenchTests(TestCase):

    # The Bench in a commit message was measured with the engine's default Network. Another
    # Network searches differently, so its Bench must come from somewhere else.

    def setUp(self):
        self.user = User.objects.create_user('Xenymor', password='x')
        Profile.objects.create(user=self.user, enabled=True)
        EngineConfig.objects.create(name='Fispur', source=REPO, nps=1000000)
        Network.objects.create(engine='Fispur', sha256='DEFAULT1', name='default-net', author='x', default=True)
        Network.objects.create(engine='Fispur', sha256='OTHERNET', name='other-net', author='x')

    def collect(self, side, network, bench='Autofill'):
        errors = []
        post   = { '%s_branch' % (side) : 'main', '%s_repo' % (side) : REPO, '%s_engine' % (side) : 'Fispur',
                   '%s_network' % (side) : network, '%s_bench' % (side) : bench, 'info' : '' }
        with mock.patch.object(verify_workload, 'resolve_commit', fake_commit('Speedup\n\nBench: 1234')):
            info = verify_workload.collect_github_info(errors, SimpleNamespace(POST=post), side)
        return (info[3] if info else None), errors

    def test_same_commit_with_two_networks(self):

        # The default Network, or none at all, takes the Bench of the commit message
        self.assertEqual(self.collect('dev', 'DEFAULT1'), (1234, []))
        self.assertEqual(self.collect('dev', ''), (1234, []))

        # Another Network does not, since its Bench differs
        bench, errors = self.collect('base', 'OTHERNET')
        self.assertIsNone(bench)
        self.assertIn('was measured with another Network', errors[0])

        # Given by hand, or confirmed by an earlier Test with that Network, it is used
        self.assertEqual(self.collect('base', 'OTHERNET', bench='5678'), (5678, []))
        dev  = Engine.objects.create(name='main', source='', sha=SHA, bench=5678)
        Test.objects.create(author='Xenymor', book_name='UHO.epd', games=10, dev=dev, base=dev, dev_engine='Fispur',
                            base_engine='Fispur', dev_repo=REPO, base_repo=REPO, dev_network='OTHERNET', base_network='OTHERNET')
        self.assertEqual(self.collect('base', 'OTHERNET'), (5678, []))

    def test_api_bench_only_offers_the_message_bench_for_the_default_network(self):

        self.client.login(username='Xenymor', password='x')
        params = { 'engine' : 'Fispur', 'repo' : REPO, 'branch' : 'main' }

        with mock.patch.object(verify_workload, 'resolve_commit', fake_commit('Bench: 1234')):
            self.assertEqual(self.client.get('/api/bench/', dict(params, network='DEFAULT1')).json()['message_bench'], 1234)
            # The commit's Bench is sent all the same, so the form can keep it for the default Network
            answer = self.client.get('/api/bench/', dict(params, network='OTHERNET')).json()
            self.assertEqual((answer['message_bench'], answer['message_applies']), (1234, False))

        # Looked up again by its sha, for another Network: the form must not reuse the cached Bench
        answer = self.client.get('/api/bench/', dict(params, network='OTHERNET', sha=SHA)).json()
        self.assertEqual(answer, { 'sha' : SHA, 'known' : None, 'message_applies' : False })
        answer = self.client.get('/api/bench/', dict(params, network='DEFAULT1', sha=SHA)).json()
        self.assertTrue(answer['message_applies'])

    def test_one_restart_fixes_both_wrong_benches(self):

        # The same commit with two Networks, as a Test of a new net. Both Benches turn out wrong,
        # and the client reports both before it stops, so one restart with the measured Benches fixes them.
        from OpenBench.config import OPENBENCH_CONFIG
        from OpenBench.models import Machine, ServerState

        dev  = Engine.objects.create(name='main', source='', sha=SHA, bench=1234)
        base = Engine.objects.create(name='main', source='', sha=SHA, bench=1234)
        test = Test.objects.create(author='Xenymor', book_name='UHO.epd', dev=dev, base=base, dev_engine='Fispur',
                                   base_engine='Fispur', dev_repo=REPO, base_repo=REPO, dev_network='OTHERNET', base_network='DEFAULT1')

        info    = { 'client_ver' : OPENBENCH_CONFIG['client_version'], 'OPENBENCH_CONFIG_CHECKSUM' : ServerState.checksum() }
        machine = Machine.objects.create(user=self.user, secret='s', info=info)
        report  = lambda binary, bench: self.client.post('/clientBenchError/', { 'machine_id' : machine.id, 'secret' : 's',
                      'test_id' : test.id, 'error' : '[%s] Wrong Bench: %d' % (binary, bench) }).json()

        self.assertEqual(report('Fispur-AAAAAAAA-OTHERNET.exe', 2222), {})
        self.assertEqual(report('Fispur-AAAAAAAA-DEFAULT1', 3333), {})
        self.assertTrue(Test.objects.get(id=test.id).finished)

        self.client.login(username='Xenymor', password='x')
        self.client.get('/test/%d/REBENCH/' % (test.id))

        test = Test.objects.get(id=test.id)
        self.assertEqual((test.finished, test.dev.bench, test.base.bench), (False, 2222, 3333))

        # Confirmed by games, each Network now autofills its own Bench
        Test.objects.filter(id=test.id).update(games=100)
        self.assertEqual(self.collect('dev', 'OTHERNET'), (2222, []))
        self.assertEqual(self.collect('dev', 'DEFAULT1'), (3333, []))
