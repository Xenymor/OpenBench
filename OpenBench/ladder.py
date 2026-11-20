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

# Ladders measure the progress of a branch, usually main: every new commit of it plays a fixed
# number of games against one anchor commit, which stays the same for the whole Ladder. The
# Elo of each of those Tests is a point on a line, which shows whether the patches that passed
# their SPRTs one by one also add up, and whether anything slipped through that lost Elo.
#
#   >>> ladder, errors = create_ladder(user, fields)   # The anchor is the branch's head by default
#   >>> measure(ladder)                                 # Tests the branch's head, if it is new
#
# A Ladder looks at its branch every poll_hours, from the WorkflowWatcher. Pushes that Github
# announces, see OpenBench/automation.py, and MERGE Nodes ask it to look right away, through
# request_poll(). While one of its Tests is still running, a Ladder waits, and then measures
# whatever the head is by then. So a burst of pushes costs one Test, not one for each.

import datetime
import traceback

from types import SimpleNamespace

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

import OpenBench.stats

from OpenBench import github
from OpenBench.models import Book, EngineConfig, Ladder, LadderPoint, Network, Test
from OpenBench.workflows import notifications

DEFAULT_GAMES = 2000

# Fields that a Ladder keeps in its settings, as the Test form calls them
SETTINGS = ['games', 'time_control', 'options', 'book_name', 'network', 'priority', 'throughput', 'notify']

def default_preset(engine):
    config = EngineConfig.objects.filter(name=engine).first()
    return ((config.presets or {}).get('test_presets') or {}).get('default', {}) if config else {}

def build_post(ladder, dev_sha):

    # The POST of the Test form for the branch's head against the anchor. Empty settings
    # use the default test preset of the engine, as the form would fill them in.
    preset   = default_preset(ladder.engine)
    settings = ladder.settings or {}
    config   = EngineConfig.objects.get(name=ladder.engine)

    def pick(field, *fallbacks):
        for value in [settings.get(field)] + [preset.get(name) for name in fallbacks[:-1]] + [fallbacks[-1]]:
            if value not in [None, '']:
                return str(value)

    book    = Book.objects.filter(enabled=True).order_by('name').values_list('name', flat=True).first() or 'NONE'
    options = pick('options', 'both_options', 'dev_options', 'Threads=1 Hash=8')
    control = pick('time_control', 'both_time_control', 'dev_time_control', '10.0+0.1')
    network = str(settings.get('network', '') or '')

    return {
        'dev_engine'  : ladder.engine, 'dev_repo'  : ladder.repo, 'dev_branch'  : dev_sha,           'dev_bench'  : 'Autofill',
        'base_engine' : ladder.engine, 'base_repo' : ladder.repo, 'base_branch' : ladder.anchor_sha, 'base_bench' : str(ladder.anchor_bench),
        'dev_network' : network,       'base_network' : network,
        'dev_options' : options,       'base_options' : options,
        'dev_time_control' : control,  'base_time_control' : control,
        'book_name'     : pick('book_name', 'book_name', book),
        'upload_pgns'   : 'FALSE',
        'test_mode'     : 'GAMES',
        'test_max_games': pick('games', str(DEFAULT_GAMES)),
        'test_bounds'   : '[0.00, 3.00]', 'test_confidence' : '[0.05, 0.05]', # Unused by fixed games
        'priority'      : pick('priority', 'priority', '0'),
        'throughput'    : pick('throughput', 'throughput', '1000'),
        'workload_size' : str(preset.get('workload_size', 32)),
        'syzygy_wdl'    : str(preset.get('syzygy_wdl', 'DISABLED')),
        'syzygy_adj'    : str(preset.get('syzygy_adj', 'OPTIONAL')),
        'win_adj'       : str(preset.get('win_adj', 'movecount=3 score=400')),
        'draw_adj'      : str(preset.get('draw_adj', 'movenumber=40 movecount=8 score=10')),
        'scale_method'  : 'BASE',
        'scale_nps'     : str(config.nps),
        'notify'        : pick('notify', 'all'),
        'info'          : '',
    }

def create_ladder(user, fields):

    # (Ladder, None), or (None, errors). fields are those of the form on /ladders/
    from OpenBench.workloads.verify_workload import known_bench, message_bench, parse_bench

    errors = []
    name   = str(fields.get('name', '')).strip()
    engine = str(fields.get('engine', '')).strip()
    branch = str(fields.get('branch', '') or 'main').strip()
    anchor = str(fields.get('anchor', '') or '').strip() or branch
    config = EngineConfig.objects.filter(name=engine).first()
    repo   = github.normalize_repo(fields.get('repo') or (config.source if config else ''))

    if not name or len(name) > 128:
        errors.append('A Ladder needs a name of at most 128 characters')

    if not config:
        errors.append('Unknown Engine')

    if not repo:
        errors.append('Sources must be found on https://github.com/<User>/<Repo>')

    try: games = int(fields.get('games') or DEFAULT_GAMES); assert 2 <= games <= 1000000
    except: errors.append('Games must be a number of at least 2'); games = DEFAULT_GAMES

    try: poll_hours = int(fields.get('poll_hours') or 6); assert 1 <= poll_hours <= 24 * 30
    except: errors.append('Poll Hours must be within [1, 720]'); poll_hours = 6

    network = str(fields.get('network', '') or '').strip()
    if network and not Network.objects.filter(engine=engine, sha256=network).exists():
        errors.append('Unknown Network')

    if errors:
        return None, errors

    try: commit = github.commit(repo, engine, anchor)
    except github.GithubError as error:
        return None, ['%s could not be found: %s' % (anchor, error)]

    given = parse_bench(fields.get('anchor_bench', ''))
    known = known_bench(engine, commit['sha'], network)
    bench = given or (known[0] if known else None) or message_bench(commit['message'])

    if not bench:
        return None, ['Unable to find a Bench for the anchor %s, so please give it' % (commit['sha'][:8])]

    settings = { field : str(fields.get(field, '') or '').strip() for field in SETTINGS }
    settings['games'] = str(games)

    ladder = Ladder.objects.create(
        name=name, author=user.username, engine=engine, repo=repo, branch=branch,
        anchor_sha=commit['sha'], anchor_bench=bench, anchor_label=first_line(commit['message']),
        settings=settings, poll_hours=poll_hours)

    return ladder, None

def first_line(message):
    return (message or '').strip().split('\n')[0][:256]

def running(ladder):
    # Whether a Test of the Ladder still plays
    return ladder.points.filter(test__finished=False, test__deleted=False).exists()

def measure(ladder, force=False):

    # Starts a Test for the head of the branch, unless it was measured already. Returns
    # the new LadderPoint, or None. force measures it again, for example after it failed.
    commit = github.commit(ladder.repo, ladder.engine, ladder.branch)
    last   = ladder.points.order_by('-id').first()

    if commit['sha'] == ladder.anchor_sha:
        return None

    if last and last.sha == commit['sha'] and not (force and (last.error or not last.test or last.test.deleted)):
        return None

    return create_point(ladder, commit)

def create_point(ladder, commit):

    # Imported late, since the workload modules only import cleanly once the views are loaded
    import OpenBench.views
    from OpenBench.workloads import create_workload as Create

    user    = User.objects.get(username=ladder.author)
    request = SimpleNamespace(POST=build_post(ladder, commit['sha']), user=user)
    point   = LadderPoint(ladder=ladder, sha=commit['sha'], label=first_line(commit['message']))

    try:
        with transaction.atomic():

            test, errors = Create.create_new_test(request)
            if errors:
                raise ValueError('\n'.join(errors))

            test.dev.name  = '%s@%s' % (ladder.branch, commit['sha'][:8])
            test.base.name = 'anchor@%s' % (ladder.anchor_sha[:8])
            test.dev.save(); test.base.save()

            test.info = ('[Ladder #%d: %s] %s' % (ladder.id, ladder.name, point.label))[:1024]
            test.save()

            Create.finalize_workload(test, user)
            point.test = test
            point.save()

    except Exception as error:
        point.error = str(error)[:1024]
        point.save()
        notifications.announce('attention', 'Ladder #%d %s could not measure %s' % (ladder.id, ladder.name, commit['sha'][:8]),
                               [point.error], '/ladder/%d/' % (ladder.id))

    return point

def request_poll(repo, branch):
    # Ladders on this branch look at it with the next pass of the watcher
    repo = github.normalize_repo(repo)
    for ladder in Ladder.objects.filter(branch=branch, enabled=True):
        if github.normalize_repo(ladder.repo) == repo:
            Ladder.objects.filter(id=ladder.id).update(last_polled=None)

def poll_all():

    # Called by the WorkflowWatcher on every pass. Github is asked only when a Ladder is due
    # and has no running Test, so that waiting Ladders cost a query of the database at most.
    now = timezone.now()

    for ladder in Ladder.objects.filter(enabled=True):

        due = ladder.last_polled is None or now - ladder.last_polled >= datetime.timedelta(hours=ladder.poll_hours)
        if not due or running(ladder):
            continue

        Ladder.objects.filter(id=ladder.id).update(last_polled=now)

        try: measure(ladder)
        except Exception:
            traceback.print_exc()

def points(ladder):

    # The course of the Ladder, as the page draws it
    rows = []

    for point in ladder.points.select_related('test').order_by('id'):

        row = { 'sha' : point.sha, 'label' : point.label, 'created' : point.created.isoformat(), 'error' : point.error }

        if (test := point.test) and not test.deleted:
            lower, elo, upper = OpenBench.stats.Elo(test.results())
            row.update({ 'test' : test.id, 'games' : test.games, 'max_games' : test.max_games, 'finished' : test.finished,
                         'elo' : elo, 'lower' : lower, 'upper' : upper })

        rows.append(row)

    return rows
