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

# Advances every Workflow that is not FINISHED or ABORTED, as the PGNWatcher does
# for the PGNs: one thread, in the one process that holds the watcher lockfile.

import sys
import threading
import traceback

from django.db import close_old_connections

from OpenBench.models import Workflow

WORKFLOW_INTERVAL = 15 # Seconds between passes

class WorkflowWatcher(threading.Thread):

    def __init__(self, stop_event, *args, **kwargs):
        self.stop_event = stop_event
        super().__init__(*args, **kwargs)

    def process_pending(self):

        # Imported late, since this module is imported while the apps are still loading,
        # and the runner pulls in the workload modules, which import OpenBench.views
        from OpenBench.workflows import runner

        final   = [Workflow.Status.FINISHED, Workflow.Status.ABORTED]
        pending = Workflow.objects.exclude(status__in=final).values_list('id', flat=True)

        for workflow_id in list(pending):

            if self.stop_event.is_set():
                return

            try: # One broken Workflow must not hold up the others
                runner.step(workflow_id)

            except Exception as error:
                if 'database is locked' not in str(error).lower():
                    traceback.print_exc()
                    sys.stdout.flush()

    def run(self):

        # Loop until we are shutdown by the atexit.register()
        while not self.stop_event.is_set():

            try: # Never exit on errors, to keep the watcher alive
                close_old_connections()
                self.process_pending()

            except Exception as error:
                # Silent while the Workflow tables are still being migrated in
                if 'openbench_workflow' not in str(error).lower():
                    traceback.print_exc()
                    sys.stdout.flush()

            self.stop_event.wait(timeout=WORKFLOW_INTERVAL)
