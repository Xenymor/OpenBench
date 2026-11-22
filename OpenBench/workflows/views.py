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
#   /workflow/templates/...         Templates, which pushes start, see the end of this module

import json
import traceback

from django.db.models import Count, Q
from django.http import JsonResponse
from django.utils import timezone

import OpenBench.eta
import OpenBench.model_utils
import OpenBench.stats
import OpenBench.views

from OpenBench.config import OPENBENCH_CONFIG
from OpenBench import automation
from OpenBench.models import Book, EngineConfig, Network, Profile, PushEvent, Workflow, WorkflowNode, WorkflowTemplate
from OpenBench.templatetags.mytags import workload_url
from OpenBench import error_watch
from OpenBench.workflows import eta
from OpenBench.workflows import graph as Graph
from OpenBench.workflows import runner
from OpenBench.workloads.checks import describe as describe_checks

from OpenBench.workloads.modify_workload import bench_corrections

def enabled_profile(request):
    if request.user.is_authenticated:
        return Profile.objects.filter(user=request.user, enabled=True).first()

def workflows(request):

    # Counted by the database, and without the graphs, which the list never shows
    decided = Q(nodes__state__in=[WorkflowNode.State.FINISHED, WorkflowNode.State.SKIPPED])
    query   = Workflow.objects.defer('graph').order_by('-id').annotate(
        total=Count('nodes'), done=Count('nodes', filter=decided))[:200]

    # Deleting is offered where it is allowed, as for the other actions in modify_workflow()
    profile = enabled_profile(request)
    rows    = [{ 'workflow' : workflow, 'done' : workflow.done, 'total' : workflow.total,
                 'can_delete' : bool(profile and (profile.approver or workflow.author == request.user.username)) }
               for workflow in query]

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

    if not (workflow := Workflow.objects.filter(id=pk).first()):

        # The editor and the polled state read JSON, which a redirect would only confuse
        if action in ['state', 'edit'] and (action == 'state' or request.method == 'POST'):
            return JsonResponse({ 'errors' : ['This Workflow no longer exists'] }, status=404)

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

    # Deleting is the one action left once a Workflow is over
    if action == 'delete':
        runner.delete(workflow.id, request.user.username)
        return OpenBench.views.redirect(request, '/workflows/', status='Workflow #%d was deleted' % (workflow.id))

    if workflow.is_final():
        return OpenBench.views.redirect(request, target, error='The Workflow is already %s' % (workflow.status))

    key = request.POST.get('key', '')

    try:
        if   action == 'pause'   : runner.set_paused(workflow.id, True, request.user.username)
        elif action == 'resume'  : runner.set_paused(workflow.id, False, request.user.username)
        elif action == 'abort'   : runner.abort(workflow.id, request.user.username)
        elif action == 'retry'   : runner.retry(workflow.id, key)
        elif action == 'resolve' : runner.resolve(workflow.id, key, request.POST.get('outcome', ''))
        elif action == 'fixbench': runner.use_measured_bench(workflow.id, key, request.user.username)
        else: return OpenBench.views.redirect(request, target, error='Unknown Workflow action')

    except WorkflowNode.DoesNotExist:
        return OpenBench.views.redirect(request, target, error='No such Node exists')

    except runner.WorkflowError as error:
        return OpenBench.views.redirect(request, target, error=str(error))

    return OpenBench.views.redirect(request, target, status='Workflow was updated: %s' % (action))

def workflow_state(workflow):

    # Everything the page needs to draw the progress of each Node
    nodes = {}
    rows  = { node.key : node for node in workflow.nodes.select_related('test', 'test__spsa_run', 'test__dev', 'test__base') }

    # When each Node will start and finish, at the earliest, see OpenBench/workflows/eta.py
    try: times = eta.estimate(workflow.graph, rows, timezone.now()) if not workflow.is_final() and not workflow.paused else {}
    except Exception:
        traceback.print_exc()
        times = {}

    for node in rows.values():
        nodes[node.key] = {
            'state'   : node.state,
            'held'    : node.held,
            'outcome' : node.outcome,
            'error'   : node.error,
            'trigger' : node.trigger,
            'tuned'   : node.tuned,
            'detail'  : node.detail,
            'pinned'  : node.pinned or {},
            'ports'   :Graph.outcomes(node.kind), # What the Node can be decided as, by hand
            'test'    : test_state(node.test) if node.test else None,
            'bench'   : bench_state(node) if node.state == 'STOPPED' and node.test else None,
            'eta'     : eta.serialize({ node.key : times[node.key] })[node.key] if node.key in times else None,
        }

    # The last Node that may still run, at the earliest
    finishes = [times[key]['finish'] for key, node in rows.items() if key in times and node.state in ['WAITING', 'RUNNING']]
    finish   = max(finishes) if finishes and None not in finishes else None

    return { 'status' : workflow.status, 'paused' : workflow.paused, 'nodes' : nodes,
             'finish' : finish.isoformat() if finish else None }

def bench_state(node):

    # What workers measured, for a Test they stopped because of a wrong Bench. The same as
    # the Test's own page offers, since both restart it with apply_measured_bench()
    return bench_corrections(node.test) or None

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

        # How likely it passes, while it runs, see OpenBench/eta.py
        if not test.finished and (chance := OpenBench.eta.pass_probability(test)) is not None:
            state['elo'] += ' · pass %s' % (OpenBench.eta.describe_probability(chance))

    if test.test_mode == 'SPSA':
        total = 2 * test.spsa_run.pairs_per * test.spsa_run.iterations
        state['summary'] = '%.1f%% of %d' % (100.0 * test.games / max(1, total), total)
        if test.spsa_run.converged_at is not None:
            state['summary'] += ', converged'

    # Warnings from its creation, and errors while playing
    state['warnings'] = describe_checks(test.checks or {})
    if test.crashes or test.timelosses or test.illegals:
        state['errors'] = error_watch.summary({ 'crashes' : test.crashes, 'timelosses' : test.timelosses, 'illegals' : test.illegals })

    return state

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                                 TEMPLATES                                   #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

#   /workflow/templates/                    List of all Templates, and how to set up the webhook
#   /workflow/templates/new/[?from=<id>]    The editor, starting from a Workflow or a Template
#   /workflow/templates/<name>/edit/        The editor for a Template
#   /workflow/templates/<name>/<action>/    POST: start (for a branch), toggle, delete

def can_change_template(request, template):
    profile = enabled_profile(request)
    return bool(profile and (profile.approver or template.author == request.user.username))

def templates(request):

    rows = [{ 'template' : template, 'repo' : automation.template_repo(template.graph),
              'can_change' : can_change_template(request, template) }
            for template in WorkflowTemplate.objects.order_by('name')]

    data = {
        'rows'    : rows,
        'pushes'  : PushEvent.objects.order_by('-id')[:20],
        'webhook' : bool(automation.read_secret()),
    }

    return OpenBench.views.render(request, 'workflow_templates.html', data)

def new_template(request):

    if not enabled_profile(request):
        return OpenBench.views.redirect(request, '/login/', error='Only enabled users can create Templates')

    if request.method == 'POST':
        return save_template(request, None)

    graph, name = None, ''
    source = request.GET.get('from', '')

    if source.isdigit() and (workflow := Workflow.objects.filter(id=int(source)).first()):
        graph, name = automation.unresolve(workflow.graph), ''

    elif source and (template := WorkflowTemplate.objects.filter(name=source).first()):
        graph, name = template.graph, 'copy-of-%s' % (template.name)

    edit = { 'submit_url' : '/workflow/templates/new/', 'submit_text' : 'Create Template', 'locked' : [], 'state' : None, 'template' : True }
    return render_editor(request, graph, name, edit)

def save_template(request, template):

    if not (body := parse_body(request)):
        return JsonResponse({ 'errors' : ['Malformed request'] }, status=400)

    name, graph = body
    name = str(name or '').strip()

    if errors := automation.verify_template(name, graph):
        return JsonResponse({ 'errors' : errors }, status=400)

    taken = WorkflowTemplate.objects.filter(name__iexact=name)
    if template:
        taken = taken.exclude(id=template.id)

    if taken.exists():
        return JsonResponse({ 'errors' : ['A Template named %s already exists' % (name)] }, status=400)

    if template:
        template.name, template.graph = name, graph
        template.save()
    else:
        template = WorkflowTemplate.objects.create(name=name, graph=graph, author=request.user.username)

    return JsonResponse({ 'redirect' : '/workflow/templates/' })

def template(request, name, action):

    if not (found := WorkflowTemplate.objects.filter(name=name).first()):
        if request.method == 'POST' and action == 'edit':
            return JsonResponse({ 'errors' : ['This Template no longer exists'] }, status=404)
        return OpenBench.views.redirect(request, '/workflow/templates/', error='No such Template exists')

    if not can_change_template(request, found):
        if request.method == 'POST' and action == 'edit':
            return JsonResponse({ 'errors' : ['You cannot change another user\'s Template'] }, status=403)
        return OpenBench.views.redirect(request, '/workflow/templates/', error='You cannot change another user\'s Template')

    if action == 'edit':

        if request.method == 'POST':
            return save_template(request, found)

        edit = { 'submit_url' : '/workflow/templates/%s/edit/' % (found.name), 'submit_text' : 'Save Template',
                 'locked' : [], 'state' : None, 'template' : True }
        return render_editor(request, found.graph, found.name, edit)

    if request.method != 'POST':
        return OpenBench.views.redirect(request, '/workflow/templates/')

    if action == 'toggle':
        found.enabled = not found.enabled
        found.save()
        return OpenBench.views.redirect(request, '/workflow/templates/', status='%s was %s' % (found.name, 'enabled' if found.enabled else 'disabled'))

    if action == 'delete':
        found.delete()
        return OpenBench.views.redirect(request, '/workflow/templates/', status='Deleted Template %s' % (found.name))

    if action == 'start':

        branch = request.POST.get('branch', '').strip()
        if not Graph.BRANCH_PATTERN.match(branch) or branch.startswith('@'):
            return OpenBench.views.redirect(request, '/workflow/templates/', error='Give the branch to start the Template for')

        workflow, errors = automation.start_template(found, branch)
        if errors:
            return OpenBench.views.redirect(request, '/workflow/templates/', error='\n'.join(errors))

        return OpenBench.views.redirect(request, '/workflow/%d/' % (workflow.id), status='Started from Template %s' % (found.name))

    return OpenBench.views.redirect(request, '/workflow/templates/', error='Unknown Template action')
