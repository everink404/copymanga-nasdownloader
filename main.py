import logging
import traceback

from dispatcher import DownloadDispatcher
from updater import updater
from utils import config
from utils.task_status import progress
from utils.log import configure_logging
from utils.notify import notifier

configure_logging()

log = logging.getLogger(__name__)


def main(selection=None):
    config.reload()
    notifier.clear()

    try:
        all_tasks = updater.process_updates(selection)

        if not all_tasks:
            if notifier.errors:
                log.warning("更新检查存在失败，不能确认所有订阅均无更新；请查看错误记录")
            else:
                log.info("没有发现需要下载的更新")
            return

        log.info(f"发现来自 {len(set(t['site'] for t in all_tasks))} 个站点的更新任务")

        DownloadDispatcher.download_site(all_tasks)

    except Exception as e:
        log.error(f"主程序运行出错: {e}")
        log.debug(traceback.format_exc())
        notifier.add_error("System", "Main Loop", str(e))

    finally:
        if notifier.errors:
            progress(error='；'.join(notifier.errors)[-1000:])
        notifier.flush()


if __name__ == '__main__':
    main()
