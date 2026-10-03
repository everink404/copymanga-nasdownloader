import json
import importlib
import unittest
from unittest.mock import patch

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

import copymanga_browse as browse
from utils import copymanga_web as web
from utils.request import RequestHandler
from updater.copymanga import CopyMangaUpdater


PAGE = '''<div class="comicParticulars-title-right"><h6 title="Comic">Comic</h6>
<a href="/author/a/comics">Author</a><li>狀態：連載中</li></div>
<div class="comicParticulars-left-img"><img data-src="/cover.jpg"></div>
<p class="intro">Description</p><a href="/comic/comic/chapter/last">第3話</a>
<span id="dnt" value="9"></span><script>var ccz = '0123456789abcdef';</script>'''


def payload(count=3):
    return {'build': {'path_word': 'comic', 'type': [{'id': 1, 'name': '話'}]},
            'groups': {'default': {'path_word': 'default', 'name': '默认', 'count': count,
                       'chapters': [{'id': f'id-{i}', 'name': f'chapter-{i}', 'type': 1}
                                    for i in range(count)]}}}


def encrypted(data):
    iv = 'abcdefghijklmnop'
    padder = PKCS7(128).padder()
    raw = json.dumps(data, ensure_ascii=False).encode()
    raw = padder.update(raw) + padder.finalize()
    cipher = Cipher(algorithms.AES(b'0123456789abcdef'), modes.CBC(iv.encode())).encryptor()
    return iv + (cipher.update(raw) + cipher.finalize()).hex()


class WebsiteTests(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.object(web, '_cache', {}),
                        patch.object(web.config, 'CM_WEB_URL', 'https://example.test'),
                        patch.object(web.config, 'CM_PROXY', {}),
                        patch.object(web.config, 'CM_RATE_LIMIT_PER_MINUTE', 0)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def handler(self, data, page=PAGE):
        calls = []
        def respond(request):
            calls.append(request)
            self.assertNotIn('authorization', request.headers)
            if request.url.path == '/comic/comic':
                return httpx.Response(200, text=page, headers={'set-cookie': 'session=public; Path=/'})
            self.assertEqual(request.url.path, '/comicdetail/comic/chapters')
            self.assertEqual(request.headers.get('dnts'), '9')
            self.assertEqual(request.headers.get('referer'), 'https://example.test/comic/comic')
            self.assertIn('session=public', request.headers.get('cookie', ''))
            return httpx.Response(200, json={'code': 200, 'results': encrypted(data)})
        def factory(**kwargs):
            handler = RequestHandler(**kwargs)
            handler.client.close()
            handler.client = httpx.Client(transport=httpx.MockTransport(respond))
            return handler
        return patch.object(web, 'RequestHandler', side_effect=factory), calls

    def test_cookie_session_anonymous_directory_and_local_pagination(self):
        factory, calls = self.handler(payload(501))
        with factory, patch.object(browse, 'loginhelper') as login, \
             patch.object(browse, '_fetch', side_effect=AssertionError('APP API must not be called')):
            comic = browse.detail('comic')
            self.assertFalse(comic['metadata_only'])
            self.assertEqual(comic['brief'], 'Description')
            self.assertEqual(comic['cover'], 'https://example.test/cover.jpg')
            page = browse.chapters('comic', 'default', 500, 100)
            self.assertEqual(page['total'], 501)
            self.assertEqual(page['items'][0]['uuid'], 'id-500')
            self.assertEqual(len(browse._all_chapters('comic', 'default')), 501)
            login.assert_not_called()
            self.assertEqual(len(calls), 2)

    def test_empty_directory_keeps_metadata_but_does_not_cache_or_create_subscription(self):
        factory, calls = self.handler(payload(0))
        with factory:
            comic = browse.detail('comic')
            self.assertTrue(comic['metadata_only'])
            self.assertIn('空目录', comic['warning'])
            self.assertEqual(web._cache, {})
            with self.assertRaises(web.DirectoryError):
                CopyMangaUpdater().get_chapters({'path_word': 'comic', 'group_word': 'default'})
            self.assertEqual(len(calls), 8)

    def test_empty_directory_recovers_once_with_a_new_session(self):
        factories = [self.handler(payload(0))[0], self.handler(payload(3))[0]]
        created = []
        # Capture each mock factory independently, then replay the two sessions.
        for factory in factories:
            with factory:
                created.append(web.RequestHandler(headers={}, proxy={}, copymanga=True))
        with patch.object(web, 'RequestHandler', side_effect=created):
            result = web.catalog('comic')
        self.assertNotIn('directory_error', result)
        self.assertEqual(len(result['directories']['default']), 3)
        self.assertTrue(web._cache)
        self.assertTrue(all(handler.client.is_closed for handler in created))

    def test_public_chapter_content_and_rejects_incomplete_pages(self):
        pages = [{'url': 'https://cdn.example/one.jpg'}, {'url': 'https://cdn.example/two.jpg'}]
        for count, values, success in [(2, pages, True), (3, pages, False), (0, [], False),
                                       (1, [{'url': 'file:///private'}], False)]:
            calls = []
            html = (f'<span class="comicCount">{count}</span><script>'
                    f"var cct = '0123456789abcdef'; var contentKey = '{encrypted(values)}';</script>")
            def respond(request):
                calls.append(request)
                self.assertNotIn('authorization', request.headers)
                if request.url.path == '/comic/comic':
                    return httpx.Response(200, text=PAGE, headers={'set-cookie': 'session=public; Path=/'})
                self.assertEqual(request.url.path, '/comic/comic/chapter/id-1')
                self.assertEqual(request.headers['referer'], 'https://example.test/comic/comic')
                self.assertIn('session=public', request.headers['cookie'])
                return httpx.Response(200, text=html)
            def factory(**kwargs):
                handler = RequestHandler(**kwargs)
                handler.client.close()
                handler.client = httpx.Client(transport=httpx.MockTransport(respond))
                return handler
            with patch.object(web, 'RequestHandler', side_effect=factory):
                if success:
                    result = web.chapter_contents('comic', 'id-1')
                    self.assertEqual(result, {'contents': pages, 'words': [1, 2], '_source': 'website'})
                else:
                    with self.assertRaises(web.DirectoryError):
                        web.chapter_contents('comic', 'id-1')
            self.assertEqual(len(calls), 2)

    def test_empty_directory_retry_network_error_preserves_metadata(self):
        factory, _ = self.handler(payload(0))
        with factory:
            empty = web._catalog_once('comic')
        with patch.object(web, '_catalog_once', side_effect=[empty, web.DirectoryError('无法连接')]):
            result = web.catalog('comic')
        self.assertEqual(result['comic']['brief'], 'Description')
        self.assertIn('无法连接', result['comic']['warning'])
        self.assertFalse(web._cache)

    def test_public_chapter_rejects_redirect_to_another_chapter(self):
        def respond(request):
            if request.url.path.endswith('id-1'):
                return httpx.Response(302, headers={'location': '/comic/comic/chapter/other'})
            return httpx.Response(200, text=PAGE)
        def factory(**kwargs):
            handler = RequestHandler(**kwargs)
            handler.client.close()
            handler.client = httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True)
            return handler
        with patch.object(web, 'RequestHandler', side_effect=factory):
            with self.assertRaises(web.DirectoryError):
                web.chapter_contents('comic', 'id-1')

    def test_rejects_truncated_wrong_comic_duplicate_ids_and_bad_cipher(self):
        for data in [dict(payload(), build={'path_word': 'other'}),
                     {'build': {'path_word': 'comic'}, 'groups': {'default': {'chapters': [], 'count': 5}}}]:
            with self.assertRaises(web.DirectoryError):
                web.normalize_directory(data, 'comic')
        data = payload()
        data['groups']['default']['chapters'][1]['id'] = 'id-0'
        with self.assertRaises(web.DirectoryError):
            web.normalize_directory(data, 'comic')
        with self.assertRaises(web.DirectoryError):
            web.decrypt_directory('not encrypted', 'short')

    def test_proxy_changes_invalidate_cache_and_updates_force_refresh(self):
        factory, calls = self.handler(payload())
        with factory:
            browse.detail('comic')
            browse.detail('comic')
            self.assertEqual(len(calls), 2)
            with patch.object(web.config, 'CM_PROXY', {'http': 'http://proxy.test:7890'}):
                browse.detail('comic')
                self.assertEqual(len(calls), 4)
            chapters = CopyMangaUpdater().get_chapters({'path_word': 'comic', 'group_word': 'default'})
            self.assertEqual(len(calls), 6)
            self.assertEqual(CopyMangaUpdater().find_subsequent_uuids(chapters, 'chapter-1'), [('id-2', 'chapter-2')])
            with self.assertRaises(web.DirectoryError):
                CopyMangaUpdater().find_subsequent_uuids(chapters, 'removed chapter')

    def test_multiple_groups_preserve_website_order_and_types(self):
        data = payload()
        data['groups']['special'] = {'name': '番外组', 'count': 1,
                                    'chapters': [{'id': 'special-1', 'name': '番外', 'type': 3}]}
        groups, directories = web.normalize_directory(data, 'comic')
        self.assertEqual([g['path_word'] for g in groups], ['default', 'special'])
        self.assertEqual([c['index'] for c in directories['default']], [0, 1, 2])
        self.assertEqual(directories['special'][0]['type'], 3)

    def test_failed_update_reports_error_and_preserves_records(self):
        updater = importlib.import_module('updater.updater')
        record = {'path_word': 'comic', 'group_word': 'default', 'name': 'Comic',
                  'latest_chapter': 'chapter-1', 'last_download_date': 'unchanged',
                  'ep_pattern': '', 'vol_pattern': ''}
        saved = dict(record)
        with patch.object(updater, 'load_updater_json', return_value={'copymanga': [record]}), \
             patch.object(web, 'group_chapters', side_effect=web.DirectoryError('目录请求失败')), \
             patch.object(updater.notifier, 'add_error') as error, \
             patch.object(updater, 'update_chapter_record') as write:
            self.assertEqual(updater.process_updates(), [])
            error.assert_called_once()
            write.assert_not_called()
            self.assertEqual(record, saved)


if __name__ == '__main__':
    unittest.main()
