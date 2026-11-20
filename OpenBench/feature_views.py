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

# Web interface of the Github webhook, the Ladders, and the changelog:
#
#   /api/github/push/           Github's webhook, see OpenBench/automation.py
#   /ladders/                   List of all Ladders, and the form to create one
#   /ladder/<id>/               The course of a Ladder
#   /ladder/<id>/<action>/      POST: measure, anchor, toggle, delete
#   /changelog/                 Passed SPRTs over a span of time, ready to paste

import datetime
import json
import re

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

import OpenBench.stats
import OpenBench.views

from OpenBench import automation, github, ladder
from OpenBench.config import OPENBENCH_CONFIG
from OpenBench.models import Book, EngineConfig, Ladder, Network, Profile, Test

def enabled_profile(request):
    if request.user.is_authenticated:
        return Profile.objects.filter(user=request.user, enabled=True).first()

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                                   WEBHOOK                                   #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

@csrf_exempt
def github_push(request):

    if request.method != 'POST':
        return JsonResponse({ 'error' : 'Github posts its pushes here' }, status=405)

    status, answer = automation.receive(request.headers, request.body)
    return JsonResponse(answer, status=status)

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                                   LADDERS                                   #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def ladders(request):

    if request.method == 'POST':

        if not enabled_profile(request):
            return OpenBench.views.redirect(request, '/login/', error='Only enabled users can create Ladders')

        created, errors = ladder.create_ladder(request.user, request.POST)
        if errors:
            return OpenBench.views.redirect(request, '/ladders/', error='\n'.join(errors))

        ladder.request_poll(created.repo, created.branch) # Measure the current head right away
        return OpenBench.views.redirect(request, '/ladder/%d/' % (created.id), status='Created Ladder %s' % (created.name))

    rows = []
    for item in Ladder.objects.order_by('-id'):
        latest = next((row for row in reversed(ladder.points(item)) if 'elo' in row and row['current']), None)
        rows.append({ 'ladder' : item, 'latest' : latest, 'count' : item.points.count() })

    data = {
        'rows'     : rows,
        'books'    : Book.objects.filter(enabled=True).order_by('name'),
        'networks' : Network.objects.order_by('engine', '-id'),
        'default'  : profile.engine if (profile := enabled_profile(request)) else '',
    }

    return OpenBench.views.render(request, 'ladders.html', data)

def can_change_ladder(request, item):
    profile = enabled_profile(request)
    return bool(profile and (profile.approver or item.author == request.user.username))

def ladder_view(request, pk, action=None):

    if not (item := Ladder.objects.filter(id=pk).first()):
        return OpenBench.views.redirect(request, '/ladders/', error='No such Ladder exists')

    target = '/ladder/%d/' % (item.id)

    if action is None:
        data = { 'ladder' : item, 'points' : ladder.points(item), 'can_change' : can_change_ladder(request, item) }
        return OpenBench.views.render(request, 'ladder.html', data)

    if request.method != 'POST':
        return OpenBench.views.redirect(request, target)

    if not can_change_ladder(request, item):
        return OpenBench.views.redirect(request, target, error='You cannot change another user\'s Ladder')

    if action == 'measure':
        try: point = ladder.measure(item, force=True)
        except github.GithubError as error:
            return OpenBench.views.redirect(request, target, error=str(error))

        if point is None:
            return OpenBench.views.redirect(request, target, status='%s is measured already, or is the anchor' % (item.branch))
        if point.error:
            return OpenBench.views.redirect(request, target, error=point.error)
        return OpenBench.views.redirect(request, target, status='Measuring %s' % (point.sha[:8]))

    if action == 'anchor':
        if errors := ladder.set_anchor(item, request.POST.get('anchor', ''), request.POST.get('anchor_bench', '')):
            return OpenBench.views.redirect(request, target, error='\n'.join(errors))
        return OpenBench.views.redirect(request, target, status='The anchor is %s from now on' % (item.anchor_sha[:8]))

    if action == 'toggle':
        item.enabled = not item.enabled
        item.save()
        return OpenBench.views.redirect(request, target, status='The Ladder was %s' % ('enabled' if item.enabled else 'paused'))

    if action == 'delete':
        item.delete() # Its Tests stay, as ordinary Tests
        return OpenBench.views.redirect(request, '/ladders/', status='Deleted Ladder %s' % (item.name))

    return OpenBench.views.redirect(request, target, error='Unknown Ladder action')

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                                  CHANGELOG                                  #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

DATE_PATTERN = re.compile(r'^\d{4}-\d{2}-\d{2}$')

def parse_since(engine, value, errors):

    # A date, or a branch, tag or sha of the engine, whose commit date is used, or 30 days ago
    value = value.strip()

    if not value:
        return timezone.now() - datetime.timedelta(days=30)

    if DATE_PATTERN.match(value):
        try: return datetime.datetime.strptime(value, '%Y-%m-%d').replace(tzinfo=datetime.timezone.utc)
        except ValueError: pass

    config = EngineConfig.objects.filter(name=engine).first()
    if config:
        try:
            date = github.commit(config.source, engine, value)['date']
            return datetime.datetime.fromisoformat(date.replace('Z', '+00:00'))
        except (github.GithubError, KeyError, ValueError) as error:
            errors.append('%s is neither a date nor a commit of %s: %s' % (value, engine, error))

    return timezone.now() - datetime.timedelta(days=30)

def changelog_rows(engine, base, since, until=None):

    # Passed SPRTs against the base branch, oldest first: gainers, and simplifications
    tests = Test.objects.filter(test_mode='SPRT', passed=True, deleted=False, dev_engine=engine,
                                base__name=base, updated__gte=since).select_related('dev', 'base')
    if until:
        tests = tests.filter(updated__lt=until)

    gainers, simplifications = [], []

    for test in tests.order_by('updated', 'id'):

        lower, elo, upper = OpenBench.stats.Elo(test.results())
        row = {
            'test'   : test,
            'elo'    : elo,
            'error'  : max(upper - elo, elo - lower),
            'notes'  : test.notes.strip().split('\n')[0] if test.notes.strip() else '',
            'tags'   : test.tags.split(),
        }

        (gainers if test.elolower + test.eloupper >= 0 else simplifications).append(row)

    return gainers, simplifications

def changelog_text(gainers, simplifications):

    # Markdown, to paste into release notes
    def line(row):
        test = row['test']
        text = '- %s: %+.2f ± %.2f Elo at %s (Test #%d)' % (test.dev.name, row['elo'], row['error'], test.dev_time_control, test.id)
        return text + (' - %s' % (row['notes']) if row['notes'] else '')

    lines = []
    if gainers:
        lines += ['## Gainers', ''] + [line(row) for row in gainers] + ['']
    if simplifications:
        lines += ['## Simplifications', ''] + [line(row) for row in simplifications] + ['']
    return '\n'.join(lines).strip()

def changelog(request):

    profile = enabled_profile(request)
    engines = list(EngineConfig.objects.filter(enabled=True).order_by('name').values_list('name', flat=True))
    engine  = request.GET.get('engine') or (profile.engine if profile and profile.engine in engines else '') or (engines[0] if engines else '')
    base    = request.GET.get('base', '').strip() or 'main'
    errors  = []

    since = parse_since(engine, request.GET.get('since', ''), errors)
    until = None
    if DATE_PATTERN.match(request.GET.get('until', '').strip()):
        until = datetime.datetime.strptime(request.GET['until'].strip(), '%Y-%m-%d').replace(tzinfo=datetime.timezone.utc) + datetime.timedelta(days=1)

    gainers, simplifications = changelog_rows(engine, base, since, until)

    data = {
        'engines'         : engines,
        'form'            : { 'engine' : engine, 'base' : base, 'since' : request.GET.get('since', ''), 'until' : request.GET.get('until', '') },
        'since'           : since,
        'gainers'         : gainers,
        'simplifications' : simplifications,
        'total'           : sum(row['elo'] for row in gainers),
        'text'            : changelog_text(gainers, simplifications),
    }

    return OpenBench.views.render(request, 'changelog.html', data, error='\n'.join(errors) or None)
