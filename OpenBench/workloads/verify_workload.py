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
#   >>> errors, (dev_info, followup_info) = verify_workload(request, 'TUNE')
#
# For Verifying and Collection information on Datagen:
#   >>> errors, (dev_info, base_info) = verify_workload(request, 'DATAGEN')

import os
import re
import requests
import traceback

from types import SimpleNamespace

import OpenBench.config
import OpenBench.followup_utils
import OpenBench.utils

from OpenBench.models import *

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
        engine   = collect_github_info(errors, request, 'dev')
        followup = collect_followup_info(errors, request)
        return errors, (engine, followup)

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

    if request.POST.get('test_mode') == 'SPRT':
        verify_ltc_creation(errors, request)
        verify_followup_options_length(errors, request, ['dev_options', 'base_options'], '')

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

    verify_followup_creation(errors, request)

    # The LTC follow-up of a tune is chained onto its SPRT follow-up
    if request.POST.get('ltc_enabled', 'NO') == 'YES' and request.POST.get('followup_enabled', 'NO') != 'YES':
        errors.append('LTC follow-up requires the SPRT follow-up')
    else:
        verify_ltc_creation(errors, request)

    if request.POST.get('followup_enabled', 'NO') == 'YES':
        verify_followup_options_length(errors, request, ['dev_options'], estimate_tuned_options(request))

def verify_followup_creation(errors, request):

    if request.POST.get('followup_enabled', 'NO') != 'YES':
        return

    verify_bounds_pair(errors, request, 'followup_bounds', 'Follow-up SPRT Bounds')
    verify_conf_pair(errors, request, 'followup_confidence', 'Follow-up Confidence')
    verify_time_control(errors, request, 'followup_time_control', 'Follow-up Time Control')

    try: assert 0.00 < float(request.POST['followup_at_percent']) <= 100.00
    except: errors.append('Follow-up "Start at %" must be within (0, 100]')

def verify_ltc_creation(errors, request):

    if request.POST.get('ltc_enabled', 'NO') != 'YES':
        return

    verify_bounds_pair(errors, request, 'ltc_bounds', 'LTC Bounds')
    verify_conf_pair(errors, request, 'ltc_confidence', 'LTC Confidence')
    verify_time_control(errors, request, 'ltc_time_control', 'LTC Time Control')
    verify_option_overrides(errors, request, 'ltc_options', 'LTC Options')

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

def verify_option_overrides(errors, request, field, field_name):
    try:
        overrides = request.POST.get(field, '')

        # Nothing but Name=Value pairs may be given
        assert not OpenBench.followup_utils.OPTION_PATTERN.sub('', overrides).strip()

        # Threads and Hash are optional here, but must still be sane when given
        for option in ['Threads', 'Hash']:
            if (value := OpenBench.utils.extract_option(overrides, option)) is not None:
                assert int(value) >= 1

    except: errors.append('%s must be Name=Value pairs, with Threads and Hash at least 1' % (field_name))

def estimate_tuned_options(request):

    # Longest Name=Value string the SPSA follow-up could pass, without knowing the tuned values
    try:
        options = []
        for line in request.POST['spsa_inputs'].splitlines():
            name, dtype, value, minimum, maximum = map(str.strip, line.split(',')[:5])
            width = 24 if dtype == 'float' else max(len(str(int(float(x)))) for x in (minimum, maximum))
            options.append('%s=%s' % (name, '9' * width))
        return ' '.join(options)

    except: return '' # Malformed SPSA inputs are reported by verify_spsa_inputs()

def verify_followup_options_length(errors, request, fields, extra):

    # Options of a follow-up are built while results are reported, so they must fit the database now
    limit     = Test._meta.get_field('dev_options').max_length
    overrides = request.POST.get('ltc_options', '') if request.POST.get('ltc_enabled', 'NO') == 'YES' else ''

    try:
        for field in fields:
            options = OpenBench.followup_utils.merge_options(request.POST[field], extra)
            options = OpenBench.followup_utils.merge_options(options, overrides)
            assert len(options) <= limit
    except: errors.append('Options of the follow-up would exceed %d characters' % (limit))

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


def collect_github_info(errors, request, field):

    # Get branch name / commit sha / tag, and the API path for it
    branch = request.POST['{0}_branch'.format(field)]
    bysha  = bool(re.search('^[0-9a-fA-F]{40}$', branch))

    # All API requests will share this common path. Some engines are private.
    base    = request.POST['%s_repo' % (field)].replace('github.com', 'api.github.com/repos')
    engine  = request.POST['%s_engine' % (field)]
    private = EngineConfig.objects.filter(name=engine).values_list('private', flat=True).first()
    headers = {}

    ## Step 1: Verify the target of the API requests

    # Private engines must have a token stored in credentials.enginename
    if private and not (headers := OpenBench.utils.read_git_credentials(engine)):
        errors.append('Server does not have access tokens for this engine')
        return

    # Do not allow private engines to use forked repos ( We don't have a token! )
    if requests_illegal_fork(request, field):
        errors.append('Forked Repositories are not allowed for Private engines')
        return

    # Avoid leaking our credentials to other websites
    if not base.startswith('https://api.github.com/'):
        errors.append('OpenBench may only reach Github\'s API')
        return

    ## Step 2: Connect to the Github API for the given Branch or Commit SHA.
    ## - Parse the most recent commit message for a bench, unless one was supplied.
    ## - We will translate any branch name into a commit SHA for later use
    ## - Construct the URL to download the source code from Github into a .zip file.

    try: # Fetch data from the Github API

        # Lookup branch or commit sha, but will fail for tags
        url  = OpenBench.utils.path_join(base, 'commits' if bysha else 'branches', branch)
        data = requests.get(url, headers=headers).json()

        # Check to see if the branch name was actually a tag name
        if not bysha and 'commit' not in data:
            url  = OpenBench.utils.path_join(base, 'commits', branch)
            data = requests.get(url, headers=headers).json()

        # Actual branches have to go one layer deeper
        elif not bysha: data = data['commit']

        # Check that all the data we need going forward is present
        assert 'message' in data['commit'] and 'sha' in data

    except: # Unable to find for whatever reason
        traceback.print_exc()
        errors.append('%s could not be found' % (branch or 'Branch'))
        return

    # Extract the bench from the web form, or from the commit message
    if not (bench := determine_bench(request, field, data['commit']['message'])):
        errors.append('Unable to parse a Bench for %s' % (branch))
        return

    info   = request.POST['info'] or strip_message(data['commit']['message'])
    source = OpenBench.utils.path_join(base, 'zipball', data['sha'])
    return (source, branch, data['sha'], bench, info)

def collect_followup_info(errors, request):

    if request.POST.get('followup_enabled', 'NO') != 'YES':
        return None

    # Base of the follow-up SPRT lives in the same repo and engine as the tune.
    # Resolve it now, so typos fail at creation and the SHA + Bench are pinned.
    post = request.POST.copy()
    post['followup_repo'  ] = post['dev_repo']
    post['followup_engine'] = post['dev_engine']

    return collect_github_info(errors, SimpleNamespace(POST=post), 'followup')

def requests_illegal_fork(request, field):

    # Strip trailing '/'s for sanity
    config  = EngineConfig.objects.filter(name=request.POST['%s_engine' % (field)]).first()
    eng_src = config.source.rstrip('/')
    tar_src = request.POST['%s_repo' % (field)].rstrip('/')

    # Illegal if sources do not match for Private engines
    return config.private and eng_src != tar_src

def determine_bench(request, field, message):

    # Use the provided bench if possible
    try: return int(request.POST['{0}_bench'.format(field)])
    except: pass

    # Fallback to try to parse the Bench from the commit
    try:
        benches = re.findall('(?:BENCH|NODES)[ :=]+([0-9,]+)', message, re.IGNORECASE)
        return int(benches[-1].replace(',', ''))
    except: return None

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
