"""通用搜索 API：用于“发现”公众号、B站、知乎、贴吧、小红书、抖音等平台上的内容。

这些平台大多没有开放的搜索接口（或网页端需要登录、请求带签名），直接抓取既不稳定也不合规。
做法是调用正规的搜索服务，用站点限定（site / include_domains）找到相关页面，拿到标题、摘要、链接、日期；
之后能公开访问的页面（公众号文章等）再抓正文，需要登录的平台只保留摘要和链接，正文由用户粘贴。

支持三家（可以同时配置多家，在“设置”页填写 key；检索某个平台时自动选收录好的那家）：
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


def _raw() -> dict:
    p = _path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def settings() -> dict:
    """{keys: {provider: key}, primary, coverage: {provider: {site: n}}}。兼容旧格式（provider + api_key）。"""
    d = _raw()
    keys = dict(d.get("keys") or {})
    if d.get("provider") and d.get("api_key"):
        keys.setdefault(d["provider"], d["api_key"])
    env_p, env_k = os.environ.get("WEBSEARCH_PROVIDER", ""), os.environ.get("WEBSEARCH_API_KEY", "")
    if env_p in PROVIDERS and env_k:
        keys.setdefault(env_p, env_k)
    keys = {k: v for k, v in keys.items() if k in PROVIDERS and v}
    primary = d.get("primary") or d.get("provider") or next(iter(keys), "")
    if primary not in keys:
        primary = next(iter(keys), "")
    return {"keys": keys, "primary": primary, "coverage": d.get("coverage") or {},
            # 兼容旧调用
            "provider": primary, "api_key": keys.get(primary, "")}


def available() -> bool:
    return bool(settings()["keys"])


def public_settings() -> dict:
    s = settings()
    return {"provider": s["primary"], "api_key": "***" if s["api_key"] else "", "available": available(),
            "providers": PROVIDERS, "keys": {p: ("***" if s["keys"].get(p) else "") for p in PROVIDERS},
            "coverage": s["coverage"]}


def coverage(provider: str | None = None) -> dict:
    """某个搜索服务对各站点的收录情况（设置页“测试”的结果）：{site: 属于该站点的条数}。"""
    s = settings()
    return (s["coverage"].get(provider or s["primary"]) or {})


def providers_for(site: str | None) -> list[str]:
    """检索某个站点时依次尝试的服务：测试显示收录多的优先；测试显示不收录的排除；没测过的放中间。"""
    s = settings()
    order = [s["primary"]] + [p for p in s["keys"] if p != s["primary"]]
    if not site:
        return order
    scored = []
    for i, p in enumerate(order):
        n = (s["coverage"].get(p) or {}).get(site)
        if n == 0:
            continue
        scored.append((-(n if n is not None else 0.5), i, p))
    return [p for _, _, p in sorted(scored)]


def not_covered(site: str | None) -> bool:
    """所有已配置的搜索服务都测试过且都不收录该站点。"""
    return bool(site) and available() and not providers_for(site)


def save_coverage(provider: str, cov: dict):
    d = _raw()
    d.setdefault("coverage", {})[provider] = cov
    _path().write_text(json.dumps(d))


def save_settings(provider: str, api_key: str, primary: bool = True):
    """保存某个服务的 key（"***" 表示不改，空字符串表示删除）。可以同时配置多个服务。"""
    if provider and provider not in PROVIDERS:
        raise ValueError(f"不支持的搜索服务：{provider}")
    d = _raw()
    s = settings()
    keys = dict(s["keys"])
    if provider:
        if api_key == "***":
            pass
        elif (api_key or "").strip():
            keys[provider] = api_key.strip()
        else:
            keys.pop(provider, None)
    d.update(keys=keys, primary=provider if (primary and provider in keys) else s["primary"])
    d.pop("provider", None)
    d.pop("api_key", None)
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
    if not available():
        raise FetchError("config", "未配置搜索 API（在“设置”页选择服务并填写 key 后即可使用）", "websearch")
    order = providers_for(site)
    stats.update(raw=0, matched=0, retried=False, tried=[], provider=None)
    if not order:
        return []
    last_err = None
    for prov in order:          # 依次尝试：哪个服务对该站点收录好就用哪个，拿到结果就停
        try:
            rows = _search(query, site=site, count=count, since=since, provider=prov)
            matched = [x for x in rows if _host_ok(x["url"], site)]
            if site and not matched and prov != "brave":
                # 没有一条来自目标站点：多半是服务不支持站点限定参数，改用查询语法 site: 再试一次
                rows2 = _search(f"{query} site:{site}", site=None, count=count, since=since, provider=prov)
                rows += rows2
                matched = [x for x in rows2 if _host_ok(x["url"], site)]
                stats["retried"] = True
        except FetchError as e:
            if e.kind != "config" or len(order) == 1:
                last_err = e
            stats["tried"].append(f"{PROVIDERS[prov]}：{e.reason[:40]}")
            continue
        stats["raw"] += len(rows)
        stats["tried"].append(f"{PROVIDERS[prov]} {len(matched)} 条")
        if matched or not site:
            stats.update(matched=len(matched), provider=prov)
            return matched
    if last_err and not stats["raw"]:
        raise last_err
    return []


def _search(query: str, *, site: str | None, count: int, since: float | None, provider: str) -> list[dict]:
    prov, key = provider, settings()["keys"][provider]
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
