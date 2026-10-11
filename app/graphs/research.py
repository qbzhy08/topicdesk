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

from .. import db, llm
from .. import memory as mem
from .. import memory_facts as mf
from ..collect import tools as _tools  # noqa: F401  注册工具
from ..collect.dedup import canonical_url, parse_date
from ..collect.fetch import FetchError
from ..collect.images import store_images
from ..keywords import is_english
from ..collect.sources import (DOMAINS, guess_domains, is_primary, list_sources, relevance,
                                source_domains, topic_terms)
from ..collect.store import article_to_item, topic_last_run, upsert_document
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


# ---------------------------------------------------------------- 工具函数

def _doc_hits(doc: dict, aspect: dict) -> bool:
    k = aspect.get("kind", "keywords")
    meta = doc.get("meta") or {}
    if k == "any":
        return True
    if k == "primary":
        return bool(meta.get("primary")) or is_primary(doc.get("url") or "")
    if k == "recent":
        d = parse_date(doc.get("published_at"))
        return bool(d and datetime.now(timezone.utc) - d <= timedelta(days=14))
    blob = f"{doc.get('title') or ''} {(doc.get('text') or '')[:6000]}".lower()
    return any(kw.lower() in blob for kw in aspect.get("keywords", []))


def _task_docs(task_id: str) -> list[dict]:
    return db.query("SELECT id, url, title, text, published_at, source_id, content_kind, novelty, "
                    "duplicate_of, event_id, meta FROM documents WHERE status='active' AND "
                    "meta LIKE ?", (f"%{task_id}%",))


def _rel(st: dict, *texts: str) -> float:
    """中英文分别计算相关度取最大：中文看核心词，英文看任一检索式的词覆盖率。"""
    plan = st.get("plan") or {}
    best = relevance(plan.get("terms_zh") or [], *texts) if plan.get("terms_zh") else 0.0
    for q in plan.get("queries_en") or []:
        words = [w for w in re.split(r"\W+", q) if len(w) > 2]
        if words:
            best = max(best, relevance(words, *texts))
    return best


def _coverage(ctx) -> dict:
    docs = [d for d in _task_docs(ctx.task_id) if not d["duplicate_of"] and
            ((d["meta"] or {}).get("relevance") is None or (d["meta"] or {}).get("relevance") >= 0.3)]
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
general=综合新闻/政策/社会。系统会按领域选择信息源。
再把主题拆成 3-5 个需要覆盖的子问题，并给出检索关键词。
中文来源是各领域媒体 RSS（按关键词过滤最新文章）和站内搜索（掘金、华尔街见闻等）。中文核心词要用中文媒体
实际常用的说法，短（2-5 字）、常见，不要自造少见词组。
英文来源是 arXiv、OpenAlex、PubMed、Hacker News、GitHub 等（检索式要短，2-4 个英文词）。
子问题的 keywords 同时给中文和英文，因为资料有中有英。
只输出 JSON：
{{"domains": ["finance"],
  "core_terms_zh": ["主题核心词，2-4个，越短越好"],
  "aspects": [{{"name": "子问题", "keywords": ["判断资料是否覆盖该子问题的中英文关键词"]}}],
  "queries_en": ["英文检索式，2-4 条"],
  "notes": "一句话说明检索策略"}}"""


def plan(ctx):
    p = ctx.params
    st = ctx.state
    st.update(queue=[], done=[], failures=[], source_fail={}, disabled=[], zero=[],
              refine_round=0, fetches=0, follow_count={}, saved=[], stats={})
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
                                    "need": 1, "keywords": a["keywords"][:12]})
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
    if prefs.get("focus"):
        fv = _vals(prefs["focus"]["value"])
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
            if h.get("consecutive_fail", 0) >= 3 and recent and (h.get("last_ok") or 0) < h["last_fail"]:
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
        elif lang == "en":
            if not queries_en:
                continue
            for q in queries_en[:2]:
                _push(st, _search_action(s["id"], q, None, f"英文来源检索「{q}」"))
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
            _push(st, _fetch_action(e["url"], f"记忆中的时效性事实需复查：「{f['content'][:30]}」", prio=58))
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

REFINE_PROMPT = """主题「{topic}」的检索还有缺口：{gaps}。
已经用过的检索：{tried}。其中无结果的：{zero}。
请给出新的检索词：中文词要短（RSS 过滤用，1-3 个词），英文检索式 2-4 个词。只输出 JSON：
{{"terms_zh": [...], "queries_en": [...], "why": "一句话"}}"""


def decide(ctx):
    st, p = ctx.state, ctx.params
    max_docs = p.get("max_docs", 40)
    cov = _coverage(ctx)
    st["coverage"] = cov
    n_docs = len(_task_docs(ctx.task_id))
    gaps = _gaps(cov)
    cov_txt = "；".join(f"{v['name']} {v['have']}/{v['need']}" for v in cov.values())

    queue = [a for a in st["queue"] if not (a["type"] == "search" and a["source"] in st["disabled"])]
    pending_fetch = any(a["type"] == "fetch" for a in queue) and st["fetches"] < p.get("max_fetch", 15)
    pending_initial = any(a.get("initial") and a["type"] == "search" for a in queue)
    if n_docs >= max_docs and not gaps and not pending_fetch and not pending_initial:
        ctx.think(f"已收集 {n_docs} 篇（上限 {max_docs}），覆盖矩阵：{cov_txt}。覆盖足够、全文补抓完成，停止检索")
        return "images"
    if n_docs >= max_docs:
        # 达到上限后不再扩量：只补全文，以及还没轮到的来源各查一次（保证来源多样性）
        queue = [a for a in queue if a["type"] == "fetch" or a.get("initial")]
    if p.get("max_fetch", 15) <= st["fetches"]:
        queue = [a for a in queue if a["type"] != "fetch"]
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

    # 队列空：根据缺口调整关键词
    if gaps and st["refine_round"] < p.get("max_refine", 2):
        st["refine_round"] += 1
        names = [cov[g]["name"] for g in gaps]
        tried = [k for k in st["done"] if k.startswith("search:")]
        new_terms, new_en, why = [], [], ""
        if llm.available() and not p.get("no_llm"):
            try:
                d = ctx.tool("llm", messages=[{"role": "user", "content": REFINE_PROMPT.format(
                    topic=ctx.topic, gaps=names, tried=tried[-12:], zero=st["zero"][-8:])}])["data"]
                new_terms, new_en, why = d.get("terms_zh") or [], d.get("queries_en") or [], d.get("why", "")
            except Exception as e:
                why = f"LLM 失败（{str(e)[:60]}），改用规则"
        if not new_terms and not new_en:
            # 先用自动翻译给出的备用说法：哪个检索式没结果，就换同义词/其他译法
            zero_en = [k.split(":", 2)[2] for k in st["zero"] if k.split(":")[1] in
                       {s["id"] for s in list_sources() if (s["config"] or {}).get("lang") == "en"}]
            used_alt = st.setdefault("used_alt", [])
            for alt in st["plan"].get("alt_en") or []:
                if alt in used_alt:
                    continue
                head = " ".join((st["plan"]["queries_en"][0].split() if st["plan"]["queries_en"] else [])[:2])
                q = alt if len(alt.split()) >= 2 else f"{head} {alt}".strip()
                new_en.append(q)
                used_alt.append(alt)
                if len(new_en) >= 2:
                    break
            for alt in st["plan"].get("alt_zh") or []:
                if alt not in used_alt:
                    new_terms.append([alt])
                    used_alt.append(alt)
            if new_en or new_terms:
                why = (f"自动调整：{('检索式 ' + str(zero_en[:2]) + ' 无结果，') if zero_en else ''}"
                       f"改用备用译法/同义词 {[' '.join(t) for t in new_terms] + new_en}")
        if not new_terms and not new_en:
            core = st["plan"]["terms_zh"][:1]
            for g in gaps:
                kws = [k for k in next(a for a in st["aspects"] if a["id"] == g)["keywords"]
                       if not k.isascii()][:1]
                if core and kws:
                    new_terms.append(core + kws)
            if not new_terms and len(st["plan"]["terms_zh"]) > 1:
                new_terms = [[t] for t in st["plan"]["terms_zh"]]   # 放宽：单个核心词
            why = why or "规则：核心词 + 缺口子问题的关键词；无结果时放宽为单个核心词"
        added = 0
        for s in list_sources():
            if s["id"] in st["disabled"] or s["id"] == "web":
                continue
            if s["id"] not in st.get("source_ids", [s["id"]]):
                continue
            lang = (s["config"] or {}).get("lang")
            if s["method"] == "rss" and lang == "zh":
                for t in new_terms[:3]:
                    t = t if isinstance(t, list) else [t]
                    q = " ".join(t)
                    if (s["config"] or {}).get("search_feed"):
                        q = t[-1]          # 站内搜索：用缺口关键词单独检索
                    added += _push(st, _search_action(s["id"], q, t,
                                                      f"补缺口 {names}：调整关键词为 {t}", prio=45))
            elif s["method"] == "api" and lang == "zh":
                for t in new_terms[:3]:
                    t = t if isinstance(t, list) else [t]
                    q = " ".join(t)
                    added += _push(st, _search_action(s["id"], q, t,
                                                      f"补缺口 {names}：调整关键词为 {t}", prio=45))
            elif lang == "en":
                for q in new_en[:2]:
                    added += _push(st, _search_action(s["id"], q, None, f"补缺口 {names}：{q}", prio=45))
        ctx.think(f"覆盖：{cov_txt}。缺口：{names}。第 {st['refine_round']} 次调整关键词（{why}），"
                  f"新增 {added} 个行动")
        if added:
            return "decide"
    ctx.think(f"覆盖：{cov_txt}。" + (f"仍有缺口 {[cov[g]['name'] for g in gaps]}，但关键词调整次数已用完，"
                                     "停止并在报告中说明" if gaps else "覆盖足够，停止检索"))
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
        if e.kind in ("robots", "blocked", "network", "not_found", "bad_content") or \
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


def _after_search(ctx, a, status, res):
    st, p = ctx.state, ctx.params
    if status == "error":
        if isinstance(res, FetchError):
            _handle_failure(ctx, a, res)
            return
        raise res
    items = res["items"]
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
    skipped = max(0, len(items) - max(0, quota - used))
    items = items[:max(0, quota - used)]
    st["per_source"][a["source"]] = used + len(items)
    counts = {}
    for it in items:
        if it.get("relevance") is None:
            it["relevance"] = _rel(st, it.get("title") or "", it.get("summary") or "")
        r = _save(ctx, it, "api" if a["source"] in API_SOURCES else "rss")
        k = r["action"] + ("/" + r["novelty"] if r["action"] == "new" else "")
        counts[k] = counts.get(k, 0) + 1
        meta = it.get("meta") or {}
        if meta.get("aggregator"):
            if not it["url"].startswith("https://news.ycombinator.com"):
                _push(st, _fetch_action(it["url"], f"聚合站只有标题（{meta.get('points')} 分），追踪原文",
                                        prio=35 + min((meta.get("points") or 0) // 50, 20),
                                        base=it, doc_id=r["doc_id"]))
        elif it["content_kind"] != "full" and a["source"] not in ("arxiv", "github", "openalex"):
            mode = "browser" if meta.get("detail") == "browser" else "auto"
            _push(st, _fetch_action(it["url"], "RSS 只有摘要，抓取全文" +
                                    ("（前端渲染页面，用浏览器）" if mode == "browser" else ""),
                                    prio=30 + int((it.get("relevance") or 0) * 10), mode=mode,
                                    base=it, doc_id=r["doc_id"]))
        if r["action"] == "new" and meta.get("out_links"):
            _follow_primary(ctx, r["doc_id"], meta["out_links"], from_primary=it.get("primary"))
    ctx.observe(f"{a['source']}「{a['query']}」入库 {len(items)} 条 → {counts}" +
                (f"；另有 {skipped} 条超出单来源配额 {quota}，未入库" if skipped else ""))


def _after_fetch(ctx, a, status, art):
    st = ctx.state
    if status == "error":
        if isinstance(art, FetchError):
            _handle_failure(ctx, a, art)
            return
        raise art
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
    calls = []
    for a in batch:
        st["done"].append(a["key"])
        if a["type"] == "search":
            calls.append(("source_search", dict(source_id=a["source"], query=a["query"],
                                                limit=p.get("per_search", 15), terms=a.get("terms"))))
        else:
            st["fetches"] += 1
            calls.append(("page_fetch", dict(url=a["url"], mode=a.get("mode", "auto"))))
    t0 = time.time()
    results = ctx.tool_parallel(calls, max_workers=p.get("parallel", 6))
    for a, (status, out) in zip(batch, results):
        (_after_search if a["type"] == "search" else _after_fetch)(ctx, a, status, out)
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
            and ((d["meta"] or {}).get("relevance") or 0) >= 0.3]
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
    return "report"


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
    gaps = [v["name"] for v in cov.values() if v["have"] < v["need"]]
    if gaps:
        omissions.append(f"覆盖不足的子问题：{gaps}")
    mstats = st.get("mem_stats", {})
    fact_ids = st.get("mem_facts", [])
    top_docs = [d["id"] for d in sorted(docs, key=lambda d: -((d["meta"] or {}).get("relevance") or 0))
                if not d["duplicate_of"]][:30]
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
        "domains": st.get("domains"), "source_ids": st.get("source_ids"),
        "memory": memory_report,
        "total_docs": len(docs),
        "unique_docs": sum(1 for d in docs if not d["duplicate_of"]),
        "events": len({d["event_id"] for d in docs}),
        "by_source": by("source_id"), "by_kind": by("content_kind"), "by_novelty": by("novelty"),
        "coverage": cov, "failures": st["failures"], "image_stats": st.get("image_stats"),
        "actions_done": len(st["done"]), "refine_rounds": st["refine_round"],
        "plan": st["plan"], "omissions": omissions,
        "budget_note": st.get("_budget_exhausted"),
    }
    ctx.observe(f"共 {len(docs)} 篇（去重后 {st['report']['unique_docs']}，{st['report']['events']} 个事件）；"
                f"新进展/更新/背景/重复：{st['report']['by_novelty']}；可能遗漏 {len(omissions)} 项")
    return END


register_graph(Graph(
    name="research", start="plan", on_budget="images",
    nodes=dict(plan=plan, decide=decide, act=act, images=images, remember=remember, report=report),
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

def recheck(ctx):
    limit = ctx.params.get("limit", 20)
    sql = "SELECT * FROM documents WHERE status='active' AND recheck_at IS NOT NULL AND recheck_at<=?"
    args = [db.now()]
    if ctx.topic and ctx.topic != "*":
        sql += " AND topic=?"
        args.append(ctx.topic)
    due = db.query(sql + " ORDER BY recheck_at LIMIT ?", (*args, limit))
    ctx.state.setdefault("results", [])
    ctx.state.setdefault("last_run_at", None)
    ctx.state.setdefault("stats", {})
    done_ids = {r["doc_id"] for r in ctx.state["results"]}
    for d in due:
        if d["id"] in done_ids:
            continue
        try:
            art = ctx.tool("page_fetch", url=d["url"], mode="auto")
            item = article_to_item(art, d["url"], base={"source_id": d["source_id"], "url": d["url"],
                                                        "title": d["title"],
                                                        "primary": (d["meta"] or {}).get("primary")})
            item["meta"]["extract_method"] = "browser" if art.get("via_browser") else "html"
            r = upsert_document(item, d["topic"], task_id=ctx.task_id)
            outcome = r["action"]
        except FetchError as e:
            outcome = f"failed:{e.kind}"
        nxt = None if outcome == "updated" else db.now() + 3 * 86400
        db.update("documents", "id", d["id"], {"recheck_at": nxt})
        ctx.state["results"].append({"doc_id": d["id"], "title": d["title"], "outcome": outcome})
    summary = {}
    for r in ctx.state["results"]:
        summary[r["outcome"]] = summary.get(r["outcome"], 0) + 1
    ctx.observe(f"复查 {len(ctx.state['results'])} 篇：{json.dumps(summary, ensure_ascii=False)}")
    return END


register_graph(Graph(name="recheck", start="recheck", nodes=dict(recheck=recheck)))
