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

# Warnings about a new Test, kept in Test.checks, and shown on its page:
#
#   >>> checks = run_checks(test)
#   { 'same_bench' : True, 'behind' : 3, 'base_moved' : 2, 'base_branch' : 'main' }
#
#   same_bench  Dev and Base have the same Bench, with the same options and Network. The change
#               is then very likely not functional, and needs no SPRT with gainer bounds.
#   behind      Dev lacks this many commits of Base, so the result mixes in their effect.
#   base_moved  Base was given as a branch, which has this many commits more by now. That
#               happens to Workflows, whose branches are pinned when they are saved.
#
# Only keys with a finding are kept. Github is asked once per check, and any failure there
# only drops that check, so that a Test is never held up by it.

import traceback

from OpenBench import github
from OpenBench.workloads.verify_workload import GITHUB_SHA

MODES = ['SPRT', 'GAMES']

def same_bench(test):
    return (test.dev_engine == test.base_engine
            and test.dev.sha != test.base.sha
            and test.dev.bench == test.base.bench
            and test.dev_options == test.base_options
            and test.dev_network == test.base_network)

def run_checks(test):

    checks = {}

    if test.test_mode not in MODES:
        return checks

    if same_bench(test):
        checks['same_bench'] = True

    # Comparisons need both engines in one repository
    if test.dev_engine != test.base_engine or github.normalize_repo(test.dev_repo) != github.normalize_repo(test.base_repo):
        return checks

    try:
        if (behind := github.compare(test.dev_repo, test.dev_engine, test.base.sha, test.dev.sha)['behind_by']) > 0:
            checks['behind'] = behind
    except Exception:
        traceback.print_exc()

    # Only a Base given by its branch name can have moved on. Its name is how it was given.
    branch = test.base.name.strip()
    if branch and not GITHUB_SHA.match(branch) and not branch.startswith('@'):
        try:
            head = github.commit(test.base_repo, test.base_engine, branch)['sha']
            if head != test.base.sha and (moved := github.compare(test.base_repo, test.base_engine, test.base.sha, head)['ahead_by']) > 0:
                checks['base_moved']  = moved
                checks['base_branch'] = branch
        except Exception:
            traceback.print_exc()

    return checks

def describe(checks):

    # The warnings as sentences, for the Test's page, the form, and the Workflows
    lines = []

    if checks.get('same_bench'):
        lines.append('Dev and Base have the same Bench, so the change is probably not functional')

    if checks.get('behind'):
        lines.append('Dev is %d commit%s behind Base, consider rebasing' % (checks['behind'], 's' if checks['behind'] != 1 else ''))

    if checks.get('base_moved'):
        lines.append('%s has %d newer commit%s than the tested Base' % (
            checks.get('base_branch', 'Base'), checks['base_moved'], 's' if checks['base_moved'] != 1 else ''))

    return lines
