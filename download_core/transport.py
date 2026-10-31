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

    def __init__(self, client=None, proxy=None, api_rate=12, image_rate=60, scope=None, auth_hosts=()):
        self.client = client or httpx.Client(proxy=proxy or None, timeout=20, follow_redirects=False, trust_env=False)
        self.rates = {'api': api_rate, 'image': image_rate, 'cover':120}
        self.scope = scope
        self.auth_hosts = set(auth_hosts)
        self.requests = 0
        self.last_url=None
        self.origin_cookies={}

    def get(self, url, budget, role='image', allowed_hosts=(), headers=None):
        return self.request(url, budget, role, allowed_hosts, headers)

    def request(self, url, budget, role='api', allowed_hosts=(), headers=None, method='GET', data=None):
        initial = urlparse(url)
        if initial.scheme != 'https' or initial.hostname not in allowed_hosts or initial.username or initial.password:
            raise CoreError('PROTOCOL_ERROR', '请求地址不在源声明的范围内')
        request_headers = dict(headers or {})
        sensitive = ('authorization', 'cookie', 'proxy-authorization')
        if any(k.lower() in sensitive for k in request_headers) and (role != 'api' or initial.hostname not in self.auth_hosts):
            raise CoreError('PROTOCOL_ERROR', '请求禁止向此主机发送账号凭据')
        scope = self.scope or initial.hostname
        url = budget.redirect_url or url
        if urlparse(url).netloc!=initial.netloc:
            request_headers={k:v for k,v in request_headers.items() if k.lower() not in sensitive}
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
                # Keep a website's anonymous session for its directory request,
                # but partition even broad Domain cookies by exact origin. Images
                # never receive any website/API cookies.
                parsed_url=urlparse(url)
                origin=(parsed_url.scheme,parsed_url.hostname,parsed_url.port or 443)
                self.client.cookies=self.origin_cookies.get(origin,httpx.Cookies()) if role=='api' else httpx.Cookies()
                with self.client.stream(method, url, headers=request_headers, data=data, timeout=45 if role == 'image' else 20) as response:
                    if role=='api':self.origin_cookies[origin]=httpx.Cookies(self.client.cookies)
                    if response.status_code in (301, 302, 303, 307, 308):
                        target = str(response.url.join(response.headers.get('location', '')))
                        parsed = urlparse(target)
                        if parsed.scheme != 'https' or parsed.hostname not in allowed_hosts or parsed.username or parsed.password or redirect == 5:
                            raise CoreError('PROTOCOL_ERROR', '拒绝不安全的重定向')
                        if method != 'GET':
                            raise CoreError('PROTOCOL_ERROR', '登录请求不跟随重定向')
                        if parsed.netloc != urlparse(url).netloc:
                            request_headers = {k:v for k,v in request_headers.items() if k.lower() not in sensitive}
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
                        raise CoreError('SITE_RESTRICTED', 'CopyManga 返回 210：站点限制了当前请求，请检查账号或网络后重试')
                    if response.status_code == 401:
                        raise CoreError('AUTH_REQUIRED', '账号登录已失效，请重新配置或登录')
                    if response.status_code != 200:
                        raise CoreError('HTTP_ERROR', f'源请求失败（HTTP {response.status_code}）')
                    chunks, count = [], 0
                    for chunk in response.iter_bytes():
                        count += len(chunk)
                        if count > 25 * 1024 * 1024:
                            raise CoreError('INVALID_IMAGE', '图片响应超出单页限制')
                        chunks.append(chunk)
                    budget.redirect_url = None
                    self.last_url=str(response.url)
                    return b''.join(chunks)
            except httpx.RequestError:
                raise CoreError('NETWORK', '源连接失败，请检查网络与代理', retry_at=time.time() + (3 if budget.used == 1 else 10)) from None
        raise CoreError('PROTOCOL_ERROR', '重定向次数过多')

    def close(self):
        self.client.close()
