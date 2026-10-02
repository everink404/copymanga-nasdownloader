"""Offline regression tests: python -m unittest discover -s tests -v.

HTTP, CBZ packaging and record writes are mocked; no account or network needed.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

import httpx

from utils import config
from utils import request as request_module
from plugins.copymanga import login
from updater.copymanga import CopyMangaUpdater
import updater.copymanga as chapter_updater


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Isolate optional CBZ and other-site dependencies; exercise the real downloader.
cbz_modules = {name: ModuleType(name) for name in
               ('cbz', 'cbz.comic', 'cbz.constants', 'cbz.page')}
cbz_modules['cbz.comic'].ComicInfo = Mock()
cbz_modules['cbz.page'].PageInfo = Mock()
for name in ('PageType', 'YesNo', 'Manga', 'AgeRating', 'Format'):
    setattr(cbz_modules['cbz.constants'], name, Mock())
with patch.dict(sys.modules, cbz_modules):
    images = load_module('tested_downloader', 'downloader.py')
record_writer = ModuleType('updater.updater')
record_writer.update_chapter_record = Mock()
with patch.dict(sys.modules, {'downloader': images, 'updater.updater': record_writer}):
    plugin = load_module('tested_copymanga', 'plugins/copymanga/main.py')


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.patches = [
            patch.object(config, 'CM_RATE_LIMIT_PER_MINUTE', 12),
            patch.object(request_module, 'cm_rate_limiter', request_module.CopyMangaRateLimiter()),
            patch.object(request_module.time, 'monotonic', self.clock.monotonic),
            patch.object(request_module.time, 'sleep', self.clock.sleep),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def handler(self, statuses, copymanga=True):
        handler = request_module.RequestHandler(copymanga=copymanga)
        handler.client.close()
        handler.client = Mock()
        handler.client.request.side_effect = statuses
        return handler

    def test_shared_limit_including_retries_and_other_sites(self):
        first = self.handler([httpx.Response(200)])
        second = self.handler([httpx.Response(503), httpx.Response(200)])
        image = self.handler([httpx.Response(200)], copymanga=False)
        first.get('/api/one')
        image.get('https://cdn.example/page.jpg')
        self.assertEqual(self.clock.now, 0)
        second.get('/api/two')
        self.assertEqual(self.clock.now, 10)
        self.assertEqual(second.client.request.call_count, 2)

    def test_disabled_limit(self):
        with patch.object(config, 'CM_RATE_LIMIT_PER_MINUTE', 0):
            handler = self.handler([httpx.Response(200), httpx.Response(200)])
            handler.get('/one')
            handler.get('/two')
        self.assertEqual(self.clock.sleeps, [])

    def test_proxy_authentication_failure_is_identified(self):
        handler = self.handler([httpx.ProxyError('407 Proxy Authentication Required')] * 3)
        self.assertIsNone(handler.get('/rank'))
        self.assertEqual(handler.last_error_kind, 'proxy_auth')

    def test_210_json_and_non_json_do_not_exit(self):
        for response in (httpx.Response(210, json={'message': 'blocked'}),
                         httpx.Response(210, text='<html>blocked</html>')):
            handler = self.handler([response, httpx.Response(200)])
            self.assertIsNone(handler.get('/blocked'))
            self.assertEqual(handler.get('/next').status_code, 200)

    def test_429_retry_after_and_default(self):
        for headers, expected in (({'Retry-After': '7'}, 7), ({}, 60),
                                  ({'Retry-After': 'invalid'}, 60)):
            self.clock.sleeps.clear()
            handler = self.handler([httpx.Response(429, headers=headers), httpx.Response(200)], False)
            self.assertEqual(handler.get('/retry').status_code, 200)
            self.assertEqual(self.clock.sleeps, [expected])

    def test_retry_after_http_date(self):
        from datetime import datetime, timezone
        with patch.object(request_module, 'datetime') as date:
            date.now.return_value = datetime(2026, 9, 29, 0, 0, tzinfo=timezone.utc)
            self.assertEqual(request_module.retry_after_seconds('Tue, 29 Sep 2026 00:00:20 GMT'), 20)
            self.assertEqual(request_module.retry_after_seconds('Mon, 28 Sep 2026 00:00:00 GMT'), 0)

    def test_429_exhaustion_and_network_failure(self):
        handler = self.handler([httpx.Response(429)] * 3, False)
        self.assertIsNone(handler.get('/blocked'))
        self.assertEqual(self.clock.sleeps, [60, 60])
        handler = self.handler([httpx.ConnectError('offline')] * 3, False)
        self.assertIsNone(handler.get('/offline'))
        self.assertEqual(handler.client.request.call_count, 3)

    def test_login_uses_shared_handler_and_closes_client(self):
        with patch.object(login, 'RequestHandler') as factory:
            handler = factory.return_value
            handler.post.return_value = None
            self.assertIsNone(login.login(username='user', password='encoded', salt=1,
                                          url='https://api.example', proxy={'https': 'http://proxy'}))
            self.assertTrue(factory.call_args.kwargs['copymanga'])
            handler.client.close.assert_called_once()

    def test_config_default_disabled_invalid(self):
        for raw, expected in (('', 12), ('0', 0), ('24', 24), ('-1', 12), ('bad', 12)):
            with patch.dict(os.environ, {'CMNAS_CM_RATE_LIMIT_PER_MINUTE': raw}):
                config.reload()
                self.assertEqual(config.CM_RATE_LIMIT_PER_MINUTE, expected)


class ChapterTests(unittest.TestCase):
    def setUp(self):
        self.task = dict(name='series', path_word='series', site='copymanga',
                         ep_pattern='', vol_pattern='', chapter_infos=[('a', 'one'), ('b', 'two')])

    def test_completion_updates_only_selected_group(self):
        import importlib
        real_updater = importlib.import_module('updater.updater')
        with tempfile.TemporaryDirectory() as directory, patch.object(config, 'DATA_PATH', directory):
            records = {'copymanga': [
                dict(name='Comic', path_word='comic', group_word=group, latest_chapter='',
                     last_download_date='', ep_pattern='', vol_pattern='')
                for group in ('default', 'special')]}
            Path(directory, 'updater.json').write_text(json.dumps(records), encoding='utf-8')
            self.assertTrue(real_updater.update_chapter_record('copymanga', 'comic', 'chapter', group_word='special'))
            saved = json.loads(Path(directory, 'updater.json').read_text(encoding='utf-8'))['copymanga']
            self.assertEqual(saved[0]['latest_chapter'], '')
            self.assertEqual(saved[1]['latest_chapter'], 'chapter')

    def test_missing_and_malformed_responses(self):
        for response in (None, httpx.Response(200, text='bad json'),
                         httpx.Response(200, json={'results': None})):
            with patch.object(plugin.request, 'get', return_value=response):
                self.assertIsNone(plugin.get_chapter('series', 'id'))
            with patch.object(chapter_updater.copymanga_web, 'group_chapters',
                              side_effect=chapter_updater.copymanga_web.DirectoryError('目录请求失败')):
                with self.assertRaises(chapter_updater.copymanga_web.DirectoryError):
                    CopyMangaUpdater().get_chapters(dict(path_word='series', group_word='default'))

    def test_updater_uses_shared_complete_directory(self):
        chapters = [{'uuid': str(i), 'name': str(i), 'index': i} for i in range(501)]
        record = dict(path_word='series', group_word='default')
        with patch.object(chapter_updater.copymanga_web, 'group_chapters', return_value=chapters) as get:
            self.assertEqual(len(CopyMangaUpdater().get_chapters(record)), 501)
            get.assert_called_once_with('series', 'default', refresh=True)

    def test_failed_image_preserves_directory_and_resumes(self):
        chapter = dict(contents=[{'url': 'https://cdn.example/1'}, {'url': 'https://cdn.example/2'}], words=[1, 2])
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(config, 'DOWNLOAD_PATH', directory), \
             patch.object(config, 'USE_CM_CNAME', True), \
             patch.object(plugin, 'get_chapter', return_value=chapter), \
             patch.object(images.request, 'get', side_effect=[httpx.Response(200, content=b'page1'), None,
                                                             httpx.Response(200, content=b'page2')]) as get, \
             patch.object(plugin, 'postprocess') as pack, \
             patch.object(plugin.updater, 'update_chapter_record') as record, \
             patch.object(plugin.notifier, 'add_success') as success:
            self.assertFalse(plugin.download_chapter(self.task, 'a', 'one'))
            pack.assert_not_called()
            record.assert_not_called()
            success.assert_not_called()
            save_path = Path(directory, 'series', 'one')
            self.assertEqual((save_path / '0001.jpg').read_bytes(), b'page1')
            self.assertFalse((save_path / '0002.jpg').exists())
            self.assertTrue(plugin.download_chapter(self.task, 'a', 'one'))
            self.assertEqual(get.call_count, 3)  # Existing page1 is reused.
            self.assertEqual((save_path / '0002.jpg').read_bytes(), b'page2')
            pack.assert_called_once()
            record.assert_called_once_with('copymanga', 'series', 'one', group_word='default')
            success.assert_called_once()

    def test_failed_or_exceptional_chapter_stops_cursor_advancing(self):
        for result in (False, RuntimeError('failure')):
            with patch.object(plugin, 'download_chapter', side_effect=[result, True]) as download, \
                 patch.object(plugin.time, 'sleep'):
                plugin.download_task(self.task)
                self.assertEqual(download.call_count, 1)

    def test_other_comics_continue_after_failed_chapter(self):
        with patch.object(plugin, 'download_chapter', side_effect=[False, True]) as download, \
             patch.object(plugin.time, 'sleep'):
            other = dict(self.task, name='other', chapter_infos=[('c', 'three')], current_chapter='')
            plugin.download_batch([dict(self.task, current_chapter=''), other])
            self.assertEqual(download.call_count, 2)
            self.assertEqual(download.call_args.args[0]['name'], 'other')

    def test_download_login_is_deferred_and_uses_current_proxy(self):
        with patch.object(config, 'CM_USERNAME', 'user'), \
             patch.object(config, 'CM_PASSWORD', 'password'), \
             patch.object(config, 'CM_PROXY', {'http': 'http://new-proxy:7890'}), \
             patch.object(plugin, 'loginhelper', return_value='download-token') as login, \
             patch.object(plugin, 'RequestHandler') as handler, \
             patch.object(plugin, 'download_task') as download:
            previous = plugin.request
            plugin.download_batch([dict(self.task, current_chapter='')])
            self.assertEqual(handler.call_args.kwargs['headers']['authorization'], 'Token download-token')
            self.assertEqual(handler.call_args.kwargs['proxy'], {'http': 'http://new-proxy:7890'})
            handler.return_value.client.close.assert_called_once()
            self.assertIs(plugin.request, previous)
            download.assert_called_once()
            login.assert_called_once()

    def test_download_login_failure_does_not_advance_records(self):
        with patch.object(config, 'CM_USERNAME', 'user'), \
             patch.object(config, 'CM_PASSWORD', 'password'), \
             patch.object(plugin, 'loginhelper', return_value=None), \
             patch.object(plugin, 'download_task') as download, \
             patch.object(plugin.updater, 'update_chapter_record') as write:
            plugin.download_batch([dict(self.task, current_chapter='')])
            download.assert_not_called()
            write.assert_not_called()

    def test_invalid_image_list_is_not_packaged(self):
        for chapter in (dict(contents=[], words=[]), dict(contents=[{'url': 'x'}], words=[])):
            with patch.object(plugin, 'get_chapter', return_value=chapter), \
                 patch.object(plugin, 'postprocess') as pack:
                self.assertFalse(plugin.download_chapter(self.task, 'a', 'one'))
                pack.assert_not_called()

    def test_interrupted_write_and_zero_byte_file_are_redownloaded(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(images.request, 'get', return_value=httpx.Response(200, content=b'complete')) as get:
            filename = str(Path(directory, 'page.jpg'))
            Path(filename).touch()
            with patch.object(images.os, 'replace', side_effect=OSError('interrupted')):
                self.assertFalse(images.downloader('https://cdn.example/1', filename))
            self.assertEqual(Path(filename).stat().st_size, 0)
            self.assertTrue(images.downloader('https://cdn.example/1', filename))
            self.assertEqual(Path(filename).read_bytes(), b'complete')
            self.assertFalse(Path(filename + '.part').exists())
            self.assertEqual(get.call_count, 2)


if __name__ == '__main__':
    unittest.main()
