import logging
import os
import time
from typing import List, Dict, Any

from downloader import downloader, postprocess
from plugins.copymanga.headers import HEADERS
from plugins.copymanga.login import loginhelper
from updater import updater
from utils import config
from utils import copymanga_web
from utils.notify import notifier
from utils.rename import rename_series
from utils.request import RequestHandler

log = logging.getLogger(__name__)

request = RequestHandler(headers=HEADERS, proxy=config.CM_PROXY, copymanga=True)


def get_chapter(path_word: str, uuid: str):
    """获取章节详情"""
    data = request.get(f"/api/v3/comic/{path_word}/chapter2/{uuid}")
    if data is None:
        if request.last_status_code == 210:
            log.warning('章节 API 返回 210, 尝试官网公开章节页: %s/%s', path_word, uuid)
            try:
                return copymanga_web.chapter_contents(path_word, uuid)
            except copymanga_web.DirectoryError as error:
                log.error('官网章节回退失败: %s', error)
        log.error(f"漫画章节请求失败：{path_word}/{uuid}")
        return None
    try:
        chapter = data.json()['results']['chapter']
        if not isinstance(chapter, dict):
            raise ValueError("章节详情不是对象")
        return chapter
    except Exception as e:
        log.error(f"漫画章节内容解析失败：{e}")
        return None


def download_chapter(task: Dict[str, Any], uuid: str, chapter_name: str, image_handler=None):
    """下载单个章节"""
    chapter = get_chapter(task['path_word'], uuid)
    if not chapter:
        log.error(f"无法获取章节 {chapter_name} (UUID: {uuid}) 的内容")
        reason = '获取章节内容失败'
        if request.last_status_code == 210:
            reason = 'API 返回 210, 官网也未取得完整图片列表; 未下载或更新完成记录。'
        notifier.add_error("copymanga", f"{task['name']} - {chapter_name}", reason)
        return False

    contents = chapter.get('contents')
    words = chapter.get('words')
    if not contents or not isinstance(contents, list) or not isinstance(words, list) or len(contents) != len(words):
        log.error(f"章节图片列表无效：{task['name']} - {chapter_name}")
        notifier.add_error("copymanga", f"{task['name']} - {chapter_name}", "章节图片列表无效")
        return False

    current_name = chapter_name
    log.info(f"已获取到 {task['name']} {current_name} 的内容，开始安排下载")

    # Web pages have their own order; never reuse partial API pages by filename.
    directory_name = current_name + '.__website' if chapter.get('_source') == 'website' else current_name
    save_path = os.path.join(config.DOWNLOAD_PATH, task['name'], directory_name)
    os.makedirs(save_path, exist_ok=True)

    # 下载所有图片
    download_failed = False
    for index, url in enumerate(contents):
        image_path = os.path.join(save_path, f"{chapter['words'][index]:04d}.jpg")
        full_url = url['url'].replace("c800x.jpg", "c1500x.jpg").replace("c800x.webp", "c1500x.webp")

        options = {}
        if image_handler is not None:
            options = {'request_handler': image_handler, 'headers': {
                'Referer': config.CM_WEB_URL.rstrip('/') + '/comic/' + task['path_word'] + '/chapter/' + uuid}}
        if downloader(full_url, image_path, **options):
            log.info(f"已下载 {task['name']} {current_name} {chapter['words'][index]:04d}.jpg")
        else:
            log.error(f"下载失败: {task['name']} {current_name} {chapter['words'][index]:04d}.jpg")
            download_failed = True

    if download_failed:
        notifier.add_error("copymanga", f"{task['name']} - {current_name}", "部分图片下载失败")
        log.error(f"保留章节临时目录以便下次补下载：{save_path}")
        return False

    log.info(f"{task['name']} {current_name} 下载完成，开始进行cbz打包")

    # 文件重命名处理
    if not config.USE_CM_CNAME:
        chapter_filename, chapter_num, is_special = rename_series(
            current_name, task['ep_pattern'], task['vol_pattern'])
    else:
        chapter_filename, chapter_num, is_special = current_name, 0, False

    postprocess(
        task['name'], current_name,
        chapter_filename, chapter_num, save_path, is_special
    )

    # 更新下载记录
    updater.update_chapter_record(
        task['site'], task['path_word'], current_name, group_word=task.get('group_word', 'default')
    )

    notifier.add_success("copymanga", task['name'], current_name)
    log.info(f"{task['name']} {current_name} cbz打包完成")
    return True


def download_task(task: Dict[str, Any], image_handler=None):
    """处理单个漫画任务的所有章节下载"""
    if not task.get('chapter_infos'):
        log.info(f"{task['name']} 没有待下载章节")
        return True

    log.info(f"开始处理 {task['name']} 的 {len(task['chapter_infos'])} 个章节")

    for uuid, name in task['chapter_infos']:
        try:
            success = download_chapter(task, uuid, name, image_handler=image_handler)
            if not success:
                log.error(f"章节下载失败: {task['name']} {name}, UUID: {uuid}")
                # latest_chapter 是顺序游标，不能越过未完成的章节。
                log.warning('%s 本次下载未完成, 保留进度供下次重试。', task['name'])
                return False
        except Exception as e:
            log.error(f"章节处理异常: {e}")
            notifier.add_error("copymanga", f"{task['name']} - {name}", str(e))
            return False
        time.sleep(3)

    log.info(f"{task['name']} 需要更新的下载已完成")
    return True


def download_batch(tasks: List[Dict[str, Any]]):
    """批量处理多个下载任务"""
    if not tasks:
        log.info("当前没有需要更新的内容")
        return

    global request
    headers = HEADERS.copy()
    token = config.CM_TOKEN
    if config.CM_USERNAME and config.CM_PASSWORD:
        token = loginhelper(username=config.CM_USERNAME, password=config.CM_PASSWORD, url=config.CM_API_URL)
        if not token:
            notifier.add_error('copymanga', '下载登录', '账号登录失败，未开始下载或更改完成记录')
            return
    headers['authorization'] = (token if token.startswith(('Token ', 'Bearer ')) else f'Token {token}') if token else ''
    previous_request = request
    request = RequestHandler(headers=headers, proxy=config.CM_PROXY, copymanga=True)
    image_handler = None
    try:
        # CDN requests share the current proxy, but must not receive the account token.
        image_handler = RequestHandler(headers={'User-Agent': HEADERS['User-Agent']}, proxy=config.CM_PROXY)
        log.info(f"检测到 {len(tasks)} 个漫画有更新内容")

        completed = 0
        for task in tasks:
            debug_uuids = "\n".join([f"  - {uuid} ({name})" for uuid, name in task.get('chapter_infos', [])])

            debug = (
                f"漫画名称: {task['name']}\n"
                f"路径标识: {task['path_word']}\n"
                f"当前章节: {task['current_chapter']}\n"
                f"待更新数: {len(task.get('chapter_infos', []))}\n"
                f"UUID列表:\n{debug_uuids}"
            )
            # 打印任务信息
            info = (
                f"漫画名称: {task['name']}\n"
                f"当前章节: {task['current_chapter'] or '无'}\n"
                f"待更新数: {len(task.get('chapter_infos', []))}"
            )
            log.info(info)
            log.debug(debug)

            # 开始下载该漫画
            if download_task(task, image_handler=image_handler):
                completed += 1

        log.info('漫画下载处理结束: 完成 %s 部, 未完成 %s 部。', completed, len(tasks) - completed)

    finally:
        if image_handler is not None:
            image_handler.client.close()
        request.client.close()
        request = previous_request
