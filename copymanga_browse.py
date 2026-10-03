"""CopyManga metadata browsing and safe subscription creation."""

import json
import ast
import os
import re
import time
from threading import Lock, RLock
from urllib.parse import urlparse

from fastapi import HTTPException
from bs4 import BeautifulSoup

from plugins.copymanga.headers import HEADERS
from plugins.copymanga.login import loginhelper
from utils import config
from utils import copymanga_web
from utils.request import RequestHandler


_write_lock = RLock()
_token_lock = Lock()
_cached_login = (None, None, 0)
_word = re.compile(r"^[A-Za-z0-9_-]{1,160}$")


def _identifier(value):
    if not isinstance(value, str) or not _word.fullmatch(value):
        raise HTTPException(400, "漫画或分组标识无效")
    return value


def _handler(auth=True):
    global _cached_login
    headers = HEADERS.copy()
    headers.pop('authorization', None)
    token = config.CM_TOKEN if auth else ''
    if auth and config.CM_USERNAME and config.CM_PASSWORD:
        credentials = (config.CM_API_URL, config.CM_USERNAME, config.CM_PASSWORD,
                       tuple(sorted(config.CM_PROXY.items())))
        with _token_lock:
            if _cached_login[0] != credentials or time.monotonic() >= _cached_login[2]:
                token = loginhelper(username=config.CM_USERNAME, password=config.CM_PASSWORD,
                                    url=config.CM_API_URL)
                _cached_login = (credentials, token, time.monotonic() + (1800 if token else 60))
            token = _cached_login[1]
    if token:
        headers['authorization'] = token if token.startswith(('Token ', 'Bearer ')) else f'Token {token}'
    return RequestHandler(headers=headers, proxy=config.CM_PROXY, copymanga=True)


def _response(path, params=None, auth=True):
    handler = _handler(auth=auth)
    try:
        response = handler.get(path, params=params)
    finally:
        handler.client.close()
    if response is None:
        status = handler.last_status_code
        if status == 210:
            message = handler.last_message
            if not isinstance(message, str) or not message:
                message = '站点未提供具体原因，请查看日志。'
            # Do not expose configured credentials even if upstream reflects them.
            auth_header = handler.headers.get('authorization', '')
            auth_token = auth_header.split(' ', 1)[-1] if isinstance(auth_header, str) else ''
            for secret in (config.CM_PASSWORD, config.CM_TOKEN, config.CM_PROXY.get('http'), auth_token):
                if secret:
                    message = message.replace(secret, '[已隐藏]')
            raise HTTPException(503, f"CopyManga 返回 210：{message}")
        if status == 429:
            raise HTTPException(503, "CopyManga 返回 429：请求过于频繁，重试后仍未恢复。请稍后再试。")
        if status in (401, 403):
            raise HTTPException(502, "CopyManga 拒绝访问，请检查账号或 API 地址。")
        if handler.last_error_kind == 'network':
            raise HTTPException(502, "无法连接 CopyManga API，请在系统参数配置中检查 API 地址和代理。")
        if handler.last_error_kind == 'proxy_auth':
            raise HTTPException(502, "代理返回 407：请在 HTTP 代理地址中填写正确的代理用户名和密码。")
        if isinstance(status, int):
            raise HTTPException(502, f"CopyManga API 返回 HTTP {status}，请稍后重试或检查 API 地址。")
        raise HTTPException(502, "CopyManga 请求失败，请检查 API 地址、账号和代理。")
    return response


def _fetch(path, params=None, auth=True):
    response = _response(path, params, auth)
    try:
        payload = response.json()
        if not isinstance(payload, dict) or payload.get('code') not in (None, 200):
            raise ValueError('CopyManga 返回错误')
        results = payload['results']
        if not isinstance(results, dict):
            raise ValueError('CopyManga 数据格式错误')
        return results
    except (ValueError, KeyError, TypeError):
        raise HTTPException(502, "CopyManga 返回了无法解析的数据")


def _names(items):
    if not isinstance(items, list):
        return []
    return [item['name'] for item in items if isinstance(item, dict) and item.get('name')]


def _comic(item):
    if not isinstance(item, dict):
        return None
    comic = item.get('comic', item)
    if not isinstance(comic, dict) or not comic.get('path_word'):
        return None
    status = comic.get('status')
    last = comic.get('last_chapter')
    return {
        'path_word': comic['path_word'], 'name': comic.get('name') or comic['path_word'],
        'cover': comic.get('cover') or '', 'brief': comic.get('brief') or '',
        'authors': _names(comic.get('author')), 'themes': _names(comic.get('theme')),
        'status': status.get('display', '') if isinstance(status, dict) else
                  {0: '连载中', 1: '已完结', 2: '短篇'}.get(status, status or ''),
        'last_chapter': last.get('name', '') if isinstance(last, dict) else '',
    }


def _groups(results):
    comic = results.get('comic')
    groups = results.get('groups') or (comic.get('groups') if isinstance(comic, dict) else None)
    normalized = []
    if isinstance(groups, dict):
        entries = groups.items()
    elif isinstance(groups, list):
        entries = [(g.get('path_word'), g) for g in groups if isinstance(g, dict)]
    else:
        entries = []
    for key, group in entries:
        if not isinstance(group, dict):
            continue
        word = group.get('path_word') or key
        if isinstance(word, str) and _word.fullmatch(word):
            normalized.append({'path_word': word, 'name': group.get('name') or word})
    return normalized or [{'path_word': 'default', 'name': '默认'}]


def _web_page(path, params=None):
    base = config.CM_WEB_URL.rstrip('/')
    parsed = urlparse(base)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise HTTPException(400, 'CopyManga 网页地址无效，请检查系统设置。')
    response = _response(base + path, params, auth=False)
    return BeautifulSoup(response.text, 'html.parser')


def filters():
    results = _fetch('/api/v3/theme/comic/count', {'limit': 500}, auth=False)
    items = results.get('list')
    if not isinstance(items, list):
        raise HTTPException(502, 'CopyManga 分类数据格式错误')
    return {'themes': [{'title': item['name'], 'value': item['path_word']}
                       for item in items if isinstance(item, dict)
                       and item.get('name') and item.get('path_word')]}


def browse(query='', rank='day', offset=0, limit=20, mode='rank',
           theme='', ordering='-datetime_updated', region='', status='', q_type=''):
    if mode not in ('all', 'rank') or ordering not in (
            '-datetime_updated', 'datetime_updated', '-popular', 'popular'):
        raise HTTPException(400, '浏览模式或排序无效')
    if region not in ('', '0', '1', '2') or status not in ('', '0', '1', '2'):
        raise HTTPException(400, '地区或状态无效')
    if q_type not in ('', 'name', 'author', 'local') or (theme and not _word.fullmatch(theme)):
        raise HTTPException(400, '搜索范围或分类无效')
    if query.strip():
        if theme or region or status:
            raise HTTPException(400, '名称搜索不支持分类、地区或状态筛选，请清空这些条件后搜索。')
        results = _fetch('/api/kb/web/searchcl/comics', {
            'q': query.strip(), 'q_type': q_type, 'limit': limit,
            'offset': offset, 'platform': 2}, auth=False)
    elif mode == 'all':
        params = {'limit': limit, 'offset': offset, 'ordering': ordering}
        params.update({k: v for k, v in {'theme': theme, 'region': region, 'status': status}.items() if v != ''})
        soup = _web_page('/comics', params)
        box = soup.select_one('.exemptComic-box[list][total]')
        if box is None:
            raise HTTPException(502, 'CopyManga 网页列表结构已变化或受到限制，无法解析。')
        try:
            results = {'list': ast.literal_eval(box['list']), 'total': int(box['total'])}
        except (ValueError, SyntaxError, TypeError, RecursionError):
            raise HTTPException(502, 'CopyManga 网页列表数据格式错误')
    else:
        if theme or region or status:
            raise HTTPException(400, '排行榜不支持分类筛选，请切换全部漫画。')
        if rank not in ('day', 'week', 'month', 'total'):
            raise HTTPException(400, "排行榜类型无效")
        results = _fetch('/api/v3/ranks', {'date_type': rank, 'type': 1,
                                        'limit': limit, 'offset': offset}, auth=False)
    items = results.get('list')
    if not isinstance(items, list):
        raise HTTPException(502, "CopyManga 列表格式错误")
    comics = [_comic(item) for item in items]
    if any(comic is None for comic in comics):
        raise HTTPException(502, 'CopyManga 列表缺少漫画标识，无法解析；请重试。')
    total = results.get('total')
    total = total if isinstance(total, int) and not isinstance(total, bool) and total >= 0 else None
    return {'items': comics, 'total': total, 'offset': offset, 'limit': limit,
            'has_more': offset + len(items) < total if total is not None else len(items) == limit}


def detail(path_word):
    path_word = _identifier(path_word)
    try:
        return copymanga_web.catalog(path_word)['comic']
    except copymanga_web.DirectoryError as error:
        raise HTTPException(502, str(error)) from None


def chapters(path_word, group_word, offset=0, limit=100):
    path_word, group_word = _identifier(path_word), _identifier(group_word)
    if offset < 0 or limit < 1 or limit > 500:
        raise HTTPException(400, '章节分页参数无效')
    try:
        items = copymanga_web.group_chapters(path_word, group_word)
    except copymanga_web.DirectoryError as error:
        raise HTTPException(502, str(error)) from None
    return {'items': items[offset:offset + limit], 'total': len(items),
            'offset': offset, 'limit': limit}


def _all_chapters(path_word, group_word):
    collected = []
    for offset in range(0, 10000, 500):
        page = chapters(path_word, group_word, offset, 500)
        collected.extend(page['items'])
        total = page['total']
        if (not page['items'] or len(page['items']) < 500
                or (isinstance(total, int) and offset + len(page['items']) >= total)):
            break
    else:
        raise HTTPException(502, "章节数量超出支持范围")
    if not collected:
        raise HTTPException(400, "章节目录为空，无法选择下载范围")
    if any(c['index'] is None for c in collected):
        raise HTTPException(502, "章节顺序数据缺失")
    collected.sort(key=lambda c: c['index'])
    return collected


def subscribe(data, path, replace=False):
    path_word = _identifier(data.path_word)
    group_word = _identifier(data.group_word)
    comic = detail(path_word)
    if group_word not in {g['path_word'] for g in comic['groups']}:
        raise HTTPException(400, "所选章节分组不存在")
    chapter_list = _all_chapters(path_word, group_word)
    names = [c['name'] for c in chapter_list]
    if len(set(names)) != len(names):
        raise HTTPException(409, "该分组存在同名章节，当前下载记录无法可靠区分，暂不能从页面添加")
    if data.mode == 'all':
        cursor = ''
        first = chapter_list[0]['name']
    elif data.mode == 'future':
        cursor = chapter_list[-1]['name']
        first = None
    elif data.mode == 'from':
        position = next((i for i, c in enumerate(chapter_list) if c['uuid'] == data.chapter_uuid), None)
        if position is None:
            raise HTTPException(400, "所选起始章节不存在")
        cursor = chapter_list[position - 1]['name'] if position else ''
        first = chapter_list[position]['name']
    else:
        raise HTTPException(400, "下载范围无效")

    name = (data.name or comic['name']).strip()
    if (not name or name in ('.', '..') or len(name) > 160
            or any(ord(c) < 32 or c in '/\\:*?"<>|' for c in name)):
        raise HTTPException(400, "保存名称无效")
    record = {'name': name, 'path_word': path_word, 'group_word': group_word,
              'latest_chapter': cursor, 'last_download_date': '',
              'ep_pattern': '', 'vol_pattern': '', 'cover': comic.get('cover', ''), 'download_mode': data.mode}
    with _write_lock:
        try:
            with open(path, encoding='utf-8') as stream:
                current = json.load(stream)
        except FileNotFoundError:
            current = {}
        except (OSError, ValueError):
            raise HTTPException(500, "无法读取现有订阅配置")
        if not isinstance(current, dict) or not isinstance(current.get('copymanga', []), list):
            raise HTTPException(500, "现有订阅配置格式错误")
        records = current.setdefault('copymanga', [])
        existing = next((r for r in records if r.get('path_word') == path_word and r.get('group_word', 'default') == group_word), None)
        if replace and existing is None:
            raise HTTPException(404, '订阅不存在')
        if not replace and any(r.get('path_word') == path_word and r.get('group_word', 'default') == group_word
               for r in records if isinstance(r, dict)):
            raise HTTPException(409, "该漫画分组已经订阅")
        if replace:
            existing.update({k: v for k, v in record.items() if k not in ('last_download_date', 'ep_pattern', 'vol_pattern')})
            record = existing
        else:
            records.append(record)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = path + '.browse.tmp'
        try:
            with open(temporary, 'w', encoding='utf-8') as stream:
                json.dump(current, stream, indent=2, ensure_ascii=False)
            os.replace(temporary, path)
        except OSError:
            raise HTTPException(500, "保存订阅失败")
    return {'record': record, 'first_download_chapter': first}
