import time
import threading
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
import httpx
from .models import CoreError


@dataclass
class Budget:
    used: int = 0
    maximum: int = 6
    redirect_url: str | None = None

    def take(self):
        if self.used >= self.maximum:
            raise CoreError('RETRY_EXHAUSTED', '请求次数已达上限，请手动重试')
        self.used += 1


class Transport:
    """Only this class performs source HTTP; no cookies/credentials across hosts."""
    shared_lock = threading.RLock()
    shared_due = {}
    shared_cooldown = {}

    def __init__(self, client=None, proxy=None, api_rate=12, image_rate=60, scope=None):
        self.client = client or httpx.Client(proxy=proxy or None, timeout=20, follow_redirects=False, trust_env=False)
        self.rates = {'api': api_rate, 'image': image_rate}
        self.scope = scope
        self.requests = 0

    def get(self, url, budget, role='image', allowed_hosts=(), headers=None):
        initial = urlparse(url)
        if initial.scheme != 'https' or initial.hostname not in allowed_hosts or initial.username or initial.password:
            raise CoreError('PROTOCOL_ERROR', '请求地址不在源声明的范围内')
        request_headers = dict(headers or {})
        if any(k.lower() in ('authorization', 'cookie', 'proxy-authorization') for k in request_headers):
            raise CoreError('PROTOCOL_ERROR', '图片请求禁止账号凭据')
        scope = self.scope or initial.hostname
        url = budget.redirect_url or url
        for redirect in range(6):
            with self.shared_lock:
                now = time.time()
                due = max(self.shared_cooldown.get(scope, 0), self.shared_due.get((scope, role), 0))
                if due > now:
                    raise CoreError('RATE_LIMIT', '等待请求限流', retry_at=due)
                budget.take()
                rate = self.rates.get(role, 12)
                self.shared_due[(scope, role)] = now + (60 / rate if rate else 0)
            self.requests += 1
            try:
                with self.client.stream('GET', url, headers=request_headers, timeout=45 if role == 'image' else 20) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        target = str(response.url.join(response.headers.get('location', '')))
                        parsed = urlparse(target)
                        if parsed.scheme != 'https' or parsed.hostname not in allowed_hosts or parsed.username or parsed.password or redirect == 5:
                            raise CoreError('PROTOCOL_ERROR', '拒绝不安全的重定向')
                        url = target; budget.redirect_url = target
                        continue
                    if response.status_code == 429:
                        value = response.headers.get('retry-after', '')
                        try: delay = max(0, int(value))
                        except ValueError:
                            try: delay = max(0, parsedate_to_datetime(value).timestamp() - time.time())
                            except (ValueError, TypeError, OverflowError): delay = 60
                        retry_at = time.time() + delay
                        with self.shared_lock: self.shared_cooldown[scope] = retry_at
                        raise CoreError('RATE_LIMIT', '站点限流，等待后重试', retry_at=retry_at)
                    if response.status_code >= 500:
                        raise CoreError('UPSTREAM_5XX', '上游暂时不可用', retry_at=time.time() + (3 if budget.used == 1 else 10))
                    if response.status_code == 210:
                        raise CoreError('SITE_RESTRICTED', '源站限制了当前请求')
                    if response.status_code != 200:
                        raise CoreError('HTTP_ERROR', f'图片请求失败（HTTP {response.status_code}）')
                    chunks, count = [], 0
                    for chunk in response.iter_bytes():
                        count += len(chunk)
                        if count > 25 * 1024 * 1024:
                            raise CoreError('INVALID_IMAGE', '图片响应超出单页限制')
                        chunks.append(chunk)
                    budget.redirect_url = None
                    return b''.join(chunks)
            except httpx.RequestError:
                raise CoreError('NETWORK', '图片连接失败', retry_at=time.time() + (3 if budget.used == 1 else 10)) from None
        raise CoreError('PROTOCOL_ERROR', '重定向次数过多')

    def close(self):
        self.client.close()
