import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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
            self.assertEqual(fetch.call_args.args[0], '/api/v3/search/comic')
            result = browse.browse(rank='week')
            self.assertEqual(result['total'], 2)
            self.assertEqual(fetch.call_args.args[0], '/api/v3/ranks')

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


if __name__ == '__main__':
    unittest.main()
