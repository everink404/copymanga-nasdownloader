import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from bs4 import BeautifulSoup

from fastapi import HTTPException

import copymanga_browse as browse


class SubscriptionInput:
    def __init__(self, mode, chapter_uuid='', group_word='default'):
        self.path_word = 'comic'
        self.group_word = group_word
        self.name = ''
        self.mode = mode
        self.chapter_uuid = chapter_uuid


class BrowseTests(unittest.TestCase):
    def test_rank_and_search_normalization(self):
        with patch.object(browse, '_fetch', return_value={'list': [
            {'comic': {'path_word': 'first', 'name': 'First', 'author': [{'name': 'A'}]}},
            {'path_word': 'second', 'name': 'Second', 'cover': 'https://example/cover.jpg'},
        ], 'total': 8}) as fetch:
            result = browse.browse('key', offset=20)
            self.assertEqual([item['path_word'] for item in result['items']], ['first', 'second'])
            self.assertEqual(result['items'][0]['authors'], ['A'])
            self.assertEqual(result['total'], 8)
            self.assertEqual(fetch.call_args.args[0], '/api/kb/web/searchcl/comics')
            self.assertEqual(fetch.call_args.args[1]['q'], 'key')
            self.assertEqual(fetch.call_args.args[1]['platform'], 2)
            result = browse.browse(rank='week')
            self.assertEqual(result['total'], 8)
            self.assertEqual(fetch.call_args.args[0], '/api/v3/ranks')
            self.assertEqual(fetch.call_args.args[1]['limit'], 20)
            result = browse.browse(rank='week', offset=20)
            self.assertEqual(fetch.call_args.args[1]['offset'], 20)
            self.assertFalse(result['has_more'])

    def test_missing_total_is_unknown_and_invalid_items_are_not_empty_results(self):
        with patch.object(browse, '_fetch', return_value={'list': [{'path_word': 'one'}]}):
            result = browse.browse('x', limit=1)
            self.assertIsNone(result['total'])
            self.assertTrue(result['has_more'])
        with patch.object(browse, '_fetch', return_value={'list': [{'name': 'lost'}]}):
            with self.assertRaises(HTTPException):
                browse.browse('x')

    def test_all_browse_uses_server_filters_and_pagination(self):
        soup = BeautifulSoup('''<div class="exemptComic-box" total="21"
          list="[{'path_word': 'last', 'name': 'Last', 'status': 1}]"></div>''', 'html.parser')
        with patch.object(browse, '_web_page', return_value=soup) as page:
            result = browse.browse(mode='all', offset=20, theme='aiqing', region='0', status='1')
            self.assertEqual(result['total'], 21)
            self.assertEqual(result['items'][0]['status'], '已完结')
            self.assertFalse(result['has_more'])
            self.assertEqual(page.call_args.args[1]['offset'], 20)
            self.assertEqual(page.call_args.args[1]['theme'], 'aiqing')
            self.assertEqual(page.call_args.args[1]['region'], '0')
        with patch.object(browse, '_web_page', return_value=BeautifulSoup('<html>Blocked</html>', 'html.parser')):
            with self.assertRaises(HTTPException):
                browse.browse(mode='all')

    def test_search_rejects_unsupported_filter_combination(self):
        with self.assertRaises(HTTPException) as error:
            browse.browse('x', theme='aiqing')
        self.assertEqual(error.exception.status_code, 400)

    def test_210_falls_back_to_public_metadata_but_cannot_subscribe(self):
        page = BeautifulSoup('''<div class="comicParticulars-title-right"><h6 title="Public">Public</h6>
          <a href="/author/a/comics">Author</a><li>狀態：連載中</li></div>
          <div class="comicParticulars-left-img"><img data-src="cover.jpg"></div>
          <p class="intro">Description</p>''', 'html.parser')
        with patch.object(browse, '_fetch', side_effect=HTTPException(503, 'CopyManga 返回 210：站点说明')), \
             patch.object(browse, '_web_page', return_value=page):
            result = browse.detail('comic')
            self.assertEqual(result['name'], 'Public')
            self.assertEqual(result['authors'], ['Author'])
            self.assertTrue(result['metadata_only'])
            self.assertEqual(result['groups'], [])
            with tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory, 'updater.json'))
                with self.assertRaises(HTTPException):
                    browse.subscribe(SubscriptionInput('all'), path)
                self.assertFalse(Path(path).exists())

    def test_detail_groups_and_chapter_pagination(self):
        with patch.object(browse, '_fetch', side_effect=[
            {'comic': {'path_word': 'comic', 'name': 'Comic'},
             'groups': {'default': {'name': '默认'}, 'special': {'path_word': 'special', 'name': '番外'}}},
            {'list': [{'uuid': 'one', 'name': '1', 'index': 1}], 'total': 501},
        ]) as fetch:
            self.assertEqual([g['path_word'] for g in browse.detail('comic')['groups']], ['default', 'special'])
            self.assertEqual(browse.chapters('comic', 'special', 500, 100)['total'], 501)
            self.assertEqual(fetch.call_args.args[1]['offset'], 500)

    def test_subscription_modes_duplicate_and_preserved_sites(self):
        comic = {'name': 'Comic', 'groups': [{'path_word': 'default'}, {'path_word': 'special'}]}
        chapters = [
            {'uuid': 'a', 'name': 'one', 'index': 1},
            {'uuid': 'b', 'name': 'two', 'index': 2},
            {'uuid': 'c', 'name': 'three', 'index': 3},
        ]
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(browse, 'detail', return_value=comic), \
             patch.object(browse, '_all_chapters', return_value=chapters):
            path = Path(directory, 'updater.json')
            path.write_text(json.dumps({'terra_historicus': [{'name': 'existing'}]}), encoding='utf-8')
            result = browse.subscribe(SubscriptionInput('from', 'b'), str(path))
            self.assertEqual(result['record']['latest_chapter'], 'one')
            self.assertEqual(result['first_download_chapter'], 'two')
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['terra_historicus'], [{'name': 'existing'}])
            with self.assertRaises(HTTPException) as duplicate:
                browse.subscribe(SubscriptionInput('all'), str(path))
            self.assertEqual(duplicate.exception.status_code, 409)
            result = browse.subscribe(SubscriptionInput('future', group_word='special'), str(path))
            self.assertEqual(result['record']['latest_chapter'], 'three')
            self.assertEqual(len(json.loads(path.read_text(encoding='utf-8'))['copymanga']), 2)

    def test_duplicate_names_cannot_create_ambiguous_cursor(self):
        with patch.object(browse, 'detail', return_value={
            'name': 'Comic', 'groups': [{'path_word': 'default'}]}), \
             patch.object(browse, '_all_chapters', return_value=[
                 {'uuid': 'a', 'name': 'same', 'index': 1},
                 {'uuid': 'b', 'name': 'same', 'index': 2}]):
            with self.assertRaises(HTTPException) as error:
                browse.subscribe(SubscriptionInput('from', 'b'), 'unused.json')
            self.assertEqual(error.exception.status_code, 409)

    def test_failed_api_is_error_not_empty_chapters(self):
        with patch.object(browse, '_handler') as handler:
            handler.return_value.get.return_value = None
            with self.assertRaises(HTTPException) as error:
                browse.chapters('comic', 'default')
            self.assertEqual(error.exception.status_code, 502)

    def test_browser_explains_site_limit_and_connection_failure(self):
        with patch.object(browse, '_handler') as handler:
            handler.return_value.get.return_value = None
            handler.return_value.last_status_code = 210
            handler.return_value.last_message = '请更新正版 APP 并等待1小时'
            with self.assertRaises(HTTPException) as blocked:
                browse.browse()
            self.assertEqual(blocked.exception.status_code, 503)
            self.assertIn('210', blocked.exception.detail)
            self.assertIn('等待1小时', blocked.exception.detail)

            handler.return_value.last_status_code = None
            handler.return_value.last_error_kind = 'network'
            with self.assertRaises(HTTPException) as disconnected:
                browse.browse()
            self.assertIn('API 地址和代理', disconnected.exception.detail)
            handler.return_value.last_error_kind = 'proxy_auth'
            with self.assertRaises(HTTPException) as proxy:
                browse.browse()
            self.assertIn('407', proxy.exception.detail)

    def test_proxy_change_invalidates_login_cache_and_uses_token_auth(self):
        with patch.object(browse.config, 'CM_USERNAME', 'user'), \
             patch.object(browse.config, 'CM_PASSWORD', 'secret'), \
             patch.object(browse.config, 'CM_TOKEN', ''), \
             patch.object(browse.config, 'CM_PROXY', {'http': 'http://first:7890'}), \
             patch.object(browse, '_cached_login', (None, None, 0)), \
             patch.object(browse, 'loginhelper', return_value='test-token') as login, \
             patch.object(browse, 'RequestHandler') as handler:
            browse._handler()
            self.assertEqual(handler.call_args.kwargs['headers']['authorization'], 'Token test-token')
            browse._handler()
            self.assertEqual(login.call_count, 1)
            browse.config.CM_PROXY = {'http': 'http://second:7890'}
            browse._handler()
            self.assertEqual(login.call_count, 2)
            browse._handler(auth=False)
            self.assertNotIn('authorization', handler.call_args.kwargs['headers'])


if __name__ == '__main__':
    unittest.main()
