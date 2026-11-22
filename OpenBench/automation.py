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

# Push-to-Test: Workflows that start themselves when a branch is pushed.
#
# A WorkflowTemplate is a Workflow graph in which '$branch' stands for the pushed branch.
# A commit message that names a Template starts it for the branch it was pushed to:
#
#   [ob]           the Template named 'default'
#   [ob:ltc]       the Template named 'ltc'
#
# Only the message of the newest commit of a push counts, so that rebasing a branch
# with an old marker in it starts nothing. Pushes without new commits, like a new branch
# for a commit that exists already, start nothing either. Github announces pushes through a webhook:
#
#   Payload URL    https://<your site>/api/github/push/
#   Content type   application/json
#   Secret         the first line of Config/credentials.webhook, which is never committed
#   Events         Just the push event
#
# The webhook only records the push as a PushEvent, so that Github gets its answer at once.
# The WorkflowWatcher processes them, see process_pushes(): it starts the Templates, and
# asks the Ladders of the pushed branch to look at it, see OpenBench/ladder.py.

import copy
import datetime
import hashlib
import hmac
import json
import os
import re
import traceback

from django.contrib.auth.models import User
from django.utils import timezone

from OpenBench import github
from OpenBench import ladder
from OpenBench.models import PushEvent, WorkflowTemplate
from OpenBench.workflows import notifications
from OpenSite.settings import PROJECT_PATH

SECRET_FILE    = os.path.join(PROJECT_PATH, 'Config', 'credentials.webhook')
MARKER_PATTERN = re.compile(r'\[ob(?::([A-Za-z0-9_.-]{1,64}))?\]', re.IGNORECASE)
NAME_PATTERN   = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')
PLACEHOLDER    = '$branch'
DEFAULT_NAME   = 'default'
OWN_BRANCHES   = 'openbench/' # Prefix of the branches that the MERGE and COMMIT Nodes make
KEEP_DAYS      = 90 # Processed pushes are kept this long, to be looked at on the Templates page

def read_secret(path=None):
    try:
        with open(path or SECRET_FILE) as fin:
            return fin.readline().strip() or None
    except OSError:
        return None

def verify_signature(secret, body, signature):
    expected = 'sha256=' + hmac.new(secret.encode('utf-8'), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or '')

def markers(message):
    # Names of the Templates that a commit message asks for, in order, without repeats
    names = [(name or DEFAULT_NAME).lower() for name in MARKER_PATTERN.findall(message or '')]
    return list(dict.fromkeys(names))

def receive(headers, body):

    # (status, answer) for a request of Github's webhook. Never raises.
    if not (secret := read_secret()):
        return 404, { 'error' : 'Webhooks are not set up on this server' }

    if not verify_signature(secret, body, headers.get('X-Hub-Signature-256', '')):
        return 403, { 'error' : 'Bad signature' }

    event = headers.get('X-GitHub-Event', '')
    if event == 'ping':
        return 200, { 'pong' : True }

    if event != 'push':
        return 200, { 'ignored' : event }

    try: data = json.loads(body)
    except ValueError:
        return 400, { 'error' : 'Malformed payload' }

    ref = str(data.get('ref', ''))
    if not ref.startswith('refs/heads/') or data.get('deleted') or not data.get('head_commit'):
        return 200, { 'ignored' : ref }

    # Only new commits count. A branch made for a commit that exists already, as the MERGE and
    # COMMIT Nodes make them, repeats that commit's message, and its marker would start the
    # Template again, for the new branch, which would make a branch of its own, and so on.
    if not data.get('commits') or ref.startswith('refs/heads/%s' % (OWN_BRANCHES)):

        # Ladders of the branch still look at its head, which may have moved, like after a force push
        if repo := github.normalize_repo((data.get('repository') or {}).get('html_url', '')):
            ladder.request_poll(repo, ref[len('refs/heads/'):])

        return 200, { 'ignored' : 'No new commits on %s' % (ref) }

    repo     = github.normalize_repo((data.get('repository') or {}).get('html_url', ''))
    delivery = str(headers.get('X-GitHub-Delivery', '') or data.get('after', ''))[:64]

    if not repo:
        return 400, { 'error' : 'Unknown repository' }

    _, created = PushEvent.objects.get_or_create(delivery=delivery, defaults={
        'repo'    : repo,
        'branch'  : ref[len('refs/heads/'):][:256],
        'sha'     : str(data.get('after', ''))[:64],
        'message' : str(data['head_commit'].get('message', ''))[:20000],
    })

    return 202, { 'queued' : created }

def substitute(value, branch):

    # '$branch' replaced in every string of the graph's Nodes, but never in their keys
    if isinstance(value, str):
        return value.replace(PLACEHOLDER, branch)
    if isinstance(value, dict):
        return { key : substitute(item, branch) for key, item in value.items() }
    if isinstance(value, list):
        return [substitute(item, branch) for item in value]
    return value

def instantiate(template, branch):
    graph = copy.deepcopy(template.graph)
    graph['nodes'] = substitute(graph.get('nodes', {}), branch)
    return graph

def template_repo(graph):
    return github.normalize_repo((graph.get('defaults') or {}).get('dev_repo', ''))

def tests_itself(graph):

    # Keys of the SPRTs that would test an engine against the very same engine: the same branch,
    # in the same repository, with the same options, Network and time control. A marker that
    # reaches the branch the Template tests against, for example through the message of a merge,
    # does that, which can never tell anything. Other differences, like Threads, are fine.
    defaults, same = graph.get('defaults', {}), []

    def side(config, name):
        return (str(config.get(name, '')).strip(), str(defaults.get('%s_repo' % (name), '')),
                str(defaults.get('%s_options' % (name), '')) + '|' + str(config.get('%s_options' % (name), '')),
                str(defaults.get('%s_network' % (name), '')) + '|' + str(config.get('%s_network' % (name), '')),
                str(defaults.get('%s_time_control' % (name), '')), str(config.get('%s_bench' % (name), '')))

    for key, node in graph['nodes'].items():
        config = node.get('config', {})
        if node.get('kind') == 'SPRT' and not config.get('existing_test') and side(config, 'dev') == side(config, 'base'):
            same.append(key)

    return sorted(same)

def start_template(template, branch):

    # (Workflow, None) or (None, errors). Imported late, since the runner pulls in the views.
    from OpenBench.workflows import runner

    if not (user := User.objects.filter(username=template.author).first()):
        return None, ['The author of Template %s no longer exists' % (template.name)]

    graph = instantiate(template, branch)
    if same := tests_itself(graph):
        return None, ['Template %s would test %s against itself (%s)' % (template.name, branch, ', '.join(same))]

    name = ('%s: %s' % (template.name, branch))[:128]
    workflow, errors = runner.create_workflow(user, name, graph)

    if errors:
        return None, errors

    # Only the newest one runs: the older Workflows of the Template for the branch are aborted,
    # which stops their Tests. Only once the new one got going, so that a push whose Nodes fail
    # at once, for example without a Bench, leaves the running one alone. Only older ones, by
    # their id, so that two pushes handled at the same time never abort each other.
    from OpenBench.models import Workflow
    Workflow.objects.filter(id=workflow.id).update(template=template.name, branch=branch[:256])
    workflow.replaced = []

    if workflow.status != Workflow.Status.ATTENTION:
        older = Workflow.objects.filter(template=template.name, branch=branch[:256], id__lt=workflow.id)
        for previous in older.exclude(status__in=[Workflow.Status.FINISHED, Workflow.Status.ABORTED]):
            runner.abort(previous.id, 'OpenBench')
            workflow.replaced.append(previous.id)

    return workflow, None

def handle(event):

    # What happened for one push, as a short text for PushEvent.result
    results = []

    # Ladders of the branch measure the push, whatever its message
    ladder.request_poll(event.repo, event.branch)

    for name in markers(event.message):

        template = WorkflowTemplate.objects.filter(name__iexact=name, enabled=True).first()

        if not template:
            results.append('No enabled Template "%s"' % (name))
            notifications.announce('attention', 'Push of %s asked for an unknown Template' % (event.branch),
                                   ['No enabled Template is named "%s"' % (name)], '/workflow/templates/')
            continue

        if template_repo(template.graph) != github.normalize_repo(event.repo):
            results.append('Template %s is for another repository' % (template.name))
            continue

        # For example the base branch, after a MERGE brought a commit with a marker into it
        if tests_itself(instantiate(template, event.branch)):
            results.append('Template %s skipped, since it would test %s against itself' % (template.name, event.branch))
            continue

        workflow, errors = start_template(template, event.branch)

        if errors:
            results.append('Template %s failed: %s' % (template.name, '; '.join(errors)))
            notifications.announce('attention', 'Push of %s could not start Template %s' % (event.branch, template.name),
                                   errors[:10], '/workflow/templates/')

        else:
            replaced = getattr(workflow, 'replaced', [])
            results.append('Started Workflow #%d' % (workflow.id) + (', replacing #%s' % (', #'.join(map(str, replaced))) if replaced else ''))
            notifications.announce('done', 'Push of %s started Workflow #%d' % (event.branch, workflow.id),
                                   ['%s, from Template %s' % (workflow.name, template.name)]
                                   + (['Replaces Workflow #%s, which was aborted' % (', #'.join(map(str, replaced)))] if replaced else []),
                                   '/workflow/%d/' % (workflow.id))

    return '; '.join(results) or 'No markers'

def process_pushes(limit=20):

    # Called by the WorkflowWatcher. Each push is marked first, so that a broken one is never retried forever
    events = list(PushEvent.objects.filter(processed=False).order_by('id')[:limit])

    # Only when there is something to do, the old ones are dropped as well
    if events:
        PushEvent.objects.filter(processed=True, created__lt=timezone.now() - datetime.timedelta(days=KEEP_DAYS)).delete()

    for event in events:

        if not PushEvent.objects.filter(id=event.id, processed=False).update(processed=True):
            continue # Taken by another process

        try: result = handle(event)
        except Exception as error:
            traceback.print_exc()
            result = 'Unexpected error: %s' % (error)

        PushEvent.objects.filter(id=event.id).update(result=result[:1024])

def verify_template(name, graph):

    # Errors of a Template that is about to be saved. Its engines are only known once a
    # branch is pushed, so only the structure and the settings are verified, as for a Workflow.
    from OpenBench.workflows import graph as Graph
    from OpenBench.workflows import runner

    if not NAME_PATTERN.match(str(name or '')):
        return ['Template names are 1-64 of [A-Za-z0-9_.-], as used in markers like [ob:name]']

    if errors := Graph.validate(graph):
        return errors

    if any(node.get('config', {}).get('existing_test') for node in graph['nodes'].values()):
        return ['Templates cannot attach existing Tests, since they run again for every push']

    if not template_repo(graph):
        return ['The defaults need the Dev Source, which pushes are matched against']

    return runner.verify_settings(graph)

def unresolve(graph):

    # A Workflow's graph as the start of a Template: the same, without attached Tests,
    # which each push would otherwise share. Its branches still have to become '$branch'.
    graph = copy.deepcopy(graph)
    for node in graph.get('nodes', {}).values():
        node.get('config', {}).pop('existing_test', None)
    return graph
