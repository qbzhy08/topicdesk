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

from datetime import datetime, timedelta, timezone
import os
import re
from urllib.parse import quote, urljoin, urlsplit

from bs4 import BeautifulSoup

from .. import db
from .dedup import iso, parse_date
from .extract import extract_article, html_to_text
from .fetch import FetchError, fetcher

PRIMARY_DOMAINS = (
    # 科技/AI
    "arxiv.org", "github.com", "huggingface.co", "openreview.net", "aclanthology.org",
    "paperswithcode.com", "openai.com", "anthropic.com", "deepmind.google", "ai.meta.com",
    "qwenlm.github.io", "deepseek.com", "moonshot.cn", "zhipuai.cn", "blog.google",
    # 政府与监管、金融一手披露
    "gov.cn", "pbc.gov.cn", "stats.gov.cn", "csrc.gov.cn", "cninfo.com.cn", "sse.com.cn",
    "szse.cn", "bse.cn", "hkexnews.hk", "sec.gov", "federalreserve.gov", "imf.org", "worldbank.org",
    # 学术与医学
    "doi.org", "openalex.org", "pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov", "nature.com",
    "science.org", "cell.com", "thelancet.com", "nejm.org", "who.int", "biorxiv.org", "medrxiv.org",
    "ssrn.com", "nber.org")

# 领域：决定一个主题用哪些来源。general 为综合新闻，任何领域都可兜底。
DOMAINS = {"tech": "科技 / AI / 软件", "finance": "金融 / 经济 / 公司", "science": "学术 / 医学 / 科研",
           "general": "综合新闻 / 政策 / 社会", "community": "社区 / 小众 / 亚文化 / 生活经验"}
DOMAIN_HINTS = {
    "finance": ["股票", "股市", "基金", "证券", "金融", "经济", "财报", "上市", "利率", "汇率", "通胀", "央行",
                "债券", "期货", "投资", "量化", "A股", "港股", "美股", "估值", "营收", "stock", "market", "finance",
                "econom", "inflation", "bond", "fund", "trading", "earnings"],
    "science": ["医学", "药", "疾病", "临床", "基因", "蛋白", "细胞", "疫苗", "癌", "健康", "物理", "化学", "生物",
                "气候", "材料", "论文", "研究", "clinical", "disease", "drug", "gene", "protein", "climate",
                "physics", "biology", "medical", "health"],
    "tech": ["大模型", "人工智能", "AI", "模型", "算法", "芯片", "编程", "软件", "开源", "互联网", "手机", "机器人",
             "自动驾驶", "云", "数据库", "LLM", "GPU", "agent", "software", "chip", "robot", "代码", "框架"],
    "general": ["政策", "法规", "社会", "教育", "国际", "外交", "城市", "就业", "消费", "文化", "体育", "policy"],
    "community": ["工作室", "博主", "up主", "UP主", "网红", "主播", "粉丝", "圈", "二次元", "动漫", "游戏", "cos",
                  "男娘", "伪娘", "女装", "饭圈", "八卦", "吐槽", "攻略", "避坑", "探店", "有男娘", "成员",
                  "小红书", "抖音", "b站", "B站", "贴吧", "知乎", "公众号"],
}


def guess_domains(text: str) -> list[str]:
    low = (text or "").lower()
    hits = [d for d, kws in DOMAIN_HINTS.items() if any(k.lower() in low for k in kws)]
    return hits or ["general", "tech"]


def source_domains(src: dict) -> list[str]:
    return (src.get("config") or {}).get("domains") or ["tech"]

BUILTIN = [
    dict(id="ithome", name="IT之家", method="rss", lang="zh", base_url="https://www.ithome.com",
         config={"domains": ['tech'], "feed": "https://www.ithome.com/rss/", "full_text": True}),
    dict(id="36kr", name="36氪", method="rss", lang="zh", base_url="https://36kr.com",
         config={"domains": ['tech', 'finance'], "feed": "https://36kr.com/feed", "full_text": True}),
    dict(id="sspai", name="少数派", method="rss", lang="zh", base_url="https://sspai.com",
         config={"domains": ['tech'], "feed": "https://sspai.com/feed", "full_text": False, "detail": "browser"}),
    dict(id="leiphone", name="雷锋网", method="rss", lang="zh", base_url="https://www.leiphone.com",
         config={"domains": ['tech'], "feed": "https://www.leiphone.com/feed", "full_text": "auto"}),
    dict(id="ifanr", name="爱范儿", method="rss", lang="zh", base_url="https://www.ifanr.com",
         config={"domains": ['tech'], "feed": "https://www.ifanr.com/feed", "full_text": "auto"}),
    dict(id="infoq", name="InfoQ 中文", method="rss", lang="zh", base_url="https://www.infoq.cn",
         config={"domains": ['tech'], "feed": "https://www.infoq.cn/feed", "full_text": False, "detail": "browser",
                 "note": "RSS 只有链接，正文页为前端渲染"}),
    dict(id="geekpark", name="极客公园", method="rss", lang="zh", base_url="https://www.geekpark.net",
         config={"domains": ['tech'], "feed": "https://www.geekpark.net/rss", "full_text": "auto"}),
    dict(id="huxiu", name="虎嗅", method="rss", lang="zh", base_url="https://www.huxiu.com",
         config={"domains": ['tech', 'finance'], "feed": "https://www.huxiu.com/rss/0.xml", "full_text": "auto"}),
    dict(id="oschina", name="开源中国", method="rss", lang="zh", base_url="https://www.oschina.net",
         config={"domains": ['tech'], "feed": "https://www.oschina.net/news/rss", "full_text": "auto"}),
    dict(id="juejin", name="掘金", method="api", lang="zh", base_url="https://juejin.cn",
         config={"domains": ['tech'], "note": "站内搜索接口（POST JSON），可按关键词检索历史技术文章"}),
    dict(id="qbitai", name="量子位", method="rss", lang="zh", base_url="https://www.qbitai.com",
         config={"domains": ['tech'], "feed": "https://www.qbitai.com/feed",
                 "search_feed": "https://www.qbitai.com/?s={q}&feed=rss2", "full_text": False,
                 "note": "WordPress 站内搜索 RSS，可按主题检索历史文章"}),
    dict(id="arxiv", name="arXiv", method="api", lang="en", base_url="https://arxiv.org",
         config={"domains": ['tech', 'science', 'finance'], "note": "官方开放 API，按其使用条款每 3 秒 1 次"}),
    dict(id="hn", name="Hacker News", method="api", lang="en",
         base_url="https://news.ycombinator.com", config={"domains": ['tech'], }),
    dict(id="github", name="GitHub", method="api", lang="en", base_url="https://github.com",
         config={"domains": ['tech'], }),
    # ---- 金融
    dict(id="wallstcn", name="华尔街见闻", method="api", lang="zh", base_url="https://wallstreetcn.com",
         config={"domains": ["finance"], "note": "站内搜索接口，返回全文"}),
    dict(id="chinanews_fin", name="中新网财经", method="rss", lang="zh", base_url="https://www.chinanews.com.cn",
         config={"domains": ["finance"], "feed": "https://www.chinanews.com.cn/rss/finance.xml",
                 "full_text": False}),
    dict(id="ftchinese", name="FT中文网", method="rss", lang="zh", base_url="https://www.ftchinese.com",
         config={"domains": ["finance", "general"], "feed": "https://www.ftchinese.com/rss/news",
                 "full_text": "auto"}),
    # ---- 综合新闻
    dict(id="chinanews", name="中新网滚动新闻", method="rss", lang="zh", base_url="https://www.chinanews.com.cn",
         config={"domains": ["general"], "feed": "https://www.chinanews.com.cn/rss/scroll-news.xml",
                 "full_text": False}),
    # ---- 学术 / 医学
    dict(id="openalex", name="OpenAlex 学术", method="api", lang="en", base_url="https://openalex.org",
         config={"domains": ["science", "finance", "tech", "general"],
                 "note": "开放学术数据库，覆盖各学科，按相关度检索论文摘要"}),
    dict(id="pubmed", name="PubMed", method="api", lang="en", base_url="https://pubmed.ncbi.nlm.nih.gov",
         config={"domains": ["science"], "note": "NCBI 官方 E-utilities 接口（按其条款限速）"}),
    # ---- 社区 / 内容平台（小众话题、经验、亚文化）
    dict(id="bilibili", name="B站", method="api", lang="zh", base_url="https://www.bilibili.com",
         config={"domains": ["community", "general", "tech"], "fetch": False,
                 "note": "公开搜索接口（WBI 签名），取视频标题、简介、标签、播放量"}),
    dict(id="sogou_wechat", name="公众号（搜狗）", method="search", lang="zh", base_url="https://weixin.sogou.com",
         config={"domains": ["community", "general", "tech", "finance"],
                 "note": "搜狗微信搜索，无需 key；频繁访问会出验证码，只做低频补充"}),
    dict(id="web_search", name="全网搜索", method="websearch", lang="zh", base_url="",
         config={"domains": ["community", "general", "tech", "finance", "science"],
                 "note": "通过搜索 API 检索全网（需在设置页填写 key），替代不允许抓取的百度搜索"}),
    dict(id="wechat", name="微信公众号", method="websearch", lang="zh", base_url="https://mp.weixin.qq.com",
         config={"domains": ["community", "general", "tech", "finance"], "note": "搜索 API 限定 mp.weixin.qq.com，文章页可抓全文"}),
    dict(id="zhihu", name="知乎", method="websearch", lang="zh", base_url="https://www.zhihu.com",
         config={"domains": ["community", "general", "tech", "finance", "science"], "fetch": False, "note": "只保留搜索摘要（正文需登录）"}),
    dict(id="tieba", name="百度贴吧", method="websearch", lang="zh", base_url="https://tieba.baidu.com",
         config={"domains": ["community", "general"], "fetch": False, "note": "只保留搜索摘要"}),
    dict(id="xiaohongshu", name="小红书", method="websearch", lang="zh", base_url="https://www.xiaohongshu.com",
         config={"domains": ["community", "general", "tech", "finance"], "fetch": False, "note": "只保留搜索摘要（网页需登录，条款禁止抓取）"}),
    dict(id="douyin", name="抖音", method="websearch", lang="zh", base_url="https://www.douyin.com",
         config={"domains": ["community", "general", "tech", "finance"], "fetch": False, "note": "只保留搜索摘要（网页需登录，条款禁止抓取）"}),
    dict(id="web", name="通用网页", method="html", lang="any", base_url="", config={"domains": ['tech', 'finance', 'science', 'general'], }),
]


def is_primary(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return any(host == d or host.endswith("." + d) for d in PRIMARY_DOMAINS)


def seed_sources():
    for s in BUILTIN:
        row = db.query_one("SELECT id, config FROM sources WHERE id=?", (s["id"],))
        if row and ((row["config"] or {}).get("domains") != s["config"].get("domains")
                    or (row["config"] or {}).get("fetch") != s["config"].get("fetch")):
            # 内置来源的领域和抓取策略随版本更新同步（新增了“社区”领域）
            db.update("sources", "id", s["id"], {"config": {**(row["config"] or {}),
                                                            "domains": s["config"].get("domains", ["tech"]),
                                                            "fetch": s["config"].get("fetch")}})
        if not row:
            db.insert("sources", dict(id=s["id"], name=s["name"], method=s["method"],
                                      base_url=s["base_url"],
                                      config={**s["config"], "lang": s["lang"]}, enabled=1,
                                      health={}))


def list_sources(enabled_only=True) -> list[dict]:
    sql = "SELECT * FROM sources" + (" WHERE enabled=1" if enabled_only else "")
    return db.query(sql + " ORDER BY rowid")


def record_health(source_id: str, ok: bool, err: FetchError | None = None, n: int = 0, note: str | None = None):
    row = db.query_one("SELECT health FROM sources WHERE id=?", (source_id,))
    if not row:
        return
    h = row["health"] or {}
    if ok:
        h.update(last_ok=db.now(), consecutive_fail=0, last_count=n, last_note=note)
        h["empty_streak"] = 0 if n else h.get("empty_streak", 0) + 1
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


def _get_feed(url: str) -> tuple[str, str]:
    """取 RSS 原文。被 WAF/JS 验证拦截时，改用浏览器通过验证后再取（备用入口）。"""
    try:
        return fetcher.get(url, cache_ttl=20 * 60).text, "http"
    except FetchError as e:
        if e.kind != "blocked":
            raise
        from .browser import fetch_raw
        try:
            r = fetch_raw(url)
        except FetchError as e2:
            raise FetchError("blocked", f"{e.reason}；浏览器备用入口也失败：{e2.reason}", url) from e2
        return r["content"].decode("utf-8", "replace"), "browser"


def search_rss(source: dict, query: str, limit: int = 20, terms: list[str] | None = None, **_) -> list[dict]:
    cfg = source["config"]
    use_search = bool(cfg.get("search_feed") and query)
    url = cfg["search_feed"].replace("{q}", quote(query)) if use_search else cfg["feed"]
    text, via = _get_feed(url)
    entries = parse_feed(text)
    if not entries and not use_search:
        raise FetchError("bad_content", "RSS 中没有条目（可能返回了网页而不是 RSS）", url)
    terms = terms or topic_terms(query)
    # 站内搜索已按关键词匹配过（常常匹配的是正文），不再过滤；
    # 最新文章 RSS：命中任一核心词就先收进来（约 1/n），抓到全文后再精细判断相关度
    min_rel = 0.0 if use_search else min(0.5, 0.99 / max(len(terms), 1))
    ft = cfg.get("full_text")
    items = []
    for e in entries:
        art = extract_article(e["content_html"], e["url"], is_fragment=True)
        text_ = art["text"]
        rel = relevance(terms, e["title"], text_)
        if query and rel < min_rel:
            continue
        is_full = ft is True or (ft == "auto" and art["content_kind"] == "full")
        kind = art["content_kind"] if is_full else "summary"
        items.append({
            "url": e["url"], "title": e["title"], "author": e["author"],
            "published_at": iso(e["published_at"]) or e["published_at"],
            "summary": text_[:300], "text": text_ if is_full else None,
            "content_kind": kind, "images": art["images"] if is_full else [],
            "source_id": source["id"], "primary": False, "relevance": rel,
            "meta": {"feed": url, "feed_via": via, "site_search": use_search,
                     "detail": cfg.get("detail", "html"), "out_links": art["links"][:20]},
        })
    if not use_search:
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

def search_hn(source: dict, query: str, limit: int = 15, since: float | None = None, **_) -> list[dict]:
    url = ("https://hn.algolia.com/api/v1/search?tags=story&hitsPerPage=" + str(limit) +
           "&query=" + quote(query) + (f"&numericFilters=created_at_i>{int(since)}" if since else ""))
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

def search_github(source: dict, query: str, limit: int = 8, since: float | None = None, **_) -> list[dict]:
    q = query + (f" pushed:>{_day(since)}" if since else "")     # 只要窗口内仍在维护的仓库
    url = ("https://api.github.com/search/repositories?sort=stars&order=desc&per_page=" +
           str(limit) + "&q=" + quote(q))
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
            "published_at": repo.get("pushed_at") or repo.get("created_at"),   # 时效看最近更新，而非创建时间
            "summary": desc, "text": desc, "content_kind": "summary", "images": [],
            "source_id": "github", "primary": True,
            "meta": {"stars": repo.get("stargazers_count"), "pushed_at": repo.get("pushed_at"),
                     "created_at": repo.get("created_at"),
                     "license": (repo.get("license") or {}).get("spdx_id"),
                     "doc_type": "代码仓库"},
        })
    return items


# ---------------------------------------------------------------- 掘金（站内搜索 API）

def search_juejin(source: dict, query: str, limit: int = 15, **_) -> list[dict]:
    import random
    url = ("https://api.juejin.cn/search_api/v1/search?aid=2608&uuid=" +
           str(random.randint(7 * 10**18, 8 * 10**18)) + "&spider=0")
    body = {"key_word": query, "id_type": 2, "cursor": "0", "limit": limit, "search_type": 0,
            "sort_type": 0, "version": 1}
    r = fetcher.post_json(url, body, cache_ttl=3600,
                          headers={"Origin": "https://juejin.cn", "Referer": "https://juejin.cn/"})
    try:
        d = r.json()
    except ValueError as e:
        raise FetchError("bad_content", f"掘金返回的不是 JSON：{r.text[:80]}", url) from e
    if d.get("err_no") not in (0, None):
        raise FetchError("blocked", f"掘金接口错误 {d.get('err_no')}：{d.get('err_msg')}", url)
    items = []
    for row in d.get("data") or []:
        m = row.get("result_model") or {}
        info = m.get("article_info") or {}
        aid = m.get("article_id") or info.get("article_id")
        if not aid:
            continue
        ctime = info.get("ctime")
        items.append({
            "url": f"https://juejin.cn/post/{aid}",
            "title": html_to_text(row.get("title_highlight") or info.get("title") or ""),
            "author": (m.get("author_user_info") or {}).get("user_name"),
            "published_at": iso(int(ctime)) if str(ctime or "").isdigit() else None,
            "summary": html_to_text(row.get("content_highlight") or info.get("brief_content") or ""),
            "text": None, "content_kind": "summary", "images": [], "source_id": "juejin",
            "primary": False,
            "meta": {"views": info.get("view_count"), "likes": info.get("digg_count"),
                     "site_search": True, "detail": "html"},
        })
    if not items and d.get("data") is None:
        raise FetchError("bad_content", f"掘金返回结构变化：{list(d)[:6]}", url)
    return items


# ---------------------------------------------------------------- 华尔街见闻

def search_wallstcn(source: dict, query: str, limit: int = 15, **_) -> list[dict]:
    url = ("https://api-one.wallstcn.com/apiv1/search/article?cursor=&limit=" + str(limit) +
           "&query=" + quote(query))
    r = fetcher.get(url, cache_ttl=1800, headers={"Referer": "https://wallstreetcn.com/"})
    d = r.json()
    if d.get("code") not in (20000, 0, None):
        raise FetchError("blocked", f"华尔街见闻接口错误 {d.get('code')}：{d.get('message')}", url)
    items = []
    for it in ((d.get("data") or {}).get("items") or []):
        link = it.get("uri") or ""
        if not link.startswith("http"):
            continue
        art = extract_article(it.get("content") or "", link, is_fragment=True)
        paid = bool(it.get("is_paid") or it.get("is_priced"))
        full = art["content_kind"] in ("full", "excerpt") and not paid
        img = (it.get("image") or {}).get("uri")
        items.append({
            "url": link, "title": html_to_text(it.get("title") or ""),
            "author": (it.get("author") or {}).get("display_name"),
            "published_at": iso(it.get("display_time")) if it.get("display_time") else None,
            "summary": html_to_text(it.get("content_short") or "") or art["text"][:300],
            "text": art["text"] if full else None, "content_kind": art["content_kind"] if full else "summary",
            "images": (art["images"] + ([{"url": img, "caption": None, "position": 0, "reject_reason": None,
                                         "alt": None}] if img else [])) if full else [],
            "source_id": "wallstcn", "primary": False,
            "meta": {"paid": paid, "comments": it.get("comment_count"), "site_search": True,
                     "original_source": it.get("source_name") or None, "detail": "html"},
        })
    return items


# ---------------------------------------------------------------- OpenAlex（全学科学术）

def _openalex_abstract(inv: dict | None) -> str:
    if not inv:
        return ""
    pos = sorted((i, w) for w, idx in inv.items() for i in idx)
    return " ".join(w for _, w in pos)


def search_openalex(source: dict, query: str, limit: int = 10, since: float | None = None, **_) -> list[dict]:
    url = ("https://api.openalex.org/works?per_page=" + str(limit) + "&search=" + quote(query) +
           "&filter=has_abstract:true" + (f",from_publication_date:{_day(since)}" if since else "") +
           "&mailto=topicdesk@example.com")
    r = fetcher.get(url, respect_robots=False, cache_ttl=6 * 3600)
    items = []
    for w in r.json().get("results") or []:
        land = ((w.get("primary_location") or {}).get("landing_page_url") or w.get("doi") or w.get("id"))
        abstract = _openalex_abstract(w.get("abstract_inverted_index"))
        items.append({
            "url": land, "title": w.get("display_name") or w.get("title") or "",
            "author": ", ".join((a.get("author") or {}).get("display_name", "") for a in (w.get("authorships") or [])[:5]),
            "published_at": w.get("publication_date"), "summary": abstract[:600], "text": abstract,
            "content_kind": "summary", "images": [], "source_id": "openalex", "primary": True,
            "meta": {"doi": w.get("doi"), "cited_by": w.get("cited_by_count"), "doc_type": "论文摘要",
                     "venue": ((w.get("primary_location") or {}).get("source") or {}).get("display_name")},
        })
    return items


# ---------------------------------------------------------------- PubMed

def search_pubmed(source: dict, query: str, limit: int = 10, since: float | None = None, **_) -> list[dict]:
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    r = fetcher.get(base + "esearch.fcgi?db=pubmed&retmode=json&sort=relevance&retmax=" + str(limit) +
                    "&term=" + quote(query) +
                    (f"&datetype=pdat&mindate={_day(since).replace('-', '/')}&maxdate=3000" if since else ""),
                    respect_robots=False, cache_ttl=6 * 3600)
    ids = (r.json().get("esearchresult") or {}).get("idlist") or []
    if not ids:
        return []
    r2 = fetcher.get(base + "esummary.fcgi?db=pubmed&retmode=json&id=" + ",".join(ids),
                     respect_robots=False, cache_ttl=6 * 3600)
    res = r2.json().get("result") or {}
    abstracts = {}
    try:      # 摘要用 efetch 一次批量取回，避免逐篇打开 PubMed 网页（网页常被拦截、渲染慢）
        r3 = fetcher.get(base + "efetch.fcgi?db=pubmed&retmode=xml&rettype=abstract&id=" + ",".join(ids),
                         respect_robots=False, cache_ttl=24 * 3600)
        soup = BeautifulSoup(r3.content, "xml")
        for art in soup.find_all("PubmedArticle"):
            pmid = art.find("PMID")
            parts = [(t.get("Label") + "：" if t.get("Label") else "") + t.get_text(" ", strip=True)
                     for t in art.find_all("AbstractText")]
            if pmid and parts:
                abstracts[pmid.get_text(strip=True)] = "\n".join(parts)
    except (FetchError, Exception):
        pass
    items = []
    for pid in ids:
        x = res.get(pid) or {}
        title = x.get("title") or ""
        items.append({
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pid}/", "title": title,
            "author": ", ".join(a.get("name", "") for a in (x.get("authors") or [])[:5]),
            "published_at": x.get("sortpubdate") or x.get("pubdate"),
            "summary": (abstracts.get(pid) or f"{x.get('fulljournalname', '')} {x.get('pubdate', '')}：{title}")[:600],
            "text": abstracts.get(pid), "content_kind": "summary", "images": [], "source_id": "pubmed",
            "primary": True, "meta": {"journal": x.get("fulljournalname"), "doc_type": "医学论文摘要"},
        })
    return items


# ---------------------------------------------------------------- 社区与内容平台

# 通过搜索 API 按站点检索的平台：site=None 为全网搜索（替代百度等不允许抓取的搜索引擎）
WEB_PLATFORMS = {
    "web_search": (None, "全网搜索"),
    "wechat": ("mp.weixin.qq.com", "微信公众号"),
    "zhihu": ("zhihu.com", "知乎"),
    "tieba": ("tieba.baidu.com", "百度贴吧"),
    "xiaohongshu": ("xiaohongshu.com", "小红书"),
    "douyin": ("douyin.com", "抖音"),
}
# 这些平台的网页需要登录或前端渲染且条款禁止抓取：只保留搜索结果的摘要和链接，正文请用户粘贴
SUMMARY_ONLY = {"zhihu", "tieba", "xiaohongshu", "douyin"}


def search_web(source: dict, query: str, limit: int = 10, since: float | None = None, **_) -> list[dict]:
    from .. import websearch
    site, platform = WEB_PLATFORMS.get(source["id"], (None, "全网搜索"))
    ws = {}
    rows = websearch.search(query, site=site, count=limit, since=since, stats=ws)
    if not rows:
        source["_note"] = (f"搜索服务返回 {ws.get('raw', 0)} 条，但没有一条来自 {site}"
                           + ("（已改用 site: 语法重试）" if ws.get("retried") else "")
                           + "：该服务对这个平台收录少，或主题在这个平台讨论少") if site and ws.get("raw") \
            else f"搜索服务对「{query}」没有返回结果"
    items = []
    for x in rows:
        snippet = re.sub(r"\s+", " ", x.get("snippet") or "").strip()
        only = source["id"] in SUMMARY_ONLY
        items.append({
            "url": x["url"], "title": x.get("title") or x["url"], "author": x.get("site_name"),
            "published_at": iso(x.get("published_at")) or x.get("published_at"),
            "summary": snippet[:600], "text": snippet, "content_kind": "summary", "images": [],
            "source_id": source["id"],
            "meta": {"platform": platform if site else (x.get("site_name") or _host_of(x["url"])),
                     "via": {"source": "websearch"}, "summary_only": only,
                     **({"note": f"{platform}正文需要登录才能查看，这里只有搜索摘要；需要全文请在资料页粘贴"} if only else {})},
        })
    return items


def _host_of(url: str) -> str:
    h = urlsplit(url).netloc.lower()
    return h[4:] if h.startswith("www.") else h


# ---- 微信公众号（搜狗微信搜索，无需 key；访问稍频繁就会出验证码，只适合低频补充）

def search_sogou_wechat(source: dict, query: str, limit: int = 10, **_) -> list[dict]:
    url = "https://weixin.sogou.com/weixin?type=2&ie=utf8&query=" + quote(query)
    r = fetcher.get(url, cache_ttl=3600, headers={"Referer": "https://weixin.sogou.com/"})
    if "antispider" in r.final_url or "验证码" in r.text[:5000]:
        raise FetchError("blocked", "搜狗微信搜索要求输入验证码（访问过于频繁），本次跳过", url)
    soup = BeautifulSoup(r.text, "lxml")
    items = []
    for li in soup.select("ul.news-list > li")[:limit]:
        a = li.select_one("h3 a")
        if not a or not a.get("href"):
            continue
        title = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        summ = li.select_one("p.txt-info")
        acct = li.select_one(".all-time-y2, a.account, .s-p a")
        ts = re.search(r"timeConvert\('(\d+)'\)", str(li))
        link = urljoin("https://weixin.sogou.com", a["href"])
        items.append({
            "url": link, "title": title, "author": acct.get_text(strip=True) if acct else None,
            "published_at": iso(int(ts.group(1))) if ts else None,
            "summary": summ.get_text(" ", strip=True) if summ else "", "text": None, "content_kind": "summary",
            "images": [], "source_id": source["id"],
            "meta": {"platform": "微信公众号", "account": acct.get_text(strip=True) if acct else None,
                     "detail": "browser", "via": {"source": "sogou_wechat"}},
        })
    return items


# ---- B站（公开搜索接口；需要 buvid3 Cookie 和 WBI 签名）

_MIXIN = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39,
          12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63,
          57, 62, 11, 36, 20, 34, 44, 52]
_BILI = {"t": 0.0, "mixin": "", "cookie": ""}
_BILI_H = {"Referer": "https://search.bilibili.com/", "Origin": "https://search.bilibili.com"}


def _bili_prepare():
    import time as _t
    if _BILI["mixin"] and _t.time() - _BILI["t"] < 6 * 3600:
        return
    spi = fetcher.get("https://api.bilibili.com/x/frontend/finger/spi", respect_robots=False, cache_ttl=0,
                      headers=_BILI_H).json().get("data") or {}
    cookie = f"buvid3={spi.get('b_3', '')}; buvid4={spi.get('b_4', '')}"
    nav = fetcher.get("https://api.bilibili.com/x/web-interface/nav", respect_robots=False, cache_ttl=0,
                      headers={**_BILI_H, "Cookie": cookie}).json()
    w = (nav.get("data") or {}).get("wbi_img") or {}
    stem = lambda u: (u or "").rsplit("/", 1)[-1].split(".")[0]  # noqa: E731
    raw = stem(w.get("img_url")) + stem(w.get("sub_url"))
    if len(raw) < 64:
        raise FetchError("bad_content", "B站接口没有返回签名密钥（wbi_img），接口可能已变化", "bilibili")
    _BILI.update(t=_t.time(), mixin="".join(raw[i] for i in _MIXIN)[:32], cookie=cookie)


def bili_sign(params: dict, mixin: str, wts: int | None = None) -> str:
    """WBI 签名：参数加 wts → 按键排序 → 值去掉 !'()* → URL 编码拼接 → md5(查询串 + mixin_key)。"""
    import hashlib
    import time as _t
    p = {**params, "wts": wts if wts is not None else int(_t.time())}
    q = "&".join(f"{quote(str(k), safe='')}={quote(re.sub(r'[!()*' + chr(39) + ']', '', str(p[k])), safe='-_.~')}"
                 for k in sorted(p))
    return q + "&w_rid=" + hashlib.md5((q + mixin).encode()).hexdigest()


def search_bilibili(source: dict, query: str, limit: int = 15, since: float | None = None, **_) -> list[dict]:
    import html as _html
    _bili_prepare()
    qs = bili_sign({"search_type": "video", "keyword": query, "page": 1, "page_size": min(limit, 20)}, _BILI["mixin"])
    url = "https://api.bilibili.com/x/web-interface/wbi/search/type?" + qs
    r = fetcher.get(url, respect_robots=False, cache_ttl=0, headers={**_BILI_H, "Cookie": _BILI["cookie"]})
    d = r.json()
    if d.get("code") != 0:
        _BILI["t"] = 0          # 下次重新取 Cookie 和密钥
        raise FetchError("blocked" if d.get("code") in (-412, -352, -403) else "bad_content",
                         f"B站接口返回 {d.get('code')}：{d.get('message')}", url)
    items = []
    for x in (d.get("data") or {}).get("result") or []:
        if x.get("type") != "video" or not x.get("bvid"):
            continue
        clean = lambda s: _html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()  # noqa: E731
        title, desc = clean(x.get("title")), clean(x.get("description"))
        tags = x.get("tag") or ""
        pic = x.get("pic") or ""
        items.append({
            "url": f"https://www.bilibili.com/video/{x['bvid']}", "title": title, "author": x.get("author"),
            "published_at": iso(int(x["pubdate"])) if str(x.get("pubdate") or "").isdigit() else None,
            "summary": desc[:600], "text": f"{title}\n{desc}\n标签：{tags}".strip(), "content_kind": "summary",
            "images": [{"url": ("https:" + pic) if pic.startswith("//") else pic, "alt": title, "caption": "视频封面"}]
            if pic else [],
            "source_id": source["id"],
            "meta": {"platform": "B站", "uploader": x.get("author"), "views": x.get("play"),
                     "likes": x.get("like"), "danmaku": x.get("video_review"), "duration": x.get("duration"),
                     "tags": tags, "doc_type": "视频"},
        })
    return items


SEARCHERS = {"rss": search_rss, "arxiv": search_arxiv, "hn": search_hn, "github": search_github,
             "juejin": search_juejin, "wallstcn": search_wallstcn, "openalex": search_openalex,
             "pubmed": search_pubmed, "bilibili": search_bilibili, "sogou_wechat": search_sogou_wechat,
             "websearch": search_web}


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def window_start(days: int) -> float:
    """时间窗口起点，取整到当天 0 点（UTC），让同一天内的请求 URL 不变、能命中缓存。"""
    d = datetime.now(timezone.utc) - timedelta(days=days)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def in_window(published_at, since: float | None) -> bool:
    """无日期的资料保留（无法判断），有日期的必须在窗口内。"""
    if not since or not published_at:
        return True
    d = parse_date(published_at)
    return d is None or d.timestamp() >= since


def search(source_id: str, query: str, limit: int = 15, terms: list[str] | None = None,
           since: float | None = None, stats: dict | None = None) -> list[dict]:
    """检索一个来源。健康度记录：请求是否成功、原始条数、时间窗口过滤后条数，以及 0 条时的原因。"""
    src = db.query_one("SELECT * FROM sources WHERE id=?", (source_id,))
    if src is None:
        raise FetchError("bad_content", f"未知来源 {source_id}")
    fn = SEARCHERS.get(source_id) or SEARCHERS.get(src["method"])
    if fn is None:
        raise FetchError("bad_content", f"来源 {source_id} 不支持搜索")
    try:
        items = fn(src, query, limit, terms=terms, since=since)
    except FetchError as e:
        if e.kind != "config":      # 未配置 key 不是来源故障，不计入健康度（配置后立即可用）
            record_health(source_id, False, e)
        raise
    raw = len(items)
    if since:     # 接口不支持时间过滤的来源（RSS、arXiv、掘金等）在这里统一过滤
        kept = [it for it in items if in_window(it.get("published_at"), since)]
        if stats is not None:
            stats["dropped_old"] = len(items) - len(kept)
        items = kept
    note = src.get("_note")
    if raw and not items:
        note = f"返回 {raw} 条，全部早于时间窗口，已过滤"
    elif not raw and not note:
        note = (f"最新文章中没有与「{query[:20]}」相关的（RSS 只含各站最新几十篇，属正常）"
                if src["method"] == "rss" and not (src["config"] or {}).get("search_feed")
                else f"对「{query[:20]}」没有匹配结果")
    record_health(source_id, True, n=len(items), note=None if items else note)
    return items


# ---------------------------------------------------------------- 单页抓取（含降级）

NO_BROWSER = [
    re.compile(r"huggingface\.co/spaces/"),     # Gradio/Streamlit 应用，内容在 iframe 里且不断轮询
    re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/"),  # 摘要已由 efetch 接口取得；网页常返回验证页
    re.compile(r"\.streamlit\.app/|\.gradio\.live/"),
    re.compile(r"github\.com/[\w.-]+/[\w.-]+/?($|[?#])"),   # 仓库页静态 HTML 已含 README；README 短时浏览器也拿不到更多
    re.compile(r"arxiv\.org/(abs|pdf)/"),                    # 论文页是静态的
]


# ---------------------------------------------------------------- 复查用：轻量状态查询

def github_status(repos: list[str]) -> dict:
    """批量查询仓库最近推送时间：一次搜索请求可带多个 repo: 条件，14 个仓库约 3 次请求。
    返回 {"owner/name": {"pushed_at", "stars", "description"}}。"""
    out, chunk = {}, []
    headers = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]

    def flush(names):
        if not names:
            return
        q = " ".join(f"repo:{n}" for n in names)
        r = fetcher.get("https://api.github.com/search/repositories?per_page=100&q=" + quote(q),
                        respect_robots=False, cache_ttl=0, headers=headers)
        for repo in r.json().get("items", []):
            out[repo["full_name"].lower()] = {"pushed_at": repo.get("pushed_at"),
                                              "stars": repo.get("stargazers_count"),
                                              "description": repo.get("description") or ""}
    for n in repos:
        if len(" ".join(f"repo:{x}" for x in chunk + [n])) > 240:
            flush(chunk)
            chunk = []
        chunk.append(n)
    flush(chunk)
    return out


def arxiv_status(ids: list[str]) -> dict:
    """批量查询论文最新版本：一次请求。返回 {"2410.01234": {"version", "updated", "title", "summary", "url"}}。"""
    if not ids:
        return {}
    r = fetcher.get("https://export.arxiv.org/api/query?max_results=100&id_list=" + ",".join(ids),
                    respect_robots=False, cache_ttl=0)
    out = {}
    for e in BeautifulSoup(r.text, "xml").find_all("entry"):
        m = re.search(r"abs/([\w.\-/]+?)(v(\d+))?$", e.find("id").get_text(strip=True))
        if not m:
            continue
        out[m.group(1)] = {"version": int(m.group(3) or 1), "updated": e.find("updated").get_text(strip=True),
                           "title": re.sub(r"\s+", " ", e.find("title").get_text()).strip(),
                           "summary": re.sub(r"\s+", " ", e.find("summary").get_text()).strip(),
                           "url": e.find("id").get_text(strip=True)}
    return out


def fetch_page(url: str, mode: str = "auto", fresh: bool = False) -> dict:
    """抓取单个网页。mode: html / browser / auto（静态正文太少时自动改用浏览器渲染）。
    返回 extract_article 的结果 + {final_url, via_browser, attempts}。"""
    attempts = []
    art = None
    if mode == "auto" and any(p.search(url) for p in NO_BROWSER):
        mode = "html"         # 这类页面浏览器渲染也拿不到正文，只会白等 20 多秒
    if mode in ("html", "auto"):
        try:
            r = fetcher.get(url, cache_ttl=0 if fresh else None)     # 复查必须绕过缓存
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
