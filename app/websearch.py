"""通用搜索 API：用于“发现”公众号、B站、知乎、贴吧、小红书、抖音等平台上的内容。

这些平台大多没有开放的搜索接口（或网页端需要登录、请求带签名），直接抓取既不稳定也不合规。
做法是调用正规的搜索服务，用站点限定（site / include_domains）找到相关页面，拿到标题、摘要、链接、日期；
之后能公开访问的页面（公众号文章等）再抓正文，需要登录的平台只保留摘要和链接，正文由用户粘贴。

支持三家（任选其一，在“设置”页填写 key）：
    bocha   博查 Web Search API      中文网页覆盖好（含公众号等），按次计费
    tavily  Tavily Search API        每月有免费额度，中文覆盖一般
    brave   Brave Search API         独立索引，免费档需绑卡
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

from . import config
from .collect.fetch import FetchError, fetcher

PROVIDERS = {"bocha": "博查", "tavily": "Tavily", "brave": "Brave Search"}


def _path():
    return config.DATA_DIR / "websearch.json"


def settings() -> dict:
    d = {"provider": os.environ.get("WEBSEARCH_PROVIDER", ""), "api_key": os.environ.get("WEBSEARCH_API_KEY", "")}
    p = _path()
    if p.exists():
        try:
            d.update({k: v for k, v in json.loads(p.read_text()).items() if v})
            d.setdefault("coverage", {})
        except (OSError, json.JSONDecodeError):
            pass
    return d


def available() -> bool:
    s = settings()
    return s["provider"] in PROVIDERS and bool(s["api_key"])


def public_settings() -> dict:
    s = settings()
    return {"provider": s["provider"], "api_key": "***" if s["api_key"] else "", "available": available(),
            "providers": PROVIDERS}


def coverage() -> dict:
    """当前搜索服务对各站点的收录情况（设置页“测试”的结果）：{site: 属于该站点的条数}。"""
    s = settings()
    return ((s.get("coverage") or {}).get(s["provider"]) or {})


def not_covered(site: str | None) -> bool:
    return bool(site) and coverage().get(site) == 0


def save_coverage(provider: str, cov: dict):
    p = _path()
    d = json.loads(p.read_text()) if p.exists() else {}
    d.setdefault("coverage", {})[provider] = cov
    p.write_text(json.dumps(d))


def save_settings(provider: str, api_key: str):
    if provider and provider not in PROVIDERS:
        raise ValueError(f"不支持的搜索服务：{provider}")
    old = settings()
    if api_key == "***":
        api_key = old.get("api_key", "")
    d = {"provider": provider, "api_key": (api_key or "").strip()}
    if old.get("coverage"):
        d["coverage"] = old["coverage"]
    _path().write_text(json.dumps(d))


def _freshness(since: float | None) -> int | None:
    if not since:
        return None
    return max(1, int((datetime.now(timezone.utc).timestamp() - since) / 86400))


def _host_ok(url: str, site: str | None) -> bool:
    if not site:
        return True
    h = urlsplit(url).netloc.lower()
    return h == site or h.endswith("." + site)


def search(query: str, *, site: str | None = None, count: int = 10, since: float | None = None,
           stats: dict | None = None) -> list[dict]:
    """返回 [{title, url, snippet, published_at, site_name}]。未配置时抛 FetchError("config")。
    stats（可选）记录：raw=服务返回条数，matched=属于目标站点的条数，retried=是否用 site: 语法重试过。"""
    stats = stats if stats is not None else {}
    rows = _search(query, site=site, count=count, since=since)
    stats.update(raw=len(rows), matched=sum(_host_ok(x["url"], site) for x in rows), retried=False)
    if site and not stats["matched"] and settings()["provider"] != "brave":
        # 返回了结果但没有一条来自目标站点：多半是服务不支持站点限定参数，改用查询语法 site: 再试一次
        rows2 = _search(f"{query} site:{site}", site=None, count=count, since=since)
        stats.update(retried=True, raw=stats["raw"] + len(rows2),
                     matched=sum(_host_ok(x["url"], site) for x in rows2))
        rows = rows2
    return [x for x in rows if _host_ok(x["url"], site)]


def _search(query: str, *, site: str | None, count: int, since: float | None) -> list[dict]:
    s = settings()
    if not available():
        raise FetchError("config", "未配置搜索 API（在“设置”页选择服务并填写 key 后即可使用）", "websearch")
    prov, key = s["provider"], s["api_key"]
    days = _freshness(since)
    try:
        if prov == "bocha":
            fresh = "noLimit" if not days else "oneWeek" if days <= 7 else "oneMonth" if days <= 31 else \
                "oneYear" if days <= 366 else "noLimit"
            body = {"query": query, "freshness": fresh, "summary": True, "count": min(count, 50)}
            if site:
                body["include"] = site
            r = fetcher.post_json("https://api.bochaai.com/v1/web-search", body, respect_robots=False,
                                  cache_ttl=6 * 3600, headers={"Authorization": f"Bearer {key}"}, timeout=20)
            d = r.json()
            if d.get("code") not in (200, None, "200"):
                raise FetchError("config" if d.get("code") in (401, 403, "401", "403") else "bad_content",
                                 f"博查返回错误 {d.get('code')}：{d.get('msg') or d.get('message')}", "bocha")
            rows = (((d.get("data") or {}).get("webPages") or {}).get("value")) or []
            out = [{"title": x.get("name"), "url": x.get("url"), "snippet": x.get("summary") or x.get("snippet"),
                    "published_at": x.get("datePublished") or x.get("dateLastCrawled"),
                    "site_name": x.get("siteName")} for x in rows]
        elif prov == "tavily":
            body = {"query": query, "max_results": min(count, 20), "search_depth": "basic", "api_key": key}
            if site:
                body["include_domains"] = [site]
            if days:
                body["time_range"] = "week" if days <= 7 else "month" if days <= 31 else "year"
            r = fetcher.post_json("https://api.tavily.com/search", body, respect_robots=False, cache_ttl=6 * 3600,
                                  headers={"Authorization": f"Bearer {key}"}, timeout=25)
            rows = r.json().get("results") or []
            out = [{"title": x.get("title"), "url": x.get("url"), "snippet": x.get("content"),
                    "published_at": x.get("published_date"), "site_name": None} for x in rows]
        else:  # brave
            q = f"{query} site:{site}" if site else query
            fresh = "" if not days else "&freshness=" + ("pw" if days <= 7 else "pm" if days <= 31 else "py")
            r = fetcher.get(f"https://api.search.brave.com/res/v1/web/search?count={min(count, 20)}&q={quote(q)}{fresh}",
                            respect_robots=False, cache_ttl=6 * 3600, timeout=20,
                            headers={"X-Subscription-Token": key, "Accept": "application/json"})
            rows = (r.json().get("web") or {}).get("results") or []
            out = [{"title": x.get("title"), "url": x.get("url"), "snippet": x.get("description"),
                    "published_at": x.get("page_age"), "site_name": (x.get("profile") or {}).get("name")} for x in rows]
    except FetchError as e:
        if e.kind == "blocked" and e.status in (401, 403):
            raise FetchError("config", f"{PROVIDERS[prov]} 拒绝了请求（HTTP {e.status}）：API key 无效或额度用完",
                             prov, e.status) from e
        raise
    return [x for x in out if x.get("url")]
