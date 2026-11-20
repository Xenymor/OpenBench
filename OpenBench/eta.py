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

import math
import statistics

MIN_GAMES = 400  # Before which an SPRT's estimate is too noisy
MIN_LLR   = 0.15 # Below which the direction of an SPRT is anyone's guess
MAX_RATIO = 50   # Estimates beyond this many times the games so far are no estimates
MIN_RATE  = 0.1  # Games per minute, below which a Workload stands still, and has no end in sight
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

    if games is None or not rate or rate < MIN_RATE:
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

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                     PROBABILITY THAT A RUNNING SPRT PASSES                    #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

# In the normalized Elo model of PentanomialSPRT() (stats.py), a game pair with the score x
# adds about (t1 - t0) * (x - 0.5) / sigma - (t1^2 - t0^2) / 2 to the LLR, where t0 and t1 are
# the bounds as normalized t-values, and sigma the spread of the pair scores. With the true
# t-value t, the LLR therefore walks with the drift (t1 - t0) * (t - (t0 + t1) / 2) and the
# variance (t1 - t0)^2 per pair. A walk with drift m and variance v, now at the LLR x between
# the bounds a < x < b, reaches b first with the probability
#
#     (1 - exp(-2 m (x - a) / v)) / (1 - exp(-2 m (b - a) / v)),      or (x - a) / (b - a) for m = 0
#
# The true t is unknown. After N pairs, the measured one is about normal around it, with a
# standard deviation of 1 / sqrt(N). Early on, that alone allows far more Elo than patches
# ever have, and the probabilities come out far too sure. So it is combined with a prior of how
# strong patches are: normal, with the mean and spread of the Elo of earlier SPRTs, after taking
# out their own noise, see elo_prior(). The probability is averaged over the resulting posterior.
# Simulated SPRTs with OpenBench's own LLR confirm that the probabilities are calibrated.
# Trinomial Tests are treated alike, with games in place of pairs.

MIN_PAIRS   = 100        # Before which the spread of the scores is too uncertain
GRID_STEPS  = 121        # Points of the average over the true t-value, within 6 standard deviations
PRIOR       = (0.0, 3.0) # Mean and spread of the Elo of patches, while there is too little history
PRIOR_MIN   = 1.0        # Smallest spread, so that a quiet history never makes it overconfident
PRIOR_TESTS = 200        # Finished SPRTs that the prior is taken from
PRIOR_TTL   = 600        # Seconds the prior is kept, since it changes slowly

_prior_cache = { 'time' : 0.0, 'value' : None }

def elo_prior():

    # (mean, spread) of the true Elo of recent SPRTs: the spread of their measured Elo, less
    # the part that their own noise explains. Cached, since every running SPRT on a page needs it.
    import time
    from OpenBench.models import Test
    from OpenBench.stats import Elo

    if _prior_cache['value'] and time.time() - _prior_cache['time'] < PRIOR_TTL:
        return _prior_cache['value']

    measured, noise = [], []
    for test in Test.objects.filter(test_mode='SPRT', finished=True, deleted=False, games__gte=2 * MIN_PAIRS).order_by('-id')[:PRIOR_TESTS]:
        lower, elo, upper = Elo(test.results())
        measured.append(elo)
        noise.append(((upper - lower) / 3.92) ** 2) # The 95% interval spans 3.92 standard deviations

    value = PRIOR
    if len(measured) >= 20:
        mean   = sum(measured) / len(measured)
        spread = sum((elo - mean) ** 2 for elo in measured) / (len(measured) - 1) - sum(noise) / len(noise)
        value  = (mean, max(PRIOR_MIN, math.sqrt(max(spread, 0.0))))

    _prior_cache.update(time=time.time(), value=value)
    return value

def elo_to_t(elo, sigma):
    # The normalized t-value of an Elo difference, for scores that spread by sigma
    return (1.0 / (1.0 + 10.0 ** (-elo / 400.0)) - 0.5) / sigma

def hit_upper(x, a, b, drift, variance):

    # Probability that the walk reaches b before a
    if abs(drift) < 1e-12:
        return (x - a) / (b - a)

    k = -2.0 * drift / variance
    try:
        return (1.0 - math.exp(k * (x - a))) / (1.0 - math.exp(k * (b - a)))
    except OverflowError: # A drift that strong decides it either way
        return 1.0 if drift > 0 else 0.0

def pass_probability(test, prior=None):

    # Probability that a running SPRT passes, or None if there is no telling yet.
    # prior is (mean, spread) of the Elo of patches, by default from elo_prior()
    if test.test_mode != 'SPRT':
        return None

    if test.finished:
        return 1.0 if test.passed else 0.0 if test.failed else None

    if test.use_penta:
        counts, scores, scale = test.as_penta(), [0.0, 0.25, 0.5, 0.75, 1.0], math.sqrt(2)
    else:
        counts, scores, scale = test.as_tri(), [0.0, 0.5, 1.0], 1.0

    n = sum(counts)
    if n < MIN_PAIRS:
        return None

    mean     = sum(score * count for score, count in zip(scores, counts)) / n
    variance = sum((score - mean) ** 2 * count for score, count in zip(scores, counts)) / n
    if variance <= 0:
        return None

    # The bounds as normalized t-values, exactly as PentanomialSPRT() does
    nelo_per_t = 800 / math.log(10)
    t0, t1     = (elo / nelo_per_t * scale for elo in (test.elolower, test.eloupper))

    sigma = math.sqrt(variance)
    t_hat = (mean - 0.5) / sigma
    a, b  = test.lowerllr, test.upperllr
    x     = min(max(test.currentllr, a), b)
    width = (t1 - t0) ** 2

    # The posterior of the true t-value: the measured one, with the prior of patches
    prior_elo, prior_spread = prior or elo_prior()
    prior_t   = elo_to_t(prior_elo, sigma)
    prior_var = ((elo_to_t(prior_elo + prior_spread, sigma) - elo_to_t(prior_elo - prior_spread, sigma)) / 2) ** 2
    post_var  = 1.0 / (n + 1.0 / prior_var)
    post_mean = post_var * (n * t_hat + prior_t / prior_var)

    # Average over the true t-value, with the weights of a normal distribution
    total = weight_sum = 0.0
    for step in range(GRID_STEPS):
        z      = -6.0 + 12.0 * step / (GRID_STEPS - 1)
        weight = math.exp(-0.5 * z * z)
        t      = post_mean + z * math.sqrt(post_var)
        total += weight * hit_upper(x, a, b, (t1 - t0) * (t - (t0 + t1) / 2), width)
        weight_sum += weight

    return min(1.0, max(0.0, total / weight_sum))

def describe_probability(probability):
    return '-' if probability is None else '%d%%' % (round(100 * probability))
