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

# History and convergence of SPSA tunes.
#
#   >>> converged = record(test, parameters)
#
# Called by update_test(), after the parameters of a tune were updated, while it holds the
# Test's row lock. Every so many games, the values of all parameters are kept as an
# SPSASnapshot, at most SNAPSHOTS of them over the whole tune, which the tune's page draws.
#
# A tune with SPSARun.auto_stop > 0 stops once it converged: after MIN_PROGRESS of its games,
# and once its history reaches back over the whole WINDOW,
# no parameter moved by more than auto_stop times its C_end over the last WINDOW of the games.
# Movement is the difference between the averages of the first and the second half of that
# window, so that the noise of single steps does not count. The Test is then finished, and
# SPSARun.converged_at keeps how many games it took. Workflows treat that as done.

from OpenBench.models import SPSASnapshot

SNAPSHOTS    = 200  # Over the whole tune
WINDOW       = 0.20 # Share of the planned games that convergence is judged over
MIN_PROGRESS = 0.30 # Share of the planned games before a tune may stop early
MIN_POINTS   = 6    # Snapshots within the window, at the least

def total_games(run):
    return 2 * run.pairs_per * run.iterations

def interval(run):
    return max(2 * run.pairs_per, total_games(run) // SNAPSHOTS)

def record(test, parameters):

    # Returns whether the tune stopped as converged with this report
    run = test.spsa_run

    if test.games - run.last_snapshot < interval(run) and not test.finished:
        return False

    SPSASnapshot.objects.create(spsa_run=run, games=test.games, values=[param.value for param in parameters])
    run.last_snapshot = test.games

    converged = False
    if run.auto_stop > 0 and not test.finished and run.converged_at is None:
        if (drift := window_drift(run, parameters)) is not None and drift <= run.auto_stop:
            run.converged_at = test.games
            test.finished    = True
            converged        = True

    run.save(update_fields=['last_snapshot', 'converged_at'])
    return converged

def window_drift(run, parameters, snapshots=None):

    # Largest movement of any parameter over the window, in units of its C_end,
    # or None while there is not enough history to judge
    total = total_games(run)

    if snapshots is None:
        snapshots = list(run.snapshots.order_by('games').values_list('games', 'values'))

    if not snapshots or snapshots[-1][0] < MIN_PROGRESS * total:
        return None

    start  = snapshots[-1][0] - WINDOW * total
    window = [values for games, values in snapshots if games >= start]

    # The history must reach back over the whole window. Tunes that ran before the history
    # was kept start theirs late, and a shorter span would show too little movement.
    if len(window) < MIN_POINTS or snapshots[0][0] > start + interval(run):
        return None

    half  = len(window) // 2
    drift = 0.0

    for index, param in enumerate(parameters):

        first  = [values[index] for values in window[:half] if index < len(values)]
        second = [values[index] for values in window[half:] if index < len(values)]

        if not first or not second or param.c_end <= 0:
            continue

        moved = abs(sum(second) / len(second) - sum(first) / len(first)) / param.c_end
        drift = max(drift, moved)

    return drift

def history(test):

    # Everything the tune's page needs to draw the course of each parameter
    run        = test.spsa_run
    parameters = list(run.parameters.order_by('index'))
    snapshots  = list(run.snapshots.order_by('games').values_list('games', 'values'))

    # The starting values come first, so that every line starts at its origin
    points = [(0, [param.start for param in parameters])] + snapshots

    return {
        'total'        : total_games(run),
        'games'        : [games for games, values in points],
        'auto_stop'    : run.auto_stop,
        'converged_at' : run.converged_at,
        'drift'        : window_drift(run, parameters, snapshots),
        'window'       : WINDOW,
        'params'       : [{
            'name'   : param.name,
            'start'  : param.start,
            'min'    : param.min_value,
            'max'    : param.max_value,
            'c_end'  : param.c_end,
            'values' : [values[index] if index < len(values) else None for games, values in points],
        } for index, param in enumerate(parameters)],
    }
