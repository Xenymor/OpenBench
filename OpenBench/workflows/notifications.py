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
# has committed, so that nothing is announced that was rolled back. Every finished Test,
# and every Node that needs attention, gets a message of its own, while the end of a
# Workflow is one summary. They are posted in order from a short-lived thread, so that
# Discord never slows down a step.

import os
import re
import threading
import time
import traceback

import requests

from OpenSite.settings import PROJECT_PATH

CREDENTIALS = os.path.join(PROJECT_PATH, 'Config', 'credentials.discord')

# Only Discord's own webhook endpoints, so the file can never point the server elsewhere
WEBHOOK_PATTERN = re.compile(r'^https://(?:discord|discordapp)\.com/api/webhooks/\d+/[A-Za-z0-9_-]+$')

# Per Workflow, in its defaults. Missing means 'all', as for Workflows from before
LEVELS = ['off', 'attention', 'all']

COLORS = { 'attention' : 0xEFF891, 'finished' : 0x76D576, 'passed' : 0x76D576, 'failed' : 0xFA9696, 'done' : 0x60F0FF }

TIMEOUT    = 10 # Seconds, per message
MAX_WAIT   = 10 # Seconds to wait when Discord asks to slow down, before trying once more

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
    value = str(graph.get('defaults', {}).get('notify', '') or 'all')
    return value if value in LEVELS else 'all'

def embed(kind, title, lines, url=''):

    # Discord limits titles to 256 characters, and descriptions to 4096
    entry = { 'title' : title[:256], 'description' : '\n'.join(lines)[:4000], 'color' : COLORS.get(kind, 0x8BBAD6) }
    if url:
        entry['url'] = url
    return entry

def message(entry):
    # Labels and branch names are typed by users, so they must never ping anyone
    return { 'username' : 'OpenBench', 'embeds' : [entry], 'allowed_mentions' : { 'parse' : [] } }

def build_messages(workflow, events, site=''):

    # events are ( kind, label, text ), as collected by runner.step(). One message
    # per Node, and one summary for a finished Workflow.
    url   = '%s/workflow/%d/' % (site, workflow.id) if site else ''
    name  = '#%d %s' % (workflow.id, workflow.name)
    words = { 'attention' : 'needs attention', 'passed' : 'passed', 'failed' : 'failed', 'done' : 'done' }

    messages = []
    for kind, label, text in events:
        if kind in words:
            messages.append(message(embed(kind, '%s · %s %s' % (name, label, words[kind]), [text], url)))

    if summary := ['%s: %s' % (label, text) for kind, label, text in events if kind == 'finished']:
        messages.append(message(embed('finished', '%s finished' % (name), summary, url)))

    return messages

def post(webhook, payload):

    # Discord answers 429 with how long to wait, when many messages arrive at once
    response = requests.post(webhook, json=payload, timeout=TIMEOUT)

    if response.status_code == 429:
        try: wait = float(response.json().get('retry_after', 1))
        except Exception: wait = 1
        time.sleep(min(max(wait, 0), MAX_WAIT))
        response = requests.post(webhook, json=payload, timeout=TIMEOUT)

    if response.status_code >= 400:
        print('Discord rejected a Workflow notification: %d %s' % (response.status_code, response.text[:200]))

def deliver(webhook, messages):

    # In order, and one failure never keeps the others from being sent
    for payload in messages:
        try: post(webhook, payload)
        except Exception: traceback.print_exc()

def notify(workflow, events):

    if not events or not (config := read_config()):
        return

    messages = build_messages(workflow, events, config['site'])
    threading.Thread(target=deliver, args=(config['webhook'], messages), daemon=True).start()
