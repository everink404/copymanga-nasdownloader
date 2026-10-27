import json
import os
import uuid
from pathlib import Path
from .files import atomic_bytes, sync_directory
from .models import CoreError, canonical, digest
from .store import Store

FILES = ('updater.json', 'download_tasks.json', 'system_config.json', 'web_config.json')


def strict_json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ValueError('duplicate key')
            result[key] = value
        return result
    value = json.loads(data.decode('utf-8-sig'), object_pairs_hook=unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError('non-finite number')))
    if not isinstance(value, dict): raise ValueError('expected object')
    return value


class Migration:
    """Explicit private snapshot import; legacy names never become download facts."""
    def __init__(self, data):
        self.data = Path(data)

    def detected(self):
        return any((self.data / name).exists() for name in FILES) and not (self.data / 'core-v1/state.sqlite3').exists()

    def read(self):
        raw, hashes, parsed = {}, {}, {}
        for name in FILES:
            path = self.data / name
            if not path.exists():
                hashes[name] = None; continue
            try:
                if path.is_symlink() or path.stat().st_size > 50 * 1024 * 1024: raise ValueError('unsafe file')
                raw[name] = path.read_bytes(); parsed[name] = strict_json(raw[name]); hashes[name] = digest(raw[name])
            except (OSError, ValueError, UnicodeError):
                raise CoreError('LEGACY_INVALID', f'{name} 无法严格读取，未改动原文件') from None
        return raw, hashes, parsed

    def preview(self):
        _, hashes, parsed = self.read()
        updater = parsed.get('updater.json', {})
        if any(not isinstance(v, list) or any(not isinstance(i, dict) for i in v) for v in updater.values()):
            raise CoreError('LEGACY_INVALID', '旧订阅格式无效，未按空订阅处理')
        return dict(fingerprint=digest(canonical(hashes).encode('utf-8')), files=hashes,
                    records=sum(len(v) for v in updater.values()), source_count=len(updater),
                    disabled=True, verified_downloads=0, originals_untouched=True)

    def apply(self, fingerprint, stopped_ack):
        if not stopped_ack:
            raise CoreError('LEGACY_WRITER_RUNNING', '应用前必须确认旧容器和 CLI 下载器已停止')
        final = self.data / 'core-v1'
        if final.exists():
            if (final / 'state.sqlite3').exists():
                store = Store(final)
                try: previous = store.get('migration', 'legacy')
                finally: store.close()
                if previous and previous['fingerprint'] == fingerprint: return dict(previous, already_applied=True)
            raise CoreError('CONFLICT', '已有核心数据，拒绝覆盖')
        preview = self.preview()
        if preview['fingerprint'] != fingerprint:
            raise CoreError('LEGACY_CHANGED', '旧文件在预览后变化，请重新预览')
        raw, hashes, parsed = self.read()
        snapshot_id = uuid.uuid4().hex
        snapshot = self.data / 'legacy-snapshots' / snapshot_id
        snapshot.mkdir(parents=True, mode=0o700)
        for name, data in raw.items():
            atomic_bytes(snapshot / name, data)
            os.chmod(snapshot / name, 0o600)
            if digest((snapshot / name).read_bytes()) != hashes[name]:
                raise CoreError('STORAGE_ERROR', '私有备份校验失败')
        atomic_bytes(snapshot / 'manifest.json', canonical(hashes).encode('utf-8'))
        result = dict(preview, snapshot_id=snapshot_id, state='imported_disabled_unverified')
        staging = self.data / ('core-v1.staging-' + snapshot_id)
        store = Store(staging)
        try:
            with store.transaction():
                for source, records in parsed.get('updater.json', {}).items():
                    for index, record in enumerate(records):
                        pointer = '/'+source.replace('~', '~0').replace('/', '~1')+'/'+str(index)
                        record_id = digest((fingerprint + pointer).encode('utf-8'))
                        store.put('legacy_record', record_id, dict(id=record_id, source=source, original=record,
                                  json_pointer=pointer, backup=snapshot_id, disabled=True, verified=False))
                # Credentials/config remain private; never included in public API DTOs.
                for name, body in parsed.items(): store.put('legacy_private', name, dict(original=body, backup=snapshot_id))
                store.put('migration', 'legacy', result)
        finally: store.close()
        if self.preview()['fingerprint'] != fingerprint:
            raise CoreError('LEGACY_CHANGED', '导入期间旧文件发生变化，未激活新库')
        try: final.mkdir(mode=0o700)
        except FileExistsError:
            raise CoreError('CONFLICT', '核心目录已被创建，未覆盖') from None
        os.link(staging / 'state.sqlite3', final / 'state.sqlite3')
        sync_directory(final)
        os.chmod(final / 'state.sqlite3', 0o600)
        atomic_bytes(final / 'migration.json', canonical(result).encode('utf-8'))
        os.chmod(final / 'migration.json', 0o600)
        return result
