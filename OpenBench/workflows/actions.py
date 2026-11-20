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

# The action Nodes of Workflows, MERGE and COMMIT, see graph.py for what they do.
#
#   >>> detail = perform(workflow, nodes, node)
#
# Called by runner.step(), which keeps the Workflow locked meanwhile. Returns what was done,
# like the URL of a pull request, which the Node keeps in its detail. Raises an ActionError,
# or a github.GithubError, which leaves the Node in ERROR to be retried.
#
# Actions may run again, after a retry or after a step that failed before it was committed.
# So each of them is safe to repeat: Github answers a second merge with "nothing to merge",
# finds the open pull request, and a file that already holds the values is left alone.

import re

from OpenBench import github
from OpenBench.workflows import graph as Graph
from OpenBench.workflows import notifications
from OpenBench.workflows.options import parse_options
from OpenBench.workloads.verify_workload import GITHUB_SHA, message_bench

class ActionError(Exception):
    pass

def perform(workflow, nodes, node):

    config = workflow.graph['nodes'][node.key].get('config', {})

    if node.kind == 'MERGE':
        return merge(workflow, nodes, node, config)

    if node.kind == 'COMMIT':
        return commit_values(workflow, nodes, node, config)

    raise ActionError('Unknown action %s' % (node.kind))

def node_branch(workflow, nodes, key, side, seen=None):

    # The branch that a Node tested as its side, following references to their origin,
    # or None if it was given by a sha, or belongs to an attached Test
    from OpenBench.workflows.runner import effective_trigger

    seen   = seen or set()
    config = workflow.graph['nodes'][key].get('config', {})
    value  = str(config.get(side, '') or '').strip()

    if key in seen or config.get('existing_test') or not value:
        return None
    seen.add(key)

    if not (ref := Graph.parse_ref(value)):
        return None if GITHUB_SHA.match(value) else value

    target = effective_trigger(nodes, nodes[key]) if ref[0] == 'trigger' else ref[0]
    if target not in nodes:
        return None

    return node_branch(workflow, nodes, target, 'base' if ref[1] == 'base' else 'dev', seen)

def tested_results(workflow, nodes, sha):

    # One line for every finished SPRT of the Workflow whose Dev was this commit
    lines = []
    for key, other in nodes.items():
        if other.kind == 'SPRT' and other.state == 'FINISHED' and other.test and other.test.dev.sha == sha:
            lines.append('%s: %s, %s, Test #%d' % (Graph.label(workflow.graph, key), other.outcome,
                                                   notifications.test_result(other.test), other.test.id))
    return lines

def merge(workflow, nodes, node, config):

    from OpenBench.workflows.runner import referenced_engine

    source = str(config.get('source', '') or '@trigger.dev').strip()
    target = str(config.get('target', '') or 'main').strip()
    mode   = str(config.get('mode', '') or 'pr').strip()

    if ref := Graph.parse_ref(source):
        engine = referenced_engine(nodes, node, ref)
        repo, name, sha, bench = engine['repo'], engine['engine'], engine['sha'], engine['bench']
        branch = node_branch(workflow, nodes, effective_target(nodes, node, ref), 'base' if ref[1] == 'base' else 'dev')
        title  = engine['name']

    else: # A branch, of the Dev engine of the Workflow's defaults
        defaults = workflow.graph['defaults']
        repo, name = defaults.get('dev_repo', ''), defaults.get('dev_engine', '')
        commit = github.commit(repo, name, source)
        sha, bench, branch, title = commit['sha'], message_bench(commit['message']), source, source

    title   = str(config.get('title', '') or '').strip() or '%s (Workflow #%d)' % (title, workflow.id)
    results = tested_results(workflow, nodes, sha)
    body    = '\n'.join(results + ([''] if results else []) + (['Bench: %d' % (bench)] if bench else []))

    if mode == 'ff':
        github.fast_forward(repo, name, target, sha)
        detail = 'Fast-forwarded %s to %s' % (target, sha[:8])

    elif mode == 'merge':
        answer = github.merge(repo, name, target, sha, '%s\n\n%s' % (title, body))
        detail = 'Merged into %s as %s' % (target, answer['sha'][:8]) if answer.get('sha') else '%s already contained %s' % (target, sha[:8])

    else:
        # A pull request needs a branch that points at exactly the tested commit. The branch the
        # engine came from may have moved on since, in which case one is made for the commit.
        # Tags and short shas name the commit as well, but cannot be the head of a pull request.
        if not branch or not github.branch_exists(repo, name, branch) or github.commit(repo, name, branch)['sha'] != sha:
            branch = 'openbench/wf%d-%s' % (workflow.id, node.key)
            if not github.branch_exists(repo, name, branch):
                github.create_branch(repo, name, branch, sha)

        detail = github.open_pull_request(repo, name, branch, target, title, body)

    # Ladders that follow the target measure the new commit soon, see OpenBench/ladder.py
    if mode in ['ff', 'merge']:
        from OpenBench import ladder
        ladder.request_poll(repo, target)

    return detail

def effective_target(nodes, node, ref):
    from OpenBench.workflows.runner import effective_trigger
    return effective_trigger(nodes, node) if ref[0] == 'trigger' else ref[0]

def apply_values(text, values, patterns):

    # (new text, names that were not found). Each name's first match, of the first pattern
    # that finds it, has its value replaced. A value with spaces before it keeps its width.
    missing  = []
    patterns = [patterns] if isinstance(patterns, str) else patterns

    for name, value in values:

        for pattern in patterns:
            if match := re.search(pattern.replace('{name}', re.escape(name)), text):
                break
        else:
            missing.append(name)
            continue

        old = match.group(1)
        new = value.rjust(len(old)) if old[:1].isspace() or pattern in Graph.ALIGNED_PATTERNS else value
        text = text[:match.start(1)] + new + text[match.end(1):]

    return text, missing

def commit_values(workflow, nodes, node, config):

    from OpenBench.workflows.runner import effective_trigger

    key  = str(config.get('source', '') or '').strip() or effective_trigger(nodes, node)
    spsa = nodes.get(key)

    if not spsa or spsa.kind != 'SPSA' or spsa.state != 'FINISHED' or not spsa.tuned or not spsa.test:
        raise ActionError('%s has no tuned values to commit' % (key or 'The trigger'))

    test    = spsa.test
    repo    = test.dev_repo
    name    = test.dev_engine
    path    = str(config.get('file', '') or Graph.DEFAULT_FILE).strip()
    pattern = str(config.get('pattern', '') or '').strip() or Graph.DEFAULT_PATTERNS
    branch  = str(config.get('branch', '') or '').strip() or 'openbench/tuned-wf%d-%s' % (workflow.id, node.key)
    target  = str(config.get('target', '') or 'main').strip()
    mode    = str(config.get('mode', '') or 'pr').strip()
    values  = parse_options(spsa.tuned)
    label   = Graph.label(workflow.graph, key)

    # The values belong to the commit that was tuned, so the branch starts there
    if not github.branch_exists(repo, name, branch):
        github.create_branch(repo, name, branch, test.dev.sha)

    text, blob = github.read_file(repo, name, path, branch)
    new_text, missing = apply_values(text, values, pattern)

    if missing:
        raise ActionError('Not found in %s: %s' % (path, ', '.join(missing[:20])))

    message = 'Apply the values tuned by %s (Workflow #%d, Test #%d)\n\n%s' % (
        label, workflow.id, test.id, '\n'.join('%s = %s' % (option, value) for option, value in values))

    detail = '%s already holds the values' % (branch)
    if new_text != text:
        commit = github.write_file(repo, name, path, branch, new_text, blob, message)
        detail = 'Committed to %s as %s' % (branch, commit[:8])

    if mode == 'pr':
        detail = github.open_pull_request(repo, name, branch, target, message.split('\n')[0],
            message + '\n\nThe Bench changes with the new values, so measure it before merging.')

    return detail
