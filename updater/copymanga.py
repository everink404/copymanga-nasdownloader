import logging
from typing import Dict, List, Any, Tuple

from utils import copymanga_web
from .base import BaseUpdater

log = logging.getLogger(__name__)



class CopyMangaUpdater(BaseUpdater):
    SITE_NAME = "copymanga"
    ID_FIELD = "path_word"  # 唯一标识符字段
    REQUIRED_FIELDS = BaseUpdater.REQUIRED_FIELDS + [
        'name', 'path_word', 'group_word', 'ep_pattern', 'vol_pattern'
    ]

    def get_chapters(self, record: Dict) -> List[Dict]:
        # Force a fresh complete directory for each scheduled check.
        return copymanga_web.group_chapters(record['path_word'], record['group_word'], refresh=True)

    def find_subsequent_uuids(self, chapters: List[Dict], target_chapter: str) -> List[Tuple[str, str]]:
        sorted_chapters = sorted(chapters, key=lambda x: x['index'])
        names = [chapter['name'] for chapter in sorted_chapters]
        if len(set(names)) != len(names):
            raise copymanga_web.DirectoryError('目录存在同名章节，无法安全定位完成记录。')
        if target_chapter:
            target_index = -1
            for i, chapter in enumerate(sorted_chapters):
                if chapter['name'] == target_chapter:
                    target_index = i
                    break

            if target_index == -1:
                raise copymanga_web.DirectoryError('目录中找不到最后完成的章节，未更改订阅记录。')
            if target_index == len(sorted_chapters) - 1:
                return []
            return [(chap['uuid'], chap['name']) for chap in sorted_chapters[target_index + 1:]]

        return [(chap['uuid'], chap['name']) for chap in sorted_chapters]

    def create_download_task(self, record: Dict, chapter_infos: List[Tuple[str, str]]) -> Dict[str, Any]:
        return {
            "site": self.SITE_NAME,
            "path_word": record['path_word'],
            "name": record['name'],
            "chapter_infos": chapter_infos,
            "group_word": record['group_word'],
            "current_chapter": record['latest_chapter'],
            "ep_pattern": record['ep_pattern'],
            "vol_pattern": record['vol_pattern'],
        }
