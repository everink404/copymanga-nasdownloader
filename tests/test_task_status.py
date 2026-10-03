import json
import tempfile
import threading
import unittest
from pathlib import Path
from utils.task_status import TaskManager, progress, stopping


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = str(Path(self.directory.name) / 'tasks.json')
        self.record = dict(name='comic', path_word='comic', group_word='default')

    def tearDown(self):
        self.directory.cleanup()

    def test_serial_deduplication_stop_and_history(self):
        entered = threading.Event()
        release = threading.Event()
        called = []
        def runner(job):
            called.append(job['path_word'])
            if len(called) == 1:
                entered.set()
                self.assertTrue(release.wait(3))
                self.assertTrue(stopping())
            progress(chapters_total=2, chapters_done=1, images_total=3, images_done=3,
                     byte_delta=50, event='saved')
        manager = TaskManager(runner, self.path)
        first = manager.enqueue(self.record)
        self.assertTrue(entered.wait(3))
        self.assertEqual(first['id'], manager.enqueue(self.record)['id'])
        other = manager.enqueue(dict(self.record, path_word='second'))
        self.assertEqual(called, ['comic'])
        manager.stop(first['id'])
        worker = manager.worker
        release.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        jobs = manager.snapshot()
        self.assertEqual([j['status'] for j in jobs], ['stopped', 'completed'])
        self.assertEqual(jobs[0]['bytes_downloaded'], 50)
        self.assertEqual(jobs[0]['chapters_done'], 1)
        jobs[0]['status'] = 'tampered'
        self.assertEqual(manager.snapshot()[0]['status'], 'stopped')
        self.assertEqual(TaskManager(runner, self.path).snapshot()[0]['status'], 'stopped')

    def test_failure_does_not_become_completed(self):
        def runner(job):
            progress(images_total=3, images_done=2, error='image failed')
        manager = TaskManager(runner, self.path)
        with manager.lock:
            manager.enqueue(self.record)
            worker = manager.worker
        worker.join(3)
        self.assertEqual(manager.snapshot()[0]['status'], 'failed')
        self.assertEqual(manager.snapshot()[0]['chapters_done'], 0)

    def test_restart_interrupts_active_jobs_without_auto_download(self):
        with open(self.path, 'w', encoding='utf-8') as stream:
            json.dump([dict(id='a', status='running'), dict(id='b', status='waiting')], stream)
        manager = TaskManager(lambda _: self.fail('unexpected download'), self.path)
        self.assertEqual([j['status'] for j in manager.snapshot()], ['interrupted', 'interrupted'])
        self.assertIsNone(manager.worker)

    def test_queued_cancellation_does_not_execute(self):
        entered, release = threading.Event(), threading.Event()
        called = []
        def runner(job):
            called.append(job['path_word'])
            entered.set()
            release.wait(3)
        manager = TaskManager(runner, self.path)
        manager.enqueue(self.record)
        self.assertTrue(entered.wait(3))
        second = manager.enqueue(dict(self.record, path_word='second'))
        manager.stop(second['id'])
        worker = manager.worker
        release.set()
        worker.join(3)
        self.assertEqual(called, ['comic'])
        self.assertEqual(manager.snapshot()[1]['status'], 'stopped')
