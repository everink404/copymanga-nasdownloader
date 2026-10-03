"""Serial web jobs, bounded persistent history and thread-local download progress."""
import copy
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from utils import config

context = threading.local()


def progress(**fields):
    manager = getattr(context, 'manager', None)
    if manager:
        manager.update(context.job_id, **fields)


def stopping():
    manager = getattr(context, 'manager', None)
    return bool(manager and manager.is_stopping(context.job_id))


class TaskManager:
    def __init__(self, runner, path=None):
        self.runner = runner
        self.path = path or os.path.join(config.DATA_PATH, 'download_tasks.json')
        self.lock = threading.RLock()
        self.jobs = []
        self.worker = None
        try:
            with open(self.path, encoding='utf-8') as stream:
                self.jobs = json.load(stream)[-100:]
            for job in self.jobs:
                if job['status'] in ('running', 'waiting'):
                    job.update(status='interrupted', error='容器已重启，可重试以补下载', finished_at=self.now())
            self.save()
        except (OSError, ValueError, KeyError, TypeError):
            self.jobs = []

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path + '.tmp', 'w', encoding='utf-8') as stream:
            json.dump(self.jobs, stream, ensure_ascii=False, indent=2)
        os.replace(self.path + '.tmp', self.path)

    def enqueue(self, record, origin='manual'):
        with self.lock:
            key = (record['path_word'], record.get('group_word', 'default'))
            for job in self.jobs:
                if (job['path_word'], job['group_word']) == key and job['status'] in ('waiting', 'running'):
                    return copy.deepcopy(job)
            job = dict(id=uuid.uuid4().hex, path_word=key[0], group_word=key[1], name=record['name'],
                       cover=record.get('cover', ''), status='waiting', phase='等待执行', origin=origin,
                       created_at=self.now(), started_at=None, finished_at=None, chapter='',
                       chapters_done=0, chapters_total=None, images_done=0, images_total=None,
                       bytes_downloaded=0, speed=None, error='', stop_requested=False, events=[])
            self.jobs.append(job)
            # Never drop queued jobs to make room for history.
            active = [j for j in self.jobs if j['status'] in ('waiting', 'running')]
            history = [j for j in self.jobs if j['status'] not in ('waiting', 'running')][-100:]
            self.jobs = history + active
            self.save()
            if not self.worker or not self.worker.is_alive():
                self.worker = threading.Thread(target=self.run, daemon=True)
                self.worker.start()
            return copy.deepcopy(job)

    def update(self, job_id, **fields):
        with self.lock:
            job = next(j for j in self.jobs if j['id'] == job_id)
            byte_delta = fields.pop('byte_delta', 0)
            if byte_delta:
                job['bytes_downloaded'] += byte_delta
            event = fields.pop('event', None)
            if event:
                job['events'] = (job['events'] + [dict(time=self.now(), text=str(event))])[-50:]
            job.update(fields)
            self.save()

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.jobs)

    def is_stopping(self, job_id):
        with self.lock:
            return next(j for j in self.jobs if j['id'] == job_id)['stop_requested']

    def stop(self, job_id):
        with self.lock:
            job = next((j for j in self.jobs if j['id'] == job_id), None)
            if not job:
                raise KeyError(job_id)
            if job['status'] == 'waiting':
                self.update(job_id, status='stopped', phase='已取消排队', finished_at=self.now())
            elif job['status'] == 'running':
                self.update(job_id, stop_requested=True, event='将在当前章节完成后停止')

    def run(self):
        while True:
            with self.lock:
                job = next((j for j in self.jobs if j['status'] == 'waiting'), None)
                if not job:
                    self.worker = None
                    return
                self.update(job['id'], status='running', phase='检查目录', started_at=self.now())
            context.manager, context.job_id = self, job['id']
            context.started = time.monotonic()
            try:
                self.runner(job)
                with self.lock:
                    status = 'failed' if job['error'] else ('stopped' if job['stop_requested'] else 'completed')
                self.update(job['id'], status=status, phase={'failed': '失败', 'stopped': '已停止', 'completed': '已完成'}[status],
                            speed=None, finished_at=self.now())
            except Exception:
                self.update(job['id'], status='failed', error='任务执行异常，请查看运行日志', finished_at=self.now())
            finally:
                context.manager = None
