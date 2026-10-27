import os
import sqlite3
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from .engine import Engine
from .files import safe_path, verify_archive
from .fixture import FixtureSource, Registry
from .migration import Migration
from .models import CoreError
from .store import Store


class PreviewInput(BaseModel):
    source_id: str = 'fixture_offline'
    comic_id: str = 'demo'
    group_id: str = 'default'
    mode: str = 'all'
    anchor: str | None = None


class SubscribeInput(BaseModel):
    preview_id: str
    fail_page: int | None = Field(default=None, ge=1, le=3)
    html_page: int | None = Field(default=None, ge=1, le=3)


class ImportInput(BaseModel):
    fingerprint: str
    legacy_writer_stopped_ack: bool = False


def create_app(data=None, downloads=None, cbz=None, fixture=None):
    data = Path(data or os.getenv('CMNAS_DATA_PATH', '/data'))
    downloads = Path(downloads or os.getenv('CMNAS_DOWNLOAD_PATH', '/downloads'))
    cbz = Path(cbz or os.getenv('CMNAS_CBZ_PATH', '/cbz'))
    fixture = fixture if fixture is not None else os.getenv('CMNAS_S2_FIXTURE') == '1'
    migration = Migration(data)
    guard = threading.RLock()

    @asynccontextmanager
    async def lifespan(app):
        app.state.engine = None; app.state.store = None
        if not migration.detected(): activate(app)
        yield
        if app.state.engine: app.state.engine.close()
        if app.state.store: app.state.store.close()

    def activate(app):
        store = Store(data / 'core-v1')
        try: engine = Engine(store, Registry([FixtureSource()] if fixture else []), downloads, cbz)
        except BaseException:
            store.close(); raise
        app.state.store, app.state.engine = store, engine
        engine.start()

    app = FastAPI(title='漫画下载核心 S2', lifespan=lifespan)

    def engine():
        if app.state.engine is None:
            raise CoreError('LEGACY_DETECTED', '检测到旧数据，先预览导入；原文件保持只读')
        return app.state.engine

    @app.exception_handler(CoreError)
    async def core_error(request, error):
        status = 404 if error.code == 'NOT_FOUND' else 409 if error.code in (
            'CONFLICT', 'LEGACY_DETECTED', 'LEGACY_CHANGED', 'LOCAL_RECONCILIATION_REQUIRED') else 400
        return JSONResponse(dict(error=error.code, message=error.message, retry_at=error.retry_at), status_code=status)

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request, error):
        return JSONResponse(dict(error='STORAGE_ERROR', message='数据库读写失败，未重建空数据库'), status_code=503)

    @app.get('/health')
    def health():
        return dict(status='ok', version=os.getenv('APP_VERSION', 's2-dev'), revision=os.getenv('APP_REVISION', 'local'),
                    mode='legacy_detected' if app.state.engine is None else 'core', schema_version=1, fixture=fixture)

    @app.get('/', response_class=HTMLResponse)
    def index():
        return (Path(__file__).with_name('console.html')).read_text(encoding='utf-8')

    @app.get('/api/v1/sources')
    def sources():
        return [s.descriptor() for s in engine().registry.sources.values()]

    @app.get('/api/v1/browse')
    def browse(source_id: str = 'fixture_offline', q: str = ''):
        session = engine().registry.get(source_id).open_session({})
        try: return session.browse(q)
        finally: session.close()

    @app.get('/api/v1/catalog')
    def catalog(source_id: str = 'fixture_offline', comic_id: str = 'demo', group_id: str = 'default'):
        chapters, fingerprint = engine().catalog(source_id, comic_id, group_id)
        return dict(items=[c.data() for c in chapters], fingerprint=fingerprint)

    @app.post('/api/v1/subscriptions/preview')
    def preview(body: PreviewInput):
        return engine().preview(**body.model_dump())

    @app.post('/api/v1/subscriptions')
    def subscribe(body: SubscribeInput):
        with guard:
            return engine().subscribe(body.preview_id, {k: v for k, v in body.model_dump().items() if k != 'preview_id' and v is not None})

    @app.get('/api/v1/subscriptions')
    def subscriptions():
        return [{k: s.get(k) for k in ('id', 'title', 'source_id', 'comic_id', 'group_id', 'mode', 'paused', 'cancelled')} for s in engine().store.subscriptions()]

    @app.post('/api/v1/subscriptions/{item_id}/run')
    def run(item_id: str):
        return engine().enqueue(item_id)

    @app.post('/api/v1/subscriptions/{item_id}/pause')
    def pause(item_id: str):
        core = engine()
        with core.guard:
            item = core.store.subscription(item_id)
            if not item: raise CoreError('NOT_FOUND', '订阅不存在')
            item['paused'] = True; core.store.save_subscription(item)
            for job in core.store.jobs():
                if job['subscription_id'] == item_id and job['status'] in ('waiting', 'running', 'retry_wait', 'blocked'): core.stop(job['id'])
        return dict(paused=True)

    @app.get('/api/v1/tasks')
    def tasks():
        # Only core-produced fields, no source request URLs or private configuration.
        return [{k: v for k, v in job.items() if k not in ('plan', 'identity')} for job in engine().store.jobs()]

    @app.post('/api/v1/tasks/{job_id}/retry')
    def retry(job_id: str): return engine().retry(job_id)

    @app.post('/api/v1/tasks/{job_id}/stop')
    def stop(job_id: str): return engine().stop(job_id)

    @app.get('/api/v1/artifacts')
    def artifacts():
        core = engine()
        return [dict(id=a['id'], chapter_key=a['chapter_key'], name=Path(a['relative_path']).name)
                for a in core.store.all('artifact') if (core.store.completed(a['chapter_key']) or {}).get('id') == a['id']]

    @app.get('/api/v1/artifacts/{artifact_id}')
    def artifact(artifact_id: str):
        core = engine(); item = core.store.get('artifact', artifact_id)
        if not item: raise CoreError('NOT_FOUND', '文件不存在')
        path = safe_path(core.cbz, item['relative_path'])
        if not verify_archive(path, item):
            core.recover(job_id=item['job_id'])
            raise CoreError('ARTIFACT_REPAIR_REQUIRED', '文件缺失或损坏，已登记恢复任务')
        return FileResponse(path, filename=path.name, media_type='application/vnd.comicbook+zip')

    @app.get('/api/v1/migration/preview')
    def migration_preview(): return migration.preview()

    @app.post('/api/v1/migration/apply')
    def migration_apply(body: ImportInput):
        with guard:
            if app.state.engine:
                raise CoreError('CONFLICT', '核心已激活，禁止覆盖导入')
            result = migration.apply(body.fingerprint, body.legacy_writer_stopped_ack)
            activate(app)
            return result

    return app


app = create_app()
