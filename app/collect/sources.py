"""信息源适配器。

每个来源实现 search(query, limit) -> list[Item]，Item 是统一结构的 dict：
    url, title, author, published_at, summary, text, content_kind(full/excerpt/summary),
    images(候选图片), source_id, primary(是否一手来源), meta(热度、评论数、转引关系等)

采集方式：
    rss      IT之家、36氪（全文 RSS，带图）、少数派（摘要 RSS，正文需浏览器渲染）
    api      arXiv（Atom）、Hacker News（Algolia JSON）、GitHub（REST JSON）
    html     通用网页抓取（链接导入、追踪原始来源）
    browser  前端渲染页面（Playwright）

RSS 只能拿到最新几十条，按主题关键词过滤；这是覆盖范围的已知局限，会写进覆盖报告。
用户也可以在“信息源”页面添加任意 RSS。
"""
from __future__ import annotations

import os
import re
from urllib.parse import quote, urlsplit

from bs4 import BeautifulSoup

from .. import db
from .dedup import iso
from .extract import extract_article, html_to_text
from .fetch import FetchError, fetcher

PRIMARY_DOMAINS = ("arxiv.org", "github.com", "huggingface.co", "openreview.net",
                   "aclanthology.org", "paperswithcode.com", "gov.cn", "openai.com",
                   "anthropic.com", "deepmind.google", "ai.meta.com", "qwenlm.github.io",
                   "deepseek.com", "moonshot.cn", "zhipuai.cn", "blog.google")

BUILTIN = [
    dict(id="ithome", name="IT之家", method="rss", lang="zh", base_url="https://www.ithome.com",
         config={"feed": "https://www.ithome.com/rss/", "full_text": True}),
    dict(id="36kr", name="36氪", method="rss", lang="zh", base_url="https://36kr.com",
         config={"feed": "https://36kr.com/feed", "full_text": True}),
    dict(id="sspai", name="少数派", method="rss", lang="zh", base_url="https://sspai.com",
         config={"feed": "https://sspai.com/feed", "full_text": False, "detail": "browser"}),
    dict(id="arxiv", name="arXiv", method="api", lang="en", base_url="https://arxiv.org",
         config={"note": "官方开放 API，按其使用条款每 3 秒 1 次"}),
    dict(id="hn", name="Hacker News", method="api", lang="en",
         base_url="https://news.ycombinator.com", config={}),
    dict(id="github", name="GitHub", method="api", lang="en", base_url="https://github.com",
         config={}),
    dict(id="web", name="通用网页", method="html", lang="any", base_url="", config={}),
]


def is_primary(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return any(host == d or host.endswith("." + d) for d in PRIMARY_DOMAINS)


def seed_sources():
    for s in BUILTIN:
        if not db.query_one("SELECT id FROM sources WHERE id=?", (s["id"],)):
            db.insert("sources", dict(id=s["id"], name=s["name"], method=s["method"],
                                      base_url=s["base_url"],
                                      config={**s["config"], "lang": s["lang"]}, enabled=1,
                                      health={}))


def list_sources(enabled_only=True) -> list[dict]:
    sql = "SELECT * FROM sources" + (" WHERE enabled=1" if enabled_only else "")
    return db.query(sql + " ORDER BY rowid")


def record_health(source_id: str, ok: bool, err: FetchError | None = None, n: int = 0):
    row = db.query_one("SELECT health FROM sources WHERE id=?", (source_id,))
    if not row:
        return
    h = row["health"] or {}
    if ok:
        h.update(last_ok=db.now(), consecutive_fail=0, last_count=n)
    else:
        h.update(last_fail=db.now(), consecutive_fail=h.get("consecutive_fail", 0) + 1,
                 last_error=err.to_dict() if err else None)
    h["calls"] = h.get("calls", 0) + 1
    db.update("sources", "id", source_id, {"health": h})


# ---------------------------------------------------------------- 主题匹配

_SPLIT = re.compile(r"[\s,，、;；/|]+|的|与|和|及|以及|对|在|中|关于")


def topic_terms(text: str) -> list[str]:
    terms = []
    for t in _SPLIT.split(text or ""):
        t = t.strip().strip("\"'“”《》()（）")
        if len(t) >= 2 and t.lower() not in ("the", "and", "for", "with"):
            terms.append(t)
    return list(dict.fromkeys(terms))


def _bigrams(s):
    s = re.sub(r"\s+", "", s.lower())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def relevance(terms: list[str], *texts: str) -> float:
    """每个关键词：原文出现得 1 分；长词按二元组覆盖率给部分分（≥0.6 才算）。"""
    if not terms:
        return 1.0
    blob = " ".join(t or "" for t in texts).lower()
    blob_bg = None
    score = 0.0
    for t in terms:
        tl = t.lower()
        if tl in blob:
            score += 1
            continue
        if len(tl) >= 4 and not tl.isascii():
            if blob_bg is None:
                blob_bg = _bigrams(blob)
            g = _bigrams(tl)
            cov = len(g & blob_bg) / max(len(g), 1)
            if cov >= 0.6:
                score += cov * 0.8
    return round(score / len(terms), 3)


# ---------------------------------------------------------------- RSS / Atom

def parse_feed(xml: str, base_url: str = "") -> list[dict]:
    soup = BeautifulSoup(xml, "xml")
    items = soup.find_all("item") or soup.find_all("entry")
    out = []
    for it in items:
        def g(*names):
            for n in names:
                el = it.find(n)
                if el is not None:
                    if n == "link" and el.get("href"):
                        return el["href"]
                    txt = el.get_text()
                    if txt and txt.strip():
                        return txt.strip()
            return None
        content = g("encoded", "content") or g("description", "summary") or ""
        out.append({
            "title": html_to_text(g("title") or ""),
            "url": g("link", "guid"),
            "author": g("creator", "author", "name"),
            "published_at": g("pubDate", "published", "updated", "date"),
            "content_html": content,
        })
    return [o for o in out if o["url"]]


def search_rss(source: dict, query: str, limit: int = 20, terms: list[str] | None = None) -> list[dict]:
    cfg = source["config"]
    r = fetcher.get(cfg["feed"], cache_ttl=20 * 60)
    entries = parse_feed(r.text)
    if not entries:
        raise FetchError("bad_content", "RSS 中没有条目", cfg["feed"])
    terms = terms or topic_terms(query)
    items = []
    for e in entries:
        art = extract_article(e["content_html"], e["url"], is_fragment=True)
        text = art["text"]
        rel = relevance(terms, e["title"], text)
        if query and rel < 0.5:
            continue
        kind = art["content_kind"] if cfg.get("full_text") else "summary"
        items.append({
            "url": e["url"], "title": e["title"], "author": e["author"],
            "published_at": iso(e["published_at"]) or e["published_at"],
            "summary": text[:300], "text": text if cfg.get("full_text") else None,
            "content_kind": kind, "images": art["images"] if cfg.get("full_text") else [],
            "source_id": source["id"], "primary": False, "relevance": rel,
            "meta": {"feed": cfg["feed"], "detail": cfg.get("detail", "html"),
                     "out_links": art["links"][:20]},
        })
    items.sort(key=lambda x: -x["relevance"])
    return items[:limit]


# ---------------------------------------------------------------- arXiv

def search_arxiv(source: dict, query: str, limit: int = 10, **_) -> list[dict]:
    words = [w for w in re.split(r"\s+", query.strip()) if w]
    q = " AND ".join(f'all:"{w}"' if " " in w else f"all:{w}" for w in words) or "all:*"
    if len(words) > 1 and query.startswith('"'):
        q = f"all:{query}"
    url = ("https://export.arxiv.org/api/query?search_query=" + quote(q) +
           f"&sortBy=submittedDate&sortOrder=descending&max_results={limit}")
    r = fetcher.get(url, respect_robots=False, cache_ttl=3600)
    soup = BeautifulSoup(r.text, "xml")
    items = []
    for e in soup.find_all("entry"):
        abs_url = e.find("id").get_text(strip=True)
        pdf = next((l["href"] for l in e.find_all("link") if l.get("title") == "pdf"), None)
        summary = re.sub(r"\s+", " ", e.find("summary").get_text()).strip()
        items.append({
            "url": abs_url, "title": re.sub(r"\s+", " ", e.find("title").get_text()).strip(),
            "author": ", ".join(a.find("name").get_text() for a in e.find_all("author")[:6]),
            "published_at": iso(e.find("published").get_text()),
            "summary": summary, "text": summary, "content_kind": "summary", "images": [],
            "source_id": "arxiv", "primary": True,
            "meta": {"pdf": pdf, "updated": e.find("updated").get_text(),
                     "categories": [c.get("term") for c in e.find_all("category")],
                     "doc_type": "论文摘要"},
        })
    return items


# ---------------------------------------------------------------- Hacker News

def search_hn(source: dict, query: str, limit: int = 15, **_) -> list[dict]:
    url = ("https://hn.algolia.com/api/v1/search?tags=story&hitsPerPage=" + str(limit) +
           "&query=" + quote(query))
    r = fetcher.get(url, respect_robots=False, cache_ttl=1800)
    items = []
    for h in r.json().get("hits", []):
        hn_url = f"https://news.ycombinator.com/item?id={h['objectID']}"
        target = h.get("url") or hn_url
        items.append({
            "url": target, "title": h.get("title") or "", "author": h.get("author"),
            "published_at": h.get("created_at"),
            "summary": h.get("story_text") or "", "text": None, "content_kind": "summary",
            "images": [], "source_id": "hn", "primary": is_primary(target),
            "meta": {"via": {"source": "hn", "url": hn_url}, "points": h.get("points"),
                     "comments": h.get("num_comments"), "aggregator": True},
        })
    return items


# ---------------------------------------------------------------- GitHub

def search_github(source: dict, query: str, limit: int = 8, **_) -> list[dict]:
    url = ("https://api.github.com/search/repositories?sort=stars&order=desc&per_page=" +
           str(limit) + "&q=" + quote(query))
    headers = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    r = fetcher.get(url, respect_robots=False, cache_ttl=3600, headers=headers)
    items = []
    for repo in r.json().get("items", []):
        desc = repo.get("description") or ""
        items.append({
            "url": repo["html_url"], "title": f"{repo['full_name']}: {desc}"[:200],
            "author": (repo.get("owner") or {}).get("login"),
            "published_at": repo.get("created_at"),
            "summary": desc, "text": desc, "content_kind": "summary", "images": [],
            "source_id": "github", "primary": True,
            "meta": {"stars": repo.get("stargazers_count"), "pushed_at": repo.get("pushed_at"),
                     "license": (repo.get("license") or {}).get("spdx_id"),
                     "doc_type": "代码仓库"},
        })
    return items


SEARCHERS = {"rss": search_rss, "arxiv": search_arxiv, "hn": search_hn, "github": search_github}


def search(source_id: str, query: str, limit: int = 15, terms: list[str] | None = None) -> list[dict]:
    src = db.query_one("SELECT * FROM sources WHERE id=?", (source_id,))
    if src is None:
        raise FetchError("bad_content", f"未知来源 {source_id}")
    fn = SEARCHERS.get(source_id) or SEARCHERS.get(src["method"])
    if fn is None:
        raise FetchError("bad_content", f"来源 {source_id} 不支持搜索")
    try:
        items = fn(src, query, limit, terms=terms)
    except FetchError as e:
        record_health(source_id, False, e)
        raise
    record_health(source_id, True, n=len(items))
    return items


# ---------------------------------------------------------------- 单页抓取（含降级）

def fetch_page(url: str, mode: str = "auto") -> dict:
    """抓取单个网页。mode: html / browser / auto（静态正文太少时自动改用浏览器渲染）。
    返回 extract_article 的结果 + {final_url, via_browser, attempts}。"""
    attempts = []
    art = None
    if mode in ("html", "auto"):
        try:
            r = fetcher.get(url)
            ct = r.headers.get("content-type", "")
            if "pdf" in ct or r.final_url.lower().endswith(".pdf"):
                art = _pdf_article(r)
            else:
                art = extract_article(r.text, r.final_url)
            art.update(final_url=r.final_url, via_browser=False)
            attempts.append({"mode": "html", "ok": True, "chars": len(art["text"]),
                             "cache": r.from_cache})
            if mode == "html" or len(art["text"]) >= 400:
                art["attempts"] = attempts
                return art
            attempts[-1]["note"] = "静态 HTML 正文过短，疑似前端渲染"
        except FetchError as e:
            attempts.append({"mode": "html", "ok": False, **e.to_dict()})
            if e.kind in ("robots", "not_found") or mode == "html":
                e.attempts = attempts
                raise
    from .browser import render
    try:
        rr = render(url)
    except FetchError as e:
        attempts.append({"mode": "browser", "ok": False, **e.to_dict()})
        if art is not None:              # 浏览器失败但静态页有部分内容，降级为节选
            art["attempts"] = attempts
            art["content_kind"] = "excerpt" if art["text"] else "summary"
            return art
        e.attempts = attempts
        raise
    art2 = extract_article(rr["html"], rr["final_url"])
    art2.update(final_url=rr["final_url"], via_browser=True, browser=rr["browser"])
    attempts.append({"mode": "browser", "ok": True, "chars": len(art2["text"]),
                     "browser": rr["browser"], "elapsed_ms": rr["elapsed_ms"]})
    if art is not None and len(art["text"]) > len(art2["text"]):
        art2 = {**art, "final_url": art["final_url"], "via_browser": False}
    art2["attempts"] = attempts
    return art2


def _pdf_article(r) -> dict:
    """公开 PDF：有 pypdf 就抽文本，没有则只记录元信息。"""
    text = ""
    try:
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(r.content))
        pages = [p.extract_text() or "" for p in reader.pages[:30]]
        text = "\n\n".join(f"[第{i+1}页] {t.strip()}" for i, t in enumerate(pages) if t.strip())
    except Exception:
        pass
    return {"title": r.final_url.rsplit("/", 1)[-1], "author": None, "published_at": None,
            "site_name": None, "description": None, "text": text, "paragraphs": 0,
            "content_kind": "full" if len(text) > 600 else "summary", "images": [],
            "cover": None, "license": None, "links": [], "doc_type": "PDF"}
