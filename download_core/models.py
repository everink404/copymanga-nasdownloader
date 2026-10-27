import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Optional


class CoreError(Exception):
    def __init__(self, code, message, retry_at=None):
        super().__init__(message)
        self.code, self.message, self.retry_at = code, message, retry_at


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def key(*parts):
    if not parts or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', parts[0]):
        raise CoreError('PROTOCOL_ERROR', '无效漫画源标识')
    for part in parts:
        if not isinstance(part, str) or not part or len(part.encode('utf-8')) > 512:
            raise CoreError('PROTOCOL_ERROR', '无效内容标识')
    return digest(canonical(list(parts)).encode('utf-8'))


@dataclass(frozen=True)
class Chapter:
    source_id: str
    comic_id: str
    group_id: str
    chapter_id: str
    title: str
    ordinal: int
    source_revision: Optional[str] = None

    @property
    def key(self):
        return key(self.source_id, self.comic_id, self.group_id, self.chapter_id)

    def data(self):
        return dict(asdict(self), chapter_key=self.key)


@dataclass(frozen=True)
class ImageEntry:
    ordinal: int
    page_key: str
    url: str
    expected_sha256: Optional[str] = None
    revision: Optional[str] = None
    transform_version: str = 'none'


@dataclass(frozen=True)
class Manifest:
    chapter: Chapter
    entries: tuple
    proof_kind: str = 'none'
    complete: bool = True
    expected_count: Optional[int] = None

    def validate(self):
        if self.complete is not True or not self.entries or len(self.entries) > 10000:
            raise CoreError('PROTOCOL_ERROR', '图片清单不完整')
        if self.proof_kind not in ('none', 'content_hash', 'immutable_resource'):
            raise CoreError('PROTOCOL_ERROR', '未知图片身份证明')
        if [p.ordinal for p in self.entries] != list(range(1, len(self.entries) + 1)):
            raise CoreError('PROTOCOL_ERROR', '图片顺序无效')
        if len({p.page_key for p in self.entries}) != len(self.entries):
            raise CoreError('PROTOCOL_ERROR', '图片标识重复')
        if self.expected_count is not None and self.expected_count != len(self.entries):
            raise CoreError('PROTOCOL_ERROR', '图片数量不完整')
        if self.proof_kind == 'content_hash' and any(not re.fullmatch(r'[0-9a-f]{64}', p.expected_sha256 or '') for p in self.entries):
            raise CoreError('PROTOCOL_ERROR', '缺少图片哈希')
        if self.proof_kind == 'immutable_resource' and any(not p.revision for p in self.entries):
            raise CoreError('PROTOCOL_ERROR', '缺少稳定资源版本')
        if any(not isinstance(p.page_key, str) or not p.page_key or len(p.page_key) > 512 for p in self.entries):
            raise CoreError('PROTOCOL_ERROR', '无效图片标识')
        if any(p.transform_version != 'none' for p in self.entries):
            raise CoreError('UNSUPPORTED_TRANSFORM', '本版本不支持该图片变换')
        return self

    @property
    def version(self):
        return digest(canonical([[p.page_key, p.revision, p.expected_sha256, p.transform_version]
                                 for p in self.entries]).encode('utf-8'))


def collect_catalog(session, comic_id, group_id):
    def scan():
        chapters, cursor, seen, token, advertised = [], None, set(), None, None
        for _ in range(1000):
            page = session.list_chapters(comic_id, group_id, cursor)
            if not isinstance(page, dict) or not isinstance(page.get('items'), list):
                raise CoreError('CATALOG_INCOMPLETE', '目录格式无效')
            if cursor is None:
                token, advertised = page.get('snapshot_token'), page.get('total')
            if page.get('snapshot_token') != token or page.get('total') != advertised:
                raise CoreError('CATALOG_CHANGED', '分页目录已变化')
            chapters.extend(page['items'])
            if page.get('end_reached') is True:
                break
            cursor = page.get('next_cursor')
            if not cursor or cursor in seen:
                raise CoreError('CATALOG_INCOMPLETE', '目录分页未结束或循环')
            seen.add(cursor)
        else:
            raise CoreError('CATALOG_INCOMPLETE', '目录超出分页上限')
        if any((c.source_id, c.comic_id, c.group_id) != (session.source_id, comic_id, group_id) for c in chapters):
            raise CoreError('PROTOCOL_ERROR', '目录身份不匹配')
        if len({c.key for c in chapters}) != len(chapters) or [c.ordinal for c in chapters] != list(range(1, len(chapters) + 1)):
            raise CoreError('CATALOG_INCOMPLETE', '目录重复或顺序不可靠')
        if advertised is not None and advertised != len(chapters):
            raise CoreError('CATALOG_INCOMPLETE', '目录总数不一致')
        fingerprint = digest(canonical([[c.key, c.ordinal, c.source_revision] for c in chapters]).encode('utf-8'))
        return chapters, fingerprint, token
    previous = scan()
    if previous[2] is not None:
        return previous[:2]
    for _ in range(2):
        current = scan()
        if current[1] == previous[1]:
            return current[:2]
        previous = current
    raise CoreError('CATALOG_CHANGED', '目录持续变化，请稍后重试')
