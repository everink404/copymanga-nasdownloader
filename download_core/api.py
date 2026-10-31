import os
import json
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from .engine import Engine
from .files import safe_path, verify_archive, validate_image
from .transport import Budget
from .fixture import FixtureSource, Registry
from .migration import Migration
from .models import CoreError
from .store import Store
from .sources import real_sources


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


class SettingsInput(BaseModel):
    values: dict


class PolicyInput(BaseModel):
    preview_id: str


class SubscriptionState(BaseModel):
    paused: bool | None = None
    cancelled: bool | None = None
    interval_hours: int | None = Field(default=None,ge=1,le=8760)


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
        try:
            adapters=real_sources()
            for adapter in adapters:
                saved=store.get('source_private',adapter.source_id)
                if saved:
                    empty=[k for k in ('password','token','proxy','username') if saved['config'].get(k)=='']
                    adapter.configure(dict(saved['config'],clear_secrets=empty)); adapter.generation=saved['generation']
            proxy_file=os.getenv('CMNAS_IMPORT_PROXY_FILE')
            if proxy_file and not store.get('settings_import','legacy_proxy'):
                # Explicit one-time import. Only cm_proxy is selected; no legacy
                # account, password, token or download records enter this store.
                try:
                    raw=Path(proxy_file).read_bytes()
                    if len(raw)>1024*1024: raise ValueError()
                    proxy=json.loads(raw).get('cm_proxy','')
                    if isinstance(proxy,dict): proxy=proxy.get('https') or proxy.get('http') or ''
                    if not isinstance(proxy,str) or not proxy: raise ValueError()
                except (OSError,ValueError,AttributeError):
                    raise CoreError('CONFIG_INVALID','显式指定的旧代理配置无法读取或未配置代理') from None
                with store.transaction():
                    for adapter in adapters:
                        adapter.configure({'proxy':proxy})
                        store.put('source_private',adapter.source_id,dict(config=adapter.config,generation=adapter.generation))
                    store.put('settings_import','legacy_proxy',dict(imported_at=time.time(),fields=['proxy']))
            engine = Engine(store, Registry(([FixtureSource()] if fixture else [])+adapters), downloads, cbz)
        except BaseException:
            store.close(); raise
        app.state.store, app.state.engine = store, engine
        engine.start()

    app = FastAPI(title='漫画下载器', lifespan=lifespan)

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

    @app.exception_handler(ValueError)
    @app.exception_handler(KeyError)
    @app.exception_handler(TypeError)
    async def protocol_error(request,error):
        return JSONResponse(dict(error='PROTOCOL_ERROR',message='参数或源站数据格式无效，未按空结果处理'),status_code=400)

    @app.get('/health')
    def health():
        return dict(status='ok', version=os.getenv('APP_VERSION', 's2-dev'), revision=os.getenv('APP_REVISION', 'local'),
                    mode='legacy_detected' if app.state.engine is None else 'core', schema_version=1, fixture=fixture)

    @app.get('/', response_class=HTMLResponse)
    def index():
        return (Path(__file__).with_name('console.html' if fixture else 'app.html')).read_text(encoding='utf-8')

    @app.get('/api/v1/sources')
    def sources():
        return [s.descriptor() for s in engine().registry.sources.values()]

    @app.put('/api/v1/sources/{source_id}/settings')
    def settings(source_id: str,body: SettingsInput):
        core=engine(); source=core.registry.get(source_id)
        if not hasattr(source,'configure'): raise CoreError('CONFIG_INVALID','测试源不需要配置')
        with core.guard:
            if any(j['source_id']==source_id and j['status'] in ('waiting','running','retry_wait') for j in core.store.jobs()):
                raise CoreError('CONFLICT','先停止该源的活动任务，再修改配置')
            previous=dict(source.config); generation=source.generation
            try:
                source.configure(body.values)
                core.store.put('source_private',source_id,dict(config=source.config,generation=source.generation))
            except BaseException:
                source.config=previous; source.generation=generation; raise
        return source.public_config()

    @app.get('/api/v1/browse')
    def browse(source_id: str = 'hipmh', q: str = '',page: int=1,sort: str='updated',status: str='',theme: str=''):
        if not 1<=page<=10000: raise CoreError('CONFIG_INVALID','页码无效')
        source=engine().registry.get(source_id);session=source.open_session({})
        try:
            result=session.browse(q) if source_id=='fixture_offline' else session.browse(q,page,sort,status,theme)
            if source_id!='fixture_offline':
                for comic in result['items']:
                    comic['source_id']=source_id;source.comic_cache[comic['comic_id']]=comic
                while len(source.comic_cache)>512:source.comic_cache.pop(next(iter(source.comic_cache)))
            return result
        finally: session.close()

    @app.get('/api/v1/comics/{source_id}/{comic_id}')
    def comic_detail(source_id: str,comic_id: str):
        session=engine().registry.get(source_id).open_session({})
        try:
            comic=session.get_comic(comic_id)
            source=engine().registry.get(source_id)
            if hasattr(source,'comic_cache'):source.comic_cache[comic_id]=comic
            return dict(comic,source_id=source_id,groups=session.list_groups(comic_id))
        finally: session.close()

    @app.get('/api/v1/covers/{source_id}/{comic_id}')
    def cover_image(source_id: str,comic_id: str):
        source=engine().registry.get(source_id)
        if not hasattr(source,'cover_cache'):raise CoreError('NOT_FOUND','没有封面')
        with source.cover_lock:
            cached=source.cover_cache.get(comic_id)
            if cached and cached[0]>time.time():
                source.cover_cache.move_to_end(comic_id)
                return Response(cached[1],media_type=cached[2],headers={'Cache-Control':'private, max-age=300'})
            session=source.open_session({})
            try:
                comic=source.comic_cache.get(comic_id) or session.get_comic(comic_id)
                if not comic.get('cover'):raise CoreError('NOT_FOUND','源没有提供封面')
                url=session.image_url(comic['cover'],source.config.get('cover_url',source.config['web_url']))
                budget=Budget()
                while True:
                    try:
                        content=session.transport.get(url,budget,role='cover',allowed_hosts=session.image_hosts,headers=session.image_headers)
                        break
                    except CoreError as error:
                        if error.code=='RATE_LIMIT' and error.retry_at and 0<error.retry_at-time.time()<.6:
                            time.sleep(max(0,error.retry_at-time.time()));continue
                        raise
                image=validate_image(content)
                previous=source.cover_cache.pop(comic_id,None)
                if previous:source.cover_bytes-=len(previous[1])
                source.cover_cache[comic_id]=(time.time()+3600,content,image['mime']);source.cover_bytes+=len(content)
                while source.cover_bytes>20*1024*1024:
                    _,old=source.cover_cache.popitem(last=False);source.cover_bytes-=len(old[1])
                return Response(content,media_type=image['mime'],headers={'Cache-Control':'private, max-age=300'})
            finally:session.close()

    @app.get('/api/v1/chapter-page')
    def chapter_page(source_id: str,comic_id: str,group_id: str,cursor: str | None=None):
        session=engine().registry.get(source_id).open_session({})
        try:
            result=session.list_chapters(comic_id,group_id,cursor)
            return dict(result,items=[c.data() for c in result['items']])
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
        core=engine()
        return [dict({k:s.get(k) for k in ('id','title','cover','source_id','comic_id','group_id','mode','paused','cancelled','interval_hours','next_check_at')},
                     verified_chapters=sum(1 for a in core.store.all('artifact') if a['stage']=='committed' and a['identity'][:3]==[s['source_id'],s['comic_id'],s['group_id']]
                                           and (core.store.completed(a['chapter_key']) or {}).get('id')==a['id'])) for s in core.store.subscriptions()]

    @app.patch('/api/v1/subscriptions/{item_id}')
    def subscription_state(item_id: str,body: SubscriptionState):
        core=engine()
        with core.guard:
            item=core.store.subscription(item_id)
            if not item: raise CoreError('NOT_FOUND','订阅不存在')
            item.update(body.model_dump(exclude_none=True))
            item['next_check_at']=time.time()+item.get('interval_hours',120)*3600
            core.store.save_subscription(item)
            if item.get('paused') or item.get('cancelled'):
                for job in core.store.jobs():
                    if job['subscription_id']==item_id and job['status'] in ('waiting','running','retry_wait','blocked'): core.stop(job['id'])
        return dict(id=item_id,paused=item.get('paused'),cancelled=item.get('cancelled'),interval_hours=item.get('interval_hours'))

    @app.put('/api/v1/subscriptions/{item_id}/policy')
    def policy(item_id: str,body: PolicyInput):
        core=engine()
        with core.guard:
            item=core.store.subscription(item_id); preview=core.store.get('preview',body.preview_id)
            if not item or not preview: raise CoreError('NOT_FOUND','订阅或预览不存在')
            if any(j['subscription_id']==item_id and j['status'] in ('waiting','running','retry_wait','blocked','recovery_required') for j in core.store.jobs()):
                raise CoreError('CONFLICT','存在活动或待恢复任务，先安全停止后再修改范围')
            if preview['expires']<time.time() or any(preview[k]!=item[k] for k in ('source_id','comic_id','group_id')):
                raise CoreError('RANGE_REVIEW_REQUIRED','预览已失效或不是同一漫画分组')
            if preview.get('config_generation',1)!=getattr(core.registry.get(item['source_id']),'generation',1):
                raise CoreError('RANGE_REVIEW_REQUIRED','源配置已变化，请重新预览')
            _,fingerprint=core.catalog(item['source_id'],item['comic_id'],item['group_id'])
            if fingerprint!=preview['fingerprint']: raise CoreError('CATALOG_CHANGED','目录已变化，请重新预览')
            for field in ('mode','anchor','before_anchor','after_anchor','chapter_keys'): item[field]=preview[field]
            item['baseline']=preview['chapter_keys'] if preview['mode']=='future' else []
            core.store.save_subscription(item)
        return dict(id=item_id,mode=item['mode'])

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
