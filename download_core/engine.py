import shutil
import os
import threading
import time
import uuid
from pathlib import Path
from .files import atomic_bytes, build_archive, label, publish, safe_path, validate_image, verify_archive
from .models import Chapter, CoreError, canonical, collect_catalog, digest, key
from .store import ACTIVE
from .transport import Budget


def uid():
    return uuid.uuid4().hex


class Engine:
    def __init__(self, store, registry, downloads, cbz, fault=None):
        self.store, self.registry = store, registry
        self.downloads, self.cbz = Path(downloads), Path(cbz)
        self.downloads.mkdir(parents=True, exist_ok=True); self.cbz.mkdir(parents=True, exist_ok=True)
        self.guard = threading.RLock()
        self.wake, self.shutdown = threading.Event(), threading.Event()
        self.runtime = {}
        self.fault = fault or (lambda _: None)
        self.worker = None
        self.recover(startup=True)

    def update(self, job_id, **fields):
        with self.store.transaction():
            job = self.store.job(job_id)
            event = fields.pop('event', None)
            if event:
                job['events'] = (job.get('events', []) + [dict(time=time.time(), text=event)])[-50:]
            job.update(fields); self.store.save_job(job)
            return job

    def catalog(self, source_id, comic_id, group_id):
        session = self.registry.get(source_id).open_session({})
        try: return collect_catalog(session, comic_id, group_id)
        finally: session.close()

    def preview(self, source_id, comic_id, group_id='default', mode='all', anchor=None):
        if mode not in ('all', 'future', 'from'):
            raise CoreError('PROTOCOL_ERROR', '下载范围无效')
        chapters, fingerprint = self.catalog(source_id, comic_id, group_id)
        if not chapters:
            raise CoreError('CATALOG_INCOMPLETE', '目录为空，不能设置下载范围')
        keys = [c.key for c in chapters]
        if mode == 'from' and anchor not in keys:
            raise CoreError('RANGE_REVIEW_REQUIRED', '起始章节不存在')
        preview = dict(id=uid(), source_id=source_id, comic_id=comic_id, group_id=group_id, mode=mode,
                       anchor=anchor, chapter_keys=keys, fingerprint=fingerprint, expires=time.time()+600,
                       before_anchor=keys[:keys.index(anchor)] if mode == 'from' else [],
                       after_anchor=keys[keys.index(anchor):] if mode == 'from' else [])
        self.store.put('preview', preview['id'], preview)
        return preview

    def subscribe(self, token, options=None):
        preview = self.store.get('preview', token)
        if not preview or preview['expires'] < time.time():
            raise CoreError('RANGE_REVIEW_REQUIRED', '范围预览过期，请重新预览')
        chapters, fingerprint = self.catalog(preview['source_id'], preview['comic_id'], preview['group_id'])
        if fingerprint != preview['fingerprint']:
            raise CoreError('CATALOG_CHANGED', '目录已变化，请重新预览')
        identity = key(preview['source_id'], preview['comic_id'], preview['group_id'])
        with self.store.transaction():
            if any(s['identity'] == identity for s in self.store.subscriptions()):
                raise CoreError('CONFLICT', '这个分组已订阅')
            source = self.registry.get(preview['source_id'])
            item = dict(preview, id=uid(), identity=identity, title='离线下载示例' if source.source_id == 'fixture_offline' else preview['comic_id'],
                        paused=False, cancelled=False, identity_scheme=source.identity_scheme,
                        options=options or {}, baseline=preview['chapter_keys'] if preview['mode'] == 'future' else [])
            self.store.save_subscription(item)
        return item

    def enqueue(self, subscription_id, origin='manual', parent=None, repair=False):
        with self.store.transaction():
            item = self.store.subscription(subscription_id)
            if not item or item.get('cancelled') or item.get('paused'):
                raise CoreError('SOURCE_UNAVAILABLE', '订阅不存在、已暂停或已取消')
            self.registry.get(item['source_id'])
            active = next((j for j in self.store.jobs() if j['identity'] == item['identity'] and j['status'] in ACTIVE), None)
            if active: return active
            job = dict(id=uid(), subscription_id=item['id'], identity=item['identity'], title=item['title'],
                       source_id=item['source_id'], comic_id=item['comic_id'], group_id=item['group_id'],
                       status='waiting', phase='等待执行', created_at=time.time(), started_at=None, finished_at=None,
                       chapter='', images_done=0, images_total=None, chapters_done=0, chapters_total=None,
                       bytes_downloaded=0, reused_images=0, done_keys=[], stop_after_chapter=False, error='', error_code='',
                       parent_id=parent, repair=repair, origin=origin, events=[], plan=None)
            self.store.save_job(job)
        self.wake.set()
        return job

    def pending(self, item, chapters, job):
        keys = [c.key for c in chapters]
        old = item.get('last_catalog', item['chapter_keys'])
        if old and (not keys or not set(old).intersection(keys)):
            raise CoreError('CATALOG_CHANGED', '目录身份可能重建，未自动下载')
        if item['mode'] == 'from':
            if item['anchor'] not in keys:
                raise CoreError('RANGE_REVIEW_REQUIRED', '起始章节消失，请重选范围')
            position = keys.index(item['anchor'])
            if set(keys[position:]).intersection(item['before_anchor']) or set(keys[:position]).intersection(item['after_anchor']):
                raise CoreError('RANGE_REVIEW_REQUIRED', '章节顺序越过下载起点，请检查范围')
            chapters = chapters[position:]
        elif item['mode'] == 'future':
            chapters = [c for c in chapters if c.key not in item['baseline']]
        result = []
        for chapter in chapters:
            artifact = self.store.completed(chapter.key)
            if artifact:
                if not verify_archive(safe_path(self.cbz, artifact['relative_path']), artifact):
                    self.store.invalidate(artifact)
                    if not job.get('repair'):
                        raise CoreError('ARTIFACT_REPAIR_REQUIRED', '已有 CBZ 缺失或损坏，请点击核对并修复')
                elif artifact.get('source_revision') == chapter.source_revision:
                    continue
            result.append(chapter)
        item['last_catalog'] = keys
        self.store.save_subscription(item)
        return result

    def run_job(self, job_id):
        job = self.store.job(job_id)
        item = self.store.subscription(job['subscription_id'])
        if not item or item.get('cancelled'):
            self.update(job_id, status='blocked', error='订阅已取消'); return
        context = self.runtime.get(job_id)
        if context is None:
            try:
                source = self.registry.get(item['source_id'])
                if source.identity_scheme != item['identity_scheme']:
                    raise CoreError('IDENTITY_UNSUPPORTED', '源的身份规则已变化，未自动迁移')
                context = dict(session=source.open_session(dict(item['options'], failure_id=item['id'])), chapters={})
            except CoreError as error:
                self.update(job_id, status='blocked', phase='源不可用', error=error.message, error_code=error.code)
                return
            self.runtime[job_id] = context
        self.update(job_id, status='running', phase='检查目录', started_at=job.get('started_at') or time.time(), error='', error_code='')
        try:
            if job['plan'] is None:
                chapters, _ = collect_catalog(context['session'], item['comic_id'], item['group_id'])
                pending = self.pending(item, chapters, job)
                job = self.update(job_id, plan=[c.data() for c in pending], chapters_total=len(pending))
            for record in job['plan']:
                latest = self.store.job(job_id)
                if record['chapter_key'] in latest['done_keys']: continue
                if (latest['stop_after_chapter'] and record['chapter_key'] not in context['chapters']) or self.shutdown.is_set():
                    self.update(job_id, status='stopped', phase='已停止', finished_at=time.time()); return
                chapter = Chapter(**{k: v for k, v in record.items() if k != 'chapter_key'})
                self.download_chapter(job_id, item, chapter, context)
            latest = self.store.job(job_id)
            self.update(job_id, status='completed', phase='已完成', outcome='downloaded' if latest['done_keys'] else 'no_updates', finished_at=time.time())
        except CoreError as error:
            chapter_contexts = context.get('chapters', {}).values()
            has_prepared = any(a['job_id'] == job_id and a['stage'] == 'prepared' for a in self.store.all('artifact'))
            if error.retry_at and not self.shutdown.is_set() and all(b.used < b.maximum for c in chapter_contexts for b in c['budgets'].values()):
                self.update(job_id, status='retry_wait', phase='等待重试', retry_at=error.retry_at, error=error.message, error_code=error.code)
            elif has_prepared or error.code == 'ARTIFACT_REPAIR_REQUIRED':
                self.update(job_id, status='recovery_required', phase='需要恢复', error=error.message,
                            error_code=error.code, local_reconciled=not has_prepared)
            else:
                self.update(job_id, status='blocked' if error.code in ('SOURCE_UNAVAILABLE', 'RANGE_REVIEW_REQUIRED') else 'failed',
                            phase='未完成', error=error.message, error_code=error.code, finished_at=time.time())
        except Exception:
            prepared = any(a['job_id'] == job_id and a['stage'] == 'prepared' for a in self.store.all('artifact'))
            self.update(job_id, status='recovery_required' if prepared else 'failed', phase='未完成',
                        error='任务执行异常，文件已保留；请查看日志', error_code='INTERNAL_ERROR', finished_at=time.time())
            raise
        finally:
            if self.store.job(job_id)['status'] != 'retry_wait':
                context['session'].close(); self.runtime.pop(job_id, None)

    def download_chapter(self, job_id, item, chapter, runtime):
        session = runtime['session']
        current = runtime['chapters'].get(chapter.key)
        if current is None:
            self.update(job_id, chapter=chapter.title, phase='获取图片列表', images_done=0, images_total=None)
            manifest = session.get_manifest(chapter).validate()
            if manifest.chapter.key != chapter.key:
                raise CoreError('PROTOCOL_ERROR', '图片清单章节不匹配')
            attempt = dict(id=uid(), job_id=job_id, chapter_key=chapter.key, manifest_version=manifest.version,
                           proof_kind=manifest.proof_kind, receipts=[], budgets={})
            current = dict(manifest=manifest, attempt=attempt, budgets={})
            runtime['chapters'][chapter.key] = current
            self.store.put('attempt', attempt['id'], attempt)
        manifest, attempt = current['manifest'], current['attempt']
        self.update(job_id, chapter=chapter.title, phase='下载图片', images_total=len(manifest.entries))
        for page in manifest.entries:
            old = next((r for r in attempt['receipts'] if r['ordinal'] == page.ordinal), None)
            if old: continue
            content, reused = None, False
            if manifest.proof_kind != 'none':
                for previous in reversed(self.store.all('attempt')):
                    if (previous['chapter_key'], previous['manifest_version'], previous['proof_kind']) != (chapter.key, manifest.version, manifest.proof_kind): continue
                    receipt = next((r for r in previous['receipts'] if r['page_key'] == page.page_key and r['ordinal'] == page.ordinal), None)
                    if receipt:
                        try:
                            data = safe_path(self.downloads, receipt['relative_path']).read_bytes()
                            validate_image(data, receipt['sha256'])
                            if page.expected_sha256 and receipt['sha256'] != page.expected_sha256: continue
                            content, reused = data, True; break
                        except (CoreError, OSError): pass
            if content is None:
                budget = current['budgets'].setdefault(page.ordinal, Budget())
                try:
                    content = session.transport.get(page.url, budget, allowed_hosts=('fixture.invalid',) if item['source_id'] == 'fixture_offline' else session.image_hosts)
                finally:
                    attempt['budgets'][str(page.ordinal)] = budget.used
                    self.store.put('attempt', attempt['id'], attempt)
            verified = validate_image(content, page.expected_sha256)
            relative = f".core-v1/{attempt['id']}/{page.ordinal:04d}.{verified['extension']}"
            atomic_bytes(safe_path(self.downloads, relative), content)
            receipt = dict(verified, ordinal=page.ordinal, page_key=page.page_key, transform_version=page.transform_version, relative_path=relative)
            attempt['receipts'].append(receipt); self.store.put('attempt', attempt['id'], attempt)
            job = self.store.job(job_id)
            self.update(job_id, images_done=len(attempt['receipts']), bytes_downloaded=job['bytes_downloaded'] + (0 if reused else len(content)),
                        reused_images=job.get('reused_images', 0) + int(reused))
        content_version = digest(canonical([[p['page_key'], p['sha256'], p['mime'], p['transform_version']] for p in attempt['receipts']]).encode('utf-8'))
        existing = next((a for a in self.store.all('artifact') if a['chapter_key'] == chapter.key and a['content_version'] == content_version and a['stage'] == 'committed'
                         and verify_archive(safe_path(self.cbz, a['relative_path']), a)), None)
        if existing:
            existing = dict(existing, job_id=job_id, source_revision=chapter.source_revision)
            self.store.commit_artifact(existing)
            directory = safe_path(self.downloads, '.core-v1/' + attempt['id'])
            if directory.exists(): shutil.rmtree(directory)
            return
        artifact_id = uid()
        relative = f"{item['source_id']}/{label(item['title'])}--{key(item['source_id'],item['comic_id'])}/{label(item['group_id'])}--{item['identity']}/{label(chapter.title)}--{chapter.key[:12]}--{content_version[:12]}--{artifact_id}.cbz"
        temporary = relative + '.part'
        metadata = dict(artifact_id=artifact_id, chapter_key=chapter.key, identity=[chapter.source_id, chapter.comic_id, chapter.group_id, chapter.chapter_id],
                        manifest_version=manifest.version, content_version=content_version, comic_title=item['title'], source_api_major=1,
                        core_version=os.getenv('APP_VERSION', 's2-dev'), plugin_version=self.registry.get(item['source_id']).plugin_version)
        self.update(job_id, phase='打包 CBZ')
        sha = build_archive(safe_path(self.cbz, temporary), chapter, attempt['receipts'], metadata, self.downloads)
        artifact = dict(metadata, id=artifact_id, job_id=job_id, attempt_id=attempt['id'], relative_path=relative,
                        temporary_path=temporary, sha256=sha, source_revision=chapter.source_revision, stage='prepared')
        if not verify_archive(safe_path(self.cbz, temporary), artifact):
            raise CoreError('STORAGE_ERROR', 'CBZ 回读校验失败，未发布或记为完成')
        self.store.put('artifact', artifact_id, artifact)
        self.fault('prepared')
        publish(self.cbz, artifact)
        self.fault('published')
        self.update(job_id, phase='提交完成记录')
        self.store.commit_artifact(artifact)
        self.fault('committed')
        self.cleanup(artifact)
        self.update(job_id, event='CBZ 已保存：' + chapter.title)

    def cleanup(self, artifact):
        directory = safe_path(self.downloads, '.core-v1/' + artifact['attempt_id'])
        try:
            if directory.exists(): shutil.rmtree(directory)
        except OSError:
            self.update(artifact['job_id'], event='CBZ 已完成，临时文件暂未清理；下次启动重试清理')
            return
        artifact['cleanup_done'] = True
        self.store.put('artifact', artifact['id'], artifact)

    def recover(self, startup=False, job_id=None):
        with self.guard:
            for artifact in self.store.all('artifact'):
                if job_id and artifact['job_id'] != job_id: continue
                if artifact['stage'] == 'prepared':
                    if not safe_path(self.cbz, artifact['relative_path']).exists() and not safe_path(self.cbz, artifact['temporary_path']).exists():
                        artifact['stage'] = 'abandoned_missing'
                        self.store.put('artifact', artifact['id'], artifact)
                        self.update(artifact['job_id'], status='interrupted', local_reconciled=True,
                                    error='已核对发布意图：临时和最终 CBZ 均缺失，请重试下载', error_code='ARTIFACT_MISSING')
                        continue
                    try:
                        publish(self.cbz, artifact)
                        self.store.commit_artifact(artifact)
                        self.cleanup(artifact)
                        self.update(artifact['job_id'], local_reconciled=True, error='', error_code='')
                    except CoreError as error:
                        self.update(artifact['job_id'], status='recovery_required', error_code=error.code, error=error.message, local_reconciled=False)
                elif artifact['stage'] == 'committed':
                    current = self.store.download_record(artifact['chapter_key'])
                    if not current or current['artifact_id'] != artifact['id']: continue
                    if not verify_archive(safe_path(self.cbz, artifact['relative_path']), artifact):
                        self.store.invalidate(artifact)
                        old = self.store.job(artifact['job_id'])
                        active = next((j for j in self.store.jobs() if j['identity'] == old['identity'] and j['status'] in ACTIVE), None)
                        if active is None:
                            active = dict(old, id=uid(), done_keys=[], chapters_done=0, successor_job_id=None)
                        self.store.save_job(dict(active, status='recovery_required', phase='需要恢复', error_code='ARTIFACT_REPAIR_REQUIRED',
                                                 error='已有 CBZ 缺失或损坏，请核对并修复', local_reconciled=True))
                    elif not artifact.get('cleanup_done'):
                        self.cleanup(artifact)
            if startup:
                for job in self.store.jobs():
                    if job['status'] in ('waiting', 'running', 'retry_wait'):
                        complete = job['plan'] is not None and set(c['chapter_key'] for c in job['plan']).issubset(job['done_keys'])
                        self.update(job['id'], status='completed' if complete else 'interrupted', phase='已恢复完成' if complete else '容器已重启，可重试', finished_at=time.time())

    def retry(self, job_id):
        with self.guard:
            job = self.store.job(job_id)
            if not job: raise CoreError('NOT_FOUND', '任务不存在')
            if job.get('successor_job_id'): return self.store.job(job['successor_job_id'])
            if job['status'] == 'recovery_required':
                self.recover(job_id=job_id)
                job = self.store.job(job_id)
                pending = any(a['job_id'] == job_id and a['stage'] == 'prepared' for a in self.store.all('artifact'))
                if pending or not job.get('local_reconciled', True):
                    raise CoreError('LOCAL_RECONCILIATION_REQUIRED', '发布意图尚不能核对，未开始网络修复')
                if job['plan'] is not None and set(c['chapter_key'] for c in job['plan']).issubset(job['done_keys']) and job.get('error_code') != 'ARTIFACT_REPAIR_REQUIRED':
                    return self.update(job_id, status='completed', phase='本地恢复完成', error='', error_code='')
            elif job['status'] not in ('failed', 'interrupted', 'stopped'):
                raise CoreError('CONFLICT', '当前任务不可重试')
            with self.store.transaction():
                self.store.save_job(dict(job, status='stopped', outcome='handed_off_to_retry'))
                # enqueue performs its own transaction; construct after old slot is released in this transaction.
                item = self.store.subscription(job['subscription_id'])
                if not item or item.get('paused') or item.get('cancelled'):
                    raise CoreError('SOURCE_UNAVAILABLE', '订阅已暂停或取消')
                self.registry.get(item['source_id'])
                new_id = uid()
                new_job = dict(job, id=new_id, status='waiting', phase='等待重新检查', plan=None, done_keys=[], chapters_done=0,
                               chapters_total=None, images_done=0, images_total=None, bytes_downloaded=0, reused_images=0,
                               error='', error_code='', parent_id=job_id, repair=True, successor_job_id=None,
                               stop_after_chapter=False, retry_at=0, started_at=None, finished_at=None, created_at=time.time())
                self.store.save_job(new_job)
                self.store.save_job(dict(job, status='stopped', outcome='handed_off_to_retry', successor_job_id=new_id))
            self.wake.set(); return new_job

    def stop(self, job_id):
        with self.guard:
            job = self.store.job(job_id)
            if not job: raise CoreError('NOT_FOUND', '任务不存在')
            if job['status'] == 'recovery_required':
                self.recover(job_id=job_id)
                if any(a['job_id'] == job_id and a['stage'] == 'prepared' for a in self.store.all('artifact')):
                    raise CoreError('LOCAL_RECONCILIATION_REQUIRED', '先核对发布意图，未丢弃恢复材料')
                return self.update(job_id, status='stopped', phase='修复已取消')
            if job['status'] in ('waiting', 'blocked'):
                return self.update(job_id, status='stopped', phase='已取消排队', finished_at=time.time())
            return self.update(job_id, stop_after_chapter=True)

    def start(self):
        def work():
            while not self.shutdown.is_set():
                job = next((j for j in self.store.jobs() if j['status'] == 'waiting' or (j['status'] == 'retry_wait' and j.get('retry_at', 0) <= time.time())), None)
                if job:
                    try: self.run_job(job['id'])
                    except Exception: pass  # run_job persists failure; keep the worker alive.
                else:
                    self.wake.wait(.25); self.wake.clear()
        self.worker = threading.Thread(target=work, daemon=True); self.worker.start()

    def close(self):
        self.shutdown.set(); self.wake.set()
        if self.worker: self.worker.join(60)
        if self.worker and self.worker.is_alive():
            raise CoreError('STORAGE_ERROR', '下载请求仍在结束中，未提前关闭存储')
        for context in self.runtime.values(): context['session'].close()
        self.runtime.clear()
