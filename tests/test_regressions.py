import copy
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
import local_state
import settings_server as api
import todo_store


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.config_path = os.path.join(self.temp.name, 'config.json')
        self.inbox = os.path.join(self.temp.name, 'todo_inbox.json')
        self.manager = config.ConfigManager(self.config_path)
        todo = self.manager.data['widgets']['todo_list']
        todo['tasks'] = []
        todo['notebooks'] = [{'id': 'nb_general', 'name': 'General', 'color': '#3b82f6', 'tasks': []}]
        todo['active_notebook_id'] = 'nb_general'
        self.manager.save()

    def test_unreadable_config_is_never_replaced(self):
        Path(self.config_path).write_text('{broken', encoding='utf8')
        with self.assertRaises(ValueError):
            config.ConfigManager(self.config_path)
        self.manager.data['general']['position'] = 'top'
        with self.assertRaises(ValueError):
            self.manager.save()
        self.assertEqual(Path(self.config_path).read_text(), '{broken')

    def test_two_editors_preserve_changes_and_unknown_settings(self):
        second = config.ConfigManager(self.config_path)
        self.manager.set_widget('clock', 'x', 10)
        second.set_widget('clock', 'y', 20)
        self.manager.data['custom_feature'] = {'value': 42}
        self.manager.save()
        data = self.manager.load()
        self.assertEqual((data['widgets']['clock']['x'], data['widgets']['clock']['y']), (10, 20))
        self.assertEqual(data['custom_feature'], {'value': 42})

    def test_reload_preserves_pending_local_changes(self):
        self.manager.data['appearance']['bg_color'] = '#123456'
        other = config.ConfigManager(self.config_path)
        other.set_widget('clock', 'x', 123)
        self.manager.load()
        self.manager.save()
        data = self.manager.load()
        self.assertEqual(data['appearance']['bg_color'], '#123456')
        self.assertEqual(data['widgets']['clock']['x'], 123)

    def test_concurrent_reload_and_writes_preserve_all_keys(self):
        def write(index):
            self.manager.set('test_fields', str(index), index)
            self.manager.load()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(write, range(20)))
        self.assertEqual(self.manager.load()['test_fields'], {str(i): i for i in range(20)})

    def test_modal_save_keeps_new_tasks_and_external_edits(self):
        first = todo_store.add_task('Original', config_path=self.config_path)['task']
        data = self.manager.load()
        before = copy.deepcopy(data['widgets']['todo_list']['notebooks'])
        local = copy.deepcopy(before)
        for notebook in local:
            for task in notebook['tasks']:
                if task['id'] == first['id']:
                    task['text'] = 'Edited locally'
        todo_store.add_task('New from Outlook', config_path=self.config_path)
        todo_store.set_task_done(first['id'], config_path=self.config_path)
        todo_store.commit_notebooks(local, before, config_path=self.config_path)
        tasks = todo_store.list_tasks(config_path=self.config_path)['tasks']
        self.assertEqual(len(tasks), 2)
        edited = next(t for t in tasks if t['id'] == first['id'])
        self.assertTrue(edited['done'])
        self.assertEqual(edited['text'], 'Edited locally')

    def test_inbox_arrival_during_import_survives(self):
        Path(self.inbox).write_text(json.dumps({'items': [{'text': 'First', 'id': '1'}]}), encoding='utf8')
        add = todo_store.add_task

        def concurrent_arrival(**kwargs):
            Path(self.inbox).write_text(json.dumps({'items': [{'text': 'Second', 'id': '2'}]}), encoding='utf8')
            return add(**kwargs)

        with patch.object(todo_store, 'add_task', side_effect=concurrent_arrival):
            self.assertEqual(todo_store.ingest_todo_inbox(self.inbox, config_path=self.config_path)['added'], 1)
        self.assertEqual(todo_store.ingest_todo_inbox(self.inbox, config_path=self.config_path)['added'], 1)
        self.assertEqual(todo_store.list_tasks(config_path=self.config_path)['count'], 2)

    def test_incomplete_inbox_remains_untouched(self):
        raw = '{"text":"good"}\n{"text":'
        Path(self.inbox).write_text(raw, encoding='utf8')
        with self.assertRaises(ValueError):
            todo_store.ingest_todo_inbox(self.inbox, config_path=self.config_path)
        self.assertEqual(Path(self.inbox).read_text(), raw)

    def test_bulk_import_saves_once(self):
        items = [{'text': f'Task {index}', 'id': str(index)} for index in range(30)]
        Path(self.inbox).write_text(json.dumps({'items': items}), encoding='utf8')
        with patch.object(todo_store, '_save_raw_config', wraps=todo_store._save_raw_config) as save:
            result = todo_store.ingest_todo_inbox(self.inbox, config_path=self.config_path)
        self.assertEqual(result['added'], 30)
        self.assertEqual(save.call_count, 1)

    def test_lock_timeout_stops_writer(self):
        with patch.object(local_state._kernel, 'WaitForSingleObject', return_value=258):
            with self.assertRaises(TimeoutError):
                with local_state.InterProcessLock():
                    self.fail('Entered without owning the lock')

    def test_lock_excludes_another_process(self):
        source = "from local_state import InterProcessLock\ntry:\n with InterProcessLock(timeout=100): pass\nexcept TimeoutError:\n raise SystemExit(7)"
        with local_state.InterProcessLock():
            result = subprocess.run([sys.executable, '-B', '-c', source], cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 7, result.stderr.decode(errors='replace'))

    def test_cli_and_mcp_read_the_same_store(self):
        todo_store.add_task('Synthetic integration task', config_path=self.config_path)
        setup = (
            "import functools,runpy,sys,todo_store; "
            f"todo_store.list_tasks=functools.partial(todo_store.list_tasks,config_path={self.config_path!r}); "
        )
        cwd = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, '-B', '-c', setup + "sys.argv=['todo_cli.py','list','--json']; runpy.run_module('todo_cli',run_name='__main__')"],
            cwd=cwd, capture_output=True, text=True, encoding='utf8', timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['tasks'][0]['text'], 'Synthetic integration task')
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'todo_list_tasks', 'arguments': {}}}
        result = subprocess.run(
            [sys.executable, '-B', '-c', setup + "runpy.run_module('todo_mcp_server',run_name='__main__')"],
            cwd=cwd, input=json.dumps(request) + '\n', capture_output=True, text=True, encoding='utf8', timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout)
        payload = json.loads(response['result']['content'][0]['text'])
        self.assertEqual(payload['tasks'][0]['text'], 'Synthetic integration task')


class ApiTests(unittest.TestCase):
    def setUp(self):
        StateTests.setUp(self)
        api._config_mgr_ref = self.manager
        self.server = api.ReusableThreadingHTTPServer(('127.0.0.1', 0), api.SettingsApiHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.shutdown)

    def shutdown(self):
        api.close_settings_ui(reason='test_cleanup')
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, method='GET', data=None, authenticated=False, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        self.addCleanup(conn.close)
        extra = {'X-Alioth-Token': api._api_token} if authenticated else {}
        extra.update(headers or {})
        conn.request(method, path, body=json.dumps(data) if data is not None else None, headers=extra)
        response = conn.getresponse()
        body = response.read()
        return response, body

    def test_private_api_requires_session(self):
        response, _ = self.request('/api/config')
        self.assertEqual(response.status, 401)
        response, _ = self.request('/api/config', authenticated=True)
        self.assertEqual(response.status, 200)
        self.assertIsNone(response.getheader('Access-Control-Allow-Origin'))

    def test_second_server_cannot_share_port(self):
        with self.assertRaises(OSError):
            api.ReusableThreadingHTTPServer(('127.0.0.1', self.server.server_port), api.SettingsApiHandler)

    def test_cookie_bootstrap_and_foreign_origin(self):
        response, _ = self.request('/api/session', 'POST', authenticated=True)
        cookie = response.getheader('Set-Cookie')
        self.assertIn('HttpOnly; SameSite=Strict', cookie)
        response, _ = self.request('/api/config', headers={'Cookie': cookie.split(';')[0]})
        self.assertEqual(response.status, 200)
        response, _ = self.request('/api/config', authenticated=True, headers={'Origin': 'https://untrusted.invalid'})
        self.assertEqual(response.status, 403)
        response, _ = self.request('/api/config', authenticated=True, headers={'Host': 'untrusted.invalid'})
        self.assertEqual(response.status, 403)

    def test_only_public_assets_are_served(self):
        for target in ('/config.json', '/dock.log', '/main.py', '/assets/../config.json', '/assets/../../ALIOTH%20DOCKBAR-other/private.png'):
            response, _ = self.request(target)
            self.assertEqual(response.status, 403, target)
        response, _ = self.request('/assets/logo.png')
        self.assertEqual(response.status, 200)

    def test_settings_round_trip_and_bad_payload(self):
        response, _ = self.request('/api/config', 'POST', {'general': {'position': 'top'}}, True)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.manager.load()['general']['position'], 'top')
        response, _ = self.request('/api/config', 'POST', [], True)
        self.assertEqual(response.status, 400)

    def test_panel_stays_connected_until_explicit_close(self):
        api._active_panel_token = 'test-panel'
        api._server_shutting_down = False
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        self.addCleanup(conn.close)
        conn.request('GET', '/api/panel_events?token=test-panel', headers={'X-Alioth-Token': api._api_token})
        response = conn.getresponse()
        self.assertEqual(response.readline(), b': connected\n')
        api.close_settings_ui(reason='test_explicit_close')
        self.assertIn(b'event: close', response.read())


if __name__ == '__main__':
    unittest.main(verbosity=2)
