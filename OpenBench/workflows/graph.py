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

# A Workflow is a directed acyclic graph of Nodes, stored as JSON in Workflow.graph:
#
#   { 'defaults' : { 'dev_engine' : ..., 'book_name' : ..., ... },
#     'nodes'    : { key : { 'kind' : 'SPRT', 'label' : ..., 'config' : {...}, 'x' : 0, 'y' : 0 } },
#     'edges'    : [ { 'from' : key, 'port' : 'passed', 'to' : key }, ... ] }
#
# The defaults and every Node's config use the field names of the workload forms,
# so that a Test is created by merging them into one POST, see runner.py.
#
# Once a Node is FINISHED, exactly one of its ports fired. Edges from that port
# fire, edges from its other ports are not taken, and edges from a SKIPPED Node
# are skipped. A Node starts as soon as any incoming edge fired, and is skipped
# once all are decided without any having fired. A JOIN waits until all incoming
# edges are decided, then fires 'all', 'some' or 'none' of them having fired.
# Edges from SKIPPED Nodes are left out of that count, since those never ran.
# So after 'x vs main' or 'y vs main', of which only one runs, 'all' means that
# the one which ran has passed.
#
# An SPRT also fires 'done' alongside 'passed' or 'failed', so that an IF Node
# can look at its whole result. An IF evaluates a condition like 'failed && elo > 3'
# (see conditions.py) on the SPRT that started it, or on the one named in its
# 'source', and fires 'true' or 'false'. Variables like 'x.passed' refer to other
# SPRTs by key, and the IF waits until each of those has finished or was skipped.
#
# The engines of a Node are either a branch, tag or commit sha, resolved when the
# Node starts, or a reference '@<key>.<dev|base|tuned>' to the exact engine that
# another Node tested. '@trigger' names the Node whose edge started this one, and
# looks through IF Nodes to the Node that started them.
# 'tuned' is the Dev of an SPSA, with the values frozen when it finished.
#
# This module never touches the database.

import re

from OpenBench.workflows import conditions

PORTS = {
    'SPRT' : ['passed', 'failed', 'done'],
    'SPSA' : ['done'],
    'JOIN' : ['all', 'some', 'none'],
    'IF'   : ['true', 'false'],
}

# Ports that fire with every outcome of a kind, in addition to the outcome itself
ALWAYS = { 'SPRT' : ['done'] }

def outcomes(kind):
    # Ports that a Node of this kind can finish with, as opposed to those that always fire
    return [port for port in PORTS[kind] if port not in ALWAYS.get(kind, [])]

# Engine fields of each kind, and which reference targets each kind provides
ENGINES    = { 'SPRT' : ['dev', 'base'], 'SPSA' : ['dev'], 'JOIN' : [], 'IF' : [] }
REF_FIELDS = { 'SPRT' : ['dev', 'base'], 'SPSA' : ['dev', 'tuned'], 'JOIN' : [], 'IF' : [] }

KEY_PATTERN    = re.compile(r'^[A-Za-z0-9_-]{1,32}$')
REF_PATTERN    = re.compile(r'^@([A-Za-z0-9_-]{1,32})\.(dev|base|tuned)$')
BRANCH_PATTERN = re.compile(r'^[^\s@~^:?*\[\\]{1,128}$')

MAX_NODES    = 64
MAX_POSITION = 100000 # Pixels, so that no graph can blow up the canvas of its viewers
MAX_TEST_ID  = 2 ** 31 - 1

# Edge states, as seen from the Node they point to
FIRED, NOT_TAKEN, SKIPPED, PENDING = 'FIRED', 'NOT_TAKEN', 'SKIPPED', 'PENDING'

def bench_override(config, side):

    # A Bench given on the Node, or None to autofill it, or carry it over from a reference.
    # Thousands separators are dropped, as when a Bench is read from a commit message.
    value = str(config.get('%s_bench' % (side), '') or '').strip()
    value = value.replace(',', '').replace('_', '').replace(' ', '')
    return None if value.lower() in ['', 'autofill'] else value

def network_changed(graph, config, side):

    # Whether a Node picks another Network than the one its engine would otherwise use.
    # A reference keeps the Network it was tested with, so any choice may be another one.
    value = str(config.get('%s_network' % (side), '') or '').strip()
    if not value:
        return False

    if parse_ref(config.get(side)):
        return True

    chosen  = '' if value.lower() == 'none' else value
    default = str(graph['defaults'].get('%s_network' % (side), '') or '').strip()
    return chosen != default

def parse_ref(value):

    # ('trigger' or key, field) for a reference, otherwise None
    match = REF_PATTERN.match(str(value or '').strip())
    return match.groups() if match else None

def incoming(graph, key):
    return [edge for edge in graph['edges'] if edge['to'] == key]

def outgoing(graph, key):
    return [edge for edge in graph['edges'] if edge['from'] == key]

def label(graph, key):
    return graph['nodes'][key].get('label') or key

def roots(graph):
    return [key for key in graph['nodes'] if not incoming(graph, key)]

def trigger_kinds(graph, key, seen=None):

    # Kinds of the Nodes that @trigger may refer to, looking through IF Nodes
    seen  = seen or set()
    kinds = set()

    for edge in incoming(graph, key):
        source = edge['from']
        if graph['nodes'][source]['kind'] != 'IF':
            kinds.add(graph['nodes'][source]['kind'])
        elif source not in seen:
            seen.add(source)
            kinds |= trigger_kinds(graph, source, seen) or { 'JOIN' } # An IF without incoming edges
    return kinds

def reachable(graph, starts, removed=()):

    seen, stack = set(), [key for key in starts if key not in removed]

    while stack:
        key = stack.pop()
        if key in seen: continue
        seen.add(key)
        stack.extend(edge['to'] for edge in outgoing(graph, key) if edge['to'] not in removed)

    return seen

def dominates(graph, dominator, key):

    # Every path from a root to key passes through dominator, or through an IF that
    # waits for it. So when key starts, dominator has certainly finished, and its
    # engines are known. (Unless such an IF was decided by hand, which the runner reports.)
    waiting = { other for other, node in graph['nodes'].items()
                if node['kind'] == 'IF' and dominator in condition_references(graph, other) }

    return dominator != key and key not in reachable(graph, roots(graph), removed={ dominator } | waiting)

def is_acyclic(graph):

    # Kahn's algorithm: Every Node is removed once all of its parents were
    degree = { key : len(incoming(graph, key)) for key in graph['nodes'] }
    ready  = [key for key, count in degree.items() if count == 0]
    count  = 0

    while ready:
        key    = ready.pop()
        count += 1
        for edge in outgoing(graph, key):
            degree[edge['to']] -= 1
            if degree[edge['to']] == 0:
                ready.append(edge['to'])

    return count == len(graph['nodes'])

def validate(graph):

    # Structure only. The settings of each Node are checked by the workload
    # verifications, see runner.verify_settings(). Returns a list of errors.

    try:
        nodes, edges = graph['nodes'], graph['edges']
        assert isinstance(graph.get('defaults'), dict)
        assert isinstance(nodes, dict) and isinstance(edges, list)
    except:
        return ['Malformed Workflow']

    if not nodes:
        return ['A Workflow needs at least one Node']

    if len(nodes) > MAX_NODES:
        return ['A Workflow may have at most %d Nodes' % (MAX_NODES)]

    for key, node in nodes.items():

        if not KEY_PATTERN.match(key):
            return ['Malformed Node key "%s"' % (key)]

        if not isinstance(node, dict) or node.get('kind') not in PORTS:
            return ['Unknown kind of Node "%s"' % (key)]

        if not isinstance(node.get('config', {}), dict) or len(str(node.get('label', ''))) > 64:
            return ['Malformed Node "%s"' % (key)]

        try: assert all(0 <= float(node.get(axis, 0)) <= MAX_POSITION for axis in ['x', 'y'])
        except: return ['Malformed position of Node "%s"' % (key)]

    for edge in edges:

        try: assert edge['from'] in nodes and edge['to'] in nodes
        except: return ['Edge between unknown Nodes']

        if edge.get('port') not in PORTS[nodes[edge['from']]['kind']]:
            return ['%s has no output "%s"' % (label(graph, edge['from']), edge.get('port'))]

        if edge['from'] == edge['to']:
            return ['%s may not be connected to itself' % (label(graph, edge['from']))]

    pairs = [(edge['from'], edge['port'], edge['to']) for edge in edges]
    if len(pairs) != len(set(pairs)):
        return ['Edges may not be duplicated']

    if not is_acyclic(graph):
        return ['Workflows may not contain cycles']

    errors = []
    for key, node in nodes.items():
        errors += validate_node(graph, key, node)

    # An IF also waits for the SPRTs it names. Two IFs that each wait for something
    # behind the other would never start, which is a cycle once waits count as edges.
    if not errors and not is_acyclic(with_waits(graph)):
        errors.append('IF Nodes wait for each other in a cycle, through the SPRTs named in their conditions')

    return errors

def with_waits(graph):

    waits = [{ 'from' : target, 'port' : 'wait', 'to' : key }
             for key, node in graph['nodes'].items() if node['kind'] == 'IF'
             for target in condition_references(graph, key) if target in graph['nodes']]

    return { **graph, 'edges' : graph['edges'] + waits }

def validate_node(graph, key, node):

    errors  = []
    name    = label(graph, key)
    config  = node.get('config', {})
    parents = incoming(graph, key)

    if node['kind'] == 'JOIN':
        if len(parents) < 2:
            errors.append('%s must have at least two incoming edges' % (name))
        return errors

    if node['kind'] == 'IF':
        return validate_condition(graph, key, node)

    if node['kind'] == 'SPSA':
        try: assert 0.0 < float(config.get('at_percent', 100)) <= 100.0
        except: errors.append('%s: "Done at %%" must be within (0, 100]' % (name))

    # Engines are irrelevant when attaching an existing Test
    if config.get('existing_test'):

        try: assert 0 < int(config['existing_test']) <= MAX_TEST_ID
        except: errors.append('%s refers to an invalid Test' % (name))

        if parents:
            errors.append('%s attaches an existing Test, so it cannot have incoming edges' % (name))

        return errors

    for field in ENGINES[node['kind']]:

        value = str(config.get(field, '')).strip()
        bench = bench_override(config, field)

        if bench is not None and not (bench.isdigit() and int(bench) > 0):
            errors.append('%s: %s Bench must be a positive number, or empty to autofill it' % (name, field.title()))

        # A Bench belongs to a commit and a Network. Both the Bench in a commit message and the
        # one a reference carries over were measured with another Network than the Node picks.
        if network_changed(graph, config, field) and bench is None:
            errors.append('%s: %s picks its own Network, so its Bench with that Network must be given' % (name, field.title()))

        if not (ref := parse_ref(value)):
            if not BRANCH_PATTERN.match(value):
                errors.append('%s: %s must be a branch, tag, sha, or a reference like @trigger.dev' % (name, field.title()))
            continue

        target, target_field = ref

        if target == 'trigger':

            kinds = trigger_kinds(graph, key)

            if not parents:
                errors.append('%s: @trigger needs an incoming edge' % (name))

            elif 'JOIN' in kinds:
                errors.append('%s: @trigger is ambiguous after a JOIN, refer to a Node by its key' % (name))

            elif any(target_field not in REF_FIELDS[kind] for kind in kinds):
                errors.append('%s: Not every incoming Node provides @trigger.%s' % (name, target_field))

        elif target not in graph['nodes']:
            errors.append('%s: Unknown Node "%s"' % (name, target))

        elif target_field not in REF_FIELDS[graph['nodes'][target]['kind']]:
            errors.append('%s: %s provides no "%s"' % (name, label(graph, target), target_field))

        elif not dominates(graph, target, key):
            errors.append('%s: %s must have finished on every path that leads here' % (name, label(graph, target)))

    return errors

def condition_references(graph, key):

    # Keys of the other SPRTs named in an IF's condition, which it waits for
    try: return conditions.references(graph['nodes'][key].get('config', {}).get('condition', '')) - { None }
    except conditions.ConditionError: return set()

def validate_condition(graph, key, node):

    errors = []
    name   = label(graph, key)
    config = node.get('config', {})
    source = str(config.get('source', '') or '').strip()

    if not incoming(graph, key):
        errors.append('%s must have an incoming edge' % (name))

    try: uses_own = None in conditions.references(config.get('condition', ''))
    except conditions.ConditionError as error:
        return errors + ['%s: %s' % (name, error)]

    # Named SPRTs are waited for, so none of them may only run after the IF itself
    downstream = reachable(graph, [edge['to'] for edge in outgoing(graph, key)])
    for target in sorted(condition_references(graph, key)):

        if target not in graph['nodes'] or graph['nodes'][target]['kind'] != 'SPRT':
            errors.append('%s: "%s" in the condition is not the key of an SPRT' % (name, target))

        elif target in downstream:
            errors.append('%s: %s runs after this IF, so the IF cannot wait for it' % (name, label(graph, target)))

    # Plain variables are evaluated on an SPRT, which must have finished once the IF starts
    if uses_own:
        errors += validate_condition_source(graph, key, source)

    return errors

def validate_condition_source(graph, key, source):

    name = label(graph, key)

    if not source:
        if trigger_kinds(graph, key) - { 'SPRT' }:
            return ['%s: Without a Source, every incoming edge must come from an SPRT' % (name)]

    elif source not in graph['nodes'] or graph['nodes'][source]['kind'] != 'SPRT':
        return ['%s: Source must be the key of an SPRT' % (name)]

    elif not dominates(graph, source, key):
        return ['%s: %s must have finished on every path that leads here' % (name, label(graph, source))]

    return []

def edge_state(graph, edge, states):

    state, outcome = states[edge['from']]

    if state == 'FINISHED':
        fired = [outcome] + ALWAYS.get(graph['nodes'][edge['from']]['kind'], [])
        return FIRED if edge['port'] in fired else NOT_TAKEN

    return SKIPPED if state == 'SKIPPED' else PENDING

def propagate(graph, states):

    # states maps every key to (state, outcome), as stored in the WorkflowNodes.
    # Returns { key : (state, outcome, trigger) } for every Node that changed:
    # READY Nodes are to be started, JOINs are decided, and Nodes are SKIPPED.
    # Skips and JOINs cascade, so this repeats until nothing changes anymore.

    states  = dict(states)
    changes = {}

    while True:

        changed = False

        for key, node in graph['nodes'].items():

            if states[key][0] != 'WAITING':
                continue

            parents = [(edge, edge_state(graph, edge, states)) for edge in incoming(graph, key)]
            fired   = [edge for edge, state in parents if state == FIRED]
            pending = any(state == PENDING for edge, state in parents)

            if not parents:
                change = ('READY', '', '')

            elif node['kind'] == 'IF':

                # Started like any other Node, but only once every SPRT it names is decided
                waiting = any(states.get(target, ('', ''))[0] not in ['FINISHED', 'SKIPPED']
                              for target in condition_references(graph, key))

                if fired and not waiting:
                    change = ('READY', '', fired[0]['from'])

                elif not fired and not pending:
                    change = ('SKIPPED', '', '')

                else: continue

            elif node['kind'] != 'JOIN':

                if fired: # Started by the first edge that fired, in the order of the graph
                    change = ('READY', '', fired[0]['from'])

                elif not pending:
                    change = ('SKIPPED', '', '')

                else: continue

            else:

                if pending:
                    continue

                ran = [edge for edge, state in parents if state != SKIPPED]

                if not ran:
                    change = ('SKIPPED', '', '')

                else:
                    outcome = 'all' if len(fired) == len(ran) else 'some' if fired else 'none'
                    change  = ('FINISHED', outcome, '')

            states[key]  = change[:2]
            changes[key] = change
            changed      = True

        if not changed:
            return changes
