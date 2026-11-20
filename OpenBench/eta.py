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

# Estimates of how long Workloads will still run.
#
#   >>> remaining_games(test)        # Games still to play, or None if there is no telling
#   >>> remaining_seconds(test, now) # The same at the recent throughput, see Test.games_per_minute()
#
# An SPRT's LLR grows about linearly with the games, as long as the results keep their
# distribution. So the LLR per game so far tells how many games it takes to reach the bound
# it heads for. Early on, or while the LLR stays close to 0, that is too noisy to say.
#
# Workloads that have not started yet, like the waiting Nodes of a Workflow, are estimated
# from history instead, see typical_seconds(): the median games of recent finished SPRTs at
# the same time control, at the median throughput those had.

import statistics

MIN_GAMES = 400  # Before which an SPRT's estimate is too noisy
MIN_LLR   = 0.15 # Below which the direction of an SPRT is anyone's guess
MAX_RATIO = 50   # Estimates beyond this many times the games so far are no estimates
HISTORY   = 30   # Finished Workloads looked at for typical_seconds()

def remaining_games(test, target=None):

    # target replaces the planned games of a tune, as for a Workflow Node that is done earlier
    if test.finished:
        return 0

    if test.test_mode == 'SPRT':

        if test.games < MIN_GAMES or abs(test.currentllr) < MIN_LLR:
            return None

        target = test.upperllr if test.currentllr > 0 else test.lowerllr
        games  = test.games * (target / test.currentllr - 1.0)
        return int(games) if 0 <= games <= MAX_RATIO * test.games else None

    if test.test_mode in ['GAMES', 'DATAGEN']:
        return max(0, test.max_games - test.games)

    if test.test_mode == 'SPSA':
        run = test.spsa_run
        return max(0, int(target if target is not None else 2 * run.pairs_per * run.iterations) - test.games)

    return None

def remaining_seconds(test, now, target=None):

    games = remaining_games(test, target)
    rate  = test.games_per_minute(now)

    if games is None or not rate:
        return None

    return 60.0 * games / rate

def describe(seconds):

    # '~2h 10m', '~45m', '~3d 4h'
    if seconds is None:
        return '-'

    minutes = int(round(seconds / 60.0))
    if minutes < 1:
        return '<1m'

    if minutes < 60:
        return '~%dm' % (minutes)

    hours = minutes // 60
    if hours < 48:
        return '~%dh %02dm' % (hours, minutes % 60)

    return '~%dd %dh' % (hours // 24, hours % 24)

def typical_seconds(mode, time_control, games=None):

    # How long a Workload of this mode and time control takes, from recent ones, or None.
    # games, if given, replaces the typical number of games, as for SPSA tunes, whose length is known.
    from django.db.models import Q
    from django.utils import timezone
    from OpenBench.models import Test

    now      = timezone.now()
    finished = Test.objects.filter(finished=True, deleted=False, dev_time_control=time_control, rate_updated__isnull=False)
    rates    = [rate for test in finished.order_by('-id')[:HISTORY] if (rate := test.games_per_minute(now))]

    if not rates:
        return None

    if games is None:
        decided = finished.filter(Q(passed=True) | Q(failed=True), test_mode=mode)
        played  = list(decided.order_by('-id').values_list('games', flat=True)[:HISTORY])
        if not played:
            return None
        games = statistics.median(played)

    return 60.0 * games / statistics.median(rates)
