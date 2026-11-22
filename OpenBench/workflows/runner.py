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

# Executes Workflows, see graph.py for their structure and semantics.
#
#   >>> workflow, errors = create_workflow(user, name, graph)
#   >>> step(workflow.id)
#
# step() is called by the WorkflowWatcher every few seconds, and after every
# change made through the web interface. It holds a select_for_update() lock on
# the Workflow, so that each Node is started exactly once. It never runs inside
# of update_test(), so the Github API never blocks the Clients reporting results.
#
# Tests are created through the very same create_new_test() and create_new_tune()
# as the web forms, from a POST that merges the Workflow's defaults, the Node's
# config, and the engines it refers to. So they are verified the very same way.

import traceback

from types import SimpleNamespace

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

import OpenBench.spsa_utils
import OpenBench.stats
import OpenBench.views # Before the workload modules, which only import cleanly once it is loaded

from OpenBench.github import GithubError
from OpenBench.models import LogEvent, Profile, Test, Workflow, WorkflowNode
from OpenBench.utils import measured_benches
from OpenBench.workflows import actions
from OpenBench.workflows import conditions
from OpenBench.workflows import graph as Graph
from OpenBench.workflows import notifications
from OpenBench.workflows.options import merge_options, is_option_string, estimate_tuned_options
from OpenBench.workloads import create_workload as Create
from OpenBench.workloads import verify_workload as Verify
from OpenBench.workloads.checks import same_bench
from OpenBench.workloads.modify_workload import apply_measured_bench, WorkloadActionError

State  = WorkflowNode.State
Status = Workflow.Status

# Fields of the defaults that a Node may override, other than its engines
OVERRIDABLE = [
    'test_bounds', 'test_confidence', 'priority', 'throughput',
    'spsa_alpha', 'spsa_gamma', 'spsa_A_ratio', 'spsa_iterations', 'spsa_pairs_per',
    'spsa_reporting_type', 'spsa_distribution_type', 'spsa_auto_stop',
]

# Stand-in for the engines while only the settings are checked, see verify_settings()
PLACEHOLDER = { 'branch' : 'main', 'bench' : '1' }

class WorkflowError(Exception):
    pass

def setting(defaults, config, field):

    # The Node's value, unless left empty, in which case the default, see node_defaults()
    value = config.get(field, '')
    if str(value).strip() == '':
        value = defaults.get(field, '')
    return str(value).strip()

def node_defaults(graph, key):

    # The Workflow's defaults, with the values of the preset that the Node loaded on top.
    # Graph.validate() ensures that a preset never picks the engines, see PRESET_EXCLUDED.
    values = graph['nodes'][key].get('config', {}).get('preset_values') or {}
    return { **graph['defaults'], **{ field : str(value) for field, value in values.items() } }

def preset_options(config, side):
    # Options of the Node's preset, which also apply on top of those a reference inherits
    return str((config.get('preset_values') or {}).get('%s_options' % (side), ''))

def option_limit():
    return Test._meta.get_field('dev_options').max_length

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                         BUILDING THE WORKLOAD POSTS                         #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def build_post(graph, key, engines):

    # engines maps 'dev' and 'base' to { 'repo', 'engine', 'branch', 'bench',
    # 'network', 'options' }. Returns the POST that the workload form would send.

    node     = graph['nodes'][key]
    config   = node.get('config', {})
    defaults = node_defaults(graph, key)

    post = { name : str(value) for name, value in defaults.items() }
    post.update({ field : setting(defaults, config, field) for field in OVERRIDABLE })
    post['info']   = ''
    post['notify'] = 'off' # The Workflow announces its Tests itself, see collect_events()

    # One time control for both engines, unless the Workflow's defaults differ per engine
    time_control = str(config.get('time_control', '')).strip()

    for side in Graph.ENGINES[node['kind']]:
        engine = engines[side]
        post['%s_repo'         % (side)] = engine['repo']
        post['%s_engine'       % (side)] = engine['engine']
        post['%s_branch'       % (side)] = engine['branch']
        post['%s_bench'        % (side)] = str(engine['bench'])
        post['%s_network'      % (side)] = engine['network']
        post['%s_options'      % (side)] = engine['options']
        post['%s_time_control' % (side)] = time_control or str(defaults.get('%s_time_control' % (side), defaults.get('time_control', '')))

    if node['kind'] == 'SPRT':
        post['test_mode'] = 'SPRT'

    if node['kind'] == 'SPSA':
        post['spsa_inputs']  = str(config.get('spsa_inputs', '')).strip()
        post['scale_method'] = 'DEV' # The only Scale Method of a tune

    return post

def network_override(config, side, network):

    # A Node may pick its own Network: empty keeps the given one, 'none' runs without
    value = str(config.get('%s_network' % (side), '') or '').strip()
    if not value:
        return network
    return '' if value.lower() == 'none' else value

def branch_engine(graph, key, side):

    # An engine given by its branch, which create_new_test() will resolve
    config   = graph['nodes'][key].get('config', {})
    defaults = node_defaults(graph, key)

    return {
        'repo'    : str(defaults.get('%s_repo'    % (side), '')),
        'engine'  : str(defaults.get('%s_engine'  % (side), '')),
        'network' : network_override(config, side, str(defaults.get('%s_network' % (side), ''))),
        'branch'  : str(config.get(side, '')).strip(),
        'bench'   : Graph.bench_override(config, side) or 'Autofill',
        'options' : merge_options(str(defaults.get('%s_options' % (side), '')), str(config.get('%s_options' % (side), ''))),
    }

def snapshot(test):

    # The engines exactly as they are tested, so that later Nodes can refer to them
    return {
        side : {
            'repo'    : getattr(test, '%s_repo'    % (side)),
            'engine'  : getattr(test, '%s_engine'  % (side)),
            'network' : getattr(test, '%s_network' % (side)),
            'options' : getattr(test, '%s_options' % (side)),
            'name'    : getattr(test, side).name,
            'sha'     : getattr(test, side).sha,
            'bench'   : getattr(test, side).bench,
        } for side in ['dev', 'base']
    }

def effective_trigger(nodes, node):

    # The Node that started this one, looking through IF Nodes, which only pass it on
    key, seen = node.trigger, set()
    while key in nodes and nodes[key].kind == 'IF' and key not in seen:
        seen.add(key)
        key = nodes[key].trigger
    return key

def referenced_engine(nodes, node, ref):

    # The engine that '@target.field' refers to, as a snapshot
    target, field = ref
    target = effective_trigger(nodes, node) if target == 'trigger' else target

    if not (source := nodes.get(target)) or not source.test:
        raise WorkflowError('%s has no tested engine to refer to' % (target or '@trigger'))

    engine = snapshot(source.test)['base' if field == 'base' else 'dev']

    if field == 'tuned':
        engine['options'] = merge_options(engine['options'], source.tuned)
        engine['name']    = '%s-tuned' % (engine['name'])

    return engine

def resolve_engines(workflow, nodes, node):

    # Engines of the Node, and the names they should be shown with
    graph   = workflow.graph
    config  = graph['nodes'][node.key].get('config', {})
    engines = {}
    names   = {}

    for side in Graph.ENGINES[node.kind]:

        if not (ref := Graph.parse_ref(config.get(side))):
            engines[side] = branch_engine(graph, node.key, side)

            # Tested at the commit it was pinned to, but shown by its branch
            if sha := (node.pinned or {}).get(side):
                names[side] = engines[side]['branch']
                engines[side]['branch'] = sha

        else:
            engine = referenced_engine(nodes, node, ref)
            names[side]   = engine['name']
            engines[side] = {
                'repo'    : engine['repo'],
                'engine'  : engine['engine'],
                'network' : network_override(config, side, engine['network']),
                'branch'  : engine['sha'], # Pinned to the exact commit, so its Bench carries over,
                'bench'   : Graph.bench_override(config, side) or engine['bench'], # unless given for another net
                'options' : merge_options(merge_options(engine['options'], preset_options(config, side)),
                                          str(config.get('%s_options' % (side), ''))),
            }

        # Allows to name an engine that is given by its sha, as done for migrated follow-ups
        if name := str(config.get('%s_name' % (side), '')).strip():
            names[side] = name

    return engines, names

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                            CREATING A WORKFLOW                              #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def verify_settings(graph, locked={}):

    # The workload verifications for every Node, without asking Github yet.
    # locked maps the keys of Nodes that have already started to their Tests,
    # which are left alone, since the defaults no longer apply to them.
    errors = []

    for key, node in graph['nodes'].items():

        config = node.get('config', {})
        name   = Graph.label(graph, key)
        found  = []

        if not Graph.ENGINES[node['kind']] or config.get('existing_test') or key in locked:
            continue

        engines = {}
        for side in Graph.ENGINES[node['kind']]:
            engines[side] = { **branch_engine(graph, key, side), **PLACEHOLDER }

        post    = build_post(graph, key, engines)
        request = SimpleNamespace(POST=post)

        if node['kind'] == 'SPRT':
            Verify.verify_test_creation(found, request)

        if node['kind'] == 'SPSA':
            Verify.verify_tune_creation(found, request)

        for side in Graph.ENGINES[node['kind']]:
            if not is_option_string(str(config.get('%s_options' % (side), ''))):
                found.append('%s Options must be Name=Value pairs' % (side.title()))

        errors += ['%s: %s' % (name, error) for error in found]

    return errors + verify_option_lengths(graph, locked)

def verify_option_lengths(graph, locked={}):

    # Options of a Node are only built when it starts, so they must fit the database now.
    # The tuned values are not known yet, so the longest possible ones are assumed.
    # Nodes that already started have their actual options, as tested.

    errors, limit = [], option_limit()

    def estimate(key, side):

        config = graph['nodes'][key].get('config', {})

        if test := locked.get(key):
            return getattr(test, '%s_options' % (side))

        if not (ref := Graph.parse_ref(config.get(side))):
            options = branch_engine(graph, key, side)['options']

        else:
            targets = [edge['from'] for edge in Graph.incoming(graph, key)] if ref[0] == 'trigger' else [ref[0]]
            options = max([estimate(target, 'base' if ref[1] == 'base' else 'dev') for target in targets], key=len)
            if ref[1] == 'tuned':
                inputs  = str(graph['nodes'][ref[0] if ref[0] != 'trigger' else targets[0]].get('config', {}).get('spsa_inputs', ''))
                options = merge_options(options, estimate_tuned_options(inputs))
            options = merge_options(merge_options(options, preset_options(config, side)), str(config.get('%s_options' % (side), '')))

        return options

    for key, node in graph['nodes'].items():

        if not Graph.ENGINES[node['kind']] or node.get('config', {}).get('existing_test') or key in locked:
            continue

        try:
            for side in Graph.ENGINES[node['kind']]:
                assert len(estimate(key, side)) <= limit
        except AssertionError:
            errors.append('%s: Options would exceed %d characters' % (Graph.label(graph, key), limit))
        except Exception:
            pass # Broken references are reported by Graph.validate()

    return errors

def verify_existing_tests(graph, user, locked={}):

    # Tests attached to Nodes must exist, match the kind, and belong to no other Workflow.
    # Aborting a Workflow stops its Tests, so only Tests the user may stop can be attached.
    errors, tests = [], {}
    approver = Profile.objects.filter(user=user, approver=True).exists()

    for key, node in graph['nodes'].items():

        if not (test_id := node.get('config', {}).get('existing_test')) or key in locked:
            continue

        name = Graph.label(graph, key)
        test = Test.objects.filter(id=int(test_id)).first()

        if not test or test.deleted:
            errors.append('%s: Test #%s does not exist' % (name, test_id))

        elif test.test_mode != node['kind']:
            errors.append('%s: Test #%s is not an %s' % (name, test_id, node['kind']))

        elif test.author != user.username and not approver:
            errors.append('%s: Test #%s belongs to another user' % (name, test_id))

        elif WorkflowNode.objects.filter(test=test).exists() or test.id in tests.values():
            errors.append('%s: Test #%s already belongs to a Workflow' % (name, test_id))

        else:
            tests[key] = test.id

    return errors, tests

def pinnable_sides(graph, key):

    # Engines of the Node given by a branch, tag or sha, which are pinned to their commit
    node   = graph['nodes'][key]
    config = node.get('config', {})

    if config.get('existing_test') or Graph.resolves_on_start(config):
        return []

    return [side for side in Graph.ENGINES[node['kind']] if not Graph.parse_ref(config.get(side))]

def lookup_commit(errors, engine):

    # (sha, message) of the branch, tag or sha of an engine, or None after adding an error
    post = { 'x_engine' : engine['engine'], 'x_repo' : engine['repo'], 'x_branch' : engine['branch'] }
    if commit := Verify.resolve_commit(errors, post, 'x'):
        return commit[1], commit[2]
    return None

def pin_commits(graph, locked={}):

    # ({ key : { side : sha } }, errors) for the Nodes that have not started. Each branch is
    # looked up once, however many Nodes use it. The Bench that a Node would autofill must be
    # found as well, so that neither fails only once the Node starts, hours later. At that
    # point, a Bench that workers have confirmed for the commit in the meantime still wins.
    # Expects verify_settings() to have passed, so that every engine and repo is known.
    pins, errors, commits = {}, [], {}

    for key in graph['nodes']:

        if key in locked:
            continue

        config = graph['nodes'][key].get('config', {})
        name   = Graph.label(graph, key)

        for side in pinnable_sides(graph, key):

            engine = branch_engine(graph, key, side)
            where  = (engine['engine'], engine['repo'], engine['branch'])

            if where not in commits:
                found = []
                commits[where] = (lookup_commit(found, engine), found)

            commit, found = commits[where]

            if not commit:

                # Every further lookup would fail the same way, so Github is not asked again
                if any('rate limit' in error for error in found):
                    return {}, found

                errors += ['%s: %s: %s' % (name, side.title(), error) for error in found]
                continue

            sha, message = commit

            if (Graph.bench_override(config, side) is None
                and not Verify.known_bench(engine['engine'], sha, engine['network'])
                and not (Verify.message_bench(message) and Verify.message_applies(engine['engine'], engine['network']))):

                # The commit's Bench belongs to the default Network, see Verify.message_applies()
                if Verify.message_bench(message):
                    errors.append('%s: The Bench in the commit message of %s was measured with another Network, so give the %s Bench'
                                  % (name, engine['branch'], side.title()))
                else:
                    errors.append('%s: Unable to parse a %s Bench for %s' % (name, side.title(), engine['branch']))
                continue

            pins.setdefault(key, {})[side] = sha

    return pins, list(dict.fromkeys(errors))

def create_workflow(user, name, graph):

    if not (name := str(name or '').strip()) or len(name) > 128:
        return None, ['A Workflow needs a name of at most 128 characters']

    if errors := Graph.validate(graph):
        return None, errors

    errors, tests = verify_existing_tests(graph, user)
    errors += verify_settings(graph)

    if errors:
        return None, errors

    pins, errors = pin_commits(graph)
    if errors:
        return None, errors

    with transaction.atomic():

        workflow = Workflow.objects.create(author=user.username, name=name, graph=graph)

        for key, node in graph['nodes'].items():

            WorkflowNode.objects.create(
                workflow = workflow,
                key      = key,
                kind     = node['kind'],
                state    = State.RUNNING if key in tests else State.WAITING,
                test_id  = tests.get(key),
                pinned   = pins.get(key, {}),
                started  = timezone.now() if key in tests else None,
            )

        silence(tests)

    return step(workflow.id), None

def silence(tests):
    # Attached Tests are announced by their Workflow from now on, and no longer on their own
    Test.objects.filter(id__in=tests.values()).update(notify='off')

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                         EDITING A RUNNING WORKFLOW                          #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def is_locked(node):

    # Started, or decided, Nodes are fixed, so that their outcomes and engines stay valid
    return node.test_id is not None or node.state in [State.RUNNING, State.FINISHED, State.STOPPED]

def incoming_pairs(graph, key):
    return sorted((edge['from'], edge['port']) for edge in Graph.incoming(graph, key))

def verify_locked_nodes(old, new, locked):

    # Locked Nodes keep their key, kind, settings and incoming edges. Only their label,
    # position and outgoing edges may change. New outgoing edges from a port that has
    # already fired simply start their target right away.
    errors = []

    for key in locked:

        name = Graph.label(old, key)

        if key not in new['nodes']:
            errors.append('%s has already started, and cannot be removed' % (name))

        elif (new['nodes'][key]['kind'] != old['nodes'][key]['kind']
              or new['nodes'][key].get('config', {}) != old['nodes'][key].get('config', {})):
            errors.append('%s has already started, so its settings cannot change' % (name))

        elif incoming_pairs(new, key) != incoming_pairs(old, key):
            errors.append('%s has already started, so its incoming edges cannot change' % (name))

    return errors

def update_workflow(workflow_id, user, name, graph):

    if not (name := str(name or '').strip()) or len(name) > 128:
        return None, ['A Workflow needs a name of at most 128 characters']

    if errors := Graph.validate(graph):
        return None, errors

    # Branches are looked up again for every Node that has not started, which is how a
    # waiting Node picks up new commits. Github may be slow to answer, so that happens
    # before the lock is taken. A Node that starts in the meantime is locked below, and
    # keeps the commits it started with.
    started = { node.key : node.test for node in WorkflowNode.objects.filter(workflow_id=workflow_id).select_related('test') if is_locked(node) }

    if errors := verify_settings(graph, started):
        return None, errors

    pins, errors = pin_commits(graph, started)
    if errors:
        return None, errors

    with transaction.atomic():

        # Under the same lock as step(), so no Node can start while the edit is applied
        workflow = Workflow.objects.select_for_update().get(id=workflow_id)
        rows     = { node.key : node for node in workflow.nodes.select_related('test') }
        locked   = { key : node.test for key, node in rows.items() if is_locked(node) }

        if workflow.status == Status.ABORTED:
            return None, ['Aborted Workflows cannot be edited']

        errors  = verify_locked_nodes(workflow.graph, graph, locked)
        found, tests = verify_existing_tests(graph, user, locked)
        errors += found + verify_settings(graph, locked)

        if errors:
            return None, errors

        # Every Node that has not started is set back to WAITING, and decided anew.
        # That also retries Nodes in ERROR, and revisits SKIPPED ones with new edges.
        for key, node in rows.items():
            if key not in locked and key not in graph['nodes']:
                node.delete()

        for key, node in graph['nodes'].items():

            if key in locked:
                continue

            row = rows.get(key) or WorkflowNode(workflow=workflow, key=key)
            row.kind, row.state, row.outcome, row.trigger, row.error, row.tuned, row.detail = node['kind'], State.WAITING, '', '', '', '', ''
            row.test_id, row.started, row.finished = tests.get(key), None, None
            row.pinned = pins.get(key, {})

            if key in tests:
                row.state, row.started = State.RUNNING, timezone.now()

            row.save()

        silence(tests)

        workflow.name   = name
        workflow.graph  = graph
        workflow.status = Status.RUNNING # Even a FINISHED Workflow may be extended
        workflow.save()

    return step(workflow_id), None

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                            EXECUTING A WORKFLOW                             #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def finish(node, outcome):
    node.state    = State.FINISHED
    node.outcome  = outcome
    node.finished = timezone.now()

def observe(workflow, node):

    # Picks up the outcome of a running Test, or notices it being stopped or resumed.
    # Returns whether the Node changed.

    if node.state not in [State.RUNNING, State.STOPPED]:
        return False

    # Held by the pause of the Workflow: stopped on purpose, so it needs no attention
    if node.held:
        return False

    before = (node.state, node.outcome, node.error)
    test   = node.test

    if test is None:
        node.state, node.error = State.STOPPED, 'The Test no longer exists'
        return True

    if test.deleted:
        node.state, node.error = State.STOPPED, 'The Test was deleted'

    elif node.kind == 'SPRT' and (test.passed or test.failed):
        finish(node, 'passed' if test.passed else 'failed')

    elif node.kind == 'SPSA' and (test.games >= spsa_games_needed(workflow, node) or converged(test)):
        node.tuned = OpenBench.spsa_utils.spsa_tuned_options(test)
        finish(node, 'done')
        stop_finished_tune(workflow, node, test)

    elif test.finished:
        node.state = State.STOPPED
        if before[0] != State.STOPPED: # Looked up once, when the Test stops
            node.error = stop_reason(test)

    else: # Running, possibly again after a restart
        node.state, node.error = State.RUNNING, ''

    return before != (node.state, node.outcome, node.error)

def stop_finished_tune(workflow, node, test):

    # A tune that is done at its "Done at %" would otherwise play on to its full length, for
    # values that the Workflow has frozen already. Stopped right away, so the workers move on.
    # A tune that was attached as an existing Test was started by the user, and keeps running.
    if test.finished or workflow.graph['nodes'][node.key].get('config', {}).get('existing_test'):
        return

    Test.objects.filter(id=test.id).update(finished=True)
    test.finished = True

    percent = float(workflow.graph['nodes'][node.key].get('config', {}).get('at_percent') or 100)
    summary = 'STOP Workflow #%d done at %g%%' % (workflow.id, percent)
    LogEvent.objects.create(author=workflow.author, summary=summary[:128], log_file='', test_id=test.id)

def stop_reason(test):

    # The Server stops a Test with too many errors, see OpenBench/error_watch.py
    if test.stop_reason:
        return test.stop_reason[:1024]

    # Workers stop a Test whose engine does not build or bench correctly, and log why
    event = LogEvent.objects.filter(test_id=test.id).exclude(machine_id=0).order_by('-id').first()
    if event and 'Bench' in event.summary:
        return ('Stopped by a worker: %s' % (event.summary))[:1024]
    return 'The Test was stopped'

def converged(test):
    # A tune that stopped itself, since its values no longer moved, see OpenBench/spsa_history.py
    return test.finished and test.spsa_run.converged_at is not None

def spsa_games_needed(workflow, node):
    config  = workflow.graph['nodes'][node.key].get('config', {})
    percent = float(config.get('at_percent') or 100)
    run     = node.test.spsa_run
    return 2 * run.pairs_per * run.iterations * percent / 100.0

def start(workflow, nodes, node):

    # Creates the Test of a Node. Failures are kept on the Node as ERROR,
    # inside of a savepoint so that they never undo the rest of the step()

    try:
        with transaction.atomic():
            test = create_test(workflow, nodes, node)

        node.state, node.test, node.error = State.RUNNING, test, ''
        node.started = timezone.now()

    except WorkflowError as error:
        node.state, node.error = State.ERROR, str(error)[:1024]

    except Exception as error:
        traceback.print_exc()
        node.state, node.error = State.ERROR, ('Unexpected error: %s' % (error))[:1024]

    node.save()

def sprt_result(node):

    # The variables a condition may use, see conditions.VARIABLES. Passed and failed
    # follow the Node's outcome, which may also have been decided by hand.
    test = node.test
    lower, elo, upper = OpenBench.stats.Elo(test.results())
    return {
        'passed'    : node.outcome == 'passed', 'failed'    : node.outcome == 'failed',
        'elo'       : elo,                      'elo_lower' : lower, 'elo_upper' : upper,
        'llr'       : test.currentllr,          'games'     : float(test.games),
    }

def decide_condition(workflow, nodes, node):

    # IF Nodes need no Test: they decide right away, on the results of SPRTs.
    # Plain variables belong to the source, or the trigger. Others name their SPRT.
    config    = workflow.graph['nodes'][node.key].get('config', {})
    source    = str(config.get('source', '') or '').strip() or effective_trigger(nodes, node)
    condition = config.get('condition', '')

    try:
        results, details = {}, []

        for target in sorted(conditions.references(condition), key=lambda key: key or ''):

            key = target or source
            if not (sprt := nodes.get(key)) or sprt.kind != 'SPRT' or sprt.state != State.FINISHED or not sprt.test:
                raise WorkflowError('%s has no finished SPRT to evaluate' % (key or 'The trigger'))

            results[target] = sprt_result(sprt)
            details.append('%s: %s' % (Graph.label(workflow.graph, key), conditions.describe(results[target])))

        node.detail = '; '.join(dict.fromkeys(details))[:256] # The source may also be named by its key
        node.error  = ''
        finish(node, 'true' if conditions.evaluate(condition, results) else 'false')

    except (WorkflowError, conditions.ConditionError) as error:
        node.state, node.error = State.ERROR, str(error)[:1024]

    node.started = timezone.now()
    node.save()

def perform_action(workflow, nodes, node):

    # MERGE and COMMIT act on Github right away, see actions.py. A failure is kept as
    # an ERROR, to be retried, or decided by hand.
    try:
        node.detail = actions.perform(workflow, nodes, node)[:256]
        node.error  = ''
        finish(node, 'done')

    except (actions.ActionError, WorkflowError, GithubError) as error:
        node.state, node.error = State.ERROR, str(error)[:1024]

    except Exception as error:
        traceback.print_exc()
        node.state, node.error = State.ERROR, ('Unexpected error: %s' % (error))[:1024]

    node.started = timezone.now()
    node.save()

def create_test(workflow, nodes, node):

    engines, names   = resolve_engines(workflow, nodes, node)
    post             = build_post(workflow.graph, node.key, engines)
    user             = User.objects.get(username=workflow.author)
    request          = SimpleNamespace(POST=post, user=user)

    for side in Graph.ENGINES[node.kind]:
        if len(post['%s_options' % (side)]) > option_limit():
            raise WorkflowError('%s Options exceed %d characters' % (side.title(), option_limit()))

    if node.kind == 'SPRT':
        test, errors = Create.create_new_test(request)

        # A Dev with the Bench of its Base is likely no functional change, which only has to
        # show that it loses nothing, see OpenBench/workloads/checks.py
        config = workflow.graph['nodes'][node.key].get('config', {})
        if test and (bounds := str(config.get('same_bench_bounds', '') or '').strip()) and same_bench(test):
            Create.apply_sprt_bounds(test, bounds)
            test.info = ('[Same Bench, so bounds %s] %s' % (bounds, test.info))[:1024]

    if node.kind == 'SPSA':
        test, errors = Create.create_new_tune(request)

    if errors:
        raise WorkflowError('\n'.join(errors))

    for side, name in names.items():
        engine = getattr(test, side)
        engine.name = name[:128]
        engine.save()

    label     = Graph.label(workflow.graph, node.key)
    test.info = ('[Workflow #%d: %s] %s' % (workflow.id, label, test.info))[:1024]
    test.save()

    # A Base pinned when the Workflow was saved may have newer commits by now
    Create.finalize_workload(test, user, check_base='base' in (node.pinned or {}))
    return test

def compute_status(workflow, nodes):

    states = [node.state for node in nodes.values()]

    if workflow.status == Status.ABORTED:
        return Status.ABORTED

    if all(state in [State.FINISHED, State.SKIPPED] for state in states):
        return Status.FINISHED

    if any(state in [State.STOPPED, State.ERROR] for state in states):
        return Status.ATTENTION

    return Status.PAUSED if workflow.paused else Status.RUNNING

def step(workflow_id):

    with transaction.atomic():

        # Deleted in the meantime, for example while the watcher went through its list
        if not (workflow := Workflow.objects.select_for_update().filter(id=workflow_id).first()):
            return None

        if workflow.is_final():
            return workflow

        query  = workflow.nodes.select_related('test', 'test__spsa_run', 'test__dev', 'test__base')
        nodes  = { node.key : node for node in query }
        before = ({ key : node.state for key, node in nodes.items() }, workflow.status)

        for node in nodes.values():
            if observe(workflow, node):
                node.save()

        # IF Nodes decide immediately, which may decide further Nodes, so repeat until settled
        decided = True
        while decided:

            decided = False
            states  = { key : (node.state, node.outcome) for key, node in nodes.items() }
            changes = Graph.propagate(workflow.graph, states)

            for key, (state, outcome, trigger) in changes.items():

                node = nodes[key]

                if state == 'READY' and not workflow.paused:
                    node.trigger = trigger
                    if node.kind == 'IF':
                        decide_condition(workflow, nodes, node)
                        decided = decided or node.state == State.FINISHED
                    elif node.kind in Graph.ACTIONS:
                        perform_action(workflow, nodes, node)
                        decided = decided or node.state == State.FINISHED
                    else:
                        start(workflow, nodes, node)

                elif state != 'READY':
                    node.state, node.outcome = state, outcome
                    node.finished = timezone.now()
                    node.save()

        # The watcher steps every Workflow every few seconds, so only write actual changes
        if (status := compute_status(workflow, nodes)) != workflow.status:
            workflow.status = status
            workflow.save(update_fields=['status', 'updated'])

        # Announced only once all of this is committed, and never what was rolled back
        if events := collect_events(workflow, nodes, *before):
            transaction.on_commit(lambda: notifications.notify(workflow, events))

    return workflow

def result_text(node):

    if node.state == State.SKIPPED:
        return 'skipped'

    if node.kind == 'SPRT' and node.test:
        return '%s, %s' % (node.outcome, notifications.test_result(node.test))

    if node.kind == 'SPSA' and node.tuned:
        return '%s, %s' % (node.outcome, node.tuned[:300])

    if node.kind in Graph.ACTIONS and node.detail:
        return '%s, %s' % (node.outcome or node.state.lower(), node.detail)

    return node.outcome or node.state.lower()

def collect_events(workflow, nodes, states, status):

    # What happened during one step, as ( kind, label, text ) for notifications.notify().
    # states and status are from before the step, so each change is announced once.
    level  = notifications.level(workflow.graph)
    events = []

    if level == 'off':
        return events

    for key, node in nodes.items():

        changed = states.get(key) != node.state
        label   = Graph.label(workflow.graph, key)

        if changed and node.state in [State.STOPPED, State.ERROR]:
            events.append(('attention', label, node.error or node.state.lower()))

        if changed and level == 'all' and node.state == State.FINISHED and node.kind in ['SPRT', 'SPSA'] + Graph.ACTIONS:
            events.append((node.outcome, label, result_text(node)))

    if workflow.status == Status.FINISHED and status != Status.FINISHED:
        events += [('finished', Graph.label(workflow.graph, key), result_text(node)) for key, node in nodes.items()]

    return events

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                           ACTIONS FROM THE USERS                            #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def set_paused(workflow_id, paused, username='OpenBench'):

    # Pausing holds the Tests that run, so that the workers move on to other Workloads, and
    # resuming continues them. Nothing is announced, and held Nodes stay RUNNING meanwhile.
    with transaction.atomic():
        workflow = Workflow.objects.select_for_update().get(id=workflow_id)
        workflow.paused = paused
        workflow.save()

        if paused:
            hold_tests(workflow, username)
        else:
            release_tests(workflow, username)

    return step(workflow_id)

def hold_tests(workflow, username):
    for node in workflow.nodes.filter(state=State.RUNNING, held=False).select_related('test'):
        if node.test and not node.test.finished:
            Test.objects.filter(id=node.test.id).update(finished=True)
            node.held = True
            node.save(update_fields=['held'])
            LogEvent.objects.create(author=username, summary='PAUSE Workflow #%d' % (workflow.id), log_file='', test_id=node.test.id)

def release_tests(workflow, username):
    for node in workflow.nodes.filter(held=True).select_related('test'):
        node.held = False
        node.save(update_fields=['held'])

        # Unless decided, deleted, or running again meanwhile. The time it was held does not count towards its speed.
        if node.test and node.test.finished and not (node.test.passed or node.test.failed or node.test.deleted):
            Test.objects.filter(id=node.test.id).update(finished=False, rate_updated=None)
            LogEvent.objects.create(author=username, summary='RESUME Workflow #%d' % (workflow.id), log_file='', test_id=node.test.id)

def stop_tests(workflow, username):
    workflow.nodes.filter(held=True).update(held=False) # Held Tests are stopped already, and stay so
    for node in workflow.nodes.filter(state=State.RUNNING).select_related('test'):
        if node.test and not node.test.finished:
            node.test.finished = True
            node.test.save()
            LogEvent.objects.create(author=username, summary='STOP', log_file='', test_id=node.test.id)

def abort(workflow_id, username):

    # Stops every Test that is still running, and never starts anything again
    with transaction.atomic():

        workflow = Workflow.objects.select_for_update().get(id=workflow_id)
        workflow.status = Status.ABORTED
        workflow.save()
        stop_tests(workflow, username)

    return workflow

def delete(workflow_id, username):

    # Removes the Workflow and its Nodes for good. Their Tests stay, as ordinary Tests,
    # but are stopped first if they still run, since nothing would follow them anymore.
    with transaction.atomic():
        workflow = Workflow.objects.select_for_update().get(id=workflow_id)
        stop_tests(workflow, username)
        workflow.delete()

def retry(workflow_id, key):

    # Attempts to start a Node that failed to start, for example after pushing a Bench
    with transaction.atomic():
        workflow = Workflow.objects.select_for_update().get(id=workflow_id)
        node     = workflow.nodes.get(key=key)

        if node.state != State.ERROR:
            raise WorkflowError('Only Nodes in ERROR can be retried')

        node.state, node.error = State.WAITING, ''
        node.save()

    return step(workflow_id)

def use_measured_bench(workflow_id, key, username):

    # Restarts the stopped Test of a Node with the Bench the workers measured. Like the REBENCH
    # action of the Test's page, this restarts the same Test, and the Node follows it, see observe().
    # So the Test keeps its commit: a branch is not looked up again, and cannot have moved on.
    with transaction.atomic():
        workflow = Workflow.objects.select_for_update().get(id=workflow_id)
        node     = workflow.nodes.get(key=key)

        if node.state != State.STOPPED or not node.test_id:
            raise WorkflowError('Only stopped Nodes can be restarted with a measured Bench')

        test = Test.objects.select_for_update().get(id=node.test_id)

        try: apply_measured_bench(test)
        except WorkloadActionError as error:
            raise WorkflowError(str(error))

        test.save()
        LogEvent.objects.create(author=username, summary='REBENCH', log_file='', test_id=test.id)

    return step(workflow_id)

def resolve(workflow_id, key, outcome):

    # Decides a stopped or failed Node by hand, as if its Test had that outcome
    with transaction.atomic():
        workflow = Workflow.objects.select_for_update().get(id=workflow_id)
        node     = workflow.nodes.select_related('test', 'test__spsa_run').get(key=key)

        if node.state not in [State.STOPPED, State.ERROR]:
            raise WorkflowError('Only STOPPED or ERROR Nodes can be decided by hand')

        if outcome not in Graph.outcomes(node.kind):
            raise WorkflowError('%s cannot be decided as "%s"' % (node.kind, outcome))

        if node.kind == 'SPSA' and node.test:
            node.tuned = OpenBench.spsa_utils.spsa_tuned_options(node.test)

        finish(node, outcome)
        node.error = ''
        node.save()

    return step(workflow_id)
