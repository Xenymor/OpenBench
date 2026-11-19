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
#   >>> modify_workload(request, id, action)
#
# Given a request, a workload id, and an action, attempt to modify the workload as
# requested. This will return the user to the index, or a login page, with some
# indication as to success, or a reason for failure.

from django.db import transaction

import OpenBench.utils
import OpenBench.views

from OpenBench.models import *

def modify_workload(request, id, action=None):

    actions = {
        'APPROVE' : approve_workload, 'RESTART' : restart_workload,
        'STOP'    : stop_workload,    'DELETE'  : delete_workload,
        'RESTORE' : restore_workload, 'MODIFY'  : tweak_workload,
        'REBENCH' : rebench_workload,
    }

    # Make sure the requested Action is a known one
    if action not in actions.keys():
        return OpenBench.views.redirect(request, '/index/', error='Unknown Workload action')

    # Make sure that the workload exists
    if not (workload := Test.objects.filter(id=id).first()):
        return OpenBench.views.redirect(request, '/index/', error='No such Workload exists')

    # Must be logged in to interact with workloads
    if not request.user.is_authenticated:
        return OpenBench.views.redirect(request, '/login/', error='Only users may modify Workloads')

    # Must be an approver, or interacting with their own workload
    profile = Profile.objects.get(user=request.user)
    if not profile.approver and workload.author != request.user.username:
        return OpenBench.views.redirect(request, '/index/', error='You cannot interact with another user\'s Workload')

    # Must be an approver
    if action == 'APPROVE' and not profile.approver:
        return OpenBench.views.redirect(request, '/index/', error='You cannot approve this Workload')

    # Make the change; Record the change; Save the change. All at once, since
    # a REBENCH that was only half saved would no longer find the Bench to use
    try:
        with transaction.atomic():
            message = actions[action](request, profile, workload)
            LogEvent.objects.create(author=request.user.username, summary=action, log_file='', test_id=id)
            workload.save()

    except WorkloadActionError as error:
        return OpenBench.views.redirect(request, '/%s/%d/' % (workload.workload_type_str(), workload.id), error=str(error))

    # Send back to the index, notifying them of the success
    return OpenBench.views.redirect(request, '/index/', status=message)

class WorkloadActionError(Exception):
    pass

def bench_corrections(workload):

    # { side : { 'given', 'measured' } } for a stopped Workload whose Bench workers disputed.
    # Finished with a result, it has nothing to restart, so the Events are not even looked at.
    # Neither when deleted, since it would have to be restored first.
    if not workload.finished or workload.passed or workload.failed or workload.deleted:
        return {}

    found = OpenBench.utils.measured_benches(workload, OpenBench.utils.workload_sides(workload))
    return { side : { 'given' : getattr(workload, side).bench, 'measured' : sorted(values) }
             for side, values in found.items() }

def approve_workload(request, profile, workload):
    workload.approved = True;
    return 'Workload was Approved!'

def restart_workload(request, profile, workload):
    workload.finished = False;
    workload.rate_updated = None # Time spent stopped should not count towards the throughput
    return 'Workload was Restarted!'

def rebench_workload(request, profile, workload):
    return apply_measured_bench(workload)

def apply_measured_bench(workload):

    # Restarts a Workload that workers stopped for a wrong Bench, expecting the Bench they
    # measured from now on. Each Workload has Engines of its own, so they can be changed.
    # Shared by the REBENCH action and by Workflows, whose Nodes then follow their Test.
    # The caller saves the Workload and logs the REBENCH, in the same transaction.
    if not workload.finished or workload.passed or workload.failed:
        raise WorkloadActionError('Only stopped Workloads can be restarted with a measured Bench')

    if workload.deleted:
        raise WorkloadActionError('This Workload was deleted, so restore it before restarting it')

    if not (corrections := bench_corrections(workload)):
        raise WorkloadActionError('No worker reported a wrong Bench for this Workload, which may be restarted as usual')

    if conflicts := ['%s: %s' % (side.title(), ', '.join(map(str, info['measured'])))
                     for side, info in corrections.items() if len(info['measured']) > 1]:
        raise WorkloadActionError('Workers measured different Benches (%s), so check the build by hand' % ('; '.join(conflicts)))

    for side, info in corrections.items():
        engine = getattr(workload, side)
        engine.bench = info['measured'][0]
        engine.save()

    workload.finished     = False
    workload.rate_updated = None # Time spent stopped should not count towards the throughput

    changes = ', '.join('%s %d -> %d' % (side.title(), info['given'], info['measured'][0]) for side, info in corrections.items())
    return 'Workload was Restarted with the measured Bench (%s)!' % (changes)

def stop_workload(request, profile, workload):
    workload.finished = True;
    return 'Workload was Stopped!'

def delete_workload(request, profile, workload):
    workload.deleted = True
    return 'Workload was Deleted!'

def restore_workload(request, profile, workload):
    workload.deleted = False
    return 'Workload was Restored!'

def tweak_workload(request, profile, workload):

    try: # Priority can be any integer value
        workload.priority = int(request.POST['priority'])
    except: pass

    try: # Throughput must be at least 1
        workload.throughput = max(1, int(request.POST['throughput']))
    except: pass

    try: # Must be at least one. Cannot be changed for Tuning workloads
        if workload.test_mode != 'SPSA':
            workload.workload_size = max(1, int(request.POST['workload_size']))
    except: pass

    try:
        workload.info = request.POST['info']
    except: pass

    return 'Workload was Modified!'
