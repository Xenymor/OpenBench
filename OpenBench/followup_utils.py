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

# Automatic follow-up SPRTs, spawned from update_test() while it holds the
# select_for_update() lock on the parent Test. That lock, together with the
# followup_test Id being set before the parent is saved, guarantees that each
# follow-up is created exactly once. A follow-up that cannot be created marks
# followup_test as FOLLOWUP_FAILED, so it is neither retried nor allowed to
# roll back the results that were just reported for the parent.
#
#   SPSA -> SPRT : Once at_percent of the tune has been played, the tuned values
#                  are tested against a Base that was pinned at creation time.
#
#   SPRT -> SPRT : Once an SPRT passes, the same Dev and Base are tested again
#                  at a longer time control, with optional option overrides.
#
# Test.followup holds the settings for the follow-up. For an SPSA, it may itself
# contain an 'ltc' entry, which becomes the followup of the created SPRT.

import math
import re
import traceback

from django.db import transaction

import OpenBench.spsa_utils

from OpenBench.models import Engine, LogEvent, Test

# Name=Value, where Value may be quoted to contain spaces, as in extract_option()
OPTION_PATTERN = re.compile(r'''(\S+?)=("[^"]*"|'[^']*'|\S*)''')

# Stored in followup_test when creating the follow-up raised an exception
FOLLOWUP_FAILED = -1

def parse_options(options):
    return [match.groups() for match in OPTION_PATTERN.finditer(options)]

def merge_options(options, overrides):

    # Overrides replace options of the same name, and new ones are appended.
    # UCI option names are case-insensitive, so Hash= also replaces hash=
    if not overrides.strip():
        return options

    merged = {}
    for name, value in parse_options(options) + parse_options(overrides):
        merged[name.lower()] = (name, value)

    return ' '.join('%s=%s' % (name, value) for name, value in merged.values())

def copy_common_settings(source, test):

    test.author       = source.author
    test.book_name    = source.book_name
    test.upload_pgns  = source.upload_pgns

    test.priority     = source.priority
    test.throughput   = source.throughput

    test.syzygy_wdl   = source.syzygy_wdl
    test.syzygy_adj   = source.syzygy_adj
    test.win_adj      = source.win_adj
    test.draw_adj     = source.draw_adj

    test.scale_method = source.scale_method
    test.scale_nps    = source.scale_nps

def apply_sprt_settings(test, settings):

    test.test_mode = 'SPRT'
    test.elolower  = settings['elolower']
    test.eloupper  = settings['eloupper']
    test.alpha     = settings['alpha']
    test.beta      = settings['beta']
    test.lowerllr  = math.log(test.beta / (1.0 - test.alpha))
    test.upperllr  = math.log((1.0 - test.beta) / test.alpha)
    test.approved  = settings['auto_approve']

def log_followup(parent, test, kind):
    summary = 'CREATE %s FOLLOW-UP OF %d P=%d TP=%d' % (kind, parent.id, test.priority, test.throughput)
    LogEvent.objects.create(author=parent.author, summary=summary, log_file='', test_id=test.id)

def create_safely(parent, create):

    # Savepoint, so that a failure only discards the follow-up, never the parent's update
    try:
        with transaction.atomic():
            return create(parent)

    except Exception:
        traceback.print_exc()
        parent.followup_test = FOLLOWUP_FAILED
        summary = 'FAILED TO CREATE FOLLOW-UP OF %d' % (parent.id)
        LogEvent.objects.create(author=parent.author, summary=summary, log_file='', test_id=parent.id)
        return None

def maybe_create_spsa_followup(tune):
    return create_safely(tune, create_spsa_followup)

def maybe_create_ltc_followup(sprt):
    return create_safely(sprt, create_ltc_followup)

def create_spsa_followup(tune):

    if tune.test_mode != 'SPSA' or not tune.followup or tune.followup_test:
        return None

    followup    = tune.followup
    total_games = 2 * tune.spsa_run.pairs_per * tune.spsa_run.iterations

    if tune.games < total_games * followup['at_percent'] / 100.0:
        return None

    tuned = OpenBench.spsa_utils.spsa_tuned_options(tune)

    test = Test()
    copy_common_settings(tune, test)
    test.info              = ('Follow-up SPRT of SPSA #%d\n%s' % (tune.id, tuned))[:1024]

    # Dev is the exact commit that was tuned, with the tuned values passed as UCI options
    test.dev               = tune.dev
    test.dev_repo          = tune.dev_repo
    test.dev_engine        = tune.dev_engine
    test.dev_options       = merge_options(tune.dev_options, tuned)
    test.dev_network       = tune.dev_network
    test.dev_netname       = tune.dev_netname
    test.dev_time_control  = followup['time_control']

    # Base was resolved and pinned when the tune was created
    test.base              = Engine.objects.create(
        name=followup['base_name'], source=followup['base_source'],
        sha=followup['base_sha'], bench=followup['base_bench'])
    test.base_repo         = tune.dev_repo
    test.base_engine       = tune.dev_engine
    test.base_options      = tune.dev_options
    test.base_network      = tune.dev_network
    test.base_netname      = tune.dev_netname
    test.base_time_control = followup['time_control']

    test.workload_size     = 32
    test.followup          = followup.get('ltc') # Chains into an LTC follow-up, if requested

    apply_sprt_settings(test, followup)
    test.save()

    tune.followup_test = test.id
    log_followup(tune, test, 'SPSA')
    return test

def create_ltc_followup(sprt):

    if sprt.test_mode != 'SPRT' or not sprt.passed or not sprt.followup or sprt.followup_test:
        return None

    followup = sprt.followup

    test = Test()
    copy_common_settings(sprt, test)
    test.info              = ('LTC follow-up of #%d\n%s' % (sprt.id, sprt.info))[:1024]

    # Same commits as the passed SPRT, only the time control and overrides differ
    test.dev               = sprt.dev
    test.dev_repo          = sprt.dev_repo
    test.dev_engine        = sprt.dev_engine
    test.dev_options       = merge_options(sprt.dev_options, followup['options'])
    test.dev_network       = sprt.dev_network
    test.dev_netname       = sprt.dev_netname
    test.dev_time_control  = followup['time_control']

    test.base              = sprt.base
    test.base_repo         = sprt.base_repo
    test.base_engine       = sprt.base_engine
    test.base_options      = merge_options(sprt.base_options, followup['options'])
    test.base_network      = sprt.base_network
    test.base_netname      = sprt.base_netname
    test.base_time_control = followup['time_control']

    test.workload_size     = sprt.workload_size
    test.use_tri           = sprt.use_tri
    test.use_penta         = sprt.use_penta

    apply_sprt_settings(test, followup)
    test.save()

    sprt.followup_test = test.id
    log_followup(sprt, test, 'LTC')
    return test
