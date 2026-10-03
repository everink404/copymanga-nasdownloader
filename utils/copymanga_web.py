"""Anonymous public metadata and directory, shared by browsing and updates."""
import copy
import json
import logging
import re
import time
from threading import Lock
from urllib.parse import urlparse, urljoin

from bs4 import BeautifulSoup
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from utils import config
from utils.request import RequestHandler, HEADERS


class DirectoryError(ValueError):
    pass


class EmptyDirectoryError(DirectoryError):
    pass


log = logging.getLogger(__name__)
_cache = {}
_lock = Lock()
_word = re.compile(r'^[A-Za-z0-9_-]{1,160}$')


def _get(handler, url, **kwargs):
    response = handler.get(url, **kwargs)
    if response is None:
        status = handler.last_status_code
        reason = f'HTTP {status}' if isinstance(status, int) else '无法连接'
        raise DirectoryError(f'CopyManga 官网请求失败（{reason}），请检查官网地址和代理。')
    return response


def decrypt_directory(value, key, context='目录'):
    try:
        if not isinstance(value, str) or len(value) <= 16:
            raise ValueError('missing ciphertext')
        decryptor = Cipher(algorithms.AES(key.encode('utf-8')),
                           modes.CBC(value[:16].encode('utf-8'))).decryptor()
        plain = decryptor.update(bytes.fromhex(value[16:])) + decryptor.finalize()
        unpadder = PKCS7(128).unpadder()
        return json.loads(unpadder.update(plain) + unpadder.finalize())
    except (ValueError, TypeError, AttributeError, UnicodeError):
        raise DirectoryError(f'官网{context}数据无法解析，网站格式可能已变化。') from None


def normalize_directory(payload, slug):
    if not isinstance(payload, dict) or not isinstance(payload.get('build'), dict):
        raise DirectoryError('官网目录格式错误。')
    if payload['build'].get('path_word') != slug:
        raise DirectoryError('官网目录与所选漫画不匹配。')
    raw_groups = payload.get('groups')
    if not isinstance(raw_groups, dict) or not raw_groups:
        raise DirectoryError('官网没有提供章节分组。')
    groups, directories = [], {}
    for word, group in raw_groups.items():
        if not isinstance(word, str) or not _word.fullmatch(word) or not isinstance(group, dict):
            raise DirectoryError('官网章节分组格式错误。')
        if group.get('path_word', word) != word or not isinstance(group.get('chapters'), list):
            raise DirectoryError('官网章节分组格式错误。')
        entries = group['chapters']
        count = group.get('count')
        if count is not None and (type(count) is not int or count != len(entries)):
            raise DirectoryError('官网目录数量不完整，暂不能检查订阅。')
        normalized, seen = [], set()
        for position, chapter in enumerate(entries):
            if not isinstance(chapter, dict):
                raise DirectoryError('官网章节数据不完整。')
            identifier, name = chapter.get('id'), chapter.get('name')
            if (not isinstance(identifier, str) or not _word.fullmatch(identifier)
                    or identifier in seen or not isinstance(name, str) or not name.strip()):
                raise DirectoryError('官网章节标识或名称无效。')
            seen.add(identifier)
            normalized.append({'uuid': identifier, 'name': name,
                               'index': position, 'type': chapter.get('type'),
                               'datetime_created': chapter.get('datetime_created')})
        # Preserve the website's display order; chapter types are not API groups.
        groups.append({'path_word': word, 'name': group.get('name') or word})
        directories[word] = normalized
    if not any(directories.values()):
        raise EmptyDirectoryError('官网返回了空目录，尚未确认完整章节，暂不能添加或检查订阅。')
    return groups, directories


def _catalog_once(slug, refresh=False):
    if not isinstance(slug, str) or not _word.fullmatch(slug):
        raise DirectoryError('漫画标识无效。')
    base = config.CM_WEB_URL.rstrip('/')
    parsed = urlparse(base)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise DirectoryError('CopyManga 官网地址无效。')
    key = (base, slug, tuple(sorted(config.CM_PROXY.items())))
    with _lock:
        cached = _cache.get(key)
        if not refresh and cached and time.monotonic() < cached[0]:
            return copy.deepcopy(cached[1])
    headers = {**HEADERS, 'Accept': 'text/html,application/json',
               'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.8'}
    handler = RequestHandler(headers=headers, proxy=config.CM_PROXY, copymanga=True)
    page_url = base + '/comic/' + slug
    try:
        response = _get(handler, page_url)
        # Honor a legitimate website redirect when constructing the same-origin directory URL.
        page_url = str(response.url)
        html = response.text
        soup = BeautifulSoup(html, 'html.parser')
        title = soup.select_one('.comicParticulars-title-right h6')
        if title is None or not title.get_text(strip=True):
            raise DirectoryError('官网未返回漫画资料，可能是错误页或网站格式已变化。')
        cover = soup.select_one('.comicParticulars-left-img img')
        intro = soup.select_one('p.intro')
        status = ''
        for row in soup.select('.comicParticulars-title-right li'):
            text = row.get_text(strip=True)
            if text.startswith(('狀態：', '状态：')):
                status = text.split('：', 1)[1]
        last = soup.select_one('a[href*="/chapter/"]')
        comic = {'path_word': slug, 'name': title.get_text(strip=True),
                 'cover': urljoin(page_url, (cover.get('data-src') or cover.get('src') or '')) if cover else '',
                 'brief': intro.get_text(strip=True) if intro else '', 'status': status,
                 'authors': [a.get_text(strip=True) for a in soup.select('.comicParticulars-title-right a[href^="/author/"]')],
                 'themes': [a.get_text(strip=True).lstrip('#') for a in soup.select('.comicParticulars-left-theme-all a')],
                 'last_chapter': last.get_text(strip=True) if last else '',
                 'groups': [], 'metadata_only': True, 'source': 'website'}
        result = {'comic': comic, 'directories': {}}
        try:
            match = re.search(r"\bvar\s+ccz\s*=\s*(['\"])(.*?)\1", html)
            dnt = soup.select_one('#dnt[value]')
            if match is None or dnt is None:
                raise DirectoryError('官网目录参数缺失，网站格式可能已变化。')
            directory_url = urljoin(page_url, '/comicdetail/' + slug + '/chapters')
            data = _get(handler, directory_url, headers={
                'Referer': page_url, 'dnts': dnt['value'],
                'Accept': 'application/json',
                'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8'})
            envelope = data.json()
            if not isinstance(envelope, dict) or envelope.get('code') != 200:
                raise DirectoryError('官网目录请求未成功。')
            payload = decrypt_directory(envelope.get('results'), match[2])
            raw_groups = payload.get('groups') if isinstance(payload, dict) else None
            counts = {word: len(group['chapters']) for word, group in raw_groups.items()
                      if isinstance(word, str) and _word.fullmatch(word)
                      and isinstance(group, dict) and isinstance(group.get('chapters'), list)} if isinstance(raw_groups, dict) else {}
            report = log.warning if not any(counts.values()) else log.info
            report('官网目录响应: 漫画=%s 主机=%s HTTP=%s 分组章节数=%s 配置代理=%s',
                   slug, urlparse(str(data.url)).hostname, data.status_code, counts, bool(config.CM_PROXY))
            groups, directories = normalize_directory(payload, slug)
            comic.update(groups=groups, metadata_only=False)
            result['directories'] = directories
        except (DirectoryError, ValueError) as error:
            comic['warning'] = '资料已获取；目录获取失败：' + str(error)
            result['directory_error'] = str(error)
            result['directory_empty'] = isinstance(error, EmptyDirectoryError)
    finally:
        handler.client.close()
    # Failed/empty results never replace successful cache entries.
    if not result.get('directory_error'):
        with _lock:
            now = time.monotonic()
            for stale in [k for k, v in _cache.items() if v[0] <= now]:
                del _cache[stale]
            if len(_cache) >= 64:
                _cache.pop(next(iter(_cache)))
            _cache[key] = (now + 30, copy.deepcopy(result))
    return result


def catalog(slug, refresh=False):
    result = _catalog_once(slug, refresh=refresh)
    if result.get('directory_empty'):
        log.warning('官网目录为空: %s; 重新获取网页与会话后重试一次, 不使用旧目录推进订阅。', slug)
        try:
            result = _catalog_once(slug, refresh=True)
        except DirectoryError as error:
            result['directory_error'] = str(error)
            result['comic']['warning'] = '资料已获取；目录重试失败：' + str(error)
    return result


def chapter_contents(slug, chapter_id):
    """Read the image list exposed by a public chapter page, without downloading images."""
    if not all(isinstance(word, str) and _word.fullmatch(word) for word in (slug, chapter_id)):
        raise DirectoryError('漫画或章节标识无效。')
    base = config.CM_WEB_URL.rstrip('/')
    if urlparse(base).scheme not in ('http', 'https') or not urlparse(base).netloc:
        raise DirectoryError('CopyManga 官网地址无效。')
    handler = RequestHandler(headers={**HEADERS, 'Accept': 'text/html'},
                             proxy=config.CM_PROXY, copymanga=True)
    try:
        page = _get(handler, base + '/comic/' + slug)
        page_url = str(page.url)
        chapter_url = urljoin(page_url, '/comic/' + slug + '/chapter/' + chapter_id)
        response = _get(handler, chapter_url, headers={'Referer': page_url})
        # Reject redirects to another chapter or a login/error page.
        if urlparse(str(response.url)).path.rstrip('/') != urlparse(chapter_url).path:
            raise DirectoryError('官网没有返回所选章节页。')
        html = response.text
        key = re.search(r"\bvar\s+cct\s*=\s*(['\"])(.*?)\1", html)
        content = re.search(r"\bvar\s+contentKey\s*=\s*(['\"])(.*?)\1", html)
        count_node = BeautifulSoup(html, 'html.parser').select_one('.comicCount')
        count = count_node.get_text(strip=True) if count_node else ''
        if key is None or content is None or not content[2]:
            raise DirectoryError('官网未提供章节图片列表, API 限制仍未恢复。')
        pages = decrypt_directory(content[2], key[2], context='章节图片')
        if not isinstance(pages, list) or not pages:
            raise DirectoryError('官网章节图片列表为空。')
        if not count.isascii() or not count.isdigit() or int(count) != len(pages):
            raise DirectoryError('官网章节图片数量不完整, 不下载或更改完成记录。')
        for image in pages:
            url = image.get('url') if isinstance(image, dict) else None
            if not isinstance(url, str) or urlparse(url).scheme not in ('http', 'https') or not urlparse(url).netloc:
                raise DirectoryError('官网章节图片地址无效。')
        log.info('官网章节图片列表有效: 漫画=%s 章节=%s 页数=%s 配置代理=%s',
                 slug, chapter_id, len(pages), bool(config.CM_PROXY))
        return {'contents': pages, 'words': list(range(1, len(pages) + 1)), '_source': 'website'}
    finally:
        handler.client.close()


def group_chapters(slug, group, refresh=False):
    data = catalog(slug, refresh=refresh)
    if data.get('directory_error'):
        raise DirectoryError(data['directory_error'])
    if group not in data['directories']:
        raise DirectoryError('所选章节分组不存在，未更改订阅记录。')
    chapters = data['directories'][group]
    if not chapters:
        raise DirectoryError('所选分组没有章节，暂不能添加或检查订阅。')
    return chapters
