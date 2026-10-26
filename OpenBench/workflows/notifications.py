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

# Discord messages about Workflows, sent through a webhook. Configured by the file
# Config/credentials.discord, which is never committed (see .gitignore):
#
#   webhook=https://discord.com/api/webhooks/<id>/<token>
#   site=https://openbench.example.com
#
# Without that file, nothing is sent. The site is optional, and only used for links.
#
# runner.step() collects what happened, and hands it to notify() once its transaction
# has committed, so that nothing is announced that was rolled back. The message is then
# posted from a short-lived thread, so that Discord never slows down a step.

import os
import re
import threading
import traceback

import requests

from OpenSite.settings import PROJECT_PATH

CREDENTIALS = os.path.join(PROJECT_PATH, 'Config', 'credentials.discord')

# Only Discord's own webhook endpoints, so the file can never point the server elsewhere
WEBHOOK_PATTERN = re.compile(r'^https://(?:discord|discordapp)\.com/api/webhooks/\d+/[A-Za-z0-9_-]+$')

# Per Workflow, in its defaults. Missing means 'attention', as for Workflows from before
LEVELS = ['off', 'attention', 'all']

COLORS = { 'attention' : 0xEFF891, 'finished' : 0x76D576, 'passed' : 0x76D576, 'failed' : 0xFA9696, 'done' : 0x60F0FF }

TIMEOUT = 10 # Seconds

def read_config(path=None):

    # { 'webhook' : ..., 'site' : ... }, or None when notifications are not set up
    try:
        with open(path or CREDENTIALS) as fin:
            values = dict(line.strip().split('=', 1) for line in fin if '=' in line)
    except OSError:
        return None

    webhook = values.get('webhook', '').strip()
    site    = values.get('site', '').strip().rstrip('/')

    if not WEBHOOK_PATTERN.match(webhook):
        return None

    return { 'webhook' : webhook, 'site' : site if site.startswith('https://') or site.startswith('http://') else '' }

def level(graph):
    value = str(graph.get('defaults', {}).get('notify', '') or 'attention')
    return value if value in LEVELS else 'attention'

def embed(kind, title, lines, url=''):

    # Discord limits titles to 256 characters, and descriptions to 4096
    entry = { 'title' : title[:256], 'description' : '\n'.join(lines)[:4000], 'color' : COLORS.get(kind, 0x8BBAD6) }
    if url:
        entry['url'] = url
    return entry

def build_message(workflow, events, site=''):

    # events are ( kind, line ), as collected by runner.step(). One message per step,
    # with one embed per kind of event, at most the ten that Discord allows.
    url    = '%s/workflow/%d/' % (site, workflow.id) if site else ''
    name   = '#%d %s' % (workflow.id, workflow.name)
    titles = {
        'attention' : '%s needs attention',
        'finished'  : '%s finished',
        'passed'    : '%s: passed',
        'failed'    : '%s: failed',
        'done'      : '%s: done',
    }

    grouped = {}
    for kind, line in events:
        grouped.setdefault(kind, []).append(line)

    embeds = [embed(kind, titles.get(kind, '%s') % (name), lines, url) for kind, lines in grouped.items()][:10]

    # Labels and branch names are typed by users, so they must never ping anyone
    return { 'username' : 'OpenBench', 'embeds' : embeds, 'allowed_mentions' : { 'parse' : [] } }

def deliver(webhook, message):
    try:
        response = requests.post(webhook, json=message, timeout=TIMEOUT)
        if response.status_code >= 400:
            print('Discord rejected a Workflow notification: %d %s' % (response.status_code, response.text[:200]))
    except Exception:
        traceback.print_exc()

def notify(workflow, events):

    if not events or not (config := read_config()):
        return

    message = build_message(workflow, events, config['site'])
    threading.Thread(target=deliver, args=(config['webhook'], message), daemon=True).start()
