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

# Web interface for Workflows:
#
#   /workflows/                     List of all Workflows
#   /workflow/new/[?clone=<id>]     Graphical editor, which POSTs the graph as JSON
#   /workflow/<id>/                 The Workflow, its Nodes, and their Tests
#   /workflow/<id>/state/           The same as JSON, polled by the page
#   /workflow/<id>/edit/            The editor for a running Workflow, which POSTs the graph as JSON
#   /workflow/<id>/<action>/        POST: pause, resume, abort, retry, resolve

import json

from django.db.models import Count, Q
from django.http import JsonResponse

import OpenBench.model_utils
import OpenBench.stats
import OpenBench.views

from OpenBench.config import OPENBENCH_CONFIG
from OpenBench.models import Book, EngineConfig, Network, Profile, Workflow, WorkflowNode
from OpenBench.templatetags.mytags import workload_url
from OpenBench.workflows import graph as Graph
from OpenBench.workflows import runner

def enabled_profile(request):
    if request.user.is_authenticated:
        return Profile.objects.filter(user=request.user, enabled=True).first()

def workflows(request):

    # Counted by the database, and without the graphs, which the list never shows
    decided = Q(nodes__state__in=[WorkflowNode.State.FINISHED, WorkflowNode.State.SKIPPED])
    query   = Workflow.objects.defer('graph').order_by('-id').annotate(
        total=Count('nodes'), done=Count('nodes', filter=decided))[:200]

    rows = [{ 'workflow' : workflow, 'done' : workflow.done, 'total' : workflow.total } for workflow in query]
    return OpenBench.views.render(request, 'workflows.html', { 'rows' : rows })

def new_workflow(request):

    if not (profile := enabled_profile(request)):
        return OpenBench.views.redirect(request, '/login/', error='Only enabled users can create Workflows')

    if request.method == 'POST':
        return create_workflow(request)

    try: clone_id = int(request.GET.get('clone', 0))
    except ValueError: clone_id = 0

    clone = Workflow.objects.filter(id=clone_id).first()
    edit  = { 'submit_url' : '/workflow/new/', 'submit_text' : 'Create Workflow', 'locked' : [], 'state' : None }

    return render_editor(request, clone.graph if clone else None, 'Copy of %s' % (clone.name) if clone else '', edit)

def render_editor(request, graph, name, edit):

    engines = EngineConfig.objects.filter(enabled=True).order_by('name')

    data = {
        'networks'       : list(Network.objects.all().values()),
        'books'          : Book.objects.filter(enabled=True).order_by('name'),
        'engines'        : engines,
        'engine_configs' : { engine.name : OpenBench.model_utils.engine_config_to_dict(engine) for engine in engines },
        'clone_graph'    : graph,
        'clone_name'     : name,
        'edit'           : edit,
    }

    return OpenBench.views.render(request, 'workflow_edit.html', data)

def parse_body(request):

    # The editor sends { 'name' : ..., 'graph' : {...} }
    try:
        body = json.loads(request.body)
        return body['name'], body['graph']
    except:
        return None

def create_workflow(request):

    if not (body := parse_body(request)):
        return JsonResponse({ 'errors' : ['Malformed request'] }, status=400)

    workflow, errors = runner.create_workflow(request.user, *body)

    if errors:
        return JsonResponse({ 'errors' : errors }, status=400)

    return JsonResponse({ 'id' : workflow.id })

def edit_workflow(request, workflow):

    # Only the author, since the Tests of a Workflow are created in the author's name
    if not enabled_profile(request) or workflow.author != request.user.username:
        error = 'Only the author can edit a Workflow'

    elif workflow.status == Workflow.Status.ABORTED:
        error = 'Aborted Workflows cannot be edited'

    else: error = None

    if error and request.method == 'POST':
        return JsonResponse({ 'errors' : [error] }, status=403)

    if error:
        return OpenBench.views.redirect(request, '/workflow/%d/' % (workflow.id), error=error)

    if request.method == 'POST':

        if not (body := parse_body(request)):
            return JsonResponse({ 'errors' : ['Malformed request'] }, status=400)

        workflow, errors = runner.update_workflow(workflow.id, request.user, *body)

        if errors:
            return JsonResponse({ 'errors' : errors }, status=400)

        return JsonResponse({ 'id' : workflow.id })

    edit = {
        'submit_url'  : '/workflow/%d/edit/' % (workflow.id),
        'submit_text' : 'Save Workflow',
        'locked'      : [node.key for node in workflow.nodes.all() if runner.is_locked(node)],
        'state'       : workflow_state(workflow),
    }

    return render_editor(request, workflow.graph, workflow.name, edit)

def workflow(request, pk, action=None):

    # The state is polled by every open page, and never needs the graph
    query = Workflow.objects.defer('graph') if action == 'state' else Workflow.objects

    if not (workflow := query.filter(id=pk).first()):
        return OpenBench.views.redirect(request, '/workflows/', error='No such Workflow exists')

    if action == 'state':

        # Rendered pages enforce this in OpenBench.views.render(), the JSON has to do it here
        if OPENBENCH_CONFIG['require_login_to_view'] and not request.user.is_authenticated:
            return JsonResponse({ 'error' : 'Login required' }, status=403)

        return JsonResponse(workflow_state(workflow))

    if action == 'edit':
        return edit_workflow(request, workflow)

    if action is not None:
        return modify_workflow(request, workflow, action)

    profile = enabled_profile(request)
    data    = {
        'workflow'   : workflow,
        'state'      : workflow_state(workflow),
        'can_modify' : bool(profile and (profile.approver or workflow.author == request.user.username)),
        'can_edit'   : bool(profile and workflow.author == request.user.username and workflow.status != Workflow.Status.ABORTED),
        'networks'   : list(Network.objects.values('engine', 'sha256', 'name')),
    }

    return OpenBench.views.render(request, 'workflow.html', data)

def modify_workflow(request, workflow, action):

    target = '/workflow/%d/' % (workflow.id)

    if request.method != 'POST':
        return OpenBench.views.redirect(request, target)

    if not (profile := enabled_profile(request)):
        return OpenBench.views.redirect(request, '/login/', error='Only enabled users may modify Workflows')

    if not profile.approver and workflow.author != request.user.username:
        return OpenBench.views.redirect(request, target, error='You cannot interact with another user\'s Workflow')

    if workflow.is_final():
        return OpenBench.views.redirect(request, target, error='The Workflow is already %s' % (workflow.status))

    key = request.POST.get('key', '')

    try:
        if   action == 'pause'   : runner.set_paused(workflow.id, True)
        elif action == 'resume'  : runner.set_paused(workflow.id, False)
        elif action == 'abort'   : runner.abort(workflow.id, request.user.username)
        elif action == 'retry'   : runner.retry(workflow.id, key)
        elif action == 'resolve' : runner.resolve(workflow.id, key, request.POST.get('outcome', ''))
        elif action == 'fixbench': runner.use_measured_bench(workflow.id, key)
        else: return OpenBench.views.redirect(request, target, error='Unknown Workflow action')

    except WorkflowNode.DoesNotExist:
        return OpenBench.views.redirect(request, target, error='No such Node exists')

    except runner.WorkflowError as error:
        return OpenBench.views.redirect(request, target, error=str(error))

    return OpenBench.views.redirect(request, target, status='Workflow was updated: %s' % (action))

def workflow_state(workflow):

    # Everything the page needs to draw the progress of each Node
    nodes = {}

    for node in workflow.nodes.select_related('test', 'test__spsa_run'):
        nodes[node.key] = {
            'state'   : node.state,
            'outcome' : node.outcome,
            'error'   : node.error,
            'trigger' : node.trigger,
            'tuned'   : node.tuned,
            'detail'  : node.detail,
            'ports'   : Graph.outcomes(node.kind), # What the Node can be decided as, by hand
            'test'    : test_state(node.test) if node.test else None,
            'bench'   : bench_state(node) if node.state == 'STOPPED' and node.test else None,
        }

    return { 'status' : workflow.status, 'paused' : workflow.paused, 'nodes' : nodes }

def bench_state(node):

    # What workers measured, for a Test they stopped because of a wrong Bench
    found = runner.measured_benches(node.test, Graph.ENGINES[node.kind])
    return { side : { 'given' : getattr(node.test, side).bench, 'measured' : sorted(values) }
             for side, values in found.items() } or None

def test_state(test):

    state = {
        'id'       : test.id,
        'url'      : workload_url(test),
        'games'    : test.games,
        'approved' : test.approved,
        'finished' : test.finished,
        'deleted'  : test.deleted,
    }

    # Names of the Networks, which the Nodes only know by their sha
    state['dev_netname']  = test.dev_netname
    state['base_netname'] = test.base_netname

    if test.test_mode == 'SPRT':
        lower, elo, upper = OpenBench.stats.Elo(test.results())
        state['summary'] = 'LLR %.2f (%.2f, %.2f)' % (test.currentllr, test.lowerllr, test.upperllr)
        state['elo']     = 'Elo %+.2f ± %.2f' % (elo, max(upper - elo, elo - lower))

    if test.test_mode == 'SPSA':
        total = 2 * test.spsa_run.pairs_per * test.spsa_run.iterations
        state['summary'] = '%.1f%% of %d' % (100.0 * test.games / max(1, total), total)

    return state
