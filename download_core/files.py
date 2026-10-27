import io
import hashlib
import json
import os
import re
import warnings
import zipfile
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring
from PIL import Image
from .models import CoreError, canonical, digest


def safe_path(root, relative):
    root = Path(root).resolve()
    path = root / relative
    if Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise CoreError('STORAGE_ERROR', '文件路径超出配置目录')
    current = path
    while current != root:
        if current.is_symlink():
            raise CoreError('STORAGE_ERROR', '拒绝通过符号链接访问文件')
        if current.parent == current:
            raise CoreError('STORAGE_ERROR', '文件路径无效')
        current = current.parent
    if not path.resolve().is_relative_to(root):
        raise CoreError('STORAGE_ERROR', '文件路径超出配置目录')
    return path


def label(value):
    value = re.sub(r'[\x00-\x1f/\\:*?"<>|]', '_', value).strip(' .')
    value = value.encode('utf-8')[:48].decode('utf-8', errors='ignore') or 'comic'
    if value.upper().split('.')[0] in {'CON', 'PRN', 'AUX', 'NUL', *('COM'+str(i) for i in range(1, 10)), *('LPT'+str(i) for i in range(1, 10))}:
        value = '_' + value
    return value


def sync_directory(path):
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY)
        try: os.fsync(descriptor)
        finally: os.close(descriptor)


def file_digest(path):
    result = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''): result.update(chunk)
    return result.hexdigest()


def atomic_bytes(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + '.part', 'wb') as stream:
        stream.write(content); stream.flush(); os.fsync(stream.fileno())
    os.replace(str(path) + '.part', path)
    sync_directory(path.parent)


def validate_image(content, expected=None):
    if not content or len(content) > 25 * 1024 * 1024:
        raise CoreError('INVALID_IMAGE', '图片为空或超出单页限制')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                image_format = image.format
                width, height = image.size
                if image_format not in ('PNG', 'JPEG', 'WEBP', 'GIF') or width * height > 100_000_000:
                    raise ValueError('image limits')
                image.verify()
            with Image.open(io.BytesIO(content)) as image:
                for frame in range(getattr(image, 'n_frames', 1)):
                    if frame > 1000:
                        raise ValueError('too many frames')
                    image.seek(frame); image.load()
    except Exception:
        raise CoreError('INVALID_IMAGE', '响应不是完整有效的图片') from None
    sha = digest(content)
    if expected and sha != expected:
        raise CoreError('INVALID_IMAGE', '图片哈希与清单不符')
    extension, mime = {'PNG': ('png', 'image/png'), 'JPEG': ('jpg', 'image/jpeg'),
                       'WEBP': ('webp', 'image/webp'), 'GIF': ('gif', 'image/gif')}[image_format]
    return dict(sha256=sha, bytes=len(content), extension=extension, mime=mime, width=width, height=height)


def build_archive(path, chapter, pages, metadata, image_root):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    info = Element('ComicInfo')
    SubElement(info, 'Title').text = chapter.title
    SubElement(info, 'Series').text = metadata['comic_title']
    SubElement(info, 'PageCount').text = str(len(pages))
    page_manifest = []
    with zipfile.ZipFile(path, 'x', compression=zipfile.ZIP_STORED) as archive:
        for page in pages:
            content = safe_path(image_root, page['relative_path']).read_bytes()
            verified = validate_image(content, page['sha256'])
            name = f"{page['ordinal']:04d}.{verified['extension']}"
            archive.writestr(name, content)
            page_manifest.append(dict(name=name, ordinal=page['ordinal'], page_key=page['page_key'], sha256=verified['sha256']))
        archive.writestr('ComicInfo.xml', tostring(info, encoding='utf-8', xml_declaration=True))
        archive.writestr('download-manifest.json', canonical(dict(metadata, pages=page_manifest)).encode('utf-8'))
    with open(path, 'rb+') as stream: os.fsync(stream.fileno())
    return file_digest(path)


def verify_archive(path, artifact):
    try:
        path = Path(path)
        if file_digest(path) != artifact['sha256']:
            return False
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read('download-manifest.json'))
            if manifest['chapter_key'] != artifact['chapter_key'] or manifest['artifact_id'] != artifact['id']:
                return False
            if manifest['content_version'] != artifact['content_version'] or manifest['identity'] != artifact['identity']:
                return False
            pages = manifest['pages']
            names = [p['name'] for p in pages]
            if not pages or len(set(names)) != len(names) or [p['ordinal'] for p in pages] != list(range(1, len(pages)+1)):
                return False
            if set(archive.namelist()) != set(names + ['ComicInfo.xml', 'download-manifest.json']):
                return False
            if archive.testzip() is not None:
                return False
            for page in pages:
                validate_image(archive.read(page['name']), page['sha256'])
        return True
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, CoreError):
        return False


def publish(root, artifact):
    final, temporary = safe_path(root, artifact['relative_path']), safe_path(root, artifact['temporary_path'])
    if final.exists():
        if not verify_archive(final, artifact):
            raise CoreError('ARTIFACT_CONFLICT', '目标文件已有其他内容，未覆盖')
    else:
        if not verify_archive(temporary, artifact):
            raise CoreError('STORAGE_ERROR', '待发布 CBZ 不完整，保留材料供恢复')
        try:
            os.link(temporary, final)
        except FileExistsError:
            if not verify_archive(final, artifact):
                raise CoreError('ARTIFACT_CONFLICT', '目标路径已占用，未覆盖') from None
        except OSError:
            raise CoreError('STORAGE_ERROR', '输出卷不支持无覆盖发布，未替换已有文件') from None
        sync_directory(final.parent)
    if not verify_archive(final, artifact):
        raise CoreError('STORAGE_ERROR', '发布文件回读校验失败，未更新完成记录')
    if temporary.exists():
        temporary.unlink()
