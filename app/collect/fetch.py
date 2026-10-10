"""HTTP 抓取层：缓存、按主机限速、robots.txt、超时重试、失败分类。

所有对外请求都经过 Fetcher.get()。失败统一抛出 FetchError(kind, reason)，
kind 用于决定降级策略：

    robots      robots.txt 不允许 → 不抓，换来源或转人工
    blocked     401/403/451、验证码页 → 访问受限
    rate_limit  429 → 退避后重试，仍失败则换来源
    not_found   404/410
    timeout     超时
    network     DNS/连接失败（如来源在当前网络不可达）
    server      5xx
    bad_content 返回内容无法解析

测试时用 set_transport() 注入假的传输函数，不会真正联网。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

from .. import config

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 TopicDesk/0.2")
BOT_NAME = "TopicDesk"


class FetchError(Exception):
    def __init__(self, kind: str, reason: str, url: str = "", status: int | None = None):
        super().__init__(f"[{kind}] {reason} ({url})")
        self.kind, self.reason, self.url, self.status = kind, reason, url, status

    def to_dict(self):
        return {"kind": self.kind, "reason": self.reason, "url": self.url, "status": self.status}


@dataclass
class Response:
    url: str
    final_url: str
    status: int
    headers: dict
    content: bytes
    from_cache: bool = False
    elapsed_ms: int = 0
    meta: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        ct = self.headers.get("content-type", "")
        enc = None
        if "charset=" in ct:
            enc = ct.split("charset=")[-1].split(";")[0].strip()
        for e in (enc, "utf-8", "gb18030"):
            if not e:
                continue
            try:
                return self.content.decode(e)
            except (LookupError, UnicodeDecodeError):
                continue
        return self.content.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.text)


# 传输函数签名：(url, headers, timeout) -> (status, headers, content, final_url)
Transport = Callable[[str, dict, float], tuple]


def _requests_transport(url: str, headers: dict, timeout: float, method: str = "GET",
                        json_body=None):
    import requests
    try:
        r = requests.request(method, url, headers=headers, timeout=timeout, allow_redirects=True,
                             json=json_body)
    except requests.exceptions.Timeout as e:
        raise FetchError("timeout", f"请求超时 {timeout}s", url) from e
    except requests.exceptions.SSLError as e:
        raise FetchError("network", f"TLS 错误：{e}", url) from e
    except requests.exceptions.ConnectionError as e:
        raise FetchError("network", f"连接失败：{str(e)[:160]}", url) from e
    return r.status_code, {k.lower(): v for k, v in r.headers.items()}, r.content, r.url


_transport: Transport = _requests_transport


def set_transport(t: Transport | None):
    """测试用：注入假传输；传 None 恢复真实网络。"""
    global _transport
    _transport = t or _requests_transport


class Fetcher:
    def __init__(self, min_interval: float = 1.0, timeout: float = 15, retries: int = 2,
                 cache_ttl: float = 6 * 3600):
        self.min_interval = min_interval
        self.timeout = timeout
        self.retries = retries
        self.cache_ttl = cache_ttl
        self._last: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._lock = threading.Lock()
        self.host_interval: dict[str, float] = {}   # 个别站点更慢，如 arxiv 3 秒

    # ---------------------------------------------------------------- 缓存
    def _cache_path(self, url: str):
        return config.CACHE_DIR / (hashlib.sha256(url.encode()).hexdigest()[:32] + ".json")

    def _cache_get(self, url: str, ttl: float):
        p = self._cache_path(url)
        if not p.exists() or ttl <= 0:
            return None
        try:
            d = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if time.time() - d["t"] > ttl:
            return None
        body = (config.CACHE_DIR / d["body"]).read_bytes() if d.get("body") else b""
        return Response(url, d["final_url"], d["status"], d["headers"], body, from_cache=True)

    def _cache_put(self, r: Response, key: str | None = None):
        body_name = hashlib.sha256(r.content).hexdigest()[:32] + ".bin"
        (config.CACHE_DIR / body_name).write_bytes(r.content)
        self._cache_path(key or r.url).write_text(json.dumps({
            "t": time.time(), "final_url": r.final_url, "status": r.status,
            "headers": {k: v for k, v in r.headers.items()
                        if k in ("content-type", "last-modified", "etag")},
            "body": body_name}))

    # ---------------------------------------------------------------- 礼貌访问
    def _throttle(self, host: str):
        gap = self.host_interval.get(host, self.min_interval)
        with self._lock:
            wait = self._last.get(host, 0) + gap - time.time()
            self._last[host] = max(time.time(), self._last.get(host, 0) + gap)
        if wait > 0:
            time.sleep(wait)

    def robots_allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                status, _, content, _ = _transport(base + "/robots.txt", {"User-Agent": UA}, 8)
                if status >= 400:
                    rp = None              # 没有 robots.txt 视为允许
                else:
                    rp.parse(content.decode("utf-8", "replace").splitlines())
            except FetchError:
                rp = None
            self._robots[base] = rp
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(BOT_NAME, url)

    # ---------------------------------------------------------------- 主入口
    def post_json(self, url: str, body: dict, **kw) -> Response:
        """POST JSON（部分站内搜索接口只支持 POST）。缓存键包含请求体。"""
        return self.get(url, _method="POST", _json=body, **kw)

    def get(self, url: str, *, respect_robots: bool = True, cache_ttl: float | None = None,
            headers: dict | None = None, timeout: float | None = None,
            _method: str = "GET", _json: dict | None = None) -> Response:
        if not url.startswith(("http://", "https://")):
            raise FetchError("bad_content", "只支持 http/https 链接", url)
        ttl = self.cache_ttl if cache_ttl is None else cache_ttl
        ckey = url if _method == "GET" else f"{_method} {url} {json.dumps(_json, sort_keys=True, ensure_ascii=False)}"
        hit = self._cache_get(ckey, ttl)
        if hit and not _looks_like_challenge(hit):     # 旧版本缓存过验证页，不能复用
            return hit
        if respect_robots and not self.robots_allowed(url):
            raise FetchError("robots", "robots.txt 不允许抓取该路径", url)

        host = urlsplit(url).netloc
        h = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8", **(headers or {})}
        last_err: FetchError | None = None
        for attempt in range(self.retries + 1):
            self._throttle(host)
            t0 = time.time()
            try:
                if _method == "GET":
                    status, rh, content, final = _transport(url, h, timeout or self.timeout)
                else:
                    status, rh, content, final = _transport(url, h, timeout or self.timeout,
                                                            method=_method, json_body=_json)
            except FetchError as e:
                last_err = e
                if e.kind in ("timeout", "network") and attempt < self.retries:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                raise
            r = Response(url, final, status, rh, content, elapsed_ms=int((time.time() - t0) * 1000))
            if status == 429 or status >= 500:
                kind = "rate_limit" if status == 429 else "server"
                last_err = FetchError(kind, f"HTTP {status}", url, status)
                if attempt < self.retries:
                    retry_after = rh.get("retry-after", "")
                    delay = float(retry_after) if retry_after.isdigit() else 1.5 * (2 ** attempt)
                    time.sleep(min(delay, 10))
                    continue
                raise last_err
            if status in (401, 403, 451):
                raise FetchError("blocked", f"HTTP {status}，访问受限", url, status)
            if status in (404, 410):
                raise FetchError("not_found", f"HTTP {status}", url, status)
            if status >= 400:
                raise FetchError("blocked", f"HTTP {status}", url, status)
            if _looks_like_challenge(r):
                raise FetchError("blocked", "返回了 WAF/反爬验证页（需要执行 JavaScript）", url, status)
            if ttl > 0:
                self._cache_put(r, ckey)
            return r
        raise last_err or FetchError("network", "未知错误", url)


def _looks_like_challenge(r: Response) -> bool:
    ct = r.headers.get("content-type", "")
    if "html" not in ct or len(r.content) > 60_000:
        return False
    low = r.content[:60_000].decode("utf-8", "ignore").lower()
    marks = ("captcha", "cf-challenge", "verify you are human", "安全验证", "访问验证",
             "请完成验证", "just a moment...", "安全检测", "waf_verification", "wafchallenge",
             "_wafchallengeid", "acw_sc__v2", "滑动验证", "人机验证")
    return any(m in low for m in marks)


fetcher = Fetcher()
fetcher.host_interval.update({"export.arxiv.org": 3.0, "api.github.com": 1.0})
