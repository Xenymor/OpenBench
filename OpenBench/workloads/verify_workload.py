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

# Module serves a singular purpose, to invoke:
# >>> verify_workload(request, type)
#
# Given a request, and a workload_type [TEST, TUNE, DATAGEN], verify all of
# the form inputs, collect all of the information from Github, verify all of
# the data from Github, and return a tuple of (errors, engines)
#
# For Verifying and Collection information on a Test:
#   >>> errors, (dev_info, base_info) = verify_workload(request, 'TEST')
#
# For Verifying and Collection information on a Tune:
#   >>> errors, dev_info = verify_workload(request, 'TUNE')
#
# For Verifying and Collection information on Datagen:
#   >>> errors, (dev_info, base_info) = verify_workload(request, 'DATAGEN')

import os
import re
import requests
import traceback

import OpenBench.config
import OpenBench.utils

from OpenBench.models import *

from django.db.models import Q

def verify_workload(request, workload_type):

    assert workload_type in [ 'TEST', 'TUNE', 'DATAGEN' ]

    errors = []

    if workload_type == 'TEST':
        verify_test_creation(errors, request)
        dev  = collect_github_info(errors, request, 'dev')
        base = collect_github_info(errors, request, 'base')
        return errors, (dev, base)

    if workload_type == 'TUNE':
        verify_tune_creation(errors, request)
        engine = collect_github_info(errors, request, 'dev')
        return errors, engine

    if workload_type == 'DATAGEN':
        verify_datagen_creation(errors, request)
        dev  = collect_github_info(errors, request, 'dev')
        base = collect_github_info(errors, request, 'base')
        return errors, (dev, base)

def verify_test_creation(errors, request):

    verifications = [

        # Verify everything about the Dev Engine
        (verify_engine         , 'dev_engine', 'Dev Engine'),
        (verify_github_repo    , 'dev_repo'),
        (verify_network        , 'dev_network', 'Dev Network', 'dev_engine'),
        (verify_options        , 'dev_options', 'Threads', 'Dev Options'),
        (verify_options        , 'dev_options', 'Hash', 'Dev Options'),
        (verify_time_control   , 'dev_time_control', 'Dev Time Control'),

        # Verify everything about the Base Engine
        (verify_engine         , 'base_engine', 'Base Engine'),
        (verify_github_repo    , 'base_repo'),
        (verify_network        , 'base_network', 'Base Network', 'base_engine'),
        (verify_options        , 'base_options', 'Threads', 'Base Options'),
        (verify_options        , 'base_options', 'Hash', 'Base Options'),
        (verify_time_control   , 'base_time_control', 'Base Time Control'),

        # Verify everything about the Test Settings
        (verify_book           , 'book_name', 'Book'),
        (verify_upload_pgns    , 'upload_pgns', 'Upload PGNs'),
        (verify_test_mode      , 'test_mode'),
        (verify_sprt_bounds    , 'test_bounds'),
        (verify_sprt_conf      , 'test_confidence'),
        (verify_max_games      , 'test_max_games'),

        # Verify everything about the General Settings
        (verify_integer        , 'priority', 'Priority'),
        (verify_greater_than   , 'throughput', 'Throughput', 0),
        (verify_syzygy_field   , 'syzygy_wdl', 'Syzygy WDL'),

        # Verify everything about the Workload Settings
        (verify_integer        , 'workload_size', 'Workload Size'),
        (verify_greater_than   , 'workload_size', 'Workload Size', 0),

        # Verify the Scaling Mechanisms
        (verify_scale_method   , 'scale_method'),
        (verify_integer        , 'scale_nps', 'Scale NPS'),
        (verify_greater_than   , 'scale_nps', 'Scale NPS', 0),

        # Verify everything about the Adjudicaton Settings
        (verify_syzygy_field   , 'syzygy_adj', 'Syzygy Adjudication'),
        (verify_win_adj        , 'win_adj'),
        (verify_draw_adj       , 'draw_adj'),
    ]

    for verification in verifications:
        verification[0](errors, request, *verification[1:])

def verify_tune_creation(errors, request):

    verifications = [

        # Verify the SPSA raw inputs and methods
        (verify_spsa_inputs           , 'spsa_inputs'),
        (verify_spsa_reporting_type   , 'spsa_reporting_type', 'Reporting Method'),
        (verify_spsa_distribution_type, 'spsa_distribution_type', 'Distribution Method'),

        # Verify everything about the Engine
        (verify_engine                , 'dev_engine', 'Engine'),
        (verify_github_repo           , 'dev_repo'),
        (verify_network               , 'dev_network', 'Network', 'dev_engine'),
        (verify_options               , 'dev_options', 'Threads', 'Options'),
        (verify_options               , 'dev_options', 'Hash', 'Options'),
        (verify_time_control          , 'dev_time_control', 'Time Control'),

        # Verify everything about the Test Settings
        (verify_book                  , 'book_name', 'Book'),
        (verify_upload_pgns           , 'upload_pgns', 'Upload PGNs'),

        # Verify everything about the General Settings
        (verify_integer               , 'priority', 'Priority'),
        (verify_greater_than          , 'throughput', 'Throughput', 0),
        (verify_syzygy_field          , 'syzygy_wdl', 'Syzygy WDL'),

        # Verify the Scaling Mechanisms
        (verify_scale_method   , 'scale_method'),
        (verify_integer        , 'scale_nps', 'Scale NPS'),
        (verify_greater_than   , 'scale_nps', 'Scale NPS', 0),

        # Verify everything about the Adjudicaton Settings
        (verify_syzygy_field          , 'syzygy_adj', 'Syzygy Adjudication'),
        (verify_win_adj               , 'win_adj'),
        (verify_draw_adj              , 'draw_adj'),

        # Verify everything about the SPSA Settings
        (verify_float                 , 'spsa_alpha', 'SPSA A-Ratio'),
        (verify_float                 , 'spsa_alpha', 'SPSA Alpha'),
        (verify_float                 , 'spsa_gamma', 'SPSA Gamma'),
        (verify_integer               , 'spsa_iterations', 'SPSA Iterations'),
        (verify_integer               , 'spsa_pairs_per', 'SPSA Pairs-Per'),
        (verify_greater_than          , 'spsa_alpha', 'SPSA A-Ratio', 0.00),
        (verify_greater_than          , 'spsa_alpha', 'SPSA Alpha', 0.00),
        (verify_greater_than          , 'spsa_gamma', 'SPSA Gamma', 0.00),
        (verify_greater_than          , 'spsa_iterations', 'SPSA Iterations', 0),
        (verify_greater_than          , 'spsa_pairs_per', 'SPSA Pairs-Per', 0),
    ]

    for verification in verifications:
        verification[0](errors, request, *verification[1:])

def verify_datagen_creation(errors, request):

    verifications = [

        # Verify everything about the Dev Engine
        (verify_engine         , 'dev_engine', 'Dev Engine'),
        (verify_github_repo    , 'dev_repo'),
        (verify_network        , 'dev_network', 'Dev Network', 'dev_engine'),
        (verify_options        , 'dev_options', 'Threads', 'Dev Options'),
        (verify_options        , 'dev_options', 'Hash', 'Dev Options'),
        (verify_time_control   , 'dev_time_control', 'Dev Time Control'),

        # Verify everything about the Base Engine
        (verify_engine         , 'base_engine', 'Base Engine'),
        (verify_github_repo    , 'base_repo'),
        (verify_network        , 'base_network', 'Base Network', 'base_engine'),
        (verify_options        , 'base_options', 'Threads', 'Base Options'),
        (verify_options        , 'base_options', 'Hash', 'Base Options'),
        (verify_time_control   , 'base_time_control', 'Base Time Control'),

        # Verify everything about the Datagen Settings
        (verify_datagen_games  , 'datagen_max_games'),
        (verify_datagen_genfens, 'datagen_custom_genfens'),
        (verify_datagen_reverse, 'datagen_play_reverses'),
        (verify_datagen_book   , 'book_name', 'Book'),
        (verify_upload_pgns    , 'upload_pgns', 'Upload PGNs'),

        # Verify everything about the General Settings
        (verify_integer        , 'priority', 'Priority'),
        (verify_greater_than   , 'throughput', 'Throughput', 0),
        (verify_syzygy_field   , 'syzygy_wdl', 'Syzygy WDL'),

        # Verify everything about the Workload Settings
        (verify_integer        , 'workload_size', 'Workload Size'),
        (verify_greater_than   , 'workload_size', 'Workload Size', 0),

        # Verify the Scaling Mechanisms
        (verify_scale_method   , 'scale_method'),
        (verify_integer        , 'scale_nps', 'Scale NPS'),
        (verify_greater_than   , 'scale_nps', 'Scale NPS', 0),

        # Verify everything about the Adjudicaton Settings
        (verify_syzygy_field   , 'syzygy_adj', 'Syzygy Adjudication'),
        (verify_win_adj        , 'win_adj'),
        (verify_draw_adj       , 'draw_adj'),
    ]

    for verification in verifications:
        verification[0](errors, request, *verification[1:])


def verify_integer(errors, request, field, field_name):
    try: int(request.POST[field])
    except: errors.append('"{0}" is not an Integer'.format(field_name))

def verify_float(errors, request, field, field_name):
    try: float(request.POST[field])
    except: errors.append('"{0}" is not a Float'.format(field_name))

def verify_greater_than(errors, request, field, field_name, value):
    try: assert float(request.POST[field]) > value
    except: errors.append('"{0}" is not greater than {1}'.format(field_name, value))

def verify_options(errors, request, field, option, field_name):
    try: assert int(OpenBench.utils.extract_option(request.POST[field], option)) >= 1
    except: errors.append('"{0}" needs to be at least 1 for {1}'.format(option, field_name))

def verify_engine(errors, request, field, field_name):
    try: assert EngineConfig.objects.filter(name=request.POST[field], enabled=True).exists()
    except: errors.append('{0} was not found in the configuration'.format(field_name))

def verify_book(errors, request, field, field_name):
    try: assert Book.objects.filter(name=request.POST[field], enabled=True).exists()
    except: errors.append('{0} was not found in the configuration'.format(field_name))

def verify_time_control(errors, request, field, field_name):
    try: OpenBench.utils.TimeControl.parse(request.POST[field])
    except: errors.append('{0} is not parsable'.format(field_name))

def verify_win_adj(errors, request, field):
    try:
        if (content := request.POST[field]) == 'None': return
        assert re.match('movecount=[0-9]+ score=[0-9]+', content)
    except: errors.append('Invalid Win Adjudication Setting. Try "None"?')

def verify_draw_adj(errors, request, field):
    try:
        if (content := request.POST[field]) == 'None': return
        assert re.match('movenumber=[0-9]+ movecount=[0-9]+ score=[0-9]+', content)
    except: errors.append('Invalid Draw Adjudication Setting. Try "None"?')

def verify_github_repo(errors, request, field):
    pattern = r'^https:\/\/github\.com\/[A-Za-z0-9-]+\/[A-Za-z0-9_.-]+\/?$'
    try: assert re.match(pattern, request.POST[field])
    except: errors.append('Sources must be found on https://github.com/<User>/<Repo>')

def verify_network(errors, request, field, field_name, engine_field):
    try:
        if request.POST[field] == '': return
        Network.objects.get(engine=request.POST[engine_field], sha256=request.POST[field])
    except: errors.append('Unknown Network Provided for {0}'.format(field_name))

def verify_test_mode(errors, request, field):
    try: assert request.POST[field] in ['SPRT', 'GAMES']
    except: errors.append('Unknown Test Mode')

def verify_sprt_bounds(errors, request, field):
    if request.POST.get('test_mode') == 'SPRT':
        verify_bounds_pair(errors, request, 'test_bounds', 'SPRT Bounds')

def verify_sprt_conf(errors, request, field):
    if request.POST.get('test_mode') == 'SPRT':
        verify_conf_pair(errors, request, 'test_confidence', 'Confidence Bounds')

# [float1, float2], as used by the SPRT Bounds and Confidence fields
FLOAT_PAIR_PATTERN = r'^\[(-?\d+(?:\.\d+)?), (-?\d+(?:\.\d+)?)\]$'

def verify_bounds_pair(errors, request, field, field_name):
    try:
        match = re.match(FLOAT_PAIR_PATTERN, request.POST[field])
        assert float(match.group(1)) < float(match.group(2))
    except: errors.append('%s must be formatted as [float1, float2]' % (field_name))

def verify_conf_pair(errors, request, field, field_name):
    try:
        match = re.match(FLOAT_PAIR_PATTERN, request.POST[field])
        assert 0.00 < float(match.group(1)) < 1.00
        assert 0.00 < float(match.group(2)) < 1.00
    except: errors.append('%s must be formatted as [float1, float2], within (0.00, 1.00)' % (field_name))

def verify_max_games(errors, request, field):
    try:
        if request.POST['test_mode'] != 'GAMES': return
        assert int(request.POST['test_max_games']) > 0
    except: errors.append('Fixed Games Tests must last at least one game')

def verify_syzygy_field(errors, request, field, field_name):
    candidates = ['OPTIONAL', 'DISABLED', '3-MAN', '4-MAN', '5-MAN', '6-MAN', '7-MAN']
    try: assert request.POST[field] in candidates
    except: errors.append('%s must be in %s' % (field_name, ', '.join(candidates)))

def verify_spsa_inputs(errors, request, field):

    try:

        if not (lines := request.POST[field].split('\n')):
            errors.append('No Parameters Provided')

        for line in lines:
            name, data_type, value, minimum, maximum, c, r = line.split(',')

            if data_type.strip() not in [ 'int', 'float' ]:
                errors.append('Datatype must be int for float, for %s' % (name))

            if float(minimum) > float(maximum):
                errors.append('Max does not exceed Min, for %s' % (name))

            if not (float(minimum) <= float(value) <= float(maximum)):
                errors.append('Value must be within [Min, Max], for %s' % (name))

            if data_type.strip() == 'float' and float(c) <= 0.00:
                errors.append('C for floats must be > 0.00, for %s' % (name))

            if float(r) <= 0.00:
                errors.append('R must be > 0.00, for %s' % (name))

    except:
        traceback.print_exc()
        errors.append('Malformed SPSA Input')

def verify_spsa_reporting_type(errors, request, field, field_name):
    candidates = ['BULK', 'BATCHED']
    try: assert request.POST[field] in candidates
    except: errors.append('%s must be in %s' % (field_name, ', '.join(candidates)))

def verify_spsa_distribution_type(errors, request, field, field_name):
    candidates = ['SINGLE', 'MULTIPLE']
    try: assert request.POST[field] in candidates
    except: errors.append('%s must be in %s' % (field_name, ', '.join(candidates)))

def verify_upload_pgns(errors, request, field, field_name):
    try: request.POST[field] in ['FALSE', 'COMPACT', 'VERBOSE']
    except: errors.append('"%s" must be FALSE, COMPACT, or VERBOSE' % (field_name))

def verify_datagen_games(errors, request, field):
    try: assert int(request.POST[field]) > 0
    except: errors.append('Data Generation must last for at least one game')

def verify_datagen_genfens(errors, request, field):
    try: assert '"' not in request.POST[field]
    except: errors.append('Quotes are not allowed in genfens args')

def verify_datagen_reverse(errors, request, field):
    try: assert request.POST[field] in ['YES', 'NO']
    except: errors.append('Play Reverses must either be YES or NO')

def verify_datagen_book(errors, request, field, field_name):
    try:
        if request.POST[field] == 'NONE': return
        assert Book.objects.filter(name=request.POST[field], enabled=True).exists()
    except: errors.append('{0} was neither NONE nor found in the configuration'.format(field_name))

def verify_scale_method(errors, request, field):
    try: assert(request.POST[field] in Test.ScaleMethod)
    except:
        choices = [f[0] for f in Test.ScaleMethod.choices]
        errors.append('Unknown Scale Method. Expected one of {%s}.' % (', '.join(choices)))


GITHUB_SHA = re.compile(r'^[0-9a-fA-F]{40}$')
MAX_BENCH  = 2 ** 31 - 1 # Engine.bench is an IntegerField, 32 bits in MySQL

class GithubRateLimited(Exception):
    pass

def collect_github_info(errors, request, field):

    if not (commit := resolve_commit(errors, request.POST, field)):
        return

    base, sha, message = commit
    branch  = request.POST['{0}_branch'.format(field)].strip()
    engine  = request.POST['{0}_engine'.format(field)]
    network = request.POST.get('{0}_network'.format(field), '')

    # Extract the bench from the web form, an earlier Workload, or the commit message
    if not (bench := determine_bench(errors, request, field, engine, sha, network, message)):
        return

    info   = request.POST['info'] or strip_message(message)
    source = OpenBench.utils.path_join(base, 'zipball', sha)
    return (source, branch, sha, bench, info)

def resolve_commit(errors, post, field):

    # (API base path, commit sha, commit message) for the branch, tag or sha of the field.
    # post holds '<field>_branch', '<field>_repo' and '<field>_engine', as the web form sends them.

    # Get branch name / commit sha / tag, and the API path for it
    branch = post['{0}_branch'.format(field)].strip()
    bysha  = bool(GITHUB_SHA.match(branch))

    # All API requests will share this common path. Some engines are private.
    base    = post['%s_repo' % (field)].replace('github.com', 'api.github.com/repos')
    engine  = post['%s_engine' % (field)]
    private = EngineConfig.objects.filter(name=engine).values_list('private', flat=True).first()
    headers = {}

    ## Step 1: Verify the target of the API requests

    # Private engines must have a token stored in credentials.enginename
    if private and not (headers := OpenBench.utils.read_git_credentials(engine)):
        errors.append('Server does not have access tokens for this engine')
        return

    # Do not allow private engines to use forked repos ( We don't have a token! )
    if requests_illegal_fork(post, field):
        errors.append('Forked Repositories are not allowed for Private engines')
        return

    # Avoid leaking our credentials to other websites
    if not base.startswith('https://api.github.com/'):
        errors.append('OpenBench may only reach Github\'s API')
        return

    ## Step 2: Connect to the Github API for the given Branch or Commit SHA.
    ## - We will translate any branch name into a commit SHA for later use
    ## - The commit message may hold the Bench, see determine_bench()
    ## - Only the sha and the message are kept, not the rest of Github's answer

    try: # Fetch data from the Github API

        # A full sha: the Git Database API sends the commit alone, without its diff
        if bysha:
            data = github_get(OpenBench.utils.path_join(base, 'git', 'commits', branch.lower()), headers)
            sha, message = data['sha'], data['message']

        else:

            # Lookup branch, but will fail for tags and short shas
            data = github_get(OpenBench.utils.path_join(base, 'branches', branch), headers)

            # Check to see if the branch name was actually a tag name
            if 'commit' not in data:
                data = github_get(OpenBench.utils.path_join(base, 'commits', branch), headers)

            # Actual branches have to go one layer deeper
            else: data = data['commit']

            sha, message = data['sha'], data['commit']['message']

        # Check that all the data we need going forward is present
        assert isinstance(sha, str) and GITHUB_SHA.match(sha) and isinstance(message, str)

    except GithubRateLimited:
        errors.append('Github\'s API rate limit was reached while looking up %s, try again later' % (branch))
        return

    except: # Unable to find for whatever reason
        traceback.print_exc()
        errors.append('%s could not be found' % (branch or 'Branch'))
        return

    return base, sha.lower(), message

def github_get(url, headers):

    # The JSON of a Github API request. Running out of requests is told apart from a missing
    # branch, since Github answers both with an error, and retrying later fixes only one of them.
    response = requests.get(url, headers=headers, timeout=30)

    if response.status_code in [403, 429] and (response.headers.get('x-ratelimit-remaining') == '0'
                                                or 'rate limit' in response.text.lower()):
        raise GithubRateLimited()

    return response.json()

def requests_illegal_fork(post, field):

    # Strip trailing '/'s for sanity
    config  = EngineConfig.objects.filter(name=post['%s_engine' % (field)]).first()
    eng_src = config.source.rstrip('/')
    tar_src = post['%s_repo' % (field)].rstrip('/')

    # Illegal if sources do not match for Private engines
    return config.private and eng_src != tar_src

def parse_bench(value):

    # None to autofill the Bench, otherwise the Bench, or 0 if it is not a positive number.
    # Thousands separators are dropped, as when a Bench is read from a commit message.
    value = re.sub(r'[,_\s]', '', str(value or ''))

    if value.lower() in ['', 'autofill']:
        return None

    return int(value) if re.fullmatch('[0-9]{1,10}', value) and 0 < int(value) <= MAX_BENCH else 0

def determine_bench(errors, request, field, engine, sha, network, message):

    # The Bench for the resolved commit, or None after adding an error
    branch = request.POST['{0}_branch'.format(field)].strip()
    given  = parse_bench(request.POST.get('{0}_bench'.format(field), ''))

    if given == 0:
        errors.append('%s Bench must be a positive number, or Autofill' % (field.title()))
        return None

    # create_workload.js names the commit it filled the Bench in for. If the branch has moved on
    # since, that Bench belongs to the older commit, so this one is autofilled instead.
    filled_for = request.POST.get('{0}_bench_sha'.format(field), '').strip().lower()

    if given and (not filled_for or filled_for == sha):
        return given

    # Carry it over from an earlier Workload of the same commit and Network. That one is
    # right even where the Bench in the commit message was measured with another Network.
    if (known := known_bench(engine, sha, network)):
        return known[0]

    if (bench := message_bench(message)):
        return bench

    errors.append('Unable to parse a Bench for %s' % (branch))
    return None

def message_bench(message):

    # Fallback to try to parse the Bench from the commit
    try:
        benches = re.findall('(?:BENCH|NODES)[ :=]+([0-9,]+)', message, re.IGNORECASE)
        bench   = int(benches[-1].replace(',', ''))
        return bench if 0 < bench <= MAX_BENCH else None
    except: return None

def known_bench(engine, sha, network):

    # (Bench, Workload) of the latest Workload that ran this commit of the engine with this
    # Network, or None. Only Benches that workers confirmed count: they played games with it,
    # and none of them measured another one (a REBENCH would have taken the measured one).
    network = network or ''

    # Engines are found by their indexed sha, and Workloads by their indexed Engines. One query
    # per side, since an OR across both would keep the database from using either index.
    engines = set(Engine.objects.filter(sha=sha).order_by('-id').values_list('id', flat=True)[:512])
    if not engines:
        return None

    fields     = ['id', 'test_mode', 'dev_id', 'base_id', 'dev_engine', 'base_engine', 'dev_network',
                  'base_network', 'dev__sha', 'dev__bench', 'base__sha', 'base__bench']
    candidates = {}

    for side in ['dev', 'base']:
        query = Test.objects.filter(deleted=False, games__gt=0, **{
            '%s_id__in' % (side) : engines, '%s_engine' % (side) : engine, '%s_network' % (side) : network })
        for workload in query.select_related('dev', 'base').only(*fields).order_by('-id')[:16]:
            candidates[workload.id] = workload

    workloads = sorted(candidates.values(), key=lambda workload: -workload.id)[:16]

    # The reports of wrong Benches for all of them at once, see utils.measured_benches()
    summaries = {}
    events    = LogEvent.objects.filter(test_id__in=candidates.keys(), summary__contains='Wrong Bench').exclude(machine_id=0)
    for test_id, summary in events.values_list('test_id', 'summary'):
        summaries.setdefault(test_id, []).append(summary)

    for workload in workloads:
        for side in OpenBench.utils.workload_sides(workload):

            if (getattr(workload, '%s_id' % (side)) not in engines
                or getattr(workload, '%s_engine'  % (side)) != engine
                or getattr(workload, '%s_network' % (side)) != network):
                continue

            if side not in OpenBench.utils.measured_benches(workload, [side], summaries.get(workload.id, [])):
                return getattr(workload, side).bench, workload

    return None

def lookup_bench(engine, repo, branch, network, sha=''):

    # What a Workload with these settings would autofill, for create_workload.js: { 'sha', 'known',
    # 'message_bench' } or { 'error' }. 'known' is the Bench of an earlier Workload, as known_bench()
    # finds it, and wins over the Bench in the commit message. Given the sha that an earlier lookup
    # resolved the branch to, only 'known' is looked up again, without asking Github.
    branch, network, sha = branch.strip(), network.strip(), sha.strip().lower()

    if not EngineConfig.objects.filter(name=engine, enabled=True).exists():
        return { 'error' : 'Unknown Engine' }

    if not re.match(r'^https:\/\/github\.com\/[A-Za-z0-9-]+\/[A-Za-z0-9_.-]+\/?$', repo):
        return { 'error' : 'Sources must be found on https://github.com/<User>/<Repo>' }

    def known_dict(sha):
        if not (known := known_bench(engine, sha, network)):
            return None
        return { 'bench' : known[0], 'workload' : { 'id' : known[1].id, 'type' : known[1].workload_type_str() } }

    if sha:
        if not GITHUB_SHA.match(sha):
            return { 'error' : 'Malformed sha' }
        return { 'sha' : sha, 'known' : known_dict(sha) }

    if not branch:
        return { 'error' : 'No Branch given' }

    errors = []
    if not (commit := resolve_commit(errors, { 'x_engine' : engine, 'x_repo' : repo, 'x_branch' : branch }, 'x')):
        return { 'error' : errors[0] }

    _, sha, message = commit
    return { 'sha' : sha, 'known' : known_dict(sha), 'message_bench' : message_bench(message) }

def strip_message(message):
    lines = message.strip().split("\n")
    bench_search = re.compile(r"(^|\s)[Bb]ench[ :]+([1-9]\d{5,7})(?!\d)")
    for i, line in enumerate(reversed(lines)):
        new_line, n = bench_search.subn("", line)
        if n:
            lines[-i - 1] = new_line
            break
    s = "\n".join(lines)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n+", r"\n", s)
    return s.rstrip()
