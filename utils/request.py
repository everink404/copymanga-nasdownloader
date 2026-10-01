import logging
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from threading import Lock

import httpx

from utils import config

log = logging.getLogger(__name__)


class CopyMangaRateLimiter:
    """所有 CopyManga API Handler 共享的进程级限流器。"""

    def __init__(self):
        self.lock = Lock()
        self.last_request = None

    def wait(self):
        with self.lock:
            rate = config.CM_RATE_LIMIT_PER_MINUTE
            if rate == 0:
                return
            if self.last_request is not None:
                delay = 60.0 / rate - (time.monotonic() - self.last_request)
                if delay > 0:
                    time.sleep(delay)
            self.last_request = time.monotonic()


cm_rate_limiter = CopyMangaRateLimiter()


def retry_after_seconds(value):
    """支持 Retry-After 的秒数和 HTTP 日期；无效值回退到 60 秒。"""
    if value:
        try:
            return max(0, int(value))
        except (ValueError, TypeError):
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(0, (retry_at - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
    return 60

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36 Edg/137.0.0.0",
    "accept-encoding": "gzip",
}


class RequestHandler:
    def __init__(self, retries=3, delay=3, timeout=10, proxy=None, headers=None, copymanga=False):
        """
        :param retries: 最大重试次数
        :param delay: 每次重试的间隔时间（秒）
        :param timeout: 请求超时时间（秒）
        """
        self.proxy = proxy or {}
        self.headers = headers or HEADERS
        self.retries = retries
        self.delay = delay
        self.timeout = timeout
        self.copymanga = copymanga
        self.api = config.CM_API_URL.rstrip('/')  # 确保API地址没有结尾斜杠
        self.last_status_code = None
        self.last_error_kind = None
        self.last_message = None
        
        # httpx proxy format: {"http://": "...", "https://": "..."} or just a string
        mounts = {}
        if self.proxy:
            if isinstance(self.proxy, dict):
                for proto, url in self.proxy.items():
                    if url:
                        mounts[f"{proto}://"] = httpx.HTTPTransport(proxy=url)
            elif isinstance(self.proxy, str):
                mounts = {"all://": httpx.HTTPTransport(proxy=self.proxy)}

        self.client = httpx.Client(
            headers=self.headers,
            timeout=self.timeout,
            mounts=mounts,
            follow_redirects=True
        )

    def _build_url(self, url: str) -> str:
        """智能构建完整URL"""
        if url.lower().startswith(('http://', 'https://')):
            return url
        return f"{self.api}/{url.lstrip('/')}"

    def request(self, method, url, **kwargs):
        full_url = self._build_url(url)
        self.last_status_code = None
        self.last_error_kind = None
        self.last_message = None

        for attempt in range(1, self.retries + 1):
            try:
                if self.copymanga:
                    cm_rate_limiter.wait()
                # 保持 headers 同步，因为有些插件会动态修改 Handler.headers
                self.client.headers.update(self.headers)
                response = self.client.request(method, full_url, **kwargs)
                self.last_status_code = response.status_code
                self.last_error_kind = 'http'

                if response.status_code in (200, 201, 202):
                    self.last_error_kind = None
                    return response
                
                if response.status_code == 210:
                    try:
                        payload = response.json()
                        detail = payload.get('results', {}).get('detail')
                        message = payload.get('message') or detail
                    except (ValueError, AttributeError):
                        message = response.text
                    self.last_message = str(message or '')[:700]
                    log.error(
                        f"[{method}] 请求失败 (状态码: 210)，URL: {full_url}，信息为：{message}；跳过当前请求，请按站点说明处理")
                    return None

                if response.status_code == 429:
                    if attempt < self.retries:
                        delay = retry_after_seconds(response.headers.get('Retry-After'))
                        log.warning(f"[{method}] 请求限流 (429)，URL: {full_url}，等待 {delay} 秒后重试 ({attempt}/{self.retries})")
                        time.sleep(delay)
                        continue
                    break
                    
                log.warning(
                    f"[{method}] 请求失败 (状态码: {response.status_code})，URL: {full_url}，尝试第 {attempt}/{self.retries} 次...")

            except httpx.RequestError as e:
                self.last_status_code = None
                self.last_error_kind = 'network'
                if isinstance(e, httpx.ProxyError) and '407' in str(e):
                    self.last_error_kind = 'proxy_auth'
                log.warning(f"[{method}] 请求异常: {e}，URL: {full_url}，尝试第 {attempt}/{self.retries} 次...")

            if attempt < self.retries:
                time.sleep(self.delay)

        log.error(f"[{method}] 请求失败: 超过最大重试次数 ({self.retries})，URL: {full_url}")
        return None

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)
