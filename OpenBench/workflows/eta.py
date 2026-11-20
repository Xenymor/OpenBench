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
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

# When the Nodes of a Workflow will start and finish, at the earliest:
#
#   >>> estimate(graph, nodes, now)
#   { key : { 'start' : datetime or None, 'finish' : datetime or None } }
#
# Running Nodes finish as OpenBench/eta.py estimates their Tests. Waiting Nodes start once
# their first incoming edge may fire (a JOIN once all may have), and run as long as recent
# Tests of their kind and time control took. Since it is not known which outcome a Node will
# have, every edge is assumed to fire: the times are the earliest ones possible, and Nodes
# that will be skipped get times as well. None means that there is no telling, for example
# behind a Node that was stopped, or without any history at that time control.

import datetime

import OpenBench.eta

from OpenBench.utils import TimeControl
from OpenBench.workflows import graph as Graph

INSTANT = ['IF', 'JOIN', 'MERGE', 'COMMIT'] # Kinds that take no time of their own

def topological(graph):

    # Keys in an order where every Node comes after those it waits for, IF waits included
    full   = Graph.with_waits(graph)
    degree = { key : len(Graph.incoming(full, key)) for key in graph['nodes'] }
    ready  = sorted(key for key, count in degree.items() if count == 0)
    order  = []

    while ready:
        key = ready.pop(0)
        order.append(key)
        for edge in Graph.outgoing(full, key):
            degree[edge['to']] -= 1
            if degree[edge['to']] == 0:
                ready.append(edge['to'])

    return order

def duration(graph, key, cache):

    # Seconds that a Node which has not started will take, or None
    node   = graph['nodes'][key]
    config = node.get('config', {})

    if node['kind'] in INSTANT:
        return 0.0

    # Imported late, since the runner pulls in the workload modules
    from OpenBench.workflows.runner import node_defaults, setting

    defaults = node_defaults(graph, key)
    control  = str(config.get('time_control', '')).strip() or str(defaults.get('dev_time_control', ''))

    try: control = TimeControl.parse(control)
    except Exception: return None

    games = None
    if node['kind'] == 'SPSA':
        try:
            games  = 2 * int(setting(defaults, config, 'spsa_pairs_per')) * int(setting(defaults, config, 'spsa_iterations'))
            games *= float(config.get('at_percent') or 100) / 100.0
        except ValueError:
            return None

    if (node['kind'], control, games) not in cache:
        cache[(node['kind'], control, games)] = OpenBench.eta.typical_seconds(node['kind'], control, games)

    return cache[(node['kind'], control, games)]

def estimate(graph, nodes, now):

    # nodes maps keys to WorkflowNodes, with their Tests
    times, cache = {}, {}
    states       = { key : (node.state, node.outcome) for key, node in nodes.items() }

    def finish_of(key):
        return times.get(key, {}).get('finish')

    for key in topological(graph):

        node = nodes.get(key)
        kind = graph['nodes'][key]['kind']

        if node is None or node.state == 'SKIPPED':
            times[key] = { 'start' : None, 'finish' : None }
            continue

        if node.state == 'FINISHED':
            done = node.finished or now
            times[key] = { 'start' : node.started or done, 'finish' : done }
            continue

        if node.state in ['STOPPED', 'ERROR']:
            times[key] = { 'start' : node.started, 'finish' : None }
            continue

        if node.state == 'RUNNING':
            seconds = OpenBench.eta.remaining_seconds(node.test, now) if node.test else None
            times[key] = { 'start' : node.started, 'finish' : now + datetime.timedelta(seconds=seconds) if seconds is not None else None }
            continue

        # WAITING: when may its incoming edges fire?
        candidates = []
        for edge in Graph.incoming(graph, key):
            state = Graph.edge_state(graph, edge, states)
            if state == Graph.FIRED:
                candidates.append(now)
            elif state == Graph.PENDING:
                candidates.append(finish_of(edge['from']))

        if not candidates:
            times[key] = { 'start' : None, 'finish' : None }
            continue

        if kind == 'JOIN':
            start = None if None in candidates else max(candidates)
        else:
            known = [time for time in candidates if time is not None]
            start = min(known) if known else None

        # An IF also waits for the SPRTs that its condition names
        if kind == 'IF' and start is not None:
            for target in Graph.condition_references(graph, key):
                if target in nodes and nodes[target].state not in ['FINISHED', 'SKIPPED']:
                    waited = finish_of(target)
                    start  = None if waited is None else max(start, waited)
                    if start is None:
                        break

        start   = max(start, now) if start is not None else None
        seconds = duration(graph, key, cache) if start is not None else None
        times[key] = { 'start' : start, 'finish' : start + datetime.timedelta(seconds=seconds) if seconds is not None else None }

    return times

def serialize(times):
    return { key : { field : value.isoformat() if value else None for field, value in entry.items() } for key, entry in times.items() }
