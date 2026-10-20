"""One-way YAML to GitHub issue/project sync. No automatic mutation retries."""
import json
import os
import re
import sys
import urllib.request
from pathlib import Path
import yaml

START = '<!-- kanban-content:start -->'
END = '<!-- kanban-content:end -->'

def request(path, token, method='GET', payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request('https://api.github.com/' + path, data=data,
        method=method, headers={'Authorization': 'Bearer ' + token,
        'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json',
        'User-Agent': 'yaml-kanban-test'})
    with urllib.request.urlopen(req, timeout=45) as result:
        return json.load(result)

def gql(query, variables):
    result = request('graphql', os.environ['PROJECT_TOKEN'], 'POST',
                     {'query': query, 'variables': variables})
    if result.get('errors'):
        raise RuntimeError(json.dumps(result['errors']))
    return result['data']

def validate(c):
    if not isinstance(c, dict):
        raise ValueError('Expected a YAML mapping')
    if not re.fullmatch(r'[a-z0-9-]+', str(c.get('namespace', ''))):
        raise ValueError('namespace must use lowercase letters, numbers, hyphens')
    if not re.fullmatch(r'https://github.com/(users|orgs)/[\w-]+/projects/\d+', str(c.get('project_url', ''))):
        raise ValueError('Use a valid project_url')
    if not isinstance(c.get('initial_status'), str) or not c['initial_status']:
        raise ValueError('Set initial_status')
    tasks = c.get('tasks')
    if not isinstance(tasks, list) or not tasks:
        raise ValueError('Supply at least one task')
    ids = []
    for t in tasks:
        if not isinstance(t, dict):
            raise ValueError('Each task must be a mapping')
        for k in ('id', 'title', 'description', 'priority', 'validation'):
            if not isinstance(t.get(k), str) or not t[k].strip():
                raise ValueError('Each task needs text in ' + k)
        if not re.fullmatch(r'[a-z0-9-]+', t['id']):
            raise ValueError('Invalid task id')
        ids.append(t['id'])
        ac = t.get('acceptance_criteria')
        if not isinstance(ac, list) or not ac or not all(isinstance(x, str) and x.strip() for x in ac):
            raise ValueError('Supply a list of acceptance criteria')
        if not isinstance(t.get('depends_on', []), list):
            raise ValueError('depends_on must be a list')
        if START in json.dumps(t) or END in json.dumps(t):
            raise ValueError('Reserved content marker in task')
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate task IDs')
    by_id = {t['id']: t for t in tasks}
    visited, active = set(), set()
    def visit(i):
        if i not in by_id: raise ValueError('Unknown dependency: ' + str(i))
        if i in active: raise ValueError('Dependency cycle')
        if i in visited: return
        active.add(i)
        for dep in by_id[i].get('depends_on', []): visit(dep)
        active.remove(i)
        visited.add(i)
    for i in ids: visit(i)
    return tasks

def marker(c, t):
    return '<!-- kanban-task:' + c['namespace'] + ':' + t['id'] + ' -->'

def body(c, t, links):
    deps = [links.get(d, d) for d in t.get('depends_on', [])]
    return '\n'.join([marker(c, t), START, '## Outcome', t['description'],
        '\n## Acceptance criteria', *['- [ ] ' + a for a in t['acceptance_criteria']],
        '\n## Validation', t['validation'], '\n## Dependencies',
        '\n'.join('- ' + d for d in deps) if deps else 'None',
        '\n## Priority', t['priority'], END])

def merge(old, new):
    # Keep human notes outside the generated block; do not overwrite malformed bodies.
    if old.count(START) != 1 or old.count(END) != 1 or old.index(START) > old.index(END):
        raise ValueError('Managed issue has missing/ambiguous content markers')
    block = new[new.index(START):new.index(END) + len(END)]
    return old[:old.index(START)] + block + old[old.index(END) + len(END):]

def run(c, repo, dry=False):
    tasks = validate(c)
    if dry:
        for t in tasks: print('\nTITLE:', t['title'], '\n' + body(c, t, {}))
        print('\nPreview only: no API calls or writes. Project access is not verified.')
        return
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', repo or ''):
        raise ValueError('Set REPOSITORY to owner/repo')
    if not os.environ.get('ISSUE_TOKEN') or not os.environ.get('PROJECT_TOKEN'):
        raise ValueError('Both ISSUE_TOKEN and PROJECT_TOKEN are required')
    kind, owner, number = re.fullmatch(r'https://github.com/(users|orgs)/([\w-]+)/projects/(\d+)', c['project_url']).groups()
    owner_type = 'user' if kind == 'users' else 'organization'
    query = ('query($owner:String!,$number:Int!){' + owner_type + '(login:$owner){projectV2(number:$number){id fields(first:100){nodes{... on ProjectV2SingleSelectField{id name options{id name}}} pageInfo{hasNextPage}}}}}')
    project = gql(query, {'owner': owner, 'number': int(number)})[owner_type]['projectV2']
    if not project: raise ValueError('Project not found or inaccessible')
    if project['fields']['pageInfo']['hasNextPage']: raise ValueError('Test supports at most 100 project fields')
    status = next((f for f in project['fields']['nodes'] if f.get('name') == 'Status'), None)
    option = next((o for o in (status or {}).get('options', []) if o['name'] == c['initial_status']), None)
    if not option: raise ValueError('Project Status has no option matching initial_status')
    items, cursor = {}, None
    while True:
        p = gql('query($id:ID!,$after:String){node(id:$id){... on ProjectV2{items(first:100,after:$after){nodes{id content{... on Issue{id}} fieldValueByName(name:"Status"){... on ProjectV2ItemFieldSingleSelectValue{name}}} pageInfo{hasNextPage endCursor}}}}}', {'id': project['id'], 'after': cursor})['node']['items']
        for item in p['nodes']:
            if (item.get('content') or {}).get('id'): items[item['content']['id']] = item
        if not p['pageInfo']['hasNextPage']: break
        cursor = p['pageInfo']['endCursor']
    token = os.environ['ISSUE_TOKEN']
    issues, page = [], 1
    while True:
        batch = request(f'repos/{repo}/issues?state=all&per_page=100&page={page}', token)
        issues.extend(x for x in batch if 'pull_request' not in x)
        if len(batch) < 100: break
        page += 1
    matches = {}
    for t in tasks:
        found = [i for i in issues if marker(c, t) in (i.get('body') or '')]
        if len(found) > 1: raise ValueError('Multiple issues share ID ' + t['id'])
        if found:
            merge(found[0]['body'], body(c, t, {}))  # preflight before mutations
            matches[t['id']] = found[0]
    for t in tasks:
        if t['id'] not in matches:
            i = request(f'repos/{repo}/issues', token, 'POST', {'title': t['title'], 'body': body(c, t, {})})
            matches[t['id']] = i
            print('CREATED', i['html_url'], flush=True)
    links = {k: i['html_url'] for k, i in matches.items()}
    for t in tasks:
        i = matches[t['id']]
        updated = merge(i['body'], body(c, t, links))
        if updated != i['body'] or i['title'] != t['title']:
            request(f"repos/{repo}/issues/{i['number']}", token, 'PATCH', {'title': t['title'], 'body': updated})
        item = items.get(i['node_id'])
        if not item:
            item = gql('mutation($p:ID!,$c:ID!){addProjectV2ItemById(input:{projectId:$p,contentId:$c}){item{id}}}', {'p': project['id'], 'c': i['node_id']})['addProjectV2ItemById']['item']
        # Fill blank statuses only. Preserve cards moved by the user.
        if not item.get('fieldValueByName'):
            gql('mutation($p:ID!,$i:ID!,$f:ID!,$o:String!){updateProjectV2ItemFieldValue(input:{projectId:$p,itemId:$i,fieldId:$f,value:{singleSelectOptionId:$o}}){projectV2Item{id}}}', {'p': project['id'], 'i': item['id'], 'f': status['id'], 'o': option['id']})
        print('SYNCED', i['html_url'], flush=True)
    print('Complete:', len(tasks), 'issues linked to', c['project_url'])

if __name__ == '__main__':
    try:
        run(yaml.safe_load(Path('tasks.yml').read_text()), os.getenv('REPOSITORY'),
            os.getenv('DRY_RUN', 'true').lower() == 'true')
    except Exception as error:
        print('STOPPED:', str(error), file=sys.stderr)
        print('Some writes may have succeeded. Keep IDs unchanged and rerun to resume.', file=sys.stderr)
        sys.exit(1)
