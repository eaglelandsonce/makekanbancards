import copy
import unittest
from unittest.mock import patch
import yaml
import sync_cards as s

class Tests(unittest.TestCase):
    def setUp(self):
        self.c = yaml.safe_load(s.Path('tasks.yml').read_text())
    def test_valid(self):
        self.assertEqual(len(s.validate(self.c)), 3)
    def test_duplicate_id(self):
        self.c['tasks'][1]['id'] = self.c['tasks'][0]['id']
        with self.assertRaises(ValueError): s.validate(self.c)
    def test_cycle(self):
        self.c['tasks'][0]['depends_on'] = ['csv-export']
        with self.assertRaises(ValueError): s.validate(self.c)
    def test_unknown_dependency(self):
        self.c['tasks'][0]['depends_on'] = ['missing']
        with self.assertRaises(ValueError): s.validate(self.c)
    def test_preserve_notes_and_resolve_links(self):
        t = self.c['tasks'][1]
        old = s.body(self.c, t, {}) + '\nHuman note'
        new = s.body(self.c, t, {'registration-form': 'https://github.com/a/b/issues/1'})
        result = s.merge(old, new)
        self.assertTrue(result.endswith('Human note'))
        self.assertIn('https://github.com/a/b/issues/1', result)
        self.assertEqual(result, s.merge(result, new))
    def test_dry_run_has_no_api_calls(self):
        with patch.object(s, 'request') as api, patch.object(s, 'gql') as gql:
            s.run(self.c, None, dry=True)
            api.assert_not_called()
            gql.assert_not_called()
    def test_sync_twice_creates_only_three_and_preserves_status(self):
        issues, items = [], {}
        project = {'id':'P','fields':{'nodes':[{'name':'Status','id':'F','options':[{'name':'Todo','id':'O'}]}], 'pageInfo':{'hasNextPage':False}}}
        def api(path, token, method='GET', payload=None):
            if method == 'GET': return copy.deepcopy(issues)
            if method == 'POST':
                n = len(issues) + 1
                issue = dict(payload, number=n, node_id='I'+str(n), html_url=f'https://github.com/a/b/issues/{n}')
                issues.append(issue)
                return copy.deepcopy(issue)
            if method == 'PATCH':
                issues[int(path.split('/')[-1])-1].update(payload)
                return {}
            raise AssertionError(method)
        def gql(query, variables):
            if 'projectV2(number' in query: return {'user':{'projectV2':project}}
            if 'items(first' in query:
                return {'node':{'items':{'nodes':copy.deepcopy(list(items.values())), 'pageInfo':{'hasNextPage':False}}}}
            if 'addProjectV2ItemById' in query:
                item = {'id':'ITEM'+variables['c'],'content':{'id':variables['c']}}
                items[variables['c']] = item
                return {'addProjectV2ItemById':{'item':copy.deepcopy(item)}}
            if 'updateProjectV2ItemFieldValue' in query:
                for item in items.values():
                    if item['id'] == variables['i']: item['fieldValueByName'] = {'name':'Todo'}
                return {}
            raise AssertionError(query)
        with patch.dict(s.os.environ, {'ISSUE_TOKEN':'fake','PROJECT_TOKEN':'fake'}), patch.object(s,'request',side_effect=api), patch.object(s,'gql',side_effect=gql):
            s.run(self.c,'a/b')
            items['I1']['fieldValueByName'] = {'name':'In Progress'}
            self.c['tasks'][0]['title'] = 'Updated title'
            s.run(self.c,'a/b')
        self.assertEqual(len(issues),3)
        self.assertEqual(len(items),3)
        self.assertEqual(issues[0]['title'],'Updated title')
        self.assertEqual(items['I1']['fieldValueByName']['name'],'In Progress')

if __name__ == '__main__': unittest.main()
