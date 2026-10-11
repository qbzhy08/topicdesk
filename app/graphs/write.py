"""Lv3 写作流程：用筛选后的资料，按 Skill 改写成文章。

load → select → outline ⇄ gap → draft → check ⇄ fix → save

- load     读取 Skill（记录版本和指纹）、研究任务、记忆里的偏好和反馈
- select   按 Skill 的选材规则从价值排序结果里取素材，编号即出处编号
- outline  按 Skill 结构列提纲；提纲发现某节缺资料 → gap 补充检索后重新选材（最多 1 轮）
- draft    逐节写作：来源结论带 [n]，系统分析以【分析】开头
- check    逐句核对出处和数字（不依赖大模型）；能确定的引错自动改正；
           仍有问题 → 先补充检索核验（最多 1 轮），再让大模型按核对结果改写有问题的节
- save     拼成 Markdown，保存为文章的一个版本（记录 Skill 版本、素材、核对报告）
"""
from __future__ import annotations

import re

from .. import db, illustrate as ill, llm, writing
from .. import memory as mem
from .. import skills as skills_mod
from ..collect.sources import list_sources
from ..collect.dedup import title_sim
from ..collect.store import article_to_item, upsert_document
from ..harness import END, Graph, register_graph

OUTLINE_PROMPT = """你是一位编辑。用下面的素材，按 Skill「{name}」给一篇文章列提纲。
读者：{reader}；篇幅：{length}
结构（按这个顺序；heading 可以改得更具体，id 不变）：
{structure}
素材（编号. 来源 | 标题 | 开头）：
{materials}
记忆中的事实（核实状态）：
{facts}
只输出 JSON：
{{"title": "文章标题（具体、不夸张，不超过 28 字）",
  "sections": [{{"id": "节的 id", "heading": "小标题", "points": ["这一节要讲的要点，一句话"], "sources": [1, 3]}}],
  "gaps": [{{"section": "节的 id", "question": "缺什么资料", "query": "检索词（2-6 个中文字或 2-4 个英文词）"}}]}}
检索阶段已经覆盖的子问题：{covered}
检索阶段已经专门补检过、仍然缺资料的方向（不要再列为缺口，写作时如实说明资料不足）：{tried}
gaps：只有某一节的目标在素材里完全找不到依据、而且不属于上面两类时才列（最多 2 个），否则给空数组。
多数情况下素材已经够用，应给空数组。"""

SECTION_PROMPT = """你在按 Skill「{name}」写文章《{title}》中的一节。
读者：{reader}
风格：{style}
{body}
{prefs}
本节小标题：{heading}
本节目标：{goal}
要点：{points}
篇幅：约 {words} 字
可用素材（只能使用这些内容；方括号编号就是出处编号）：
{materials}
{facts}
{fix}
规则：
1. 来自素材的事实、数字、引语，在句末加出处编号，例如“……扩展到 64K[2]。”；多个来源支持就写 [2][5]。
2. 你自己的推断、评价、判断单独成段，段首写“【分析】”；分析里不要出现素材里没有的数字。
3. 素材里没有的信息不要写；素材不足就写“目前公开资料未能确认……”，不要编造。
4. 记忆中的事实只是线索：要写其中的内容，就引用它标注的出处编号；没有出处编号的，不要写它的数字。
5. 直接输出这一节的正文 Markdown，不要重复小标题。正文是给读者看的：不要出现“本节”“素材”“记忆中的事实”
   “出处编号”“暂不引用”这类写作过程的说明，也不要写“综上所述”。"""

CHECK_NAME = {"weak": "出处里找不到依据", "number": "数字对不上出处", "bad_cite": "出处编号不存在",
              "uncited": "有数字但没有出处", "analysis_num": "分析里有资料中没有的数字"}

FIX_NOTE = """上一稿核对发现这些问题，请改写本节并修正：数字以出处原文为准；找不到依据的数字和说法直接删掉，
或改成不含数字的定性表述。不要在正文里解释你删了什么、为什么删（读者看不到核对过程）：
{items}"""


def _names():
    return {s["id"]: s["name"] for s in list_sources(enabled_only=False)}


def _docs(st) -> list[dict]:
    """state 里只存素材的元数据，正文按需从数据库读（state 不会因为正文变得很大）。"""
    rows = writing._doc_rows([d["id"] for d in st["docs"]])
    return [{**d, "text": (rows.get(d["id"]) or {}).get("text") or (rows.get(d["id"]) or {}).get("summary") or ""}
            for d in st["docs"]]


def _use_llm(ctx):
    return llm.available() and not ctx.params.get("no_llm")


# ---------------------------------------------------------------- load / select

def load(ctx):
    st, p = ctx.state, ctx.params
    sk = skills_mod.get(p["skill"])
    st["skill"] = {**skills_mod.public(sk), "body": sk["body"]}
    rt = db.query_one("SELECT id, topic, status, state FROM tasks WHERE id=?", (p["research_task"],))
    if not rt or rt["status"] != "done":
        raise ValueError("研究任务不存在或还没有完成")
    st["research_task"] = rt["id"]
    rs = rt["state"] or {}
    st["terms"] = ((rs.get("plan") or {}).get("terms_zh") or [])[:6]
    st["source_ids"] = ((rs.get("report") or {}).get("source_ids") or [])
    st["source_hits"] = ((rs.get("report") or {}).get("by_source") or {})
    # 检索阶段已经定向补检过、仍然缺资料的子问题：写作时不再重复检索，如实写“资料不足”
    st["research_gaps"] = [{"name": g["name"], "queries": [q.split("：", 1)[-1] for q in g.get("queries") or []]}
                           for g in (rs.get("gap_log") or {}).values() if g.get("status") == "insufficient"]
    st["covered"] = [v["name"] for v in ((rs.get("report") or {}).get("coverage") or {}).values()
                     if v.get("have", 0) >= v.get("need", 1)]
    st["verdicts"] = {}
    rec = mem.recall(ctx.user_id, ctx.topic, f"skill:{sk['id']}", 600, ctx.task_id, record_use=True)
    prefs = [x for x in rec["preferences"] if (x["meta"] or {}).get("type") in ("style", "background", "focus", "exclude")
             or x["kind"] == "feedback"]
    st["prefs"] = [x["line"] for x in prefs]
    st["gap_round"] = 0
    st["fix_round"] = 0
    st["extra_docs"] = []
    st["gap_log"] = []
    st["fixes"] = []
    if not st.get("article_id"):
        aid = p.get("article_id") or db.new_id("a_")
        if not p.get("article_id"):
            db.insert("articles", dict(id=aid, task_id=ctx.task_id, topic=ctx.topic, skill=sk["id"],
                                       skill_version=f"{sk['version']}#{sk['fingerprint']}", title=None,
                                       current_version=0, status="writing", created_at=db.now(), updated_at=db.now()))
        st["article_id"] = aid
    ctx.think(f"使用 Skill「{sk['name']}」v{sk['version']}（指纹 {sk['fingerprint']}）：读者是{sk['reader']}；篇幅 {sk['length']}；"
              f"结构 {' / '.join(s['heading'] for s in sk['structure'])}" +
              (f"；召回 {len(st['prefs'])} 条写作偏好/反馈：{'；'.join(st['prefs'])}" if st["prefs"] else ""))
    return "select"


def select(ctx):
    st = ctx.state
    sk = st["skill"]
    # 记忆中的事实要能引用：把它们的出处资料也放进素材（最多 4 篇），事实标上出处编号
    st["facts"] = writing.topic_facts(ctx.topic, sk["material"].get("facts", "all"))
    base_ids = {d["id"] for d in writing.select_material(st["research_task"], sk)["docs"]}
    fact_docs, fact_why = [], {}
    for f in st["facts"]:
        for did in f["docs"][:1]:
            if did not in base_ids and did not in fact_docs and len(fact_docs) < 4:
                fact_docs.append(did)
                fact_why[did] = f"记忆中事实「{f['content'][:20]}…」的出处"
    st["fact_docs"] = fact_docs
    mat = writing.select_material(st["research_task"], sk, fact_docs + [x for x in st["extra_docs"] if x not in fact_docs],
                                  extra_why=fact_why)
    names = _names()
    st["docs"] = [{k: v for k, v in d.items() if k != "text"} | {"label": writing.source_label(d, names),
                                                                 "chars": len(d["text"])} for d in mat["docs"]]
    st["ranking_version"] = mat["ranking_version"]
    n_of = {d["id"]: d["n"] for d in st["docs"]}
    for f in st["facts"]:
        f["refs"] = sorted({n_of[x] for x in f["docs"] if x in n_of})
    ctx.observe(f"按 Skill 选材 {len(st['docs'])} 篇（{'五分钟' if sk['material']['list'] == '5' else '三十分钟'}清单优先，"
                f"偏好 {sk['material'].get('prefer') or '无'}）；记忆中的事实 {len(st['facts'])} 条：\n" +
                "\n".join(f"[{d['n']}] {d['title'][:40]}（{d['why']}）" for d in st["docs"]))
    if not st["docs"]:
        raise ValueError("没有可用的素材（研究任务里没有正文或摘要）")
    return "outline"


# ---------------------------------------------------------------- outline / gap

def _materials_brief(docs) -> str:
    return "\n".join(f"{d['n']}. {d['label']} | {d['title'][:60]} | {re.sub(r'\s+', ' ', d['text'][:160])}" for d in docs)


def _facts_text(facts) -> str:
    v = {"supported": "已核实", "unverified": "待核实", "disputed": "有争议", "refuted": "已否定"}
    return "\n".join(f"- [{v.get(f['verify'], f['verify'])}] {f['content']}" +
                     (f"（已更正，原说法：{f['corrected_from'][:60]}）" if f.get("corrected_from") else "") +
                     (f"——出处 {''.join(f'[{n}]' for n in f['refs'])}" if f.get("refs") else "——素材里没有它的出处，不要使用其中的数字")
                     for f in facts) or "（无）"


def outline(ctx):
    st = ctx.state
    sk = st["skill"]
    docs = _docs(st)
    base = [{"id": s["id"], "heading": s["heading"], "goal": s["goal"], "words": s["words"], "points": [],
             "sources": []} for s in sk["structure"]]
    title, gaps = None, []
    if _use_llm(ctx):
        try:
            d = ctx.tool("llm", messages=[{"role": "system", "content": "你只输出 JSON。"}, {
                "role": "user", "content": OUTLINE_PROMPT.format(
                    name=sk["name"], reader=sk["reader"], length=sk["length"],
                    structure="\n".join(f"- id={s['id']}｜{s['heading']}：{s['goal']}" for s in sk["structure"]),
                    materials=_materials_brief(docs), facts=_facts_text(st["facts"]),
                    covered="、".join(st.get("covered") or []) or "（无记录）",
                    tried="、".join(g["name"] for g in st.get("research_gaps") or []) or "（无）")}],
                max_tokens=1800)["data"]
            title = str(d.get("title") or "")[:40] or None
            by = {x.get("id"): x for x in d.get("sections") or [] if isinstance(x, dict)}
            valid = {x["n"] for x in docs}
            for s in base:
                x = by.get(s["id"]) or {}
                s["heading"] = str(x.get("heading") or s["heading"])[:30]
                s["points"] = [str(t)[:80] for t in x.get("points") or []][:6]
                s["sources"] = [int(n) for n in x.get("sources") or [] if str(n).isdigit() and int(n) in valid]
            gaps = [g for g in d.get("gaps") or [] if isinstance(g, dict) and g.get("query")][:2]
        except Exception as e:  # noqa: BLE001
            ctx.think(f"大模型列提纲失败（{str(e)[:60]}），按 Skill 结构直接写")
    st["title"] = title or f"{ctx.topic}：{sk['name']}"
    st["sections"] = base
    st["outline_by"] = "llm" if title else "rule"
    ctx.think(f"提纲《{st['title']}》：" + "；".join(f"{s['heading']}（素材 {s['sources'] or '全部'}）" for s in base))
    if gaps and st["gap_round"] == 0:
        head = {x["id"]: x["heading"] for x in base}
        st["gap_queue"] = [{"query": str(g["query"])[:30],
                            "why": f"提纲发现「{head.get(g.get('section'), g.get('section'))}」缺资料：{g.get('question', '')}",
                            "section": g.get("section")} for g in gaps]
        return "gap"
    return "draft"


SEARCHABLE = ("api", "websearch")
ACADEMIC = {"arxiv", "openalex", "pubmed"}


def _gap_sources(st) -> list[dict]:
    """补检用哪些来源：检索阶段贡献资料最多、并且支持按关键词搜索的前 3 个；学术库只有在它确实贡献过资料时才用。"""
    hits = st.get("source_hits") or {}
    srcs = [s for s in list_sources() if s["id"] in st["source_ids"] and
            (s["method"] in SEARCHABLE or (s["config"] or {}).get("search_feed"))]
    srcs = [s for s in srcs if s["id"] not in ACADEMIC or hits.get(s["id"], 0) >= 2]
    srcs.sort(key=lambda s: (-hits.get(s["id"], 0), s["id"] != "web_search"))
    return srcs[:3]


def _relevant(st, query: str, it: dict) -> bool:
    """补检结果是否相关：检索词至少命中 2 个（只有 1-2 个词时全部命中），并且包含主题核心词。
    避免“量化”命中“汽车轻量化”这类同字异义。"""
    blob = f"{it.get('title')} {it.get('summary')}".lower()
    words = [w.lower() for w in re.split(r"\s+", query) if len(w) >= 2]
    need = len(words) if len(words) <= 2 else 2
    hit = sum(1 for w in words if w in blob)
    topic_ok = any(t.lower() in blob for t in (st.get("terms") or [])[:5]) or not st.get("terms")
    return hit >= need and topic_ok


def gap(ctx):
    """补充检索：并行查询（不同来源同时进行），新资料加入素材后重新选材。
    检索阶段已经补检过仍无资料的方向直接跳过，不重复花时间。"""
    st = ctx.state
    st["gap_round"] += 1
    queue, st["gap_queue"] = st.get("gap_queue") or [], []
    known = [k for g in st.get("research_gaps") or [] for k in [g["name"]] + g["queries"]]
    todo = []
    for g in queue:
        dup = next((k for k in known if title_sim(g["query"], k) >= 0.5 or g["query"] in k), None)
        if dup:
            st["gap_log"].append({**g, "sources": [], "found": [], "skipped": f"检索阶段已补检过「{dup}」仍无资料，跳过"})
        else:
            todo.append(g)
    srcs = _gap_sources(st)
    calls = [("source_search", dict(source_id=s["id"], query=g["query"], limit=6)) for g in todo for s in srcs]
    results = ctx.tool_parallel(calls) if calls else []
    known_ids = {d["id"] for d in st["docs"]} | set(st["extra_docs"])
    picks, logs = [], []
    for gi, g in enumerate(todo):
        log = {**g, "sources": [], "found": []}
        cands = []
        for si, src in enumerate(srcs):
            status, out = results[gi * len(srcs) + si]
            if status != "ok":
                log["sources"].append(f"{src['name']} 失败：{str(out)[:40]}")
                continue
            ok = [it for it in out["items"] if _relevant(st, g["query"], it)]
            log["sources"].append(f"{src['name']} {out['count']} 条，相关 {len(ok)} 条")
            cands += ok
        picks.append((log, cands[:3]))
        logs.append(log)
    # 只有摘要的结果并行抓全文
    need_fetch = [it for _, cs in picks for it in cs if it.get("content_kind") != "full" and it.get("url")]
    fetched = dict(zip([it["url"] for it in need_fetch],
                       ctx.tool_parallel([("page_fetch", {"url": it["url"]}) for it in need_fetch]) if need_fetch else []))
    for log, cs in picks:
        for it in cs:
            st_, art = fetched.get(it["url"], (None, None))
            if st_ == "ok":
                it = {**it, **article_to_item(art, it["url"], base=it)}
            it.setdefault("meta", {})["extract_method"] = "gap"
            r = upsert_document(it, ctx.topic, task_id=ctx.task_id)
            if r["doc_id"] not in known_ids:
                known_ids.add(r["doc_id"])
                st["extra_docs"].append(r["doc_id"])
                log["found"].append((it.get("title") or it["url"])[:50])
    st["gap_log"] += logs
    ctx.observe("补充检索（并行）：" + "；".join(
        f"「{g['query']}」→ {len(g['found'])} 篇（{'、'.join(g['sources'])}）" for g in logs) +
        "".join(f"；{g['skipped']}" for g in st["gap_log"] if g.get("skipped") and g in st["gap_log"][-len(queue):]))
    if any(g["found"] for g in logs):
        # 新资料追加在素材末尾（已有编号不变），挂到提出缺口的那一节；不重新列提纲，省一次大模型调用
        before = {d["id"] for d in st["docs"]}
        st["docs"] = [{k: v for k, v in d.items() if k != "text"} | {"label": writing.source_label(d, _names()),
                                                                     "chars": len(d["text"])}
                      for d in writing.select_material(st["research_task"], st["skill"], (st.get("fact_docs") or []) + [x for x in st["extra_docs"] if x not in (st.get("fact_docs") or [])])["docs"]]
        new_n = [d["n"] for d in st["docs"] if d["id"] not in before]
        for g in logs:
            sec = next((x for x in st.get("sections") or [] if x["id"] == g.get("section")), None)
            if sec and sec["sources"]:
                sec["sources"] = sorted(set(sec["sources"]) | set(new_n))
    if st.get("after_gap") == "fix":       # 核对阶段发起的补检：带着新资料去改写
        st["after_gap"] = None
        return "fix"
    return "draft"


# ---------------------------------------------------------------- draft

def _prefs_text(st) -> str:
    return ("用户的写作偏好和以往反馈（必须遵守）：\n" + "\n".join(f"- {x}" for x in st["prefs"])) if st["prefs"] else ""


def _section_materials(sec, docs) -> str:
    use = [d for d in docs if d["n"] in sec["sources"]] or docs
    focus = f"{sec['heading']} {sec['goal']} {' '.join(sec['points'])}"
    budget = max(350, min(900, 3600 // max(1, len(use))))
    return "\n\n".join(f"[{d['n']}] {d['label']}｜{d['title'][:60]}\n{writing.excerpt(d, focus, budget)}" for d in use)


def _write_section(ctx, sec, docs, fix_items=None) -> str:
    st = ctx.state
    sk = st["skill"]
    if _use_llm(ctx):
        fix = FIX_NOTE.format(items="\n".join(f"- 「{i['sentence'][:80]}」：{i.get('note') or i['status']}"
                                              + (f"（出处原文：{i['quote'][:80]}）" if i.get("quote") else "")
                                              for i in fix_items)) if fix_items else ""
        prev = f"\n上一稿：\n{sec['md']}" if fix_items else ""
        r = ctx.tool("llm", json_mode=False, max_tokens=1600, messages=[{"role": "user", "content": SECTION_PROMPT.format(
            name=sk["name"], title=st["title"], reader=sk["reader"], style="；".join(sk["style"]),
            body=sk["body"], prefs=_prefs_text(st), heading=sec["heading"], goal=sec["goal"],
            points="；".join(sec["points"]) or "（按目标自行组织）", words=sec["words"],
            materials=_section_materials(sec, docs),
            facts="记忆中的事实：\n" + _facts_text(st["facts"]), fix=fix + prev)}])
        text = r["content"].strip()
        text = re.sub(r"^#+\s*" + re.escape(sec["heading"]) + r"\s*\n", "", text)
        text, removed = writing.strip_meta(text)
        if removed:
            st["fixes"].append({"section": sec["id"], "why": "删掉混进正文的写作说明",
                                "before": "；".join(removed)[:300], "after": "（已删除）"})
        return text
    return _rule_section(sec, docs, st, fix_items)


def _rule_section(sec, docs, st, fix_items=None) -> str:
    """未配置大模型：从素材里抽取和本节目标最相关的原句，逐句标出处（摘编，不是改写）。"""
    if "【分析】" in sec["goal"] or sec["id"] in ("judgement", "verdict"):
        n_src = len({d["source_id"] for d in docs})
        disputed = [f for f in st["facts"] if f["verify"] == "disputed"]
        return (f"【分析】（未配置大模型，这一段由规则生成）本文依据 {len(docs)} 篇资料、{n_src} 个来源；"
                + (f"其中有 {len(disputed)} 条说法存在争议，结论需要等待更多独立来源。" if disputed
                   else "各来源说法没有明显冲突，但独立来源有限，结论仍需持续跟踪。"))
    use = [d for d in docs if d["n"] in sec["sources"]] or docs
    focus = writing.bigrams(f"{sec['heading']} {sec['goal']} {' '.join(st['terms'])}")
    bad = {i["sentence"] for i in fix_items or []}
    cands = []
    taken = st.setdefault("_rule_used", [])        # 各节不重复使用同一句
    for d in use:
        for i, s in enumerate(writing.sentences(d["text"])[:40]):
            if 12 <= len(s) <= 120 and s not in taken and not re.search(r"[上下左右]图|如图|见图|图\s*\d", s):
                cands.append((len(writing.bigrams(s) & focus) - i * 0.05, d["n"], s))
    cands.sort(key=lambda x: -x[0])
    out, seen = [], set()
    n_max = max(2, min(5, sec["words"] // 70))
    used_docs = set()
    for _, n, s in cands:
        if s in seen or any(writing.overlap(s, o) > 0.6 for o in list(seen) + taken):
            continue
        if (n, s) in used_docs:
            continue
        line = re.sub(r"[。！？!?；;.]?$", "", s) + f"[{n}]" + ("." if s.isascii() else "。")
        if line in bad:
            continue
        seen.add(s)
        taken.append(s)
        out.append(line)
        if len(out) >= n_max:
            break
    return "\n".join(f"- {x}" for x in out) if sec["id"] in ("what", "timeline") else "".join(out)


def draft(ctx):
    st = ctx.state
    docs = _docs(st)
    st["_rule_used"] = []
    for sec in st["sections"]:
        sec["md"] = _write_section(ctx, sec, docs)
    st["draft_by"] = "llm" if _use_llm(ctx) else "rule"
    ctx.observe(f"逐节写作完成（{'大模型' if st['draft_by'] == 'llm' else '规则摘编：未配置大模型'}），共 "
                f"{sum(len(s['md']) for s in st['sections'])} 字")
    return "check"


# ---------------------------------------------------------------- check / fix

def _check(ctx, docs):
    """字面核对 → 字面不像的句子交给大模型对照出处原文判断意思是否一致（结果缓存在 state，改写后只核新句子）。"""
    st = ctx.state
    rep = writing.check(st["sections"], docs, st["verdicts"])
    pending = [i for i in rep["items"] if i["status"] == "weak" and i["sentence"] not in st["verdicts"]]
    if pending and _use_llm(ctx):
        for k in range(0, len(pending), 15):
            chunk = pending[k:k + 15]
            try:
                d = ctx.tool("llm", messages=[{"role": "system", "content": "你只输出 JSON。"},
                                              {"role": "user", "content": writing.semantic_batch(chunk, docs)}],
                             max_tokens=1500)["data"]
            except Exception as e:  # noqa: BLE001
                ctx.think(f"语义核对失败（{str(e)[:60]}），这些句子按字面核对结果处理")
                break
            for r in d.get("results") or []:
                try:
                    it = chunk[int(r.get("i")) - 1]
                except (TypeError, ValueError, IndexError):
                    continue
                st["verdicts"][it["sentence"]] = {"verdict": r.get("verdict"), "reason": str(r.get("reason") or "")[:40]}
        rep = writing.check(st["sections"], docs, st["verdicts"])
    return rep


SERIOUS = ("number", "bad_cite", "uncited", "analysis_num", "weak")


def _serious(ctx, rep):
    probs = writing.problems(rep, SERIOUS)
    if _use_llm(ctx):
        return probs           # weak 已经过大模型语义核对，仍判不支持才算
    return [i for i in probs if i["status"] != "weak" or i.get("overlap", 1) < 0.2]


def check(ctx):
    st = ctx.state
    docs = _docs(st)
    rep = _check(ctx, docs)
    fixes = writing.autofix_citations(st["sections"], rep)
    if fixes:
        st["fixes"] += fixes
        rep = _check(ctx, docs)
    st["check"] = rep
    s = rep["summary"]
    ctx.observe(f"逐句核对：带出处 {s['cited']} 句，字面支持 {s['supported']}、改写后意思一致 {s['semantic']}"
                f"（支持率 {(s['support_rate'] or 0) * 100:.0f}%），数字对不上 {s['number']}，找不到依据 {s['weak']}，"
                f"无出处的数字 {s['uncited']}，分析 {s['analysis']} 句" + (f"；自动改正出处 {len(fixes)} 处" if fixes else ""))
    serious = _serious(ctx, rep)
    if serious and st["fix_round"] == 0:
        st["fix_round"] = 1
        # 分析里编的数字只需改写；来源结论找不到依据才值得回到检索核验
        unresolved = [i for i in serious if not i.get("alt") and i["status"] in ("number", "weak")]
        if unresolved and st["gap_round"] < 2:
            qs = []
            for i in unresolved[:2]:
                core = " ".join(i.get("numbers", [])[:1] + st["terms"][:2])
                if core.strip() and core not in [q["query"] for q in qs]:
                    qs.append({"query": core[:30], "why": f"核验「{i['sentence'][:40]}」", "section": i["section"]})
            if qs:
                st["gap_queue"] = qs
                st["after_gap"] = "fix"
                ctx.think(f"{len(unresolved)} 句在现有资料里找不到依据，先补充检索核验，再改写")
                return "gap"
        return "fix"
    return "illustrate"


def fix(ctx):
    st = ctx.state
    docs = _docs(st)
    rep = _check(ctx, docs)
    by_sec = {}
    for i in _serious(ctx, rep):
        by_sec.setdefault(i["section"], []).append(i)
    for sec in st["sections"]:
        items = by_sec.get(sec["id"])
        if not items:
            continue
        before = sec["md"]
        if _use_llm(ctx):
            # 改写时把补检到的新资料也给它（编号续在后面）
            if st["extra_docs"]:
                sec["sources"] = sorted(set(sec["sources"]) | {d["n"] for d in docs if d["id"] in st["extra_docs"]})
            sec["md"] = _write_section(ctx, sec, docs, items)
        else:
            for i in items:          # 规则模式：标注待核实，而不是悄悄删掉
                if i["sentence"] in sec["md"] and "（待核实）" not in i["sentence"]:
                    sec["md"] = sec["md"].replace(i["sentence"], i["sentence"].rstrip("。") + "（待核实）", 1)
        st["fixes"].append({"section": sec["id"], "items": [{"status": i["status"], "sentence": i["sentence"][:80],
                                                             "note": i.get("note")} for i in items[:6]],
                            "why": "；".join(f"{CHECK_NAME.get(i['status'], i['status'])}：{i['sentence'][:30]}" for i in items[:4]),
                            "before": before[:300], "after": sec["md"][:300], "rewrite": True})
    ctx.observe(f"按核对结果改写 {len(by_sec)} 节：{'、'.join(next(x['heading'] for x in st['sections'] if x['id'] == k) for k in by_sec)}")
    return "check"


# ---------------------------------------------------------------- illustrate（配图）

MATCH_PROMPT = """判断每张图片放在对应段落后面是否合适：图片的图注和原文上下文，与段落讲的是不是同一件事、同一个对象或同一组数据。
{items}
只输出 JSON：{{"results": [{{"i": 1, "match": true, "reason": "不超过 20 字"}}]}}"""


def _names_short(sid):
    return _names().get(sid, sid or "")


def illustrate(ctx):
    st = ctx.state
    sk = st["skill"]
    docs = _docs(st)
    need = int(sk["images"].get("body_min") or 2)
    used_n = set(st["check"]["used"])
    # 引用过的资料排前面；还没下载过配图的，先下载（最多 4 篇）
    order = [d for d in docs if d["n"] in used_n] + [d for d in docs if d["n"] not in used_n]
    have = {r["doc_id"] for r in db.query(
        f"SELECT DISTINCT doc_id FROM images WHERE doc_id IN ({','.join('?' * len(order))})", [d["id"] for d in order])}
    todo = []
    for d in order:
        row = db.query_one("SELECT url, meta FROM documents WHERE id=?", (d["id"],))
        if d["id"] not in have and (row["meta"] or {}).get("image_candidates") and len(todo) < 4:
            todo.append((d["id"], row["url"], row["meta"]["image_candidates"]))
    if todo:
        from ..collect.images import store_images

        def dl(u, referer=None):
            try:
                return ctx.tool("image_download", url=u, referer=referer)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason": f"下载异常：{str(e)[:80]}"}
        for did, url, cands in todo:
            store_images(did, url, cands, max_images=6, download_fn=dl)
    cands = ill.candidates([d["id"] for d in order])
    banned, figs, rejected = set(), [], []
    for _ in range(3):           # 大模型否决的图换下一张，最多重试 2 轮
        pool = [c for c in cands if c["id"] not in banned]
        figs, rejected = ill.place(pool, st["sections"], docs, need)
        if not figs or not _use_llm(ctx):
            break
        items = []
        for k, f in enumerate(figs, 1):
            sec = next(x for x in st["sections"] if x["id"] == f["section"])
            para = ill.paragraphs(sec["md"])[f["para"]]
            im = next(c for c in cands if c["id"] == f["image_id"])
            items.append(f"{k}. 图注：{(im.get('caption') or '（无）')[:80]}；图片上下文：{(im.get('context_text') or '（无）')[:160]}\n"
                         f"   段落：{writing.CITE.sub('', para)[:220]}")
        try:
            res = ctx.tool("llm", messages=[{"role": "system", "content": "你只输出 JSON。"},
                                            {"role": "user", "content": MATCH_PROMPT.format(items="\n".join(items))}])["data"]
        except Exception as e:  # noqa: BLE001
            ctx.think(f"图文相符确认失败（{str(e)[:60]}），按相似度结果放置")
            break
        bad = []
        for r in res.get("results") or []:
            try:
                f = figs[int(r.get("i")) - 1]
            except (TypeError, ValueError, IndexError):
                continue
            if r.get("match") is False:
                bad.append(f)
                rejected.append({"image": f["image_id"], "url": f["url"], "doc": f["doc_title"],
                                 "why": f"大模型判断图文不符：{str(r.get('reason') or '')[:30]}"})
            else:
                f["why"] += f"；大模型确认相符：{str(r.get('reason') or '')[:30]}"
        if not bad:
            break
        banned |= {f["image_id"] for f in bad}
    for f in figs:
        src = _names_short(f["source_id"])
        cap = (f["orig_caption"] or "").strip()
        f["caption"] = (cap if cap else "原文配图") + f"（图源：{src}《{f['doc_title'][:24]}》）"
        f["alt"] = cap or f["doc_title"][:30]
        if not cap:
            f.setdefault("warn", []).append("原文没有图注，请确认图片内容与这段一致")
    # ---- 原图不够：用核对过的数据画示意图
    diagrams = []
    if len(figs) < need:
        ev = ill.timeline_events(st["check"]["items"], st["sections"])
        if len(ev) >= 3:
            refs = sorted({c for e in ev for c in e["cites"]})
            sec_counts = {}
            for e in ev:
                sec_counts[e["section"]] = sec_counts.get(e["section"], 0) + 1
            sid = max(sec_counts, key=sec_counts.get)
            if sid in {f["section"] for f in figs}:
                sid = next((x["id"] for x in st["sections"] if x["id"] not in {f["section"] for f in figs}), sid)
            note = "示意图：根据文中出处 " + "".join(f"[{r}]" for r in refs) + " 的内容整理，非原始图片"
            path = ill._save_svg(ill.timeline_svg("时间线", ev, "根据文中引用的资料整理"))
            diagrams.append({"kind": "diagram", "diagram": "timeline", "src": "/media/" + path, "section": sid,
                             "para": max(0, len(ill.paragraphs(next(x for x in st["sections"] if x["id"] == sid)["md"])) - 1),
                             "caption": note, "alt": "时间线示意图", "why": f"原始配图不足；正文有 {len(ev)} 个带日期的已核对事件"})
    if len(figs) + len(diagrams) < need and _use_llm(ctx):
        ok_items = [i for i in st["check"]["items"] if i["status"] in ("supported", "semantic") and i["numbers"]]
        if ok_items:
            lines = "\n".join(f"- （节 {i['section']}）{i['sentence'][:160]}" for i in ok_items[:30])
            try:
                spec = ctx.tool("llm", messages=[{"role": "system", "content": "你只输出 JSON。"},
                                                 {"role": "user", "content": ill.CHART_PROMPT.format(lines=lines)}])["data"]
            except Exception:  # noqa: BLE001
                spec = {"kind": "none"}
            if spec.get("kind") == "bar":
                problem = ill.verify_chart(spec, docs)
                if problem:
                    rejected.append({"image": None, "url": None, "doc": "示意图", "why": f"没画柱状图：{problem}"})
                else:
                    refs = [int(r) for r in spec["refs"]]
                    items = [{"label": str(i["label"]), "value": float(i["value"])} for i in spec["items"]]
                    path = ill._save_svg(ill.bar_svg(str(spec.get("title") or "数据对比")[:18], str(spec.get("unit") or ""),
                                                     items, "根据文中引用的资料整理，数值均可在出处中找到"))
                    sid = spec.get("section") if spec.get("section") in {x["id"] for x in st["sections"]} else st["sections"][0]["id"]
                    diagrams.append({"kind": "diagram", "diagram": "bar", "src": "/media/" + path, "section": sid,
                                     "para": 0, "alt": "数据示意图",
                                     "caption": f"示意图：{spec.get('title') or '数据对比'}，数据来自出处 " + "".join(f"[{r}]" for r in refs) + "，非原始图片",
                                     "why": "原始配图不足；用正文中已核对的数字作图，每个数值都在出处里找到"})
    if len(figs) + len(diagrams) < need:
        nums = ill.number_items(st["check"]["items"])
        if len(nums) >= 2:
            refs = sorted({c for x in nums for c in x["cites"]})
            path = ill._save_svg(ill.numbers_svg("关键数字", nums, "数字均摘自文中引用的资料，已逐个核对"))
            sid = max({x["section"] for x in nums}, key=lambda k: sum(1 for x in nums if x["section"] == k))
            if sid in {f["section"] for f in figs + diagrams}:
                sid = next((x["id"] for x in st["sections"] if x["id"] not in {f["section"] for f in figs + diagrams}), sid)
            diagrams.append({"kind": "diagram", "diagram": "numbers", "src": "/media/" + path, "section": sid,
                             "para": max(0, len(ill.paragraphs(next(x for x in st["sections"] if x["id"] == sid)["md"])) - 1),
                             "alt": "关键数字示意图",
                             "caption": "示意图：文中的关键数字，出处 " + "".join(f"[{r}]" for r in refs) + "，非原始图片",
                             "why": "原始配图不足；用正文中字面核对通过的数字整理"})
    st["figures"] = figs + diagrams
    # ---- 封面
    cov = ill.pick_cover(cands, {f["image_id"] for f in figs}, st["terms"])
    if cov:
        st["cover"] = {"kind": "photo", "image_id": cov["id"], "src": "/media/" + cov["local_path"], "url": cov["url"],
                       "doc_title": cov["doc_title"], "doc_id": cov["doc_id"],
                       "caption": ((cov.get("caption") or "").strip() or "封面图") + f"（图源：{_names_short(cov['source_id'])}《{cov['doc_title'][:24]}》）",
                       "alt": (cov.get("caption") or cov["doc_title"])[:30], "why": "横图、清晰、与主题最相关"}
    else:
        path = ill._save_svg(ill.cover_svg(st["title"], f"{sk['name']} · {ctx.topic}"))
        st["cover"] = {"kind": "diagram", "src": "/media/" + path, "alt": "示意封面",
                       "caption": "示意封面：素材中没有合适的横版原图，按标题生成",
                       "why": "没有满足封面要求（横图、宽 ≥ 640、清晰）的原始图片"}
    st["image_rejected"] = rejected
    ctx.observe(f"配图：候选 {len(cands)} 张，正文用原图 {len(figs)} 张、示意图 {len(diagrams)} 张（要求至少 {need} 张），"
                f"封面{'用原图' if st['cover']['kind'] == 'photo' else '为示意封面'}；未采用 {len(rejected)} 张：" +
                "；".join(f"{r['doc'][:14]}：{r['why']}" for r in rejected[:5]))
    return "save"


# ---------------------------------------------------------------- save

def save(ctx):
    st = ctx.state
    docs = _docs(st)
    if not st.get("sections") or not all("md" in x for x in st["sections"]):
        raise ValueError("预算耗尽，文章还没写完，无法保存")
    rep = st.get("check") or writing.check(st["sections"], docs)
    figs, cover = st.get("figures") or [], st.get("cover")
    secs, refmap = writing.renumber(ill.insert_figures(st["sections"], figs))
    rdocs = [{**d, "n": refmap[d["n"]]} for d in docs if d["n"] in refmap]
    md = writing.assemble(st["title"], secs, rdocs, set(refmap.values()), _names(),
                          note=None if st["draft_by"] == "llm" else "未配置大模型：正文为资料原句摘编，没有改写。",
                          cover_md=ill.figure_md(cover) if cover else None)
    image_checks = ill.final_check(md, figs, cover, int(st["skill"]["images"].get("body_min") or 2))
    sk = st["skill"]
    meta = {"skill": {"id": sk["id"], "name": sk["name"], "version": sk["version"], "fingerprint": sk["fingerprint"]},
            "research_task": st["research_task"], "ranking_version": st.get("ranking_version"),
            "docs": [{**{k: d[k] for k in ("n", "id", "title", "url", "source_id", "label", "why", "kind")},
                      "ref": refmap.get(d["n"])} for d in st["docs"]],
            "refmap": {str(k): v for k, v in refmap.items()},
            "facts": st["facts"], "sections": st["sections"], "check": rep, "fixes": st["fixes"],
            "gap_log": st["gap_log"], "prefs": st["prefs"],
            "figures": figs, "cover": cover, "image_rejected": st.get("image_rejected") or [], "image_checks": image_checks, "outline_by": st["outline_by"], "draft_by": st["draft_by"]}
    v = save_version(st["article_id"], md, meta, note=ctx.params.get("note") or "初稿", title=st["title"],
                     task_id=ctx.task_id)
    st["result"] = {"article_id": st["article_id"], "version": v}
    s = rep["summary"]
    ctx.observe(f"保存文章第 {v} 版：{len(md)} 字，引用 {len(rep['used'])} 篇资料，出处支持率 "
                f"{(s['support_rate'] or 0) * 100:.0f}%，遗留问题 {s['problems']} 处")
    return END


def save_version(aid: str, md: str, meta: dict, note: str, title: str | None = None, task_id: str | None = None,
                 status: str = "draft") -> int:
    v = (db.query_one("SELECT MAX(version) v FROM article_versions WHERE article_id=?", (aid,))["v"] or 0) + 1
    db.insert("article_versions", dict(article_id=aid, version=v, markdown=md, meta=meta, note=note, created_at=db.now()))
    ch = {"current_version": v, "status": status, "updated_at": db.now()}
    if title:
        ch["title"] = title
    if task_id:
        ch["task_id"] = task_id
    db.update("articles", "id", aid, ch)
    return v


register_graph(Graph(
    name="write",
    nodes=dict(load=load, select=select, outline=outline, gap=gap, draft=draft, check=check, fix=fix,
               illustrate=illustrate, save=save),
    start="load",
    on_budget="save",
))
