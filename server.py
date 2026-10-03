import json
import logging
import os
from datetime import datetime
from typing import Dict, Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Literal

import copymanga_browse

from utils import config

UPDATER_JSON_PATH = os.path.join(config.DATA_PATH, "updater.json")
WEB_CONFIG_PATH = os.path.join(config.DATA_PATH, "web_config.json")  # 定时任务持久化文件
SYSTEM_CONFIG_PATH = os.path.join(config.DATA_PATH, "system_config.json")  # 系统配置持久化文件

import main
from utils import config, log as log_utils
from updater.updater import SITE_MAPPING

log_utils.configure_logging()
log = logging.getLogger("WebServer")

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_scheduler()
    yield
    if scheduler.running:
        scheduler.shutdown()

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

scheduler = BackgroundScheduler()


def run_subscription_job(job):
    records = get_config().get('copymanga', [])
    record = next((r for r in records if r.get('path_word') == job['path_word'] and r.get('group_word', 'default') == job['group_word']), None)
    if not record or record.get('paused'):
        raise ValueError('订阅已取消或暂停')
    main.main((job['path_word'], job['group_word']))


from utils.task_status import TaskManager
task_manager = TaskManager(run_subscription_job)


def run_downloader_task():
    for record in get_config().get('copymanga', []):
        if not record.get('paused'):
            task_manager.enqueue(record, 'schedule')


def load_schedule_config():
    if os.path.exists(WEB_CONFIG_PATH):
        try:
            with open(WEB_CONFIG_PATH, 'r') as f:
                return json.load(f)
        except:
            pass
    return {}


def save_schedule_config(cfg):
    with open(WEB_CONFIG_PATH, 'w') as f:
        json.dump(cfg, f)


def init_scheduler():
    cfg = load_schedule_config()
    log.info(f"正在加载定时任务配置: {cfg}")

    # 清理旧任务
    scheduler.remove_all_jobs()

    if cfg.get("cron"):
        try:
            scheduler.add_job(
                run_downloader_task,
                CronTrigger.from_crontab(cfg["cron"]),
                id="main_task"
            )
            log.info(f"已恢复 Cron 任务: {cfg['cron']}")
        except Exception as e:
            log.error(f"恢复 Cron 任务失败: {e}")

    if cfg.get('type') == 'interval' and (cfg.get('days', 0) or cfg.get('hours', 0)):
        scheduler.add_job(run_downloader_task, IntervalTrigger(days=cfg.get('days', 0), hours=cfg.get('hours', 0)), id='main_task')
    if not scheduler.running:
        scheduler.start()


@app.get("/api/schedule/config")
def get_schedule_config():
    return load_schedule_config()


@app.post("/api/schedule/cron")
def set_cron_schedule(data: Dict[str, str] = Body(...)):
    cron_expression = data.get("cron")
    try:
        trigger = CronTrigger.from_crontab(cron_expression)
        scheduler.remove_all_jobs()
        scheduler.add_job(run_downloader_task, trigger, id="main_task")

        save_schedule_config({"cron": cron_expression})

        return {"status": "success", "message": "定时任务已更新并保存"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cron 格式错误: {e}")


@app.get("/api/schema")
def get_dynamic_schema():
    schema = {}
    for site_key, site_class in SITE_MAPPING.items():
        if site_key != "copymanga":
            continue
        try:
            field_meta = site_class.get_field_meta()
            schema[site_key] = {
                "name": site_key,
                "fields": field_meta,
                "id_field": site_class.ID_FIELD
            }
        except Exception as e:
            log.error(f"生成站点 {site_key} 的Schema失败: {e}")
    return schema


@app.get("/api/config")
def get_config():
    if not os.path.exists(UPDATER_JSON_PATH):
        return {}
    try:
        with open(UPDATER_JSON_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        return {}


class CopyMangaSubscription(BaseModel):
    path_word: str
    group_word: str = 'default'
    name: str = ''
    mode: Literal['all', 'future', 'from']
    chapter_uuid: str = ''


@app.get('/api/copymanga/browse')
def browse_copymanga(q: str = '', rank: str = 'day', offset: int = 0, limit: int = 20,
                    mode: str = 'rank', theme: str = '', ordering: str = '-datetime_updated',
                    region: str = '', status: str = '', q_type: str = ''):
    if not 0 <= offset <= 1000000 or not 1 <= limit <= 50 or len(q) > 100 or len(theme) > 160:
        raise HTTPException(400, '搜索参数无效')
    return copymanga_browse.browse(q, rank, offset, limit, mode, theme, ordering, region, status, q_type)


@app.get('/api/copymanga/filters')
def copymanga_filters():
    return copymanga_browse.filters()


@app.get('/api/copymanga/comics/{path_word}')
def copymanga_detail(path_word: str):
    return copymanga_browse.detail(path_word)


@app.get('/api/copymanga/comics/{path_word}/groups/{group_word}/chapters')
def copymanga_chapters(path_word: str, group_word: str, offset: int = 0, limit: int = 100):
    if not 0 <= offset <= 10000 or not 1 <= limit <= 500:
        raise HTTPException(400, '目录参数无效')
    return copymanga_browse.chapters(path_word, group_word, offset, limit)


@app.post('/api/copymanga/subscriptions')
def add_copymanga_subscription(data: CopyMangaSubscription):
    return copymanga_browse.subscribe(data, UPDATER_JSON_PATH)


@app.post("/api/config")
def save_config(config_data: Dict[str, Any]):
    try:
        with copymanga_browse._write_lock:
            temporary = UPDATER_JSON_PATH + '.config.tmp'
            with open(temporary, 'w', encoding='utf-8') as f:
                json.dump(config_data, f, indent=2, ensure_ascii=False)
            os.replace(temporary, UPDATER_JSON_PATH)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/settings/system")
def get_system_settings():
    return {
        "download_path": config.DOWNLOAD_PATH,
        "cbz_path": config.CBZ_PATH,
        "use_cm_cname": config.USE_CM_CNAME,
        "log_level": config.LOG_LEVEL,
        "api_url": config.CM_API_URL,
        "cm_web_url": config.CM_WEB_URL,
        "cm_username": config.CM_USERNAME,
        "cm_password": config.CM_PASSWORD,
        "cm_proxy": config.CM_PROXY.get('http', ''),
        "push_enable": config.PUSH_ENABLE,
        "push_server": config.PUSH_SERVER,
        "push_user": config.PUSH_USER,
        "push_token": config.PUSH_TOKEN,
        "push_summary_only": config.PUSH_SUMMARY_ONLY,
        "push_markdown": config.PUSH_MARKDOWN
    }


@app.post("/api/settings/system")
def save_system_settings(settings: Dict[str, Any]):
    try:
        with open(SYSTEM_CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)

        config.reload()

        return {"status": "success", "message": "系统配置已保存并生效"}
    except Exception as e:
        log.error(f"保存系统配置失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get('/api/tasks')
def list_tasks():
    return {'items': task_manager.snapshot()}


@app.post('/api/tasks/{job_id}/stop')
def stop_task(job_id: str):
    try:
        task_manager.stop(job_id)
    except KeyError:
        raise HTTPException(404, '任务不存在')
    return {'status': 'success'}


@app.post('/api/tasks/{job_id}/retry')
def retry_task(job_id: str):
    job = next((j for j in task_manager.snapshot() if j['id'] == job_id), None)
    if not job:
        raise HTTPException(404, '任务不存在')
    if job['status'] not in ('failed', 'interrupted', 'stopped'):
        raise HTTPException(409, '仅可重试失败、中断或停止的任务')
    return queue_subscription(job['path_word'], job['group_word'])


def queue_subscription(path_word, group_word):
    record = next((r for r in get_config().get('copymanga', []) if r.get('path_word') == path_word and r.get('group_word', 'default') == group_word), None)
    if not record:
        raise HTTPException(404, '订阅不存在')
    if record.get('paused'):
        raise HTTPException(409, '请先恢复订阅')
    return task_manager.enqueue(record)


@app.post('/api/copymanga/subscriptions/{path_word}/{group_word}/run')
def run_subscription(path_word: str, group_word: str):
    return queue_subscription(path_word, group_word)


def require_idle(path_word, group_word):
    if any(j['path_word'] == path_word and j['group_word'] == group_word and j['status'] in ('waiting', 'running') for j in task_manager.snapshot()):
        raise HTTPException(409, '请先停止该订阅的任务，再修改范围或取消订阅')


@app.patch('/api/copymanga/subscriptions/{path_word}/{group_word}')
def patch_subscription(path_word: str, group_word: str, data: Dict[str, Any]):
    with task_manager.lock, copymanga_browse._write_lock:
        current = get_config()
        record = next((r for r in current.get('copymanga', []) if r.get('path_word') == path_word and r.get('group_word', 'default') == group_word), None)
        if not record:
            raise HTTPException(404, '订阅不存在')
        if set(data) - {'paused', 'cover'} or ('paused' in data and not isinstance(data['paused'], bool)):
            raise HTTPException(400, '配置字段无效')
        if 'cover' in data and (not isinstance(data['cover'], str) or len(data['cover']) > 2048 or (data['cover'] and not data['cover'].startswith(('https://', 'http://')))):
            raise HTTPException(400, '封面地址无效')
        record.update(data)
        if data.get('paused'):
            for job in task_manager.snapshot():
                if job['path_word'] == path_word and job['group_word'] == group_word and job['status'] == 'waiting':
                    task_manager.stop(job['id'])
        save_config(current)
    return record


@app.put('/api/copymanga/subscriptions/{path_word}/{group_word}/range')
def edit_subscription_range(path_word: str, group_word: str, data: CopyMangaSubscription):
    if data.path_word != path_word or data.group_word != group_word:
        raise HTTPException(400, '订阅标识不一致')
    with task_manager.lock:
        require_idle(path_word, group_word)
        return copymanga_browse.subscribe(data, UPDATER_JSON_PATH, replace=True)


@app.delete('/api/copymanga/subscriptions/{path_word}/{group_word}')
def delete_subscription(path_word: str, group_word: str):
    with task_manager.lock, copymanga_browse._write_lock:
        require_idle(path_word, group_word)
        current = get_config()
        records = current.get('copymanga', [])
        remaining = [r for r in records if (r.get('path_word'), r.get('group_word', 'default')) != (path_word, group_word)]
        if len(records) == len(remaining):
            raise HTTPException(404, '订阅不存在')
        current['copymanga'] = remaining
        save_config(current)
    return {'status': 'success'}


@app.post('/api/run')
def manual_run():
    jobs = [task_manager.enqueue(record) for record in get_config().get('copymanga', []) if not record.get('paused')]
    return {'status': 'started', 'items': jobs, 'message': '订阅已加入队列'}


@app.post("/api/schedule/interval")
def set_interval_schedule(days: int = Body(0), hours: int = Body(0)):
    return trigger_task(days, hours, save=True)


def trigger_task(days, hours, save=True):
    scheduler.remove_all_jobs()

    if days == 0 and hours == 0:
        if save: save_schedule_config({"type": "none"})
        return {"status": "stopped", "message": "定时任务已关闭"}

    trigger = IntervalTrigger(days=days, hours=hours)
    scheduler.add_job(run_downloader_task, trigger, id="main_task")

    if save:
        save_schedule_config({"type": "interval", "days": days, "hours": hours})

    return {"status": "scheduled", "desc": f"每 {days} 天 {hours} 小时执行一次"}


@app.get("/api/logs")
def get_logs():
    log_dir = os.path.join(config.DATA_PATH, 'log')
    if not os.path.exists(log_dir): return []
    files = sorted([f for f in os.listdir(log_dir) if f.endswith('.log')])
    if not files: return []
    latest = files[-1]
    try:
        with open(os.path.join(log_dir, latest), 'r', encoding='utf-8') as f:
            return f.readlines()[-200:]
    except Exception as e:
        log.error(f"读取日志文件失败: {e}")
        return [f"读取日志出错: {str(e)}"]


if os.path.exists("spa_dist"):
    app.mount("/", StaticFiles(directory="spa_dist", html=True), name="static")
