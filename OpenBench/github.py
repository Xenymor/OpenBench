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

# Small helpers around the Github API, shared by the checks of new Workloads, the
# action Nodes of Workflows, the Ladders, and the push triggers.
#
# Reading uses the token of the engine, if there is one (Config/credentials.<engine>),
# as the rest of OpenBench does. Writing always needs one, with permission to push
# and to open pull requests, and only ever goes to the engine's own repository.

import base64
import os
import re
import urllib.parse

import requests

from OpenBench.models import EngineConfig
from OpenSite.settings import PROJECT_PATH

TIMEOUT      = 30
REPO_PATTERN = re.compile(r'^https://github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$')
SHA_PATTERN  = re.compile(r'^[0-9a-fA-F]{40}$')

class GithubError(Exception):
    pass

def quote_ref(ref):

    # A branch, tag, sha or file path as part of an API URL. Refs come from users and pushes,
    # and must never reach another endpoint of the API with our token: '..' is refused, which
    # git does not allow in refs anyway, and everything is encoded, keeping only the slashes.
    ref = str(ref or '').strip()

    if not ref or '..' in ref or ref.startswith('/') or any(ord(char) < 32 for char in ref):
        raise GithubError('Malformed branch, tag or path: %s' % (ref[:64]))

    return urllib.parse.quote(ref, safe='/')

def path_join(*args):
    # As path_join(), which cannot be imported here without a cycle through the views
    return '/'.join([part.strip('/') for part in args]).rstrip('/')

def read_git_credentials(engine):
    # As OpenBench.utils.read_git_credentials()
    fpath = os.path.join(PROJECT_PATH, 'Config', 'credentials.%s' % (engine.replace(' ', '').lower()))
    if os.path.exists(fpath):
        with open(fpath) as fin:
            return { 'Authorization' : 'token %s' % fin.readlines()[0].rstrip() }

def normalize_repo(repo):
    # 'https://github.com/owner/repo', for comparing repositories however they were written
    match = REPO_PATTERN.match(str(repo or '').strip())
    return 'https://github.com/%s/%s' % (match.group(1), match.group(2)) if match else None

def api_base(repo):

    if not (repo := normalize_repo(repo)):
        raise GithubError('Sources must be found on https://github.com/<User>/<Repo>')

    return repo.replace('https://github.com/', 'https://api.github.com/repos/')

def read_headers(engine):
    return read_git_credentials(engine) or {}

def write_headers(engine, repo):

    # Our token may only ever push to the engine's own repository, never to a fork
    config = EngineConfig.objects.filter(name=engine).first()

    if not config or normalize_repo(config.source) != normalize_repo(repo):
        raise GithubError('OpenBench only writes to the configured repository of %s' % (engine))

    if not (headers := read_git_credentials(engine)):
        raise GithubError('Writing to Github needs a token in Config/credentials.%s' % (engine.replace(' ', '').lower()))

    return headers

def request(method, url, headers, payload=None, expected=(200, 201)):

    # The JSON of the answer, or a GithubError with Github's own explanation
    try:
        response = requests.request(method, url, headers=headers, json=payload, timeout=TIMEOUT)
    except requests.RequestException as error:
        raise GithubError('Github could not be reached: %s' % (error))

    if response.status_code not in expected:

        # A 422 says 'Validation Failed', and keeps what failed in its errors
        try:
            data    = response.json()
            details = [str(error.get('message') or error.get('code') or '') for error in data.get('errors') or [] if isinstance(error, dict)]
            message = '; '.join([data.get('message', '')] + [detail for detail in details if detail])
        except Exception:
            message = response.text[:200]

        raise GithubError('Github answered %d: %s' % (response.status_code, message[:500]))

    if response.status_code == 204 or not response.content:
        return { 'status' : response.status_code }

    data = response.json()
    if isinstance(data, dict):
        data.setdefault('status', response.status_code)
    return data

def get(repo, engine, path):
    return request('GET', path_join(api_base(repo), path), read_headers(engine))

def commit(repo, engine, ref):

    # { 'sha', 'message', 'date' } of a branch, tag or sha
    data = get(repo, engine, 'commits/%s' % (quote_ref(ref)))
    return {
        'sha'     : data['sha'].lower(),
        'message' : data['commit']['message'],
        'date'    : data['commit']['committer']['date'],
    }

def compare(repo, engine, base, head):
    # { 'ahead_by', 'behind_by', 'status' } of head relative to base
    data = get(repo, engine, 'compare/%s...%s' % (quote_ref(base), quote_ref(head)))
    return { 'ahead_by' : data['ahead_by'], 'behind_by' : data['behind_by'], 'status' : data['status'] }

def branch_exists(repo, engine, branch):
    try:
        request('GET', path_join(api_base(repo), 'branches', quote_ref(branch)), read_headers(engine))
        return True
    except GithubError as error:
        if '404' in str(error):
            return False
        raise

def create_branch(repo, engine, branch, sha):
    url = path_join(api_base(repo), 'git', 'refs')
    quote_ref(branch) # Only checked, since it goes into the JSON as it is
    return request('POST', url, write_headers(engine, repo), { 'ref' : 'refs/heads/%s' % (branch), 'sha' : sha })

def fast_forward(repo, engine, branch, sha):
    # Fails with Github's 422 unless sha is a descendant of the branch
    url = path_join(api_base(repo), 'git', 'refs', 'heads', quote_ref(branch))
    return request('PATCH', url, write_headers(engine, repo), { 'sha' : sha, 'force' : False })

def merge(repo, engine, branch, sha, message):
    # 201 with the merge commit, or 204 when the branch already contains sha
    url = path_join(api_base(repo), 'merges')
    return request('POST', url, write_headers(engine, repo),
                   { 'base' : branch, 'head' : sha, 'commit_message' : message }, expected=(201, 204))

def open_pull_request(repo, engine, head, base, title, body):

    # The URL of the new pull request, or of the open one that already exists for head
    url = path_join(api_base(repo), 'pulls')

    try:
        data = request('POST', url, write_headers(engine, repo), { 'title' : title, 'head' : head, 'base' : base, 'body' : body })
        return data['html_url']

    except GithubError as error:
        if 'already exists' not in str(error) and 'answered 422' not in str(error):
            raise
        failure = error

    owner = normalize_repo(repo).split('/')[3]
    query = urllib.parse.urlencode({ 'state' : 'open', 'head' : '%s:%s' % (owner, quote_ref(head) and head) })
    found = request('GET', url + '?' + query, write_headers(engine, repo))
    if isinstance(found, list) and found:
        return found[0]['html_url']
    raise failure

def read_file(repo, engine, path, ref):

    # (text, blob sha) of a file at a branch or commit
    url  = path_join(api_base(repo), 'contents', quote_ref(path)) + '?ref=%s' % (urllib.parse.quote(quote_ref(ref) and ref, safe=''))
    data = request('GET', url, read_headers(engine))

    if data.get('type') != 'file' or data.get('encoding') != 'base64':
        raise GithubError('%s is not a file' % (path))

    return base64.b64decode(data['content']).decode('utf-8'), data['sha']

def write_file(repo, engine, path, branch, text, blob_sha, message):

    # The sha of the new commit on branch
    url  = path_join(api_base(repo), 'contents', quote_ref(path))
    quote_ref(branch)
    data = request('PUT', url, write_headers(engine, repo), {
        'message' : message, 'branch' : branch, 'sha' : blob_sha,
        'content' : base64.b64encode(text.encode('utf-8')).decode('ascii'),
    })
    return data['commit']['sha']
