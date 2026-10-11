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
import time

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
{angle}检索阶段已经覆盖的子问题：{covered}
检索阶段已经专门补检过、仍然缺资料的方向（不要再列为缺口，写作时如实说明资料不足）：{tried}
gaps：只有某一节的目标在素材里完全找不到依据、而且不属于上面两类时才列（最多 2 个），否则给空数组。
多数情况下素材已经够用，应给空数组。"""

SECTION_PROMPT = """你在按 Skill「{name}」写文章《{title}》中的一节。
读者：{reader}
风格：{style}
{body}
{prefs}
{refs}
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


DEFAULT_STAGES = {"gap": True, "fix": True, "naturalize": True, "natural_recheck": True, "illustrate": True}


def _stages(ctx) -> dict:
    """写作各步骤的开关（对照/消融实验用）：gap 补检、fix 核对后改写、naturalize 自然化改写、
    natural_recheck 自然化后重新核对并回退变差的节、illustrate 配图。默认全开。"""
    return {**DEFAULT_STAGES, **(ctx.params.get("stages") or {})}


def _after_check(ctx) -> str:
    stg = _stages(ctx)
    if stg["naturalize"] and _use_llm(ctx) and not ctx.state.get("naturalized"):
        return "naturalize"
    return "illustrate" if stg["illustrate"] else "save"


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
    st["write_task"] = ctx.task_id
    st["stages_used"] = _stages(ctx)
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
    if p.get("angle"):
        st["prefs"].append(f"本次选题角度（用户指定）：{p['angle']}")
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
    st["ref_notes"] = mat.get("ref_notes") or []
    n_of = {d["id"]: d["n"] for d in st["docs"]}
    for f in st["facts"]:
        f["refs"] = sorted({n_of[x] for x in f["docs"] if x in n_of})
    ctx.observe(f"按 Skill 选材 {len(st['docs'])} 篇（{'五分钟' if sk['material']['list'] == '5' else '三十分钟'}清单优先，"
                f"偏好 {sk['material'].get('prefer') or '无'}）；记忆中的事实 {len(st['facts'])} 条：\n" +
                "\n".join(f"[{d['n']}] {d['title'][:40]}（{d['why']}）" for d in st["docs"])
                + ("\n参考资料要求：" + "；".join(st["ref_notes"]) if st["ref_notes"] else ""))
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
                    tried="、".join(g["name"] for g in st.get("research_gaps") or []) or "（无）",
                    angle=(f"用户指定的选题角度（标题和各节要点都要围绕它组织）：{ctx.params['angle']}\n"
                           if ctx.params.get("angle") else ""))}],
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
    st["title"] = (ctx.params.get("title") or "").strip()[:40] or title or f"{ctx.topic}：{sk['name']}"
    st["sections"] = base
    st["outline_by"] = "llm" if title else "rule"
    ctx.think(f"提纲《{st['title']}》：" + "；".join(f"{s['heading']}（素材 {s['sources'] or '全部'}）" for s in base))
    if gaps and st["gap_round"] == 0 and _stages(ctx)["gap"]:
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


def _refs_text(sk) -> str:
    r = sk.get("references") or {}
    if not r:
        return ""
    parts = [f"全文至少引用 {r.get('min_sources', 0)} 个不同来源", f"至少 {r.get('min_primary', 0)} 篇一手资料（论文、官方、代码仓库）"]
    if r.get("both_sides"):
        parts.append("正反两方各至少有一个独立来源，没有就如实说明")
    if r.get("background_cited"):
        parts.append("背景知识同样要带出处，不能凭常识写")
    return "参考资料要求：" + "；".join(parts) + "。本节优先用还没被其他节引用过的来源。"


def _section_materials(sec, docs) -> str:
    use = [d for d in docs if d["n"] in sec["sources"]] or docs
    focus = f"{sec['heading']} {sec['goal']} {' '.join(sec['points'])}"
    budget = max(350, min(900, 3600 // max(1, len(use))))
    return "\n\n".join(f"[{d['n']}] {d['label']}｜{d['title'][:60]}\n{writing.excerpt(d, focus, budget)}" for d in use)


def _section_input(ctx, sec, docs, fix_items=None) -> dict:
    st = ctx.state
    sk = st["skill"]
    fix = FIX_NOTE.format(items="\n".join(f"- 「{i['sentence'][:80]}」：{i.get('note') or i['status']}"
                                          + (f"（出处原文：{i['quote'][:80]}）" if i.get("quote") else "")
                                          for i in fix_items)) if fix_items else ""
    prev = f"\n上一稿：\n{sec['md']}" if fix_items else ""
    return {"json_mode": False, "max_tokens": 1600, "messages": [{"role": "user", "content": SECTION_PROMPT.format(
        name=sk["name"], title=st["title"], reader=sk["reader"], style="；".join(sk["style"]),
        body=sk["body"], prefs=_prefs_text(st), refs=_refs_text(sk), heading=sec["heading"], goal=sec["goal"],
        points="；".join(sec["points"]) or "（按目标自行组织）", words=sec["words"],
        materials=_section_materials(sec, docs),
        facts="记忆中的事实：\n" + _facts_text(st["facts"]), fix=fix + prev)}]}


def _finish_section(st, sec, content: str) -> str:
    text = (content or "").strip()
    text = re.sub(r"^#+\s*" + re.escape(sec["heading"]) + r"\s*\n", "", text)
    text, removed = writing.strip_meta(text)
    if removed:
        st["fixes"].append({"section": sec["id"], "why": "删掉混进正文的写作说明",
                            "before": "；".join(removed)[:300], "after": "（已删除）"})
    return text


def _write_sections(ctx, jobs, docs) -> dict:
    """同时写多节（各节互不依赖，提纲定了就可以并行）。jobs: [(sec, fix_items)]。返回 {节 id: 正文}。
    个别节失败（超时等）单独重试一次。"""
    st = ctx.state
    if not _use_llm(ctx):
        return {sec["id"]: _rule_section(sec, docs, st, fx) for sec, fx in jobs}
    inputs = [_section_input(ctx, sec, docs, fx) for sec, fx in jobs]
    res = ctx.tool_parallel([("llm", inp) for inp in inputs], max_workers=6)
    out = {}
    for (sec, fx), inp, (stt, r) in zip(jobs, inputs, res):
        if stt != "ok":
            r = ctx.tool("llm", **inp)          # 单独重试一次；再失败就让节点失败（可从这一步重新执行）
        out[sec["id"]] = _finish_section(st, sec, r["content"])
    return out


def _write_section(ctx, sec, docs, fix_items=None) -> str:
    return _write_sections(ctx, [(sec, fix_items)], docs)[sec["id"]]


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
    t0 = time.time()
    texts = _write_sections(ctx, [(sec, None) for sec in st["sections"]], docs)
    for sec in st["sections"]:
        sec["md"] = texts[sec["id"]]
    st.setdefault("timing", {})["逐节写作（并行）"] = round(time.time() - t0, 1)
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
        chunks = [pending[k:k + 12] for k in range(0, len(pending), 12)]
        t0 = time.time()
        res = ctx.tool_parallel([("llm", {"messages": [{"role": "system", "content": "你只输出 JSON。"},
                                                       {"role": "user", "content": writing.semantic_batch(c, docs)}],
                                          "max_tokens": 1500}) for c in chunks])
        st.setdefault("timing", {})[f"语义核对（{len(chunks)} 批并行）"] = round(time.time() - t0, 1)
        for chunk, (stt, out) in zip(chunks, res):
            d = out.get("data") if stt == "ok" and isinstance(out, dict) else None
            if not isinstance(d, dict):
                ctx.think(f"一批语义核对失败（{str(out)[:60]}），这批句子按字面核对结果处理")
                continue
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
    if serious and st["fix_round"] == 0 and _stages(ctx)["fix"]:
        st["fix_round"] = 1
        # 分析里编的数字只需改写；来源结论找不到依据才值得回到检索核验
        unresolved = [i for i in serious if not i.get("alt") and i["status"] in ("number", "weak")]
        if unresolved and st["gap_round"] < 2 and _stages(ctx)["gap"]:
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
    return _after_check(ctx)


def fix(ctx):
    st = ctx.state
    docs = _docs(st)
    rep = _check(ctx, docs)
    by_sec = {}
    for i in _serious(ctx, rep):
        by_sec.setdefault(i["section"], []).append(i)
    befores = {sec["id"]: sec["md"] for sec in st["sections"]}
    if _use_llm(ctx) and by_sec:
        for sec in st["sections"]:
            if sec["id"] in by_sec and st["extra_docs"]:    # 改写时把补检到的新资料也给它（编号续在后面）
                sec["sources"] = sorted(set(sec["sources"]) | {d["n"] for d in docs if d["id"] in st["extra_docs"]})
        t0 = time.time()
        texts = _write_sections(ctx, [(sec, by_sec[sec["id"]]) for sec in st["sections"] if sec["id"] in by_sec], docs)
        st.setdefault("timing", {})["按核对结果改写（并行）"] = round(time.time() - t0, 1)
    for sec in st["sections"]:
        items = by_sec.get(sec["id"])
        if not items:
            continue
        before = befores[sec["id"]]
        if _use_llm(ctx):
            sec["md"] = texts[sec["id"]]
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


# ---------------------------------------------------------------- naturalize（自然化改写，降低“AI 腔”）

AI_TROPES = ["首先", "其次", "最后", "综上所述", "总而言之", "值得注意的是", "值得一提的是", "不难看出", "由此可见",
             "总的来说", "不仅", "而且", "此外", "与此同时", "随着", "在……的背景下", "毋庸置疑", "至关重要", "显著",
             "进一步", "赋能", "助力", "全方位", "深度融合", "具有重要意义", "扮演着", "关键角色", "日益", "不可或缺"]

NATURAL_PROMPT = """把下面这节文章改得更像一位懂行的编辑写的，而不是 AI 生成的。读者：{reader}。风格：{style}
要求：
1. 事实、数字、日期、人名机构名一个都不能改，也不能新增；每个 [n] 出处编号都要保留，跟着它原来支持的那句话。
2. 去掉套话和空泛连接词（如：{tropes}），不用排比和对仗，不要每段都“总—分—总”。
3. 句子长短错开，能用短句就用短句；用具体的主语和动词，少用“进行”“实现”“具有……性”。
4. 以“【分析】”开头的段落仍以“【分析】”开头；表格和列表的结构保留。
5. 只输出改好的正文，不要解释。
原文：
{text}"""


def naturalize(ctx):
    """自然化改写：逐节去掉 AI 腔；改完重新核对，某节的问题变多（引错、丢出处、数字走样）就退回原稿。"""
    st = ctx.state
    docs = _docs(st)
    st["naturalized"] = True
    sk = st["skill"]
    before = {sec["id"]: sec["md"] for sec in st["sections"]}
    jobs = [sec for sec in st["sections"] if (sec.get("md") or "").strip()]
    t0 = time.time()
    res = ctx.tool_parallel([("llm", {"json_mode": False, "max_tokens": 1800, "messages": [{"role": "user", "content": NATURAL_PROMPT.format(
        reader=sk.get("reader", ""), style="；".join(sk.get("style") or []), tropes="、".join(AI_TROPES[:14]), text=sec["md"])}]})
        for sec in jobs], max_workers=6)
    log = []
    for sec, (stt, r) in zip(jobs, res):
        if stt != "ok" or not (r.get("content") or "").strip():
            log.append({"section": sec["id"], "result": "失败，保留原稿"})
            continue
        new = _finish_section(st, sec, r["content"])
        lost = set(writing.CITE.findall(sec["md"])) - set(writing.CITE.findall(new))
        if lost:
            log.append({"section": sec["id"], "result": f"丢了出处 {sorted(lost)}，保留原稿"})
            continue
        sec["md"] = new
        log.append({"section": sec["id"], "result": "已改写"})
    st.setdefault("timing", {})["自然化改写（并行）"] = round(time.time() - t0, 1)
    if _stages(ctx)["natural_recheck"]:
        old_rep = st.get("check") or writing.check([{**x, "md": before[x["id"]]} for x in st["sections"]], docs)
        rep = _check(ctx, docs)
        bad_old = {}
        for i in _serious(ctx, old_rep):
            bad_old[i["section"]] = bad_old.get(i["section"], 0) + 1
        bad_new = {}
        for i in _serious(ctx, rep):
            bad_new[i["section"]] = bad_new.get(i["section"], 0) + 1
        reverted = []
        for sec in st["sections"]:
            if bad_new.get(sec["id"], 0) > bad_old.get(sec["id"], 0):
                sec["md"] = before[sec["id"]]
                reverted.append(sec["id"])
                for x in log:
                    if x["section"] == sec["id"]:
                        x["result"] = f"改写后核对问题从 {bad_old.get(sec['id'], 0)} 处变成 {bad_new[sec['id']]} 处，退回原稿"
        st["check"] = _check(ctx, docs) if reverted else rep
    else:
        st["check"] = writing.check(st["sections"], docs, st["verdicts"])
    st["natural_log"] = log
    ctx.observe("自然化改写：" + "；".join(f"{next(x['heading'] for x in st['sections'] if x['id'] == l['section'])} {l['result']}"
                                       for l in log))
    return "illustrate" if _stages(ctx)["illustrate"] else "save"


# ---------------------------------------------------------------- illustrate（配图）

MATCH_PROMPT = """判断每张图片放在对应段落后面是否合适：图片的图注和原文上下文，与段落讲的是不是同一件事、同一个对象或同一组数据。
{items}
只输出 JSON：{{"results": [{{"i": 1, "match": true, "reason": "不超过 20 字"}}]}}"""


def _names_short(sid):
    return _names().get(sid, sid or "")


def _download_doc_images(ctx, order):
    """引用过的资料里还没下载过配图的，先下载（最多 4 篇）。所有图片并行下载，再落库。"""
    from ..collect.images import plan_downloads, store_images
    have = {r["doc_id"] for r in db.query(
        f"SELECT DISTINCT doc_id FROM images WHERE doc_id IN ({','.join('?' * len(order))})", [d["id"] for d in order])}
    todo = []
    for d in order:
        row = db.query_one("SELECT url, meta FROM documents WHERE id=?", (d["id"],))
        if d["id"] not in have and (row["meta"] or {}).get("image_candidates") and len(todo) < 4:
            todo.append((d["id"], row["url"], row["meta"]["image_candidates"]))
    if not todo:
        return 0
    jobs = [(u, page) for did, page, cands in todo for u in plan_downloads(cands, set(), 6)]
    res = ctx.tool_parallel([("image_download", {"url": u, "referer": page}) for u, page in jobs], max_workers=8)
    got = {u: (out if st == "ok" else {"ok": False, "reason": f"下载异常：{str(out)[:80]}"})
           for (u, _), (st, out) in zip(jobs, res)}
    for did, page, cands in todo:
        store_images(did, page, cands, max_images=6,
                     download_fn=lambda u, referer=None: got.get(u) or {"ok": False, "reason": "未下载"})
    return len(jobs)


def _match_messages(figs, pool, sections):
    items = []
    for k, f in enumerate(figs, 1):
        sec = next(x for x in sections if x["id"] == f["section"])
        para = ill.paragraphs(sec["md"])[f["para"]]
        im = next(c for c in pool if c["id"] == f["image_id"])
        items.append(f"{k}. 图注：{(im.get('caption') or '（无）')[:80]}；图片上下文：{(im.get('context_text') or '（无）')[:160]}\n"
                     f"   段落：{writing.CITE.sub('', para)[:220]}")
    return [{"role": "system", "content": "你只输出 JSON。"},
            {"role": "user", "content": MATCH_PROMPT.format(items="\n".join(items))}]


def _apply_match(figs, res, rejected):
    """把大模型的图文相符判断落到 figs 上，返回被否决的。"""
    bad = []
    for r in (res or {}).get("results") or []:
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
    return bad


def _place_confirmed(ctx, pool, sections, docs, need, rejected, banned=None):
    """按相似度放置，配置了大模型时再确认图文相符；被否决的换下一张（最多重试 2 轮）。"""
    banned, figs = set(banned or ()), []
    for _ in range(3):
        figs, rej = ill.place([c for c in pool if c["id"] not in banned], sections, docs, need)
        if not figs or not _use_llm(ctx):
            rejected += rej
            return figs
        try:
            res = ctx.tool("llm", messages=_match_messages(figs, pool, sections))["data"]
        except Exception as e:  # noqa: BLE001
            ctx.think(f"图文相符确认失败（{str(e)[:60]}），按相似度结果放置")
            rejected += rej
            return figs
        bad = _apply_match(figs, res, rejected)
        if not bad:
            rejected += rej
            return figs
        banned |= {f["image_id"] for f in bad}
    return [f for f in figs if f["image_id"] not in banned]


def _free_section(st, taken, prefer=None):
    ids = [x["id"] for x in st["sections"]]
    if prefer in ids and prefer not in taken:
        return prefer
    return next((i for i in ids if i not in taken), prefer if prefer in ids else ids[0])


def _last_para(st, sid):
    return max(0, len(ill.paragraphs(next(x for x in st["sections"] if x["id"] == sid)["md"])) - 1)


def illustrate(ctx):
    """配图的目标是帮读者获取信息：
    1. 原文里的信息图（图表、数据图、流程/结构图、参数表）优先，放到讲同一件事的段落后；
    2. 用文中资料里的数字主动画图（柱状/折线，每个数值都在出处里找到），有日期事件时画时间线；
    3. 还不够才用关键数字卡片、最后才用新闻照片补位（标明“装饰性”）。封面可以用照片。"""
    st = ctx.state
    sk = st["skill"]
    docs = _docs(st)
    need = int(sk["images"].get("body_min") or 2)
    used_n = set(st["check"]["used"])
    order = [d for d in docs if d["n"] in used_n] + [d for d in docs if d["n"] not in used_n]
    timing, t0 = {}, time.time()
    n_dl = _download_doc_images(ctx, order)
    timing["下载图片"] = round(time.time() - t0, 1)
    t0 = time.time()
    cands = ill.candidates([d["id"] for d in order])
    rejected = []
    info, photos = [], []
    for c in cands:
        c["quality"] = ill.quality(c["local_path"], c.get("width"), c.get("height"), c.get("url"))
        c["cls"] = ill.classify(c, c["quality"])
        if not c["quality"]["ok"]:
            continue
        (info if c["cls"]["info"] else photos).append(c)
    timing["质检分类"] = round(time.time() - t0, 1)
    t0 = time.time()
    # ---- 1. 原文信息图：先按相似度放；“图文是否相符”和“找图表数据”两次大模型调用同时发出
    figs, first_rej = (ill.place(info, st["sections"], docs, min(need, 2)) if info else ([], []))
    calls, lines = [], ""
    if _use_llm(ctx) and figs:
        calls.append(("match", ("llm", {"messages": _match_messages(figs, info, st["sections"])})))
    if _use_llm(ctx):
        lines = ill.numeric_lines(docs, st["check"]["items"])
        if lines:
            calls.append(("charts", ("llm", {"messages": [{"role": "system", "content": "你只输出 JSON。"}, {
                "role": "user", "content": ill.CHARTS_PROMPT.format(lines=lines, sections="、".join(x["id"] for x in st["sections"]))}],
                "max_tokens": 1500})))
    par = dict(zip([k for k, _ in calls], ctx.tool_parallel([c for _, c in calls]))) if calls else {}
    if "match" in par:
        stt, out = par["match"]
        bad = _apply_match(figs, out.get("data") if stt == "ok" else None, rejected)
        if bad:      # 少数情况：有图被否决，换下一张（这时再单独确认）
            keep = [f for f in figs if f not in bad]
            more = _place_confirmed(ctx, [c for c in info if c["id"] not in {f["image_id"] for f in figs}],
                                    [x for x in st["sections"] if x["id"] not in {f["section"] for f in keep}] or st["sections"],
                                    docs, min(need, 2) - len(keep), rejected)
            figs = keep + more
        else:
            rejected += first_rej
    else:
        rejected += first_rej
    for f in figs:
        f["info"] = True
        f["why"] = "信息图（" + next(c for c in cands if c["id"] == f["image_id"])["cls"]["why"] + "）；" + f["why"]
    taken = {f["section"] for f in figs}
    diagrams = []
    chart_log = []
    # ---- 2. 用资料里的数字画图（主动做，不只是补位）
    if not _use_llm(ctx):
        chart_log.append("未配置大模型：不自动找图表数据")
    specs = []
    if _use_llm(ctx):
        if not lines:
            chart_log.append("资料里带数字的句子太少，没法画图")
        else:
            stt, out = par.get("charts", ("error", "未调用"))
            if stt == "ok" and isinstance(out, dict) and isinstance(out.get("data"), dict):
                specs = (out["data"].get("charts") or [])[:2]
                if not specs:
                    chart_log.append("大模型认为资料里没有适合画柱状图/折线图的同类数字（需要 3 个以上单位相同、可比较的数值）")
            else:
                ctx.think(f"找图表数据失败（{str(out)[:60]}）")
                chart_log.append(f"找图表数据失败：{str(out)[:60]}")
    for spec in specs:
        kind = spec.get("kind")
        if kind not in ("bar", "line"):
            continue
        problem = ill.verify_chart(spec, docs)
        if problem:
            rejected.append({"image": None, "url": None, "doc": f"示意图「{str(spec.get('title'))[:16]}」", "why": f"没画：{problem}"})
            chart_log.append(f"「{str(spec.get('title'))[:16]}」没画：{problem}")
            continue
        refs = sorted({int(r) for r in spec["refs"] if str(r).isdigit()})
        items = [{"label": str(i["label"]), "value": float(i["value"])} for i in spec["items"]]
        title, unit = str(spec.get("title") or "数据对比")[:18], str(spec.get("unit") or "")
        svg = (ill.bar_svg if kind == "bar" else ill.line_svg)(title, unit, items, "数值均摘自文中引用的资料，已逐个核对")
        sid = _free_section(st, taken, spec.get("section"))
        taken.add(sid)
        tk = str(spec.get("takeaway") or "")[:40]
        chart_log.append(f"「{title}」画出{'柱状图' if kind == 'bar' else '折线图'}（{len(items)} 个数值，全部在出处中找到）")
        diagrams.append({"kind": "diagram", "diagram": kind, "src": "/media/" + ill._save_svg(svg), "section": sid,
                         "para": _last_para(st, sid), "alt": title,
                         "caption": f"{title}{'：' + tk if tk else ''}（示意图，数据来自 " + "".join(f"[{r}]" for r in refs) + "）",
                         "why": f"{'柱状图' if kind == 'bar' else '折线图'}：把资料里 {len(items)} 个同类数字放在一起比较，每个数值都在出处里找到"})
    ev = ill.timeline_events(st["check"]["items"], st["sections"])
    if len(ev) >= 3 and len(figs) + len(diagrams) < need + 1:
        refs = sorted({c for e in ev for c in e["cites"]})
        counts = {}
        for e in ev:
            counts[e["section"]] = counts.get(e["section"], 0) + 1
        sid = _free_section(st, taken, max(counts, key=counts.get))
        taken.add(sid)
        diagrams.append({"kind": "diagram", "diagram": "timeline", "src": "/media/" + ill._save_svg(
            ill.timeline_svg("时间线", ev, "根据文中引用的资料整理")), "section": sid, "para": _last_para(st, sid),
            "caption": "时间线：" + "、".join(e["date"] for e in ev[:2]) + " 等 " + str(len(ev)) + " 个节点（示意图，出处 "
                       + "".join(f"[{r}]" for r in refs) + "）",
            "alt": "时间线", "why": f"正文有 {len(ev)} 个带日期的已核对事件，排成时间线更容易看清先后"})
    # ---- 3. 还不够：关键数字卡片，再不够才用照片
    if len(figs) + len(diagrams) < need:
        nums = ill.number_items(st["check"]["items"])
        if len(nums) >= 2:
            refs = sorted({c for x in nums for c in x["cites"]})
            sid = _free_section(st, taken, nums[0]["section"])
            taken.add(sid)
            diagrams.append({"kind": "diagram", "diagram": "numbers", "src": "/media/" + ill._save_svg(
                ill.numbers_svg("关键数字", nums, "数字均摘自文中引用的资料，已逐个核对")), "section": sid,
                "para": _last_para(st, sid), "alt": "关键数字",
                "caption": "文中的关键数字（示意图，出处 " + "".join(f"[{r}]" for r in refs) + "）",
                "why": "把正文里核对通过的关键数字集中展示"})
    filler = []
    if len(figs) + len(diagrams) < need and photos:
        filler = _place_confirmed(ctx, photos, [x for x in st["sections"] if x["id"] not in taken] or st["sections"],
                                  docs, need - len(figs) - len(diagrams), rejected)
        for f in filler:
            f["decorative"] = True
            f.setdefault("warn", []).append("装饰性配图：信息图和数据图不够，用相关照片补位")
    listed = {r["image"] for r in rejected}
    for c in photos:
        if c["id"] not in listed and not any(f.get("image_id") == c["id"] for f in filler):
            rejected.append({"image": c["id"], "url": c["url"], "doc": c["doc_title"],
                             "why": f"照片类配图，对获取信息帮助不大，不放正文（{c['cls']['why']}）"})
    figs = figs + filler
    # ---- 已选中的原图：CDN 压缩版换原图
    from ..collect.images import hires_candidates

    def upgrade(fig_or_img, key="image_id"):
        iid = fig_or_img.get(key) or fig_or_img.get("id")
        row = db.query_one("SELECT * FROM images WHERE id=?", (iid,)) if iid else None
        if not row or not hires_candidates(row["url"]):
            return None
        try:
            res = ctx.tool("image_download", url=row["url"], referer=row.get("url"))
        except Exception:  # noqa: BLE001
            return None
        if res.get("ok") and res.get("upgraded_from") and (res.get("width") or 0) > (row["width"] or 0):
            db.update("images", "id", iid, {"local_path": res["local_path"], "width": res["width"], "height": res["height"],
                                            "sha256": res["sha256"], "phash": res["phash"]})
            return res
        return None
    timing["选图与作图"] = round(time.time() - t0, 1)
    t0 = time.time()
    # 已选中的旧图（检索阶段存的压缩版）并行换原图
    olds = [(f, db.query_one("SELECT * FROM images WHERE id=?", (f["image_id"],))) for f in figs]
    olds = [(f, row) for f, row in olds if row and hires_candidates(row["url"])]
    ups = ctx.tool_parallel([("image_download", {"url": row["url"], "referer": row["url"]}) for _, row in olds]) if olds else []
    upmap = {}
    for (f, row), (stt, res) in zip(olds, ups):
        if stt == "ok" and res.get("ok") and res.get("upgraded_from") and (res.get("width") or 0) > (row["width"] or 0):
            db.update("images", "id", row["id"], {"local_path": res["local_path"], "width": res["width"], "height": res["height"],
                                                  "sha256": res["sha256"], "phash": res["phash"]})
            res["upgraded_from"] = f"{row['width']}×{row['height']}"
            upmap[f["image_id"]] = res
    for f in figs:
        up = upmap.get(f["image_id"])
        if up:
            f.update(src="/media/" + up["local_path"], w=up["width"], h=up["height"])
            f["warn"] = [w for w in f.get("warn") or [] if "像素" not in w and "分辨率" not in w]
            f["why"] += f"；已从压缩版 {up['upgraded_from']} 换成原图 {up['width']}×{up['height']}"
        cap = (f["orig_caption"] or "").strip()
        f["caption"] = (cap if cap else "原文配图") + f"（图源：{_names_short(f['source_id'])}《{f['doc_title'][:24]}》）"
        f["alt"] = cap or f["doc_title"][:30]
        if not cap:
            f.setdefault("warn", []).append("原文没有图注，请确认图片内容与这段一致")
    st["figures"] = figs + diagrams
    # ---- 封面：照片可以当封面（吸引阅读），不用文字图表
    cov = ill.pick_cover(cands, {f.get("image_id") for f in figs}, st["terms"])
    if cov:
        up = upgrade(cov, "id")
        if up:
            cov = {**cov, "local_path": up["local_path"], "width": up["width"], "height": up["height"]}
        st["cover"] = {"kind": "photo", "image_id": cov["id"], "src": "/media/" + cov["local_path"], "url": cov["url"],
                       "doc_title": cov["doc_title"], "doc_id": cov["doc_id"],
                       "caption": ((cov.get("caption") or "").strip() or "封面图") + f"（图源：{_names_short(cov['source_id'])}《{cov['doc_title'][:24]}》）",
                       "alt": (cov.get("caption") or cov["doc_title"])[:30], "why": "横图、清晰、与主题最相关"}
    else:
        st["cover"] = {"kind": "diagram", "src": "/media/" + ill._save_svg(ill.cover_svg(st["title"], f"{sk['name']} · {ctx.topic}")),
                       "alt": "示意封面", "caption": "示意封面：素材中没有合适的横版原图，按标题生成",
                       "why": "没有满足封面要求（横版照片类、宽 ≥ 960、清晰）的原始图片"}
    timing["换原图与封面"] = round(time.time() - t0, 1)
    st["image_rejected"] = rejected
    st["chart_log"] = chart_log
    st["image_timing"] = timing
    n_info = sum(1 for f in figs if f.get("info"))
    ctx.observe(f"配图：候选 {len(cands)} 张（信息图 {len(info)}、照片 {len(photos)}）；正文放原文信息图 {n_info} 张、"
                f"数据图/时间线/数字卡片 {len(diagrams)} 张、照片补位 {len(filler)} 张（要求至少 {need} 张）；"
                f"封面{'用原图' if st['cover']['kind'] == 'photo' else '为示意封面'}；数据图：" + ("；".join(chart_log) or "无") +
                f"。耗时：" + "，".join(f"{k} {v}s" for k, v in timing.items()) + f"（并行下载 {n_dl} 张）")
    return "save"


# ---------------------------------------------------------------- save

def save(ctx):
    st = ctx.state
    if not st.get("sections") or not all("md" in x for x in st["sections"]):
        raise ValueError("预算耗尽，文章还没写完，无法保存")
    v, md, rep = build_and_save(st, note=ctx.params.get("note") or "初稿", task_id=ctx.task_id)
    st["result"] = {"article_id": st["article_id"], "version": v}
    s = rep["summary"]
    ctx.observe(f"保存文章第 {v} 版：{len(md)} 字，引用 {len(rep['used'])} 篇资料，出处支持率 "
                f"{(s['support_rate'] or 0) * 100:.0f}%，遗留问题 {s['problems']} 处")
    return END


def build_and_save(st, note: str, task_id: str | None, extra_meta: dict | None = None) -> tuple[int, str, dict]:
    """由写作状态（节、配图、封面、素材）拼成文章、做成稿检查并存为新版本。写作和人工审阅修改共用。"""
    docs = _docs(st)
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
            "docs": [{**{k: d.get(k) for k in ("n", "id", "title", "url", "source_id", "label", "why", "kind")},
                      "ref": refmap.get(d["n"])} for d in st["docs"]],
            "refmap": {str(k): v for k, v in refmap.items()},
            "facts": st["facts"], "sections": st["sections"], "check": rep, "fixes": st["fixes"],
            "gap_log": st["gap_log"], "prefs": st["prefs"],
            "figures": figs, "cover": cover, "image_rejected": st.get("image_rejected") or [], "image_checks": image_checks,
            "chart_log": st.get("chart_log") or [], "outline_by": st["outline_by"], "draft_by": st["draft_by"],
            "ref_check": writing.reference_check(rep, docs, sk), "ref_notes": st.get("ref_notes") or [],
            "natural_log": st.get("natural_log") or [], "stages": st.get("stages_used"),
            "write_task": st.get("write_task"), "revisions": st.get("revisions") or [], **(extra_meta or {})}
    v = save_version(st["article_id"], md, meta, note=note, title=st["title"], task_id=task_id)
    return v, md, rep


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
               naturalize=naturalize, illustrate=illustrate, save=save),
    start="load",
    on_budget="save",
))
