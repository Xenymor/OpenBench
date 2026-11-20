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

# Watches the crashes, time losses and illegal moves that workers report with their results.
#
#   >>> alert, reason = record_errors(test, crashes, timelosses, illegals)
#
# Called by update_test() while it holds the Test's row lock. Counts are added to the Test.
# alert is set for the first crash or illegal move since the last restart, so that it is
# announced right away. reason is set once a limit is reached, and the Test is then stopped.
#
# The limits count from the last restart, see reset_baseline(), so that a Test which was
# restarted on purpose is not stopped again at once. They are configured in Config/config.json:
#
#   "error_limits" : { "crashes" : 3, "illegals" : 1, "timeloss_percent" : 1.0, "timeloss_min_games" : 400 }
#
# A limit of 0 disables it. Missing values use the defaults below.

import OpenBench.config

DEFAULT_LIMITS = {
    'crashes'            : 3,    # Disconnects and stalls, which a stuck engine also causes
    'illegals'           : 1,    # Always a bug
    'timeloss_percent'   : 1.0,  # Of the games played since the last restart
    'timeloss_min_games' : 400,  # Before which the share of time losses is too noisy to judge
}

COUNTERS = ['crashes', 'timelosses', 'illegals']

def limits():

    # Runs for every report of every worker, so a broken config must never raise: null disables
    # a limit, as 0 does, and a value that is no number keeps the default
    configured = (OpenBench.config.OPENBENCH_CONFIG or {}).get('error_limits') or {}
    found      = {}

    for key, default in DEFAULT_LIMITS.items():
        value = configured.get(key, default) if isinstance(configured, dict) else default
        try: found[key] = type(default)(0 if value is None else value)
        except (TypeError, ValueError): found[key] = default

    return found

def baseline(test):
    stored = test.error_baseline or {}
    return { key : int(stored.get(key, 0)) for key in COUNTERS + ['games'] }

def reset_baseline(test):
    # The limits count from now on, as after a restart
    test.error_baseline = { 'crashes' : test.crashes, 'timelosses' : test.timelosses,
                            'illegals' : test.illegals, 'games' : test.games }
    test.stop_reason = ''

def since_restart(test):
    base = baseline(test)
    return { key : max(0, getattr(test, key) - base[key]) for key in COUNTERS + ['games'] }

def summary(counts):
    parts = [('%d crash' % counts['crashes']) + ('es' if counts['crashes'] != 1 else ''),
             ('%d time loss' % counts['timelosses']) + ('es' if counts['timelosses'] != 1 else ''),
             ('%d illegal move' % counts['illegals']) + ('s' if counts['illegals'] != 1 else '')]
    return ', '.join(parts)

def exceeded(counts, limit):

    # The first limit that the counts since the restart reach, as a reason, or None
    if limit['illegals'] and counts['illegals'] >= limit['illegals']:
        return '%d illegal move%s' % (counts['illegals'], 's' if counts['illegals'] != 1 else '')

    if limit['crashes'] and counts['crashes'] >= limit['crashes']:
        return '%d crash%s or stall%s' % (counts['crashes'], 'es' if counts['crashes'] != 1 else '', 's' if counts['crashes'] != 1 else '')

    games = counts['games']
    if limit['timeloss_percent'] and games >= max(1, limit['timeloss_min_games']):
        share = 100.0 * counts['timelosses'] / games
        if share >= limit['timeloss_percent']:
            return '%.1f%% of the games lost on time (%d of %d)' % (share, counts['timelosses'], games)

    return None

def record_errors(test, crashes, timelosses, illegals):

    # The games of the report must already be counted on the Test
    before = since_restart(test)

    test.crashes    += crashes
    test.timelosses += timelosses
    test.illegals   += illegals

    after  = since_restart(test)
    alert  = None
    reason = None

    # Announced once, for the first real error since the restart. Time losses alone are
    # announced only when they stop the Test, since the odd one happens at short time controls.
    if not (before['crashes'] or before['illegals']) and (after['crashes'] or after['illegals']):
        alert = 'First error since the start: %s' % (summary(after))

    if not test.finished and (cause := exceeded(after, limits())):
        reason = 'Stopped by OpenBench: %s' % (cause)
        test.stop_reason = reason[:256]
        test.finished    = True

    return alert, reason
