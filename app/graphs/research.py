"""Lv1 主题检索：ReAct 循环。

    plan ─→ decide ⇄ act ─→ images ─→ report
               ↑      │
               └──────┘  （每次行动的观察结果更新覆盖矩阵，决定下一步）

- plan    结合历史记忆（上次采集时间、已知资料、上次有效关键词）制定子问题与关键词；
          有 LLM 时由模型拆解，否则用规则后备。
- decide  “判断”：看覆盖矩阵和待办队列，选下一步行动；队列空了就找缺口、调整关键词；
          覆盖足够、达到资料上限或关键词调整次数用完时停止。
- act     “行动 + 观察”：调用检索/抓取工具，入库（去重、归并、版本、新进展分类），
          根据结果追加后续行动：只有标题的聚合条目 → 追原文；摘要源 → 抓全文（必要时
          浏览器渲染）；正文里引用的论文/仓库 → 追原始来源。失败时降级或转人工。
- images  为相关度最高的资料下载、质检配图。
- report  覆盖情况、失败与降级、可能遗漏。
"""
from __future__ import annotations

import json
import re
import time
from urllib.parse import urlsplit
from datetime import datetime, timedelta, timezone

from .. import db, llm, ranking, websearch
from .. import memory as mem
from .. import memory_facts as mf
from ..collect import tools as _tools  # noqa: F401  注册工具
from ..collect.dedup import canonical_url, parse_date, title_sim
from ..collect.fetch import FetchError
from ..collect.images import store_images
from ..keywords import is_english
from ..collect.sources import (DOMAINS, guess_domains, in_window, is_primary, list_sources,
                                source_domains, topic_terms, window_start)
from ..collect.store import EVENT_TITLE_SIM, article_to_item, topic_last_run, upsert_document
from ..harness import END, Graph, register_graph

DEFAULT_ASPECTS = [
    {"id": "overview", "name": "概况与背景", "kind": "any", "need": 3, "keywords": []},
    {"id": "primary", "name": "一手来源（论文/仓库/官方）", "kind": "primary", "need": 2, "keywords": []},
    {"id": "eval", "name": "评测与数据", "kind": "keywords", "need": 2,
     "keywords": ["评测", "基准", "测试", "实测", "跑分", "对比", "榜单", "benchmark", "eval",
                  "evaluation", "leaderboard", "score"]},
    {"id": "debate", "name": "争议、局限与复现", "kind": "keywords", "need": 1,
     "keywords": ["质疑", "争议", "局限", "不足", "缺陷", "复现", "翻车", "问题", "批评",
                  "limitation", "criticism", "controvers", "reproduc", "flaw", "fail"]},
    {"id": "recent", "name": "最新进展（14 天内）", "kind": "recent", "need": 2, "keywords": []},
]
MAX_FOLLOW_PER_DOC = 2
API_SOURCES = ("arxiv", "hn", "github", "juejin", "wallstcn", "openalex", "pubmed")
ACADEMIC_SOURCES = ("arxiv", "openalex", "pubmed", "github")
# 来自搜索 API / 搜索引擎的结果：命中常常是页面里碰巧出现了关键词（如股票行情页），先按标题和摘要过滤再决定是否抓全文
SEARCH_RESULT_SOURCES = ("web_search", "wechat", "sogou_wechat", "zhihu", "tieba", "xiaohongshu", "douyin")
COMMUNITY_SOURCES = ("bilibili", "sogou_wechat", "wechat", "zhihu", "tieba", "xiaohongshu", "douyin")
# 社区 / 小众话题的默认子问题：替换面向科技主题的“一手来源（论文/仓库）”“评测与数据”
COMMUNITY_ASPECTS = [
    {"id": "official", "name": "当事人 / 官方公开说法", "kind": "keywords", "need": 1,
     "keywords": ["官方", "声明", "回应", "本人", "采访", "访谈", "自述", "公告", "官宣", "澄清", "辟谣", "说明",
                  "直播中说", "亲口"]},
    {"id": "discussion", "name": "社区与粉丝讨论（B站、公众号、知乎、贴吧、小红书、抖音）", "kind": "platform", "need": 2,
     "sources": list(COMMUNITY_SOURCES)},
    {"id": "works", "name": "相关作品与内容", "kind": "keywords", "need": 1,
     "keywords": ["作品", "视频", "动画", "漫画", "游戏", "专辑", "发布", "上线", "更新", "新作", "企划", "cos", "角色"]},
]


# ---------------------------------------------------------------- 工具函数

def _doc_hits(doc: dict, aspect: dict) -> bool:
    k = aspect.get("kind", "keywords")
    meta = doc.get("meta") or {}
    if k == "any":
        return True
    if k == "primary":
        return bool(meta.get("primary")) or is_primary(doc.get("url") or "")
    if k == "platform":
        return doc.get("source_id") in (aspect.get("sources") or [])
    if k == "recent":
        d = parse_date(doc.get("published_at"))
        return bool(d and datetime.now(timezone.utc) - d <= timedelta(days=14))
    blob = f"{doc.get('title') or ''} {(doc.get('text') or '')[:6000]}".lower()
    return any(_kw_hit(kw, blob) for kw in aspect.get("keywords", []))


def _kw_hit(kw: str, blob: str) -> bool:
    """关键词命中：原文出现；英文多词短语各词都出现（不要求相邻，如 position ... interpolation）；
    中文长词按二元组覆盖 ≥0.75（“位置外推”能匹配“外推位置编码”）。"""
    k = (kw or "").strip().lower()
    if len(k) < 2:
        return False
    if k.isascii() and len(k) <= 5 and re.fullmatch(r"[a-z0-9-]+", k):
        # 短英文词/缩写（RAG、RoPE、YaRN）按整词匹配，避免 rag 命中 average
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(k)}s?(?![a-z0-9])", blob))
    if k in blob:
        return True
    if k.isascii():
        words = [w for w in re.split(r"[^a-z0-9]+", k) if len(w) > 2]
        return len(words) >= 2 and all(re.search(rf"\b{re.escape(w)}", blob) for w in words)
    if len(k) < 4:
        return False
    grams = {k[i:i + 2] for i in range(len(k) - 1)}
    return sum(g in blob for g in grams) / len(grams) >= 0.6


def _task_docs(task_id: str) -> list[dict]:
    return db.query("SELECT id, url, title, text, published_at, source_id, content_kind, novelty, "
                    "duplicate_of, event_id, meta FROM documents WHERE status='active' AND "
                    "meta LIKE ?", (f"%{task_id}%",))


GENERIC = {"llm", "llms", "ai", "model", "models", "language", "large", "open", "source", "new", "based",
           "using", "learning", "deep", "neural", "network", "networks", "data", "system", "systems", "method",
           "the", "and", "for", "with", "大模型", "模型", "人工智能", "开源", "技术", "方法", "研究", "最新", "进展"}


def _norm(t: str) -> str:
    return re.sub(r"[-_/]+", " ", (t or "").lower())


def _rel_zh(terms: list[str], title: str, body: str) -> float:
    strong = [x for x in terms if x.lower() not in GENERIC] or terms
    if not strong:
        return 0.0
    score = 0.0
    for t in strong:
        tl = t.lower()
        if tl in title:
            score += 1
        elif tl in body:
            score += 0.7
        elif len(tl) >= 4:
            g = {tl[i:i + 2] for i in range(len(tl) - 1)}
            cov = sum(x in title or x in body for x in g) / len(g)
            score += 0.5 * cov if cov >= 0.6 else 0
    return score / len(strong)


def _rel_en(q: str, title: str, body: str) -> float:
    words = [w for w in re.split(r"[^a-z0-9]+", _norm(q)) if len(w) > 1]
    strong = [w for w in words if w not in GENERIC] or words
    if not strong:
        return 0.0
    phrase = " ".join(words)
    if phrase in title:
        return 1.0
    if phrase in body:
        return 0.85
    stem = lambda w: re.escape(w[:5] if len(w) > 5 else w)   # 词干：prediction/predicting、trend/trends
    sc = 0.0
    for w in strong:
        pat = rf"\b{stem(w)}"
        sc += 1 if re.search(pat, title) else 0.6 if re.search(pat, body) else 0
    sc = 0.9 * sc / len(strong)
    if len(strong) >= 2:
        # 关键词只是零散出现、没有任何两个相邻（如 long ... context 出现在不相干的句子里）：判为低相关
        adj = any(re.search(rf"\b{stem(a)}\w*\W+(\w+\W+)?{stem(b)}", title + " " + body)
                  for a, b in zip(strong, strong[1:]))
        if not adj:
            sc = min(sc, 0.35)
    return sc


def _rel(st: dict, title: str, body: str = "") -> float:
    """相关度 0-1：核心词按短语匹配，标题权重高于正文，泛词（LLM、大模型…）不计分，零散命中封顶。"""
    plan = st.get("plan") or {}
    t, b = _norm(title), _norm((body or "")[:8000])
    best = _rel_zh(plan.get("terms_zh") or [], t, b) if plan.get("terms_zh") else 0.0
    for q in plan.get("queries_en") or []:
        best = max(best, _rel_en(q, t, b))
    return round(min(best, 1.0), 3)


def _min_rel(st: dict) -> float:
    return st.get("min_rel", 0.4)


def _coverage(ctx) -> dict:
    docs = [d for d in _task_docs(ctx.task_id) if not d["duplicate_of"] and
            ((d["meta"] or {}).get("relevance") is None or (d["meta"] or {}).get("relevance") >= _min_rel(ctx.state))]
    cov = {}
    for a in ctx.state["aspects"]:
        hits = [d["id"] for d in docs if _doc_hits(d, a)]
        cov[a["id"]] = {"name": a["name"], "need": a["need"], "have": len(hits), "docs": hits[:20]}
    return cov


def _gaps(cov: dict) -> list[str]:
    return [k for k, v in cov.items() if v["have"] < v["need"]]


def _push(state, action: dict) -> bool:
    key = action["key"]
    if key in state["done"] or any(a["key"] == key for a in state["queue"]):
        return False
    state["queue"].append(action)
    return True


def _search_action(source_id, query, terms, why, prio=50):
    return {"type": "search", "source": source_id, "query": query, "terms": terms, "why": why,
            "prio": prio, "key": f"search:{source_id}:{query}"}


def _fetch_action(url, why, prio=40, mode="auto", base=None, doc_id=None):
    return {"type": "fetch", "url": url, "mode": mode, "why": why, "prio": prio, "base": base,
            "doc_id": doc_id, "key": f"fetch:{canonical_url(url)}"}


def _prev_research(topic: str, exclude: str) -> dict | None:
    return db.query_one("SELECT id, state, updated_at FROM tasks WHERE topic=? AND graph='research' "
                        "AND status='done' AND id!=? ORDER BY updated_at DESC LIMIT 1",
                        (topic, exclude))


# ---------------------------------------------------------------- 节点：plan

PLAN_PROMPT = """你是信息检索规划助手。用户想了解的主题：「{topic}」。
{memory}
先判断主题属于哪些领域（可多选）：tech=科技/AI/软件，finance=金融/经济/公司，science=学术/医学/科研，
general=综合新闻/政策/社会，community=社区/小众话题/亚文化/个人经验/具体博主或团队（资料主要在 B站、公众号、
知乎、贴吧、小红书、抖音）。系统会按领域选择信息源。
再把主题拆成 3-5 个需要覆盖的子问题，并给出检索关键词。
中文来源是各领域媒体 RSS（按关键词过滤最新文章）和站内搜索（掘金、华尔街见闻等）。中文核心词要用中文媒体
实际常用的说法，短（2-5 字）、常见，不要自造少见词组。
英文来源是 arXiv、OpenAlex、PubMed、Hacker News、GitHub 等（检索式要短，2-4 个英文词）。
子问题的 keywords 用来判断一篇资料是否覆盖了该子问题，要给 8-12 个：中文常用说法 + 英文术语
+ 常见缩写/代表性方法名（例如“位置外推”还要给 RoPE、ALiBi、YaRN、extrapolation、position interpolation）。
每个子问题再给一条英文检索式 search_en（2-4 词，含主题词）和一个中文检索词 search_zh（2-6 字）。
只输出 JSON：
{{"domains": ["finance"],
  "core_terms_zh": ["主题核心词，2-4个，越短越好"],
  "aspects": [{{"name": "子问题", "keywords": ["中英文关键词、术语、缩写"],
               "search_en": "英文检索式", "search_zh": "中文检索词"}}],
  "queries_en": ["英文检索式，2-4 条"],
  "notes": "一句话说明检索策略"}}"""


def plan(ctx):
    p = ctx.params
    st = ctx.state
    st.update(queue=[], done=[], failures=[], source_fail={}, disabled=[], zero=[],
              refine_round=0, fetches=0, follow_count={}, saved=[], stats={})
    # 时间窗口：新闻/社区类来源默认 90 天；论文/仓库类默认放宽到 4 倍（至少 1 年），基础论文不至于被全部滤掉
    days = int(p.get("days") or 90)
    ac_days = int(p.get("academic_days") or max(365, days * 4))
    st["window"] = {"days": days, "academic_days": ac_days,
                    "since": window_start(days), "since_academic": window_start(ac_days)}
    st["min_rel"] = float(p.get("min_relevance", 0.4))
    last_run = topic_last_run(ctx.topic, exclude_task=ctx.task_id)
    st["last_run_at"] = last_run
    known = db.query_one("SELECT COUNT(*) n FROM documents WHERE topic=? AND status='active'",
                         (ctx.topic,))["n"]
    prev = _prev_research(ctx.topic, ctx.task_id)
    prev_plan = (prev or {}).get("state", {}).get("plan", {}) if prev else {}

    memory_note = ""
    if last_run:
        when = datetime.fromtimestamp(last_run).strftime("%m-%d %H:%M")
        memory_note = (f"历史记忆：该主题上次采集于 {when}，资料库已有 {known} 篇；"
                       f"上次关键词：{prev_plan.get('terms_zh')} / {prev_plan.get('queries_en')}。"
                       "本次重点找新进展和更正，已有资料只做去重比对。")
        ctx.think(memory_note)

    # ---- 召回长期记忆：用户偏好 + 主题事实/争议/历史交付（在 token 预算内）
    mem.expire_due()
    rec = mem.recall(ctx.user_id, ctx.topic, "research", p.get("memory_budget", 1200), ctx.task_id,
                     query_terms=topic_terms(ctx.topic))
    eff = mem.effective_preferences(rec, p.get("this_time"))
    st["memory"] = {"used": rec["used"], "lines": rec["text"].splitlines(), "tokens": rec["tokens"],
                    "dropped": rec["dropped"], "pref_notes": eff["notes"],
                    "prefs": {k: {"value": v["value"], "from": v["from"]} for k, v in eff["prefs"].items()}}
    if rec["used"]:
        ctx.think(f"召回记忆 {len(rec['used'])} 条（约 {rec['tokens']} token，预算外舍弃 {rec['dropped']} 条）：\n"
                  + "\n".join(rec["text"].splitlines()[:8]))
        memory_note += "\n已知记忆：\n" + rec["text"]
    for n in eff["notes"]:
        ctx.think(n)

    english_topic = is_english(ctx.topic)
    terms_zh = list(dict.fromkeys(([] if english_topic else topic_terms(ctx.topic)) + list(p.get("keywords") or [])))
    queries_en = list(p.get("en_keywords") or [])
    if english_topic and ctx.topic not in queries_en:
        queries_en.insert(0, ctx.topic)
    aspects = [dict(a) for a in DEFAULT_ASPECTS]
    prefs = eff["prefs"]
    llm_domains = []
    alt_zh, alt_en = [], []
    # ---- 中英文关键词自动翻译（默认开启）：缺哪种语言就补哪种；同义词留作检索无结果时自动调整
    if p.get("auto_translate", True):
        try:
            tr = ctx.tool("translate_keywords", topic=ctx.topic, zh=list(p.get("keywords") or []),
                          en=list(p.get("en_keywords") or []), use_llm=not p.get("no_llm"))
            added_zh = [t for t in tr["terms_zh"] if t not in terms_zh]
            added_en = [q for q in tr["queries_en"] if q not in queries_en]
            if not p.get("keywords") or english_topic:
                terms_zh = list(dict.fromkeys(terms_zh + added_zh))
            else:
                added_zh = []
            if not p.get("en_keywords"):
                queries_en = list(dict.fromkeys(queries_en + added_en))[:4]
            else:
                added_en = []
            alt_zh, alt_en = tr.get("alt_zh") or [], tr.get("alt_en") or []
            llm_domains = [x for x in tr.get("domains") or [] if x in DOMAINS] if tr.get("by") == "llm" else []
            ctx.think(f"自动翻译（{'大模型' if tr.get('by') == 'llm' else '术语词典'}"
                      f"{'，缓存' if tr.get('cached') else ''}）："
                      + (f"补充中文词 {added_zh}；" if added_zh else "")
                      + (f"补充英文检索式 {added_en}；" if added_en else "")
                      + (f"备用说法 {alt_zh + alt_en}；" if alt_zh or alt_en else "")
                      + "；".join(tr.get("notes") or []))
        except Exception as e:
            ctx.think(f"自动翻译失败（{str(e)[:60]}），按原关键词检索")

    def _vals(v):
        xs = [str(x).strip() for x in (re.split(r"[,，、;；]", v) if isinstance(v, str) else v or [])]
        return [x for x in xs if len(x) >= 2 and not x.isdigit()]     # 过滤“5”“q”这类无意义输入
    plan_src = "rules"
    if llm.available() and not p.get("no_llm"):
        try:
            res = ctx.tool("llm", messages=[
                {"role": "system", "content": "你只输出 JSON。"},
                {"role": "user", "content": PLAN_PROMPT.format(topic=ctx.topic, memory=memory_note)}])
            d = res["data"]
            llm_domains = [x for x in (d.get("domains") or []) if x in DOMAINS] or llm_domains
            terms_zh = list(dict.fromkeys((d.get("core_terms_zh") or []) + list(p.get("keywords") or []))) or terms_zh
            queries_en = list(dict.fromkeys(queries_en + (d.get("queries_en") or [])))[:4]
            for i, a in enumerate(d.get("aspects") or []):
                if a.get("name") and a.get("keywords"):
                    aspects.append({"id": f"llm{i}", "name": a["name"], "kind": "keywords",
                                    "need": 1, "keywords": [str(k) for k in a["keywords"]][:16],
                                    "search_en": str(a.get("search_en") or "")[:60],
                                    "search_zh": str(a.get("search_zh") or "")[:12]})
            plan_src = "llm"
            ctx.think(f"LLM 规划：{d.get('notes', '')}")
        except Exception as e:  # LLM 失败不影响流程
            ctx.think(f"LLM 规划失败（{str(e)[:80]}），使用规则后备")
    elif not queries_en:
        last_q = next((d["meta"].get("queries_en") for d in rec["deliveries"]
                       if (d["meta"] or {}).get("queries_en")), None) or prev_plan.get("queries_en")
        if last_q:
            queries_en = last_q
            ctx.think(f"未配置 LLM，从历史交付记忆复用有效的英文关键词 {queries_en}")

    # ---- 偏好影响检索：关注重点 → 增加关键词和子问题；排除内容 → 过滤；来源偏好 → 提高优先级
    fv = _vals(prefs["focus"]["value"]) if prefs.get("focus") else []
    if fv:
        terms_zh = list(dict.fromkeys(terms_zh + fv))
        aspects.append({"id": "user_focus", "name": f"用户关注：{'、'.join(fv)}", "kind": "keywords",
                        "need": 2, "keywords": fv})
        ctx.think(f"按{'本次要求' if prefs['focus']['from'] == 'this_time' else '长期偏好'}，"
                  f"增加关注重点 {fv}（加入检索词，并要求至少 2 篇资料覆盖）")
    st["exclude"] = _vals(prefs["exclude"]["value"]) if prefs.get("exclude") else []
    if st["exclude"]:
        ctx.think(f"按偏好排除包含 {st['exclude']} 的资料")
    st["prefer_sources"] = _vals(prefs["source_prefer"]["value"]) if prefs.get("source_prefer") else []

    # 主题核心词也作为“概况”的判断依据
    aspects[0]["keywords"] = terms_zh + queries_en
    st["aspects"] = aspects
    st["plan"] = {"terms_zh": terms_zh, "queries_en": queries_en, "by": plan_src,
                  "alt_zh": alt_zh, "alt_en": alt_en}

    # ---- 按领域选来源：用户明确指定 > 用户选择的领域 > 大模型判断 > 关键词规则
    domains = list(p.get("domains") or llm_domains or guess_domains(" ".join([ctx.topic] + terms_zh + queries_en)))
    st["domains"] = domains
    if "community" in domains and not set(domains) & {"tech", "science"}:
        drop = {"primary", "eval"}
        base = [a for a in st["aspects"] if a["id"] not in drop]
        st["aspects"] = base[:1] + [dict(a) for a in COMMUNITY_ASPECTS] + base[1:]
        ctx.think("社区 / 小众话题：子问题改为“当事人/官方说法”“社区与粉丝讨论”“相关作品与内容”，"
                  "不再要求论文/仓库类一手来源和评测数据")
    enabled = [s for s in list_sources() if s["id"] != "web"]
    if p.get("sources"):
        srcs = [s for s in enabled if s["id"] in set(p["sources"])]
        ctx.think(f"使用用户指定的 {len(srcs)} 个来源")
    else:
        srcs = [s for s in enabled if set(source_domains(s)) & set(domains)]
        if len(srcs) < 4:     # 来源太少时用综合新闻和跨学科学术库兜底
            srcs += [s for s in enabled if s not in srcs and set(source_domains(s)) & {"general"}]
        skipped = [s["name"] for s in enabled if s not in srcs]
        ctx.think(f"主题领域：{'、'.join(DOMAINS.get(x, x) for x in domains)}"
                  f"（{'用户指定' if p.get('domains') else '大模型判断' if llm_domains else '关键词规则'}）。"
                  f"选用 {len(srcs)} 个来源：{'、'.join(s['name'] for s in srcs)}"
                  + (f"；领域不匹配未用：{'、'.join(skipped)}" if skipped else ""))
    # 近期连续失败的来源先跳过（省掉每次十几秒的超时等待）；用户明确指定的来源不跳过
    if not p.get("sources") and not p.get("include_unhealthy"):
        bad = []
        for s in list(srcs):
            h = s.get("health") or {}
            recent = h.get("last_fail") and db.now() - h["last_fail"] < 12 * 3600
            robots_no = (h.get("last_error") or {}).get("kind") == "robots" and (h.get("last_ok") or 0) < (h.get("last_fail") or 0)
            if robots_no or (h.get("consecutive_fail", 0) >= 3 and recent and (h.get("last_ok") or 0) < h["last_fail"]):
                srcs.remove(s)
                bad.append(f"{s['name']}（{(h.get('last_error') or {}).get('kind', '失败')} 连续 {h['consecutive_fail']} 次）")
        if bad:
            st["skipped_unhealthy"] = bad
            ctx.think(f"跳过近期持续失败的来源：{'、'.join(bad)}。可在“信息源”页点“测试”恢复")
    st["source_ids"] = [s["id"] for s in srcs]
    for s in srcs:
        lang = (s["config"] or {}).get("lang", "zh")
        if s["method"] == "rss" and lang == "zh" and (s["config"] or {}).get("search_feed"):
            for t in terms_zh[:2]:
                _push(st, _search_action(s["id"], t, terms_zh, f"站内搜索「{t}」（可检索历史文章）",
                                         prio=55))
        elif s["method"] == "rss" and lang == "zh":
            _push(st, _search_action(s["id"], " ".join(terms_zh), terms_zh,
                                     f"中文 RSS 按核心词 {terms_zh} 过滤最新文章"))
        elif s["method"] == "api" and lang == "zh":
            for t in terms_zh[:2]:
                _push(st, _search_action(s["id"], t, terms_zh, f"中文站内搜索「{t}」（可检索历史文章）",
                                         prio=55))
        elif s["method"] in ("websearch", "search"):
            if s["method"] == "websearch" and not websearch.available():
                st.setdefault("no_websearch", []).append(s["name"])
                continue
            q = " ".join(terms_zh[:2]) or ctx.topic
            _push(st, _search_action(s["id"], q, terms_zh, f"{s['name']}检索「{q}」", prio=55))
        elif lang == "en":
            if not queries_en:
                continue
            for q in queries_en[:2]:
                _push(st, _search_action(s["id"], q, None, f"英文来源检索「{q}」"))
    if st.get("no_websearch"):
        ctx.think(f"未配置搜索 API，跳过 {'、'.join(st['no_websearch'])}（在“设置”页填写 key 后可用）")
    if not queries_en:
        st["failures"].append({"type": "plan", "kind": "missing_en_keywords",
                               "reason": "没有英文关键词（未配置 LLM 且未填写），跳过 arXiv/HN/GitHub"})
        ctx.think("没有英文关键词：英文来源本轮跳过，会在覆盖报告中列为可能遗漏")
    for u in p.get("urls") or []:
        _push(st, _fetch_action(u, "用户指定的链接", prio=90, base={"source_id": "web"}))
    # ---- 记忆驱动的核验：未解争议 → 补充检索；需复查的时效性事实 → 重新抓取原文
    verify_srcs = [s for s in srcs if (s["config"] or {}).get("lang") == "zh" and
                   (s["method"] == "api" or (s["config"] or {}).get("search_feed"))]
    for dsp in rec["disputes"]:
        q = (dsp["meta"] or {}).get("query") or dsp["content"][:16]
        for s in verify_srcs[:2]:
            _push(st, _search_action(s["id"], q, terms_zh, f"记忆中有未解争议「{dsp['content'][:40]}」，补充核验",
                                     prio=58))
    for f in rec["facts"]:
        if f["status"] != "stale":
            continue
        for e in db.query("SELECT d.url FROM evidence e JOIN documents d ON d.id=e.doc_id "
                          "WHERE e.claim_id=? AND e.stance='support' LIMIT 1", (f["id"],)):
            _push(st, dict(_fetch_action(e["url"], f"记忆中的时效性事实需复查：「{f['content'][:30]}」", prio=58), force=True))
    for a in st["queue"]:
        a["initial"] = True        # 初始检索：保证每个来源至少查一次，不因资料数量提前达标而跳过
        if a["type"] == "search" and a["source"] in st["prefer_sources"]:
            a["prio"] += 10
            a["why"] += "（偏好来源，优先）"
    st["per_source"] = {}
    ctx.observe(f"子问题 {len(aspects)} 个；初始行动 {len(st['queue'])} 个；"
                f"中文词 {terms_zh}；英文检索式 {queries_en}")
    return "decide"


# ---------------------------------------------------------------- 节点：decide

GAP_PROMPT = """主题「{topic}」的资料还没有覆盖下面这些子问题（括号里是目前判断覆盖用的关键词）：
{gaps}
已经用过、没有结果的检索：{zero}。
对每个子问题：
1) 补充 6-10 个判断覆盖用的同义说法：中文常用说法、英文术语、缩写、代表性方法/数据集名，不要重复已有的；
2) 给一条新的英文检索式 search_en（2-4 词，包含主题词）和一个中文检索词 search_zh（2-6 字）。
只输出 JSON：{{"gaps": [{{"id": "子问题 id", "keywords": [...], "search_en": "...", "search_zh": "..."}}],
"why": "一句话"}}"""

EN_GAP_SOURCES = {"science": ["arxiv", "openalex", "pubmed"], "default": ["arxiv", "openalex", "hn", "github"]}


def _gap_actions(st: dict, a: dict) -> list[dict]:
    """针对单个缺口的定向检索：只用 2 个最合适的英文源 + 2 个中文站内搜索，而不是所有来源全查一遍。"""
    by_id = {s["id"]: s for s in list_sources()}
    ok = [sid for sid in st.get("source_ids", []) if sid not in st["disabled"] and sid in by_id]
    plan = st["plan"]
    q_en0 = (plan.get("queries_en") or [""])[0]
    head_en = " ".join(q_en0.split()[:2])
    core_zh = (plan.get("terms_zh") or [""])[0]
    used_alt = st.setdefault("used_alt", [])
    kind = a.get("kind")
    en_q = zh_q = ""
    if kind == "any":                               # 概况不足：多半是译法不对，换备用说法
        alt = next((x for x in plan.get("alt_en") or [] if x not in used_alt), "")
        if alt:
            used_alt.append(alt)
            en_q = alt if len(alt.split()) >= 2 else f"{head_en} {alt}".strip()
        zh_q = next((x for x in plan.get("alt_zh") or [] if x not in used_alt), "") or core_zh
        used_alt.append(zh_q)
    elif kind in ("primary", "recent"):
        en_q, zh_q = q_en0, core_zh
    else:
        kws = a.get("keywords") or []
        en_kw = next((k for k in kws if k.isascii() and k.lower() not in head_en.lower()), "")
        en_q = a.get("search_en") or (f"{head_en} {' '.join(en_kw.split()[:2])}".strip() if en_kw else "")
        zh_kw = next((k for k in kws if not k.isascii() and 2 <= len(k) <= 6), "")
        zh_q = a.get("search_zh") or zh_kw
        if zh_q and core_zh and core_zh not in zh_q:
            zh_q = f"{core_zh} {zh_q}"
    pref = ["github", "arxiv", "openalex"] if kind == "primary" else \
        ["hn", "arxiv", "github"] if kind == "recent" else \
        EN_GAP_SOURCES["science" if "science" in (st.get("domains") or []) else "default"]
    en_srcs = [s for s in pref if s in ok][:2]
    zh_srcs = [sid for sid in ok if (by_id[sid]["config"] or {}).get("lang", "zh") == "zh" and
               (by_id[sid]["method"] in ("api", "search") or (by_id[sid]["config"] or {}).get("search_feed") or
                (by_id[sid]["method"] == "websearch" and websearch.available()))][:2]
    why = f"定向补缺口「{a['name']}」"
    acts = []
    if en_q:
        acts += [_search_action(s, en_q, None, f"{why}：{en_q}", prio=60) for s in en_srcs]
    if zh_q:
        terms = [t for t in zh_q.split() if t]
        acts += [_search_action(s, zh_q, terms, f"{why}：{zh_q}", prio=60) for s in zh_srcs]
    return acts


def decide(ctx):
    st, p = ctx.state, ctx.params
    max_docs = p.get("max_docs", 40)
    cov = _coverage(ctx)
    st["coverage"] = cov
    n_docs = len(_task_docs(ctx.task_id))
    gaps = _gaps(cov)
    cov_txt = "；".join(f"{v['name']} {v['have']}/{v['need']}" for v in cov.values())
    for g, log in st.get("gap_log", {}).items():          # 定向补检的缺口：记录何时补上、花了多久
        if log["status"] == "open" and g not in gaps:
            log.update(status="closed", seconds=round(time.time() - log["t0"], 1))
            ctx.think(f"缺口「{log['name']}」已补上（{log['how']}，用时 {log['seconds']}s）")

    queue = [a for a in st["queue"] if not (a["type"] == "search" and a["source"] in st["disabled"])]
    pending_fetch = any(a["type"] == "fetch" for a in queue) and st["fetches"] < p.get("max_fetch", 15)
    pending_initial = any(a.get("initial") and a["type"] == "search" for a in queue)
    if n_docs >= max_docs and not gaps and not pending_fetch and not pending_initial:
        ctx.think(f"已收集 {n_docs} 篇（上限 {max_docs}），覆盖矩阵：{cov_txt}。覆盖足够、全文补抓完成，停止检索")
        return "judge"
    if n_docs >= max_docs:
        # 达到上限后不再扩量：只补全文，以及还没轮到的来源各查一次（保证来源多样性）
        queue = [a for a in queue if a["type"] == "fetch" or a.get("initial") or a.get("gap")]
    if p.get("max_fetch", 15) <= st["fetches"]:
        queue = [a for a in queue if a["type"] != "fetch"]

    # ---- 子问题全部覆盖：进入收尾模式，不再继续扩量
    #   保留：还没查过的来源各查一次（来源多样性，且是并行批次，很快）；
    #         少量高价值全文补抓（追溯一手来源、高分聚合条目、高相关摘要），最多 enrich_after_cover 篇；
    #   放弃：其余摘要补抓和后续检索。
    if not gaps:
        if "covered_at" not in st:
            st["covered_at"] = {"fetches": st["fetches"], "docs": n_docs}
        budget_left = max(0, p.get("enrich_after_cover", 6) - (st["fetches"] - st["covered_at"]["fetches"]))
        initial = [a for a in queue if a.get("initial") and a["type"] == "search"]
        valuable = sorted([a for a in queue if a["type"] == "fetch" and
                           (a["prio"] >= 40 or ((a.get("base") or {}).get("relevance") or 0) >= 0.6)],
                          key=lambda a: -a["prio"])[:budget_left]
        dropped = len(queue) - len(initial) - len(valuable)
        if dropped > 0:
            st["dropped_after_cover"] = st.get("dropped_after_cover", 0) + dropped
        queue = initial + valuable
        if not queue:
            st["queue"] = []
            ctx.think(f"覆盖矩阵：{cov_txt}。所有子问题已覆盖，收尾补抓完成"
                      + (f"，放弃剩余 {st.get('dropped_after_cover', 0)} 个低价值行动" if st.get("dropped_after_cover") else "")
                      + "，停止检索")
            return "judge"
        if dropped > 0:
            ctx.think(f"所有子问题已覆盖，进入收尾：只保留 {len(initial)} 个未查来源和 {len(valuable)} 篇高价值全文补抓，"
                      f"放弃 {dropped} 个低价值行动")
    st["queue"] = queue

    if queue:
        queue.sort(key=lambda a: -a["prio"])
        # 一次取一批同类型、不同站点的行动并行执行（同一站点仍串行，遵守限速）
        first = queue[0]
        width = p.get("parallel", 6)
        if first["type"] == "fetch":
            width = min(width, max(1, p.get("max_fetch", 15) - st["fetches"]))
        batch, hosts = [], set()
        for a in list(queue):
            if len(batch) >= width:
                break
            if a["type"] != first["type"]:
                continue
            host = a.get("source") or urlsplit(a.get("url", "")).netloc
            if host in hosts:
                continue
            hosts.add(host)
            batch.append(a)
            queue.remove(a)
        st["current_batch"] = batch
        lines = [f"{a['type']} {a.get('source', '') or a.get('url', '')} {a.get('query', '')}：{a['why']}" for a in batch]
        ctx.think(f"覆盖：{cov_txt}。下一步并行执行 {len(batch)} 个行动 →\n" + "\n".join(lines))
        return "act"

    # 队列空：还有缺口 → 逐个缺口定向补检（每个缺口最多 gap_tries 次，之后标记“暂无足够资料”）
    gap_log = st.setdefault("gap_log", {})
    tries_cap = p.get("gap_tries", 1)
    active = [g for g in gaps if gap_log.get(g, {}).get("tries", 0) < tries_cap]
    if active and st["refine_round"] < p.get("max_refine", 2):
        st["refine_round"] += 1
        asp = {a["id"]: a for a in st["aspects"]}
        for g in active:
            log = gap_log.setdefault(g, {"name": cov[g]["name"], "t0": time.time(), "tries": 0,
                                         "queries": [], "added_keywords": [], "status": "open", "how": ""})
            log["tries"] += 1
        why = ""
        # 第一步（不联网）：让大模型补充同义词/英文术语/缩写，先用已有资料重新判断覆盖
        if llm.available() and not p.get("no_llm"):
            try:
                desc = "\n".join(f"- id={g}：{asp[g]['name']}（{'、'.join(asp[g].get('keywords', [])[:10]) or '无'}）"
                                 for g in active)
                d = ctx.tool("llm", messages=[{"role": "user", "content": GAP_PROMPT.format(
                    topic=ctx.topic, gaps=desc, zero=[k.split(':', 2)[-1] for k in st["zero"][-8:]])}])["data"]
                why = d.get("why", "")
                for x in d.get("gaps") or []:
                    a = asp.get(x.get("id"))
                    if not a or a["id"] not in active:
                        continue
                    new_kw = [str(k) for k in x.get("keywords") or [] if str(k) not in a.setdefault("keywords", [])]
                    a["keywords"] = (a["keywords"] + new_kw)[:28]
                    gap_log[a["id"]]["added_keywords"] += new_kw
                    if x.get("search_en"):
                        a["search_en"] = str(x["search_en"])[:60]
                    if x.get("search_zh"):
                        a["search_zh"] = str(x["search_zh"])[:12]
            except Exception as e:
                why = f"大模型补充关键词失败（{str(e)[:60]}），按已有关键词定向检索"
            cov2 = _coverage(ctx)
            closed = [g for g in active if cov2[g]["have"] >= cov2[g]["need"]]
            for g in closed:
                gap_log[g].update(status="closed", how="扩充同义词后已有资料即可覆盖，未额外检索",
                                  seconds=round(time.time() - gap_log[g]["t0"], 1))
            if closed:
                st["coverage"] = cov = cov2
                cov_txt = "；".join(f"{v['name']} {v['have']}/{v['need']}" for v in cov.values())
                ctx.think(f"扩充同义词/英文术语后，已有资料就覆盖了：{[cov2[g]['name'] for g in closed]}（未额外检索）")
            active = [g for g in active if g not in closed]
        # 第二步：剩下的缺口，每个只发 ≤4 个定向检索（2 个英文源 + 2 个中文站内搜索），一批并行
        added, lines = 0, []
        for g in active:
            acts = _gap_actions(st, asp[g])
            n = 0
            for a in acts:
                a["gap"] = g
                if _push(st, a):
                    n += 1
                    gap_log[g]["queries"].append(f"{a['source']}：{a['query']}")
            gap_log[g]["how"] = "定向检索"
            added += n
            lines.append(f"「{asp[g]['name']}」→ {n} 个检索：" + "；".join(f"{a['source']}「{a['query']}」" for a in acts))
        if active:
            ctx.think(f"覆盖：{cov_txt}。第 {st['refine_round']} 轮定向补缺口"
                      + (f"（{why}）" if why else "") + "：\n" + "\n".join(lines))
        if added or not active:
            return "decide"
    gave_up = []
    for g in gaps:
        log = gap_log.setdefault(g, {"name": cov[g]["name"], "t0": time.time(), "tries": 0, "queries": [],
                                     "added_keywords": [], "status": "open", "how": "未定向检索（轮次已用完）"})
        if log["status"] == "open":
            log.update(status="insufficient", seconds=round(time.time() - log["t0"], 1))
        gave_up.append(log["name"])
    ctx.think(f"覆盖：{cov_txt}。" + (f"缺口 {gave_up} 定向补检后仍无足够资料，不再重复检索，在报告中标注“暂无足够资料”"
                                     if gaps else "覆盖足够，停止检索"))
    return "judge"


# ---------------------------------------------------------------- 节点：judge（大模型复核相关度）

JUDGE_PROMPT = """主题：「{topic}」。需要覆盖的子问题：{aspects}。
下面是检索到的资料（编号、标题、来源、日期、开头）。请逐条判断与主题的相关程度：
3=直接讨论该主题的核心内容；2=明显相关，涉及某个子问题；1=只是顺带提到；0=无关或只是关键词巧合。
reason 用不超过 20 个字说明理由。只输出 JSON：{{"scores": [{{"i": 0, "score": 2, "reason": "..."}}]}}
{items}"""


def judge(ctx):
    """规则相关度只看字面；配置了大模型时，对相关度靠前的资料做一次批量复核，修正“关键词巧合”。
    最终相关度 = 0.4 × 规则分 + 0.6 × 大模型分/3；大模型判 0 分（无关）封顶 0.15、1 分（顺带提到）封顶 0.35。"""
    st, p = ctx.state, ctx.params
    st["judge"] = {"by": "rules", "judged": 0, "demoted": 0, "promoted": 0}
    if not llm.available() or p.get("no_llm") or p.get("no_judge"):
        return "images"
    docs = [d for d in _task_docs(ctx.task_id) if not d["duplicate_of"] and
            ((d["meta"] or {}).get("relevance") or 0) >= 0.2]
    docs.sort(key=lambda d: -((d["meta"] or {}).get("relevance") or 0))
    docs = docs[:p.get("judge_top", 30)]
    names = "、".join(a["name"] for a in st["aspects"])
    floor = _min_rel(st)
    for i0 in range(0, len(docs), 15):
        chunk = docs[i0:i0 + 15]
        lines = []
        for i, d in enumerate(chunk):
            head = re.sub(r"\s+", " ", d["text"] or "")[:200]
            lines.append(f"[{i}] {d['title'] or ''} | {d['source_id']} | {(d['published_at'] or '')[:10]} | {head}")
        try:
            res = ctx.tool("llm", messages=[{"role": "user", "content": JUDGE_PROMPT.format(
                topic=ctx.topic, aspects=names, items="\n".join(lines))}])["data"]
        except Exception as e:
            ctx.think(f"大模型复核失败（{str(e)[:60]}），保留规则相关度")
            break
        for x in res.get("scores") or []:
            try:
                d = chunk[int(x["i"])]
                sc = max(0, min(3, int(x["score"])))
            except (KeyError, ValueError, IndexError, TypeError):
                continue
            meta = dict(d["meta"] or {})
            lex = meta.get("relevance") or 0
            # 大模型判为无关 / 只是顺带提到：封顶到门槛以下，不能靠关键词字面分混进榜单
            final = round(min(0.4 * lex + 0.6 * sc / 3, {0: 0.15, 1: 0.35}.get(sc, 1.0)), 3)
            meta.update(rel_lex=lex, relevance=final, judge={"score": sc, "reason": str(x.get("reason", ""))[:40],
                                                               "task": ctx.task_id})
            db.update("documents", "id", d["id"], {"meta": meta})
            st["judge"]["judged"] += 1
            if lex >= floor > final:
                st["judge"]["demoted"] += 1
            elif final >= floor > lex:
                st["judge"]["promoted"] += 1
    st["judge"]["by"] = "llm"
    ctx.think(f"大模型复核 {st['judge']['judged']} 篇的相关度：{st['judge']['demoted']} 篇判为关键词巧合（降到门槛以下），"
              f"{st['judge']['promoted']} 篇字面不像但实际相关（升到门槛以上）")
    st["coverage"] = _coverage(ctx)
    return "images"


# ---------------------------------------------------------------- 节点：act

def _save(ctx, item: dict, how: str) -> dict:
    item.setdefault("meta", {})["extract_method"] = how
    res = upsert_document(item, ctx.topic, task_id=ctx.task_id, last_run_at=ctx.state["last_run_at"])
    st = ctx.state["stats"]
    st[res["action"]] = st.get(res["action"], 0) + 1
    if res["action"] == "new":
        st["novelty_" + res["novelty"]] = st.get("novelty_" + res["novelty"], 0) + 1
    return res


_SPECIFIC = [
    re.compile(r"arxiv\.org/(abs|pdf)/\d{4}\.\d{4,5}"),
    re.compile(r"github\.com/(?!(features|about|pricing|login|topics|orgs|sponsors|marketplace|"
               r"collections|trending|enterprise|site|security|customer-stories|readme)\b)"
               r"[\w.-]+/[\w.-]+/?($|[?#]|/(tree|blob)/)"),
    re.compile(r"huggingface\.co/(papers/\d|(?!(docs|spaces|blog|pricing|login|join|models|datasets|"
               r"tasks|learn|enterprise)\b)[\w.-]+/[\w.-]+/?$|spaces/[\w.-]+/[\w.-]+)"),
    re.compile(r"openreview\.net/(forum|pdf)\?id="),
    re.compile(r"aclanthology\.org/[\w.-]+\.\d+"),
]


def _specific_primary(url: str) -> bool:
    return any(p.search(url) for p in _SPECIFIC)


def _follow_primary(ctx, doc_id: str, links: list[dict], from_primary: bool = False):
    """二手报道里引用的具体论文/仓库/模型页：追踪原始来源。
    一手页面本身（论文页、仓库页）的导航链接不追，避免跑到 arXiv Labs、HF 首页之类的无关页面。"""
    if from_primary:
        return
    st = ctx.state
    n = st["follow_count"].get(doc_id, 0)
    for l in links or []:
        if n >= MAX_FOLLOW_PER_DOC:
            break
        u = l["url"]
        if not _specific_primary(u):
            continue
        if _push(st, _fetch_action(u, f"追踪原始来源（{doc_id} 引用：{l.get('text') or u}）",
                                   prio=60, base={"source_id": "web", "primary": True,
                                                  "meta": {"cited_by": doc_id}})):
            n += 1
    st["follow_count"][doc_id] = n


def _handle_failure(ctx, a: dict, e: FetchError):
    st = ctx.state
    rec = {"type": a["type"], "target": a.get("source") or a.get("url"), "query": a.get("query"),
           **e.to_dict(), "attempts": getattr(e, "attempts", None), "fallback": None}
    if a["type"] == "search":
        sid = a["source"]
        st["source_fail"][sid] = st["source_fail"].get(sid, 0) + 1
        # 明确拒绝/不可达/内容异常：直接停用；偶发错误（超时、5xx、限流）重试一次，仍失败再停用
        if e.kind in ("robots", "blocked", "network", "not_found", "bad_content", "config") or \
                st["source_fail"][sid] >= 2 or a["key"].endswith(":retry"):
            st["disabled"].append(sid)
            rec["fallback"] = f"本任务停用 {sid}，由其他来源继续覆盖"
        else:
            rec["fallback"] = "稍后重试"
            a2 = dict(a, prio=a["prio"] - 20, key=a["key"] + ":retry")
            _push(st, a2)
    else:
        has_summary = bool(a.get("doc_id"))
        if e.kind in ("blocked", "robots") and ctx.params.get("hitl_on_block"):
            ans = ctx.human(f"blocked:{a['key']}", "edit",
                            f"页面访问受限（{e.reason}）：{a['url']}。可以粘贴正文、提供替代链接，或跳过。",
                            {"default": {"action": "skip", "text": "", "url": ""}, "url": a["url"]})
            act = (ans or {}).get("action") if isinstance(ans, dict) else "skip"
            if act == "paste" and ans.get("text"):
                item = {"url": a["url"], "title": (a.get("base") or {}).get("title"),
                        "text": ans["text"], "content_kind": "full", "source_id": "web",
                        "meta": {"provided_by": "human"}}
                _save(ctx, item, "human")
                rec["fallback"] = "人工粘贴正文"
            elif act == "alt_url" and ans.get("url"):
                _push(ctx.state, _fetch_action(ans["url"], "人工提供的替代链接", prio=95,
                                               base=a.get("base"), doc_id=a.get("doc_id")))
                rec["fallback"] = f"改用替代链接 {ans['url']}"
            else:
                rec["fallback"] = "人工选择跳过"
        else:
            rec["fallback"] = "保留已有摘要，标记为摘要" if has_summary else "放弃该页面"
    st["failures"].append(rec)
    ctx.observe(f"失败：{e.kind} {e.reason} → {rec['fallback']}")


def _as_fetch_error(e: Exception, a: dict) -> FetchError:
    """意外异常（解析库报错、编码问题等）：只影响这一个页面/检索，记为失败并继续，不让整个步骤失败。"""
    return FetchError("bad_content", f"处理出错：{type(e).__name__}：{str(e)[:120]}", a.get("url") or a.get("source", ""))


def _fetchable(st: dict, source_id: str) -> bool:
    """来源配置 fetch=False（需要登录的平台、B站视频）时不抓详情页，只用搜索结果里的摘要。"""
    cache = st.setdefault("_fetchable", {})
    if source_id not in cache:
        row = db.query_one("SELECT config FROM sources WHERE id=?", (source_id,))
        cache[source_id] = (row["config"] or {}).get("fetch") is not False if row else True
    return cache[source_id]


def _after_search(ctx, a, status, res):
    st, p = ctx.state, ctx.params
    if status == "error":
        _handle_failure(ctx, a, res if isinstance(res, FetchError) else _as_fetch_error(res, a))
        return
    items = res["items"]
    if res.get("dropped_old"):
        st["stats"]["dropped_old"] = st["stats"].get("dropped_old", 0) + res["dropped_old"]
        ctx.observe(f"{a['source']}「{a['query']}」有 {res['dropped_old']} 条超出时间窗口，已过滤")
    if not items:
        st["zero"].append(a["key"])
        ctx.observe(f"{a['source']}「{a['query']}」无相关结果")
        return
    if st.get("exclude"):
        before = len(items)
        items = [it for it in items if not any(x.lower() in f"{it.get('title')} {it.get('summary')}".lower()
                                               for x in st["exclude"])]
        if before - len(items):
            ctx.observe(f"按偏好排除 {before - len(items)} 条（包含 {st['exclude']}）")
    # 单来源配额：避免某个来源（如站内搜索一次返回很多条）占满资料上限、挤掉其他来源
    quota = p.get("per_source", max(6, p.get("max_docs", 40) // 4))
    used = st.setdefault("per_source", {}).get(a["source"], 0)
    if a.get("gap"):          # 定向补缺口：配额用完也允许再收 3 条，否则缺口永远补不上
        room = max(0, quota - used) + 3
    else:
        room = max(0, quota - used)
    skipped = max(0, len(items) - room)
    items = items[:room]
    st["per_source"][a["source"]] = used + len(items)
    counts, skipped_fetch = {}, []
    gap_words = [w for w in re.split(r"[^\w一-鿿]+", a.get("query") or "") if len(w) > 1] \
        if a.get("gap") else []
    for it in items:
        if it.get("relevance") is None:
            it["relevance"] = _rel(st, it.get("title") or "", it.get("summary") or "")
            if gap_words:     # 定向检索的结果按该缺口的检索词判断相关度（主检索式的词未必出现）
                t, b = _norm(it.get("title") or ""), _norm(it.get("summary") or "")
                g = _rel_en(a["query"], t, b) if a["query"].isascii() else _rel_zh(gap_words, t, b)
                it["relevance"] = max(it["relevance"], round(g, 3))
        r = _save(ctx, it, "api" if a["source"] in API_SOURCES else "rss")
        k = r["action"] + ("/" + r["novelty"] if r["action"] == "new" else "")
        counts[k] = counts.get(k, 0) + 1
        meta = it.get("meta") or {}
        if meta.get("aggregator"):
            if not it["url"].startswith("https://news.ycombinator.com"):
                _push(st, _fetch_action(it["url"], f"聚合站只有标题（{meta.get('points')} 分），追踪原文",
                                        prio=35 + min((meta.get("points") or 0) // 50, 20),
                                        base=it, doc_id=r["doc_id"]))
        elif a["source"] in SEARCH_RESULT_SOURCES and (it.get("relevance") or 0) < _min_rel(st) * 0.75:
            skipped_fetch.append(it.get("title") or it["url"])
        elif it["content_kind"] != "full" and a["source"] not in ("arxiv", "github", "openalex", "pubmed") \
                and _fetchable(st, a["source"]):
            mode = "browser" if meta.get("detail") == "browser" else "auto"
            _push(st, _fetch_action(it["url"], ("搜索结果" if a["source"] in SEARCH_RESULT_SOURCES else "RSS")
                                    + "只有摘要，抓取全文" +
                                    ("（前端渲染页面，用浏览器）" if mode == "browser" else ""),
                                    prio=30 + int((it.get("relevance") or 0) * 10), mode=mode,
                                    base=it, doc_id=r["doc_id"]))
        if r["action"] == "new" and meta.get("out_links"):
            _follow_primary(ctx, r["doc_id"], meta["out_links"], from_primary=it.get("primary"))
    if skipped_fetch:
        st["stats"]["skip_fetch_irrelevant"] = st["stats"].get("skip_fetch_irrelevant", 0) + len(skipped_fetch)
    ctx.observe(f"{a['source']}「{a['query']}」入库 {len(items)} 条 → {counts}" +
                (f"；{len(skipped_fetch)} 条标题和摘要与主题不相关（如「{skipped_fetch[0][:30]}」），不抓全文" if skipped_fetch else "") +
                (f"；另有 {skipped} 条超出单来源配额 {quota}，未入库" if skipped else ""))


def _after_fetch(ctx, a, status, art):
    st = ctx.state
    if status == "error":
        _handle_failure(ctx, a, art if isinstance(art, FetchError) else _as_fetch_error(art, a))
        return
    item = article_to_item(art, a["url"], base=a.get("base"))
    rel = _rel(st, item.get("title") or "", item.get("text") or "")
    item["relevance"] = max(rel, (a.get("base") or {}).get("relevance") or 0)   # 全文到手后重新判断相关度
    how = "browser" if art.get("via_browser") else "html"
    r = _save(ctx, item, how)
    _follow_primary(ctx, r["doc_id"], art.get("links"),
                    from_primary=bool(item.get("primary")) or is_primary(art.get("final_url") or a["url"]))
    ctx.observe(f"抓取 {art.get('final_url', a['url'])}：{art['content_kind']}，{len(art['text'])} 字，"
                f"{'浏览器渲染' if art.get('via_browser') else '静态 HTML'}，候选图片 {len(art.get('images', []))} 张"
                f" → {r['action']}" + (f"（{r['relation']}）" if r.get("relation") else ""))


def act(ctx):
    """执行一批行动：网络请求并行，结果按顺序处理（入库、追加后续行动、失败降级）。"""
    st, p = ctx.state, ctx.params
    batch = st.pop("current_batch", None) or [st.pop("current")]
    # 已有全文且最近抓过的页面：直接复用库里的版本，不再联网（上次运行抓过的论文/仓库页常被再次引用）
    reused = []
    for a in [a for a in batch if a["type"] == "fetch" and not a.get("force")]:
        old = db.query_one("SELECT * FROM documents WHERE canonical_url=? AND status='active' "
                           "AND content_kind='full' AND fetched_at>? ORDER BY version DESC LIMIT 1",
                           (canonical_url(a["url"]), db.now() - p.get("refetch_hours", 24) * 3600))
        if not old:
            continue
        batch.remove(a)
        st["done"].append(a["key"])
        meta = dict(old["meta"] or {})
        _save(ctx, {"url": old["url"], "title": old["title"], "text": old["text"], "content_kind": "full",
                    "source_id": old["source_id"], "published_at": old["published_at"],
                    "author": old.get("author"), "primary": meta.get("primary"),
                    "relevance": meta.get("relevance"), "meta": {}}, "reuse")
        reused.append(a["url"])
    if reused:
        ctx.observe(f"复用库中 {len(reused)} 篇最近抓过的全文，跳过联网：" + "、".join(u[:60] for u in reused))
    if not batch:
        return "decide"
    calls = []
    for a in batch:
        st["done"].append(a["key"])
        if a["type"] == "search":
            w = st.get("window") or {}
            since = w.get("since_academic") if a["source"] in ACADEMIC_SOURCES else w.get("since")
            calls.append(("source_search", dict(source_id=a["source"], query=a["query"],
                                                limit=p.get("per_search", 15), terms=a.get("terms"),
                                                since=since)))
        else:
            st["fetches"] += 1
            calls.append(("page_fetch", dict(url=a["url"], mode=a.get("mode", "auto"))))
    t0 = time.time()
    results = ctx.tool_parallel(calls, max_workers=p.get("parallel", 6))
    for a, (status, out) in zip(batch, results):
        try:
            (_after_search if a["type"] == "search" else _after_fetch)(ctx, a, status, out)
        except Exception as e:      # 单个结果处理出错只影响它自己
            _handle_failure(ctx, a, _as_fetch_error(e, a))
    if len(batch) > 1:
        ctx.observe(f"并行 {len(batch)} 个请求，共耗时 {time.time() - t0:.1f}s")
    return "decide"


# ---------------------------------------------------------------- 节点：images

def images(ctx):
    """为本次资料处理配图：已处理过的资料直接复用（不占名额），新资料按相关度、来源多样性挑选。"""
    p = ctx.params
    max_docs, per_doc = p.get("image_docs", 8), p.get("images_per_doc", 6)
    docs = [d for d in _task_docs(ctx.task_id)
            if not d["duplicate_of"] and (d["meta"] or {}).get("image_candidates")]
    docs.sort(key=lambda d: (-(d["meta"].get("relevance") or 0), d["content_kind"] != "full"))
    done_before = {r["doc_id"] for r in db.query(
        f"SELECT DISTINCT doc_id FROM images WHERE doc_id IN ({','.join('?' * len(docs))})",
        [d["id"] for d in docs])} if docs else set()
    todo = [d for d in docs if d["id"] not in done_before]
    # 来源轮转：避免名额全被同一来源占用
    picked, per_src = [], {}
    for d in todo:
        if per_src.get(d["source_id"], 0) < max(2, max_docs // 3):
            picked.append(d)
            per_src[d["source_id"]] = per_src.get(d["source_id"], 0) + 1
        if len(picked) >= max_docs:
            break

    def dl(u, referer=None):
        try:
            return ctx.tool("image_download", url=u, referer=referer)
        except Exception as e:
            return {"ok": False, "reason": f"下载异常：{str(e)[:100]}"}

    new = {"kept": 0, "rejected": 0, "docs": 0}
    for d in picked:
        res = store_images(d["id"], d["url"], d["meta"]["image_candidates"], max_images=per_doc,
                           download_fn=dl)
        new["kept"] += res["kept"]
        new["rejected"] += res["rejected"]
        new["docs"] += 1
    ids = [d["id"] for d in _task_docs(ctx.task_id)]
    agg = db.query_one(
        f"SELECT SUM(status='kept') kept, SUM(status='rejected') rejected, COUNT(DISTINCT doc_id) docs "
        f"FROM images WHERE doc_id IN ({','.join('?' * len(ids))})", ids) if ids else {}
    ctx.state["image_stats"] = {"kept": agg.get("kept") or 0, "rejected": agg.get("rejected") or 0,
                                "docs": agg.get("docs") or 0, "new": new,
                                "reused_docs": len(done_before)}
    ctx.observe(f"配图：复用已处理资料 {len(done_before)} 篇；新处理 {new['docs']} 篇，保留 {new['kept']} 张、"
                f"筛除 {new['rejected']} 张；本次资料共有可用配图 {agg.get('kept') or 0} 张")
    return "remember"


# ---------------------------------------------------------------- 节点：remember（写入长期记忆）

def remember(ctx):
    """把本次资料中的关键事实写入主题记忆：更正传播 → 抽取事实 → 合并（支持/冲突/更正）→ 裁决争议。
    出现新的争议且还有核验机会时，回到检索做补充核验（证据冲突时补充核验）。"""
    st, p = ctx.state, ctx.params
    stats = st.setdefault("mem_stats", {})
    seen_docs = set(st.setdefault("mem_docs", []))
    stale = mf.propagate_doc_updates(ctx.topic, ctx.task_id)
    if stale:
        stats.setdefault("stale_from_updates", []).extend(stale)
        ctx.observe(f"资料更新：{len(stale)} 条旧事实在新版本中找不到原数字，标记需复查")
    docs = [d for d in _task_docs(ctx.task_id) if not d["duplicate_of"] and d["id"] not in seen_docs
            and (d["content_kind"] in ("full", "excerpt") or
                 ((d["meta"] or {}).get("primary") and len(d["text"] or "") >= 150))   # 论文摘要也算
            and ((d["meta"] or {}).get("relevance") or 0) >= _min_rel(ctx.state)]
    # 更新过的资料优先，其次一手来源和相关度
    docs.sort(key=lambda d: (d["novelty"] != "update", not (d["meta"] or {}).get("primary"),
                             -((d["meta"] or {}).get("relevance") or 0)))
    docs = docs[:p.get("fact_docs", 10)]
    full = [db.query_one("SELECT id, title, text FROM documents WHERE id=?", (d["id"],)) for d in docs]
    terms = st["plan"]["terms_zh"] + [w for q in st["plan"]["queries_en"] for w in q.split() if len(w) > 3]
    claims = mf.extract_claims(ctx, full, terms)
    fact_ids = set(st.setdefault("mem_facts", []))
    before = stats.get("conflicts", 0)
    for c in claims:
        fact_ids.add(mf.merge_claim(ctx.topic, c, ctx.task_id, stats))
    # 转载/重复报道：记录为证据（可追溯“谁在转述”），但独立来源数不增加
    for d in _task_docs(ctx.task_id):
        if not d["duplicate_of"]:
            continue
        for e in db.query("SELECT DISTINCT claim_id FROM evidence WHERE doc_id=? AND stance='support'",
                          (d["duplicate_of"],)):
            f = mem.get(e["claim_id"])
            if f and f["status"] in ("active", "stale") and mf.add_evidence(f["id"], d["id"], "转载/重复报道"):
                mf.verify(f["id"], ctx.task_id)
                stats["reprint_evidence"] = stats.get("reprint_evidence", 0) + 1
                fact_ids.add(f["id"])
    st["mem_facts"] = sorted(fact_ids)
    st["mem_docs"] = sorted(seen_docs | {d["id"] for d in docs})
    resolved = mf.resolve_disputes(ctx.topic, ctx.task_id)
    stats["disputes"] = resolved
    open_d = [r for r in resolved if r.get("open")]
    ctx.observe(f"从 {len(docs)} 篇资料抽取 {len(claims)} 条事实：新增 {stats.get('new_facts', 0)}，"
                f"被更多来源支持 {stats.get('supported', 0)}，更正 {stats.get('corrected', 0)}，"
                f"冲突 {stats.get('conflicts', 0)}；争议裁决 {len(resolved) - len(open_d)} 个，仍未解决 {len(open_d)} 个")
    new_conflicts = stats.get("conflicts", 0) - before
    if new_conflicts and open_d and not st.get("verify_round") and p.get("verify_conflicts", True) \
            and not st.get("_budget_exhausted"):
        st["verify_round"] = 1
        srcs = [s for s in list_sources() if s["id"] not in st["disabled"] and
                (s["config"] or {}).get("lang") == "zh" and s["method"] == "api"]
        added = 0
        for dsp in open_d[:3]:
            m = mem.get(dsp["dispute"])
            q = (m["meta"] or {}).get("query") or m["content"][:16]
            for s in srcs[:2]:
                added += _push(st, _search_action(s["id"], q, st["plan"]["terms_zh"],
                                                  f"发现证据冲突「{m['content'][:40]}」，补充核验", prio=70))
        if added:
            ctx.think(f"出现 {new_conflicts} 处证据冲突，回到检索补充核验（{added} 个检索行动），之后重新评估")
            return "decide"
    return "rank"


# ---------------------------------------------------------------- 节点：rank / verify（Lv2 价值评估与重排序）

def _pref_minutes(st: dict):
    return (((st.get("memory") or {}).get("prefs") or {}).get("reading_minutes") or {}).get("value")


def _rank_summary(d: dict) -> str:
    m = d["metrics_auto"]
    l5, l30 = d["lists"]["5"], d["lists"]["30"]
    fmt = lambda x: "—" if x is None else f"{x:.2f}"  # noqa: E731
    return (f"5 分钟速览 {len(l5['selected'])} 篇（{l5['used']} 分钟）；30 分钟深读 {len(l30['selected'])} 篇"
            f"（{l30['used']} 分钟）。自动标注下 P@5：价值排序 {fmt(m['ours'].get('P@5'))}、按时间 "
            f"{fmt(m['time'].get('P@5'))}、按热度 {fmt(m['popularity'].get('P@5'))}；重复率@10：价值排序 "
            f"{fmt(m['ours'].get('dup@10'))}、按时间 {fmt(m['time'].get('dup@10'))}")


def rank(ctx):
    """对本次资料做价值评估：五维打分 → 时间预算内选榜（同一事件只留一篇）→ 与基线对比 → 检查榜首是否有可信度问题。"""
    st, p = ctx.state, ctx.params
    d = ranking.evaluate(ctx.task_id, ctx.topic, aspects=st["aspects"], min_rel=_min_rel(st),
                         pref_minutes=_pref_minutes(st), weights=p.get("rank_weights"), trigger="初次评估")
    st["ranking"] = {"versions": [d["version"]], "problems": d["problems"]}
    ctx.think("价值评估（相关度 0.35、可信度 0.25、新颖性 0.2、时效性 0.2；阅读时间作为预算）：" + _rank_summary(d))
    if d["problems"] and p.get("verify_top", True) and not st.get("_budget_exhausted"):
        ctx.think("发现问题：速览榜前列有可信度不足的资料 →\n" +
                  "\n".join(f"第 {x['rank']} 名「{x['title'][:40]}」：{x['issue']}" for x in d["problems"]) +
                  "\n下一步补充核验：用标题关键词到其他来源找交叉印证，然后重新评估")
        return "verify"
    return "report"


def _corroborate_query(title: str) -> tuple[str, str]:
    t = re.sub(r"[【】\[\]（）()《》“”\"'|｜!！?？#]", " ", title or "")
    if is_english(t):
        words = [w for w in re.findall(r"[A-Za-z0-9][\w.+-]*", t) if w.lower() not in GENERIC and len(w) > 2]
        return " ".join(words[:5]), "en"
    seg = [x for x in re.split(r"[：:，,。；;\s]+", t) if x]
    core = max(seg, key=len) if seg else t
    return core[:14], "zh"


def _event_hosts(doc_id: str) -> set:
    d = db.query_one("SELECT event_id, topic FROM documents WHERE id=?", (doc_id,))
    if not d or not d["event_id"]:
        return set()
    rows = db.query("SELECT url, author FROM documents WHERE event_id=? AND topic=? AND status='active' "
                    "AND duplicate_of IS NULL", (d["event_id"], d["topic"]))
    return {ranking.source_key(r["url"], r["author"]) for r in rows}


def corroborate(ctx, problems: list[dict], *, task_id: str, topic: str, source_ids: list, window: dict,
                plan: dict) -> tuple[list[dict], dict]:
    """补充核验：对每条有问题的资料，用标题关键词到 2 个合适的来源检索，找报道同一事件的其他站点。
    找到 → 独立来源数增加、可信度提升；找不到 → 标注“待核实”，价值分打折。返回 (核验记录, flags)。"""
    by_id = {s["id"]: s for s in list_sources()}
    ok = [sid for sid in (source_ids or []) if sid in by_id]
    en_srcs = [s for s in ("hn", "openalex", "arxiv") if s in ok]
    zh_srcs = [sid for sid in ok if (by_id[sid]["config"] or {}).get("lang", "zh") == "zh" and
               (by_id[sid]["method"] in ("api", "search") or (by_id[sid]["config"] or {}).get("search_feed") or
                (by_id[sid]["method"] == "websearch" and websearch.available()))]
    calls, meta, records = [], [], {}
    for pr in problems:
        q, lang = _corroborate_query(pr["title"])
        rec = records.setdefault(pr["id"], {"id": pr["id"], "title": pr["title"], "issue": pr.get("issue"),
                                            "queries": [], "found": [], "errors": [],
                                            "hosts_before": sorted(_event_hosts(pr["id"]))})
        if not q:
            continue
        for s in (en_srcs if lang == "en" else zh_srcs)[:2]:
            since = (window or {}).get("since_academic" if s in ACADEMIC_SOURCES else "since")
            calls.append(("source_search", dict(source_id=s, query=q, limit=10, terms=None, since=since)))
            meta.append((pr, s, q))
            rec["queries"].append(f"{s}「{q}」")
    outs = ctx.tool_parallel(calls) if calls else []
    doc_urls = {pr["id"]: canonical_url(db.query_one("SELECT url FROM documents WHERE id=?", (pr["id"],))["url"])
                for pr in problems}
    fake_st = {"plan": plan}
    for (pr, s, q), (status, out) in zip(meta, outs):
        rec = records[pr["id"]]
        if status == "error":
            rec["errors"].append(f"{s}：{getattr(out, 'kind', '')} {getattr(out, 'reason', str(out))[:60]}")
            continue
        for it in out["items"]:
            if canonical_url(it["url"]) == doc_urls[pr["id"]]:
                continue
            sim = title_sim(it.get("title") or "", pr["title"])
            if sim < 0.3:
                continue
            existing = db.query_one("SELECT id, event_id FROM documents WHERE canonical_url=? AND status='active'",
                                    (canonical_url(it["url"]),))
            if existing:        # 库里已有：只记录，不覆盖它的相关度等信息
                rec["found"].append({"doc_id": existing["id"], "title": it.get("title"), "source": s,
                                     "similarity": round(sim, 2), "action": "already_known", "relation": None})
                continue
            it["relevance"] = max(_rel(fake_st, it.get("title") or "", it.get("summary") or ""),
                                  (db.query_one("SELECT meta FROM documents WHERE id=?", (pr["id"],))["meta"] or {})
                                  .get("relevance") or 0 if sim >= EVENT_TITLE_SIM else 0)
            r = upsert_document(it, topic, task_id=task_id)
            rec["found"].append({"doc_id": r["doc_id"], "title": it.get("title"), "source": s,
                                 "similarity": round(sim, 2), "action": r["action"], "relation": r.get("relation")})
    flags = {}
    for pid, rec in records.items():
        after = _event_hosts(pid)
        rec["hosts_after"] = sorted(after)
        gained = len(after) - len(rec["hosts_before"])
        if gained > 0:
            rec["outcome"] = "corroborated"
            flags[pid] = {"note": f"补充核验：新找到 {gained} 个独立来源报道同一事件（{'、'.join(sorted(set(after) - set(rec['hosts_before'])))}）"}
        else:
            rec["outcome"] = "unverified"
            flags[pid] = {"unverified": True,
                          "note": "补充核验未找到其他来源印证，标注“待核实”，价值分打 85 折"}
    return list(records.values()), flags


def verify(ctx):
    """补充核验 → 重新评估：核验速览榜前列的可疑资料，再评估一次，记录榜单变化。"""
    st = ctx.state
    prev = ranking.latest(ctx.task_id)
    recs, flags = corroborate(ctx, prev["problems"], task_id=ctx.task_id, topic=ctx.topic,
                              source_ids=st.get("source_ids"), window=st.get("window"), plan=st["plan"])
    flags = {**(prev.get("flags") or {}), **flags}
    d = ranking.evaluate(ctx.task_id, ctx.topic, aspects=st["aspects"], min_rel=_min_rel(st),
                         pref_minutes=_pref_minutes(st), weights=ctx.params.get("rank_weights"), flags=flags,
                         trigger="补充核验后重新评估", extra={"verify": recs, "triggered_by": prev["problems"]},
                         prev=prev)
    st["ranking"]["versions"].append(d["version"])
    st["ranking"]["verify"] = [{"title": r["title"][:40], "outcome": r["outcome"], "found": len(r["found"])} for r in recs]
    moves = d.get("diff", {}).get("5", [])
    ctx.observe("补充核验：" + "；".join(
        f"「{r['title'][:24]}」{'找到交叉印证' if r['outcome'] == 'corroborated' else '未找到其他来源，标注待核实'}"
        f"（检索 {len(r['queries'])} 次，相关结果 {len(r['found'])} 条）" for r in recs) +
        f"。重新评估后速览榜变化 {len(moves)} 处：" +
        "；".join(f"「{m['title'][:20]}」{m['from'] or '未入选'} → {m['to'] or '移出'}" for m in moves[:5]))
    return "report"


# ---------------------------------------------------------------- 手动重新评估（可选补充核验）

def reassess(ctx):
    """对某次检索任务重新评估：可指定要补充核验的资料；不核验时只按最新数据、标注、权重重排。"""
    p = ctx.params
    tid = p["task"]
    t = db.query_one("SELECT topic, state FROM tasks WHERE id=?", (tid,))
    tst = t["state"] or {}
    prev = ranking.latest(tid)
    flags = dict((prev or {}).get("flags") or {})
    recs, problems = [], []
    if p.get("verify"):
        items = {it["id"]: it for it in (prev or {}).get("items", [])}
        problems = [{"id": i, "title": items[i]["title"], "issue": "用户要求补充核验"} for i in p.get("doc_ids") or []
                    if i in items] or (prev or {}).get("problems") or []
        ctx.think(f"补充核验 {len(problems)} 篇：" + "；".join(x["title"][:30] for x in problems))
        recs, f2 = corroborate(ctx, problems, task_id=tid, topic=t["topic"], source_ids=tst.get("source_ids"),
                               window=tst.get("window"), plan=tst.get("plan") or {"terms_zh": [], "queries_en": []})
        flags.update(f2)
    d = ranking.evaluate(tid, t["topic"], aspects=tst.get("aspects") or [], min_rel=tst.get("min_rel", 0.4),
                         pref_minutes=(prev or {}).get("pref_minutes"),
                         weights=p.get("weights") or (prev or {}).get("weights"), flags=flags,
                         trigger=p.get("trigger") or ("补充核验后重新评估" if p.get("verify") else "重新评估"),
                         extra={"verify": recs, "by_task": ctx.task_id,
                                "triggered_by": problems if p.get("verify") else []}, prev=prev)
    ctx.state["report"] = {"kind": "reassess", "task": tid, "version": d["version"], "trigger": d["trigger"],
                           "verify": recs, "diff": d.get("diff", {})}
    ctx.observe(f"已生成第 {d['version']} 版评估：" + _rank_summary(d))
    return END


register_graph(Graph(name="reassess", start="reassess", nodes=dict(reassess=reassess)))


# ---------------------------------------------------------------- 节点：report

def report(ctx):
    st = ctx.state
    docs = _task_docs(ctx.task_id)
    cov = _coverage(ctx)
    by = lambda key: {k: sum(1 for d in docs if (d[key] or "无") == k)  # noqa: E731
                      for k in {d[key] or "无" for d in docs}}
    srcs = {s["id"]: s for s in list_sources(enabled_only=False)}
    omissions = []
    if st.get("_budget_exhausted"):
        omissions.append(f"预算耗尽（{st['_budget_exhausted']}），队列中还有 {len(st['queue'])} 个行动未执行")
    for f in st["failures"]:
        if f.get("kind") == "missing_en_keywords":
            omissions.append("英文来源未检索：" + f["reason"])
    for sid in st["disabled"]:
        omissions.append(f"{srcs.get(sid, {}).get('name', sid)} 本次不可用，相关内容可能遗漏")
    for b in st.get("skipped_unhealthy", []):
        omissions.append(f"{b} 近期持续失败，本次未检索")
    if any(s["method"] == "rss" for s in srcs.values()):
        omissions.append("RSS 只包含各站最新几十篇，更早的中文报道需要站内搜索或链接导入")
    if st.get("no_websearch"):
        omissions.append(f"未配置搜索 API，没有检索：{'、'.join(st['no_websearch'])}（设置页填写 key 后可用）")
    so = [d for d in docs if (d["meta"] or {}).get("summary_only") and not d["duplicate_of"]]
    if so:
        plats = sorted({(d["meta"] or {}).get("platform") or d["source_id"] for d in so})
        omissions.append(f"{len(so)} 篇来自{'、'.join(plats)}，只有搜索摘要（正文需登录）；重要的可在资料页粘贴正文")
    gaps = [v["name"] for v in cov.values() if v["have"] < v["need"]]
    if gaps:
        omissions.append(f"暂无足够资料的子问题（已定向补检，不再重复检索）：{gaps}")
    mstats = st.get("mem_stats", {})
    fact_ids = st.get("mem_facts", [])
    floor = _min_rel(st)
    rel_of = lambda d: (d["meta"] or {}).get("relevance")  # noqa: E731
    top_docs = [d["id"] for d in sorted(docs, key=lambda d: -(rel_of(d) or 0))
                if not d["duplicate_of"] and (rel_of(d) is None or rel_of(d) >= floor)][:30]
    # ---- 资料质量：时效分布 + 相关度分布 + 过滤了多少
    now = datetime.now(timezone.utc)
    w = st.get("window") or {"days": 90, "academic_days": 365}
    fresh = {"7d": 0, "30d": 0, "window": 0, "older": 0, "undated": 0}
    uniq = [d for d in docs if not d["duplicate_of"]]
    for d in uniq:
        dt = parse_date(d["published_at"])
        if not dt:
            fresh["undated"] += 1
            continue
        age = (now - dt).days
        fresh["7d" if age <= 7 else "30d" if age <= 30 else "window" if age <= w["days"] else "older"] += 1
    rels = [rel_of(d) for d in uniq if rel_of(d) is not None]
    quality = {
        "window_days": w["days"], "academic_days": w["academic_days"],
        "dropped_old": st["stats"].get("dropped_old", 0), "fresh": fresh,
        "relevance": {"high": sum(r >= 0.7 for r in rels), "mid": sum(floor <= r < 0.7 for r in rels),
                      "low": sum(r < floor for r in rels)},
        "min_relevance": floor, "delivered": len(top_docs), "judge": st.get("judge"),
        "avg_relevance": round(sum(rels) / len(rels), 3) if rels else None,
    }
    if quality["relevance"]["low"]:
        omissions.append(f"{quality['relevance']['low']} 篇相关度低于 {floor}，已从交付列表剔除（资料库里仍可查看）")
    if fresh["older"]:
        omissions.append(f"{fresh['older']} 篇超出 {w['days']} 天窗口（论文/仓库或追溯的一手来源），作为背景资料")
    if not st.get("delivery_id"):
        summary = (f"检索交付 {len(docs)} 篇（新进展 {sum(1 for d in docs if d['novelty'] == 'new')}、"
                   f"更新 {sum(1 for d in docs if d['novelty'] == 'update')}），涉及事实 {len(fact_ids)} 条")
        st["delivery_id"] = mf.record_delivery(
            ctx.topic, ctx.task_id, ctx.user_id, summary, fact_ids, top_docs,
            {"queries_en": st["plan"]["queries_en"], "terms_zh": st["plan"]["terms_zh"]})
    memory_report = {
        "recalled": st.get("memory", {}).get("lines", []), "recalled_ids": st.get("memory", {}).get("used", []),
        "pref_notes": st.get("memory", {}).get("pref_notes", []), "prefs": st.get("memory", {}).get("prefs", {}),
        "new_facts": mstats.get("new_facts", 0), "supported": mstats.get("supported", 0),
        "corrected": mstats.get("corrected", 0), "corrections": mstats.get("corrections", []),
        "conflicts": mstats.get("conflicts", 0), "disputes": mstats.get("disputes", []),
        "stale_from_updates": mstats.get("stale_from_updates", []), "delivery_id": st["delivery_id"],
        "facts": [{"id": f["id"], "content": f["content"], "verify": (f["meta"] or {}).get("verify"),
                   "independent": (f["meta"] or {}).get("independent"), "status": f["status"]}
                  for f in (mem.get(i) for i in fact_ids) if f][:30],
    }
    st["report"] = {
        "quality": quality, "delivered_docs": top_docs, "ranking": st.get("ranking"),
        "covered_at": st.get("covered_at"), "dropped_after_cover": st.get("dropped_after_cover", 0),
        "domains": st.get("domains"), "source_ids": st.get("source_ids"),
        "memory": memory_report,
        "total_docs": len(docs),
        "unique_docs": sum(1 for d in docs if not d["duplicate_of"]),
        "events": len({d["event_id"] for d in docs}),
        "by_source": by("source_id"), "by_kind": by("content_kind"), "by_novelty": by("novelty"),
        "coverage": cov, "failures": st["failures"], "image_stats": st.get("image_stats"),
        "actions_done": len(st["done"]), "refine_rounds": st["refine_round"],
        "gaps": [{"id": g, **{k: v for k, v in log.items() if k != "t0"}}
                 for g, log in st.get("gap_log", {}).items()],
        "plan": st["plan"], "omissions": omissions,
        "budget_note": st.get("_budget_exhausted"),
    }
    ctx.observe(f"共 {len(docs)} 篇（去重后 {st['report']['unique_docs']}，{st['report']['events']} 个事件）；"
                f"新进展/更新/背景/重复：{st['report']['by_novelty']}；可能遗漏 {len(omissions)} 项")
    return END


register_graph(Graph(
    name="research", start="plan", on_budget="images",
    nodes=dict(plan=plan, decide=decide, act=act, judge=judge, images=images, remember=remember,
               rank=rank, verify=verify, report=report),
))


# ---------------------------------------------------------------- 链接导入

def import_fetch(ctx):
    url = ctx.state.get("url_override") or ctx.params["url"]
    ctx.state.setdefault("failures", [])
    ctx.state.setdefault("queue", [])
    ctx.state.setdefault("done", [])
    ctx.state.setdefault("stats", {})
    ctx.state.setdefault("plan", {"terms_zh": topic_terms(ctx.topic), "queries_en": []})
    ctx.state.setdefault("last_run_at", None)
    ctx.state.setdefault("follow_count", {})
    try:
        art = ctx.tool("page_fetch", url=url, mode=ctx.params.get("mode", "auto"))
    except FetchError as e:
        ans = ctx.human("import_blocked", "edit",
                        f"链接抓取失败（{e.kind}：{e.reason}）。可以粘贴正文（action=paste）、"
                        f"提供替代链接（action=alt_url）或跳过（action=skip）。",
                        {"default": {"action": "skip", "text": "", "url": ""},
                         "attempts": getattr(e, "attempts", None)})
        ans = ans if isinstance(ans, dict) else {"action": "skip"}
        if ans.get("action") == "paste" and ans.get("text"):
            r = _save(ctx, {"url": url, "title": ans.get("title"), "text": ans["text"],
                            "content_kind": "full", "source_id": "web",
                            "meta": {"provided_by": "human"}}, "human")
            ctx.state["doc_id"] = r["doc_id"]
            ctx.observe("使用人工粘贴的正文入库")
            return "import_images"
        if ans.get("action") == "alt_url" and ans.get("url"):
            ctx.state["url_override"] = ans["url"]
            ctx.state["_human"].pop("import_blocked", None)
            ctx.observe(f"改用替代链接 {ans['url']}")
            return "import_fetch"
        ctx.state["failures"].append(e.to_dict())
        ctx.observe("人工选择跳过")
        return END
    item = article_to_item(art, url, base={"source_id": "web", "primary": is_primary(url),
                                           "meta": {"imported": True}})
    r = _save(ctx, item, "browser" if art.get("via_browser") else "html")
    ctx.state["doc_id"] = r["doc_id"]
    ctx.observe(f"导入：{art.get('title')}（{art['content_kind']}，{len(art['text'])} 字，"
                f"{'浏览器渲染' if art.get('via_browser') else '静态 HTML'}）→ {r['action']}/{r['novelty']}")
    return "import_images"


def import_images(ctx):
    d = db.query_one("SELECT id, url, meta FROM documents WHERE id=?", (ctx.state["doc_id"],))
    cands = (d["meta"] or {}).get("image_candidates") or []
    if cands and not db.query_one("SELECT COUNT(*) n FROM images WHERE doc_id=?", (d["id"],))["n"]:
        res = store_images(d["id"], d["url"], cands, max_images=10,
                           download_fn=lambda u, referer=None: ctx.tool("image_download", url=u,
                                                                         referer=referer))
        ctx.observe(f"配图：保留 {res['kept']}，筛除 {res['rejected']}")
    return END


register_graph(Graph(name="import", start="import_fetch",
                     nodes=dict(import_fetch=import_fetch, import_images=import_images)))


# ---------------------------------------------------------------- 定期复查

RECHECK_WHY = {"github.com": "代码仓库（可能有新提交、新版本）", "arxiv.org": "论文（可能发布新版本）"}


def _recheck_why(d: dict) -> str:
    for host, why in RECHECK_WHY.items():
        if host in (d["url"] or ""):
            return why
    return "近期新闻（可能有更正或补充）"


def recheck(ctx):
    """复查可能变化的页面（代码仓库、论文、刚发布的新闻）。
    范围：scope_task（某次任务的资料）> 主题 > 全部。定时复查只查到期的；手动复查（force）查范围内所有待复查页面。
    提速：GitHub 仓库先批量查最近推送时间、arXiv 先批量查最新版本，没变就不打开网页；
    其余网页绕过缓存并行抓取，只用静态 HTML（原先需要浏览器渲染的页面除外）。"""
    p = ctx.params
    limit = p.get("limit", 20)
    sql = "SELECT * FROM documents WHERE status='active' AND recheck_at IS NOT NULL"
    args = []
    if not p.get("force"):
        sql += " AND recheck_at<=?"
        args.append(db.now())
    if p.get("scope_task"):
        sql += " AND meta LIKE ?"
        args.append(f"%{p['scope_task']}%")
    elif ctx.topic and ctx.topic != "*":
        sql += " AND topic=?"
        args.append(ctx.topic)
    due = db.query(sql + " ORDER BY recheck_at LIMIT ?", (*args, limit))
    st = ctx.state
    st.setdefault("results", [])
    st.setdefault("last_run_at", None)
    st.setdefault("stats", {})
    scope = (f"任务 {p['scope_task']} 的资料" if p.get("scope_task") else
             f"主题「{ctx.topic}」" if ctx.topic and ctx.topic != "*" else "全部主题")
    done_ids = {r["doc_id"] for r in st["results"]}
    todo = [d for d in due if d["id"] not in done_ids]
    gh, ax, web = [], [], []
    for d in todo:
        u = d["canonical_url"] or d["url"] or ""
        m_gh = re.match(r"https?://github\.com/([\w.-]+/[\w.-]+?)/?$", u)
        m_ax = re.search(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", u)
        if m_gh:
            gh.append((m_gh.group(1), d))
        elif m_ax:
            ax.append((m_ax.group(1), d))
        else:
            web.append(d)
    ctx.think(f"复查范围：{scope}；{'手动复查：范围内所有可能变化的页面' if p.get('force') else '定时复查：只查已到复查时间的页面'}，"
              f"共 {len(due)} 篇" + (f"（代码仓库 {len(gh)}、论文 {len(ax)}、网页 {len(web)}）" if todo else "") +
              ("" if due else "。没有需要复查的页面") +
              ("。仓库和论文先批量查更新时间/版本号，没变就不打开网页；网页并行抓取" if gh or ax else ""))

    def finish(d, res, outcome):
        nxt = None if outcome == "updated" else db.now() + 3 * 86400
        db.update("documents", "id", d["id"], {"recheck_at": nxt})
        res["outcome"] = outcome
        st["results"].append(res)

    def merge_meta(d, **kw):
        meta = dict(d["meta"] or {})
        meta.update({k: v for k, v in kw.items() if v is not None})
        db.update("documents", "id", d["id"], {"meta": meta})

    def base_res(d):
        return {"doc_id": d["id"], "title": d["title"], "url": d["url"], "why": _recheck_why(d)}

    def from_upsert(res, r):
        if r["action"] == "updated":
            new = db.query_one("SELECT meta, version FROM documents WHERE id=?", (r["doc_id"],))
            ch = (new["meta"] or {}).get("change") or {}
            res.update(new_doc_id=r["doc_id"], version=new["version"],
                       change={k: ch.get(k) for k in ("similarity", "numbers_added", "numbers_removed",
                                                     "sentences_added", "sentences_removed")})
        return r["action"]

    refetch = []        # (doc, 说明)：状态显示有变化、需要打开网页看具体改了什么
    # ---- GitHub：一次搜索请求查多个仓库的最近推送时间
    if gh:
        try:
            info = ctx.tool("github_status", repos=[n for n, _ in gh])
        except FetchError as e:
            ctx.think(f"GitHub 状态接口失败（{e.kind}），改为直接抓仓库页面")
            info = None
            web += [d for _, d in gh]
        for name, d in gh if info is not None else []:
            res = base_res(d)
            i = info.get(name.lower())
            old = (d["meta"] or {}).get("pushed_at")
            if not i:
                res["error"] = "仓库不存在、已改名或设为私有"
                finish(d, res, "failed")
                continue
            merge_meta(d, pushed_at=i["pushed_at"], stars=i["stars"])
            if old and i["pushed_at"] and i["pushed_at"] > old:
                refetch.append((d, f"有新提交（最近推送 {old[:10]} → {i['pushed_at'][:10]}）"))
            else:
                res["note"] = f"最近推送 {(i['pushed_at'] or '')[:10]}" + ("，未变" if old else "（首次记录，作为下次比较的基线）")
                finish(d, res, "unchanged")
    # ---- arXiv：一次请求查多篇论文的最新版本
    if ax:
        try:
            info = ctx.tool("arxiv_status", ids=[i for i, _ in ax])
        except FetchError as e:
            ctx.think(f"arXiv 接口失败（{e.kind}），改为直接抓论文页面")
            info = None
            web += [d for _, d in ax]
        for aid, d in ax if info is not None else []:
            res = base_res(d)
            i = info.get(aid)
            if not i:
                res["error"] = "arXiv 未返回该论文"
                finish(d, res, "failed")
                continue
            mv = re.search(r"v(\d+)$", d["url"] or "")
            old_v = int(mv.group(1)) if mv else (d["meta"] or {}).get("arxiv_version")
            old_up = (d["meta"] or {}).get("updated")
            changed = (old_v and i["version"] > old_v) or (not old_v and old_up and i["updated"] > old_up)
            merge_meta(d, arxiv_version=i["version"], updated=i["updated"])
            if changed:
                r = upsert_document({"url": i["url"], "title": i["title"], "summary": i["summary"],
                                     "text": i["summary"], "content_kind": d["content_kind"] if d["content_kind"] != "full" else "summary",
                                     "source_id": d["source_id"], "primary": True,
                                     "meta": {"arxiv_version": i["version"], "updated": i["updated"]}},
                                    d["topic"], task_id=ctx.task_id)
                res["note"] = f"发布了新版本 v{old_v or '?'} → v{i['version']}"
                from_upsert(res, r)
                finish(d, res, "updated")      # 新版本本身就是更新，即使摘要没变
            else:
                res["note"] = f"仍是 v{i['version']}"
                finish(d, res, "unchanged")
    # ---- 网页（及有新提交的仓库）：绕过缓存并行抓取
    pages = [(d, "") for d in web] + refetch
    if pages:
        calls = [("page_fetch", dict(url=d["url"], fresh=True,
                                      mode="auto" if (d["meta"] or {}).get("extract_method") == "browser" else "html"))
                 for d, _ in pages]
        outs = ctx.tool_parallel(calls, max_workers=p.get("parallel", 6))
        for (d, note), (status, out) in zip(pages, outs):
            res = base_res(d)
            if note:
                res["note"] = note
            if status == "error":
                res["error"] = f"{getattr(out, 'kind', 'error')}：{getattr(out, 'reason', str(out))[:80]}"
                finish(d, res, "failed")
                continue
            item = article_to_item(out, d["url"], base={"source_id": d["source_id"], "url": d["url"],
                                                        "title": d["title"],
                                                        "primary": (d["meta"] or {}).get("primary")})
            item["meta"]["extract_method"] = (d["meta"] or {}).get("extract_method") or \
                ("browser" if out.get("via_browser") else "html")
            r = upsert_document(item, d["topic"], task_id=ctx.task_id)
            finish(d, res, from_upsert(res, r))
    summary = {}
    for r in st["results"]:
        summary[r["outcome"]] = summary.get(r["outcome"], 0) + 1
    upcoming = db.query_one("SELECT COUNT(*) n, MIN(recheck_at) t FROM documents WHERE status='active' AND "
                            "recheck_at IS NOT NULL" + (" AND meta LIKE ?" if p.get("scope_task") else
                                                        " AND topic=?" if ctx.topic and ctx.topic != "*" else ""),
                            ([f"%{p['scope_task']}%"] if p.get("scope_task") else
                             [ctx.topic] if ctx.topic and ctx.topic != "*" else []))
    st["report"] = {"kind": "recheck", "scope": scope, "force": bool(p.get("force")),
                    "checked": len(st["results"]), "summary": summary, "items": st["results"],
                    "next_due": upcoming["t"], "pending": upcoming["n"],
                    "by_kind": {"github": len(gh), "arxiv": len(ax), "web": len(web)},
                    "opened_pages": len(pages)}
    ctx.observe(f"复查 {len(st['results'])} 篇：{json.dumps(summary, ensure_ascii=False)}；"
                f"实际打开网页 {len(pages)} 个（其余用接口确认未变）")
    return END


register_graph(Graph(name="recheck", start="recheck", nodes=dict(recheck=recheck)))
