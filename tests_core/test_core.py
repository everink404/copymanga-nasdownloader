import dataclasses
import io
import json
import sqlite3
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
import httpx
from fastapi.testclient import TestClient
from download_core.api import create_app
from download_core.engine import Engine
from download_core.files import safe_path, validate_image, verify_archive
from download_core.fixture import FixtureSource, Registry
from download_core.migration import Migration
from download_core.models import CoreError, Manifest, collect_catalog, key
from download_core.store import Store
from download_core.transport import Budget, Transport


class Crash(BaseException): pass


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = FixtureSource()
        self.open()

    def open(self, fault=None):
        self.store = Store(self.root / 'data/core-v1')
        self.engine = Engine(self.store, Registry([self.source]), self.root / 'downloads', self.root / 'cbz', fault)

    def tearDown(self):
        self.engine.close(); self.store.close(); self.temporary.cleanup()

    def subscribe(self, mode='all', anchor=None, **options):
        p = self.engine.preview('fixture_offline', 'demo', 'default', mode, anchor)
        return self.engine.subscribe(p['id'], options)

    def download(self, item):
        job = self.engine.enqueue(item['id'])
        self.engine.run_job(job['id'])
        return self.store.job(job['id'])

    def test_full_download_zip_and_idempotence(self):
        item = self.subscribe(); job = self.download(item)
        self.assertEqual((job['status'], job['chapters_done']), ('completed', 2))
        artifacts = self.store.all('artifact')
        self.assertEqual(len(artifacts), 2)
        for artifact in artifacts:
            path = safe_path(self.engine.cbz, artifact['relative_path'])
            self.assertTrue(verify_archive(path, artifact))
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(archive.namelist(), ['0001.png', '0002.png', '0003.png', 'ComicInfo.xml', 'download-manifest.json'])
                self.assertNotIn(b'https://', archive.read('download-manifest.json'))
        second = self.download(item)
        self.assertEqual(second['outcome'], 'no_updates')
        self.assertEqual(len(self.store.all('artifact')), 2)

    def test_partial_failure_preserves_pages_without_fact_or_archive(self):
        job = self.download(self.subscribe(fail_page=2))
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['chapters_done'], 0)
        self.assertEqual(len(list((self.root/'downloads').rglob('*.png'))), 1)
        self.assertEqual(list((self.root/'cbz').rglob('*.cbz')), [])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM downloads').fetchone()[0], 0)
        retry = self.engine.retry(job['id'])
        self.assertEqual(self.engine.retry(job['id'])['id'], retry['id'])
        self.engine.run_job(retry['id'])
        done = self.store.job(retry['id'])
        self.assertEqual((done['status'], done['chapters_done'], done['reused_images']), ('completed', 2, 1))

    def test_none_proof_does_not_reuse_pages(self):
        job = self.download(self.subscribe(fail_page=2, proof_kind='none'))
        retry = self.engine.retry(job['id']); self.engine.run_job(retry['id'])
        self.assertEqual(self.store.job(retry['id'])['reused_images'], 0)

    def test_html_is_rejected(self):
        job = self.download(self.subscribe(html_page=2))
        self.assertEqual((job['status'], job['error_code']), ('failed', 'INVALID_IMAGE'))
        self.assertFalse(self.store.all('artifact'))
        self.assertTrue(list((self.root/'downloads').rglob('*.png')))

    def test_future_baseline_is_not_completion(self):
        job = self.download(self.subscribe(mode='future'))
        self.assertEqual((job['outcome'], job['chapters_done']), ('no_updates', 0))
        self.assertFalse(self.store.all('artifact'))

    def test_from_anchor_inclusive(self):
        chapters, _ = self.engine.catalog('fixture_offline', 'demo', 'default')
        job = self.download(self.subscribe(mode='from', anchor=chapters[1].key))
        self.assertEqual(job['chapters_done'], 1)
        self.assertIsNone(self.store.completed(chapters[0].key))
        self.assertIsNotNone(self.store.completed(chapters[1].key))

    def test_prepared_and_published_restart_recover_without_network(self):
        for stage in ('prepared', 'published'):
            with self.subTest(stage=stage):
                self.engine.close(); self.store.close()
                # Separate store/output per crash checkpoint.
                self.root = Path(self.temporary.name) / stage
                self.open(fault=lambda event: (_ for _ in ()).throw(Crash()) if event == stage else None)
                item = self.subscribe(proof_kind='none'); job = self.engine.enqueue(item['id'])
                with self.assertRaises(Crash): self.engine.run_job(job['id'])
                self.engine.close(); self.store.close()
                old_session = self.source.open_session
                self.source.open_session = lambda *_: (_ for _ in ()).throw(AssertionError('recovery requested network'))
                try: self.open()
                finally: self.source.open_session = old_session
                artifacts = self.store.all('artifact')
                self.assertEqual(len(artifacts), 1)
                self.assertEqual(artifacts[0]['stage'], 'committed')
                self.assertEqual(self.store.job(job['id'])['chapters_done'], 1)
                self.assertEqual(self.store.job(job['id'])['status'], 'interrupted')

    def test_corrupt_archive_repair_new_path_no_duplicate_fact(self):
        item = self.subscribe(); self.download(item)
        original = self.store.all('artifact')[0]
        path = safe_path(self.engine.cbz, original['relative_path']); path.write_bytes(b'broken original')
        self.engine.recover()
        recovery = next(j for j in self.store.jobs() if j['status'] == 'recovery_required')
        self.assertEqual(self.engine.enqueue(item['id'])['id'], recovery['id'])
        repaired = self.engine.retry(recovery['id']); self.engine.run_job(repaired['id'])
        self.assertEqual(self.store.job(repaired['id'])['status'], 'completed')
        current = self.store.completed(original['chapter_key'])
        self.assertNotEqual(current['relative_path'], original['relative_path'])
        self.assertEqual(current['content_version'], original['content_version'])
        self.assertEqual(path.read_bytes(), b'broken original')
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM downloads WHERE state='complete'").fetchone()[0], 2)
        self.engine.recover()
        self.assertFalse(any(j['status'] == 'recovery_required' for j in self.store.jobs()))

    def test_published_conflict_never_overwrites(self):
        def conflict(stage):
            if stage == 'prepared':
                artifact = self.store.all('artifact')[-1]
                safe_path(self.engine.cbz, artifact['relative_path']).write_bytes(b'foreign')
        self.engine.fault = conflict
        job = self.download(self.subscribe())
        self.assertEqual(job['status'], 'recovery_required')
        artifact = self.store.all('artifact')[0]
        path = safe_path(self.engine.cbz, artifact['relative_path'])
        self.assertEqual(path.read_bytes(), b'foreign')
        with self.assertRaises(CoreError): self.engine.retry(job['id'])
        self.assertEqual(self.store.job(job['id'])['status'], 'recovery_required')

    def test_active_identity_deduplicates(self):
        item = self.subscribe(); first = self.engine.enqueue(item['id'])
        self.assertEqual(self.engine.enqueue(item['id'])['id'], first['id'])
        self.engine.stop(first['id'])
        self.assertEqual(self.store.job(first['id'])['status'], 'stopped')

    def test_second_writer_and_unknown_schema_fail_closed(self):
        with self.assertRaises(CoreError): Store(self.root/'data/core-v1')
        other = self.root/'unknown'; other.mkdir()
        db = sqlite3.connect(other/'state.sqlite3'); db.execute('PRAGMA user_version=99'); db.close()
        before = (other/'state.sqlite3').read_bytes()
        with self.assertRaises(CoreError): Store(other)
        self.assertEqual(before, (other/'state.sqlite3').read_bytes())

    def test_identity_group_and_path_protection(self):
        self.assertNotEqual(key('fixture_offline', 'demo', 'a', '1'), key('fixture_offline', 'demo', 'b', '1'))
        with self.assertRaises(CoreError): safe_path(self.root, '../outside')
        with self.assertRaises(CoreError): safe_path(self.root, str(self.root/'absolute'))
        with self.assertRaises(CoreError): validate_image(b'<html>no image</html>')


class NetworkTests(unittest.TestCase):
    def test_source_rate_limit_is_shared_between_sessions(self):
        calls=[]; scope=object()
        def respond(request): calls.append(request); return httpx.Response(200, content=b'ok')
        first=Transport(httpx.Client(transport=httpx.MockTransport(respond)), scope=scope)
        second=Transport(httpx.Client(transport=httpx.MockTransport(respond)), scope=scope)
        try:
            self.assertEqual(first.get('https://fixture.invalid/p', Budget(), role='api', allowed_hosts=('fixture.invalid',)), b'ok')
            with self.assertRaises(CoreError) as caught: second.get('https://fixture.invalid/p', Budget(), role='api', allowed_hosts=('fixture.invalid',))
            self.assertEqual(caught.exception.code, 'RATE_LIMIT')
            self.assertEqual(len(calls), 1)
        finally: first.close(); second.close()

    def test_retry_after_and_budget(self):
        calls = []
        def respond(request): calls.append(request); return httpx.Response(429, headers={'Retry-After': '15'})
        transport = Transport(httpx.Client(transport=httpx.MockTransport(respond)), api_rate=0, image_rate=0, scope=object())
        try:
            before = time.time()
            with self.assertRaises(CoreError) as caught:
                transport.get('https://fixture.invalid/page', Budget(), allowed_hosts=('fixture.invalid',))
            self.assertEqual(caught.exception.code, 'RATE_LIMIT')
            self.assertGreaterEqual(caught.exception.retry_at, before+15)
            with self.assertRaises(CoreError): transport.get('https://fixture.invalid/page', Budget(), allowed_hosts=('fixture.invalid',))
            self.assertEqual(len(calls), 1)
        finally: transport.close()

    def test_429_default_60_and_redirect_no_credentials(self):
        transport = Transport(httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(429))), api_rate=0, image_rate=0, scope=object())
        try:
            before = time.time()
            with self.assertRaises(CoreError) as caught: transport.get('https://fixture.invalid/page', Budget(), allowed_hosts=('fixture.invalid',))
            self.assertGreaterEqual(caught.exception.retry_at, before+60)
        finally: transport.close()
        transport = Transport(httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(302, headers={'location':'http://evil.invalid/p'}))), api_rate=0, image_rate=0)
        try:
            with self.assertRaises(CoreError) as caught: transport.get('https://fixture.invalid/p', Budget(), allowed_hosts=('fixture.invalid',))
            self.assertEqual(caught.exception.code, 'PROTOCOL_ERROR')
            with self.assertRaises(CoreError): transport.get('https://fixture.invalid/p', Budget(), allowed_hosts=('fixture.invalid',), headers={'Authorization':'private'})
            budget = Budget(used=6)
            with self.assertRaises(CoreError): budget.take()
        finally: transport.close()


class MigrationApiTests(unittest.TestCase):
    def test_legacy_private_byte_backup_unknown_source_no_false_facts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); data=root/'data'; data.mkdir()
            raw = b'{"unknown-source": [{"title":"old", "password":"private-test-value", "chapters":["done"]}]}'
            (data/'updater.json').write_bytes(raw)
            migration = Migration(data); preview=migration.preview()
            with self.assertRaises(CoreError): migration.apply(preview['fingerprint'], False)
            result=migration.apply(preview['fingerprint'], True)
            self.assertEqual((data/'legacy-snapshots'/result['snapshot_id']/'updater.json').read_bytes(), raw)
            self.assertEqual((data/'updater.json').read_bytes(), raw)
            store=Store(data/'core-v1')
            try:
                self.assertEqual(len(store.all('legacy_record')), 1)
                self.assertFalse(store.subscriptions())
                self.assertEqual(store.db.execute('SELECT count(*) FROM downloads').fetchone()[0], 0)
            finally: store.close()
            self.assertTrue(migration.apply(preview['fingerprint'], True)['already_applied'])

    def test_bad_or_changed_legacy_does_not_activate(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); file=root/'updater.json'; file.write_text('{bad')
            migration=Migration(root)
            with self.assertRaises(CoreError): migration.preview()
            file.write_text('{"old":[]}'); preview=migration.preview()
            file.write_text('{"new":[]}')
            with self.assertRaises(CoreError): migration.apply(preview['fingerprint'], True)
            self.assertFalse((root/'core-v1').exists())

    def test_http_download_status_and_legacy_readonly(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with TestClient(create_app(root/'data', root/'downloads', root/'cbz', fixture=True)) as client:
                self.assertEqual(client.get('/health').json()['mode'], 'core')
                self.assertIn('下载任务', client.get('/').text)
                preview=client.post('/api/v1/subscriptions/preview', json={}).json()
                sub=client.post('/api/v1/subscriptions', json={'preview_id':preview['id']}).json()
                job=client.post('/api/v1/subscriptions/'+sub['id']+'/run').json()
                deadline=time.time()+8
                while time.time()<deadline:
                    tasks=client.get('/api/v1/tasks').json()
                    if tasks[0]['status']=='completed': break
                    time.sleep(.03)
                self.assertEqual(tasks[0]['status'], 'completed')
                artifacts=client.get('/api/v1/artifacts').json()
                response=client.get('/api/v1/artifacts/'+artifacts[0]['id'])
                self.assertEqual(response.status_code, 200)
                with zipfile.ZipFile(io.BytesIO(response.content)) as archive: self.assertIsNone(archive.testzip())
                self.assertEqual(client.post('/api/system-config',json={}).status_code, 404)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/'data').mkdir(); (root/'data/updater.json').write_text('{"old":[]}')
            with TestClient(create_app(root/'data', root/'downloads', root/'cbz', fixture=True)) as client:
                self.assertEqual(client.get('/health').json()['mode'], 'legacy_detected')
                self.assertEqual(client.post('/api/v1/subscriptions/preview',json={}).status_code, 409)
                self.assertFalse((root/'data/core-v1').exists())


if __name__ == '__main__': unittest.main()
