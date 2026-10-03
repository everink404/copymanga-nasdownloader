import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient
from utils import config


class TaskAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        fake_main = ModuleType('main')
        fake_main.main = Mock()
        spec = importlib.util.spec_from_file_location('tested_task_server', Path(__file__).resolve().parents[1] / 'server.py')
        cls.server = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'main': fake_main}), patch.object(config, 'DATA_PATH', cls.directory.name):
            spec.loader.exec_module(cls.server)
        cls.client = TestClient(cls.server.app)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        self.record = dict(name='comic', path_word='comic', group_word='default', latest_chapter='',
                           last_download_date='', ep_pattern='', vol_pattern='')
        Path(self.server.UPDATER_JSON_PATH).write_text(json.dumps({'copymanga': [self.record], 'other': [{'preserved': True}]}), encoding='utf-8')
        self.manager = Mock()
        self.manager.snapshot.return_value = []
        self.manager.lock = __import__('threading').RLock()
        self.manager.enqueue.side_effect = lambda record, *args: dict(record, id='queued')
        self.patch = patch.object(self.server, 'task_manager', self.manager)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_targeted_run_and_paused_subscription(self):
        self.assertEqual(self.client.post('/api/copymanga/subscriptions/comic/default/run').json()['id'], 'queued')
        response = self.client.patch('/api/copymanga/subscriptions/comic/default', json={'paused': True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.post('/api/copymanga/subscriptions/comic/default/run').status_code, 409)
        self.assertEqual(self.client.post('/api/run').json()['items'], [])
        self.assertEqual(self.server.get_config()['other'], [{'preserved': True}])

    def test_active_subscription_cannot_be_deleted_or_range_changed(self):
        self.manager.snapshot.return_value = [dict(id='a', path_word='comic', group_word='default', status='running')]
        self.assertEqual(self.client.delete('/api/copymanga/subscriptions/comic/default').status_code, 409)
        response = self.client.put('/api/copymanga/subscriptions/comic/default/range', json=dict(path_word='comic', group_word='default', mode='all'))
        self.assertEqual(response.status_code, 409)

    def test_delete_preserves_other_sources_and_files(self):
        saved_file = Path(self.directory.name) / 'saved.cbz'
        saved_file.write_bytes(b'unchanged')
        self.assertEqual(self.client.delete('/api/copymanga/subscriptions/comic/default').status_code, 200)
        self.assertEqual(self.server.get_config()['copymanga'], [])
        self.assertEqual(self.server.get_config()['other'], [{'preserved': True}])
        self.assertEqual(saved_file.read_bytes(), b'unchanged')

    def test_retry_validates_state_and_subscription(self):
        self.manager.snapshot.return_value = [dict(id='a', path_word='comic', group_word='default', status='completed')]
        self.assertEqual(self.client.post('/api/tasks/a/retry').status_code, 409)
        self.manager.snapshot.return_value[0]['status'] = 'failed'
        self.assertEqual(self.client.post('/api/tasks/a/retry').status_code, 200)
        self.assertEqual(self.client.post('/api/tasks/missing/retry').status_code, 404)

    def test_patch_rejects_progress_overwrite(self):
        self.assertEqual(self.client.patch('/api/copymanga/subscriptions/comic/default', json={'latest_chapter': 'fake'}).status_code, 400)
