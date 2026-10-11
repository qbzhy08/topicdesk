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

from .. import db, llm, writing
from .. import memory as mem
from .. import skills as skills_mod
from ..collect.sources import list_sources
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
gaps：只有某一节的目标在素材里明显找不到依据时才列（最多 2 个），否则给空数组。"""

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
4. 直接输出这一节的正文 Markdown，不要重复小标题，不要写“本节”“综上所述”。"""

CHECK_NAME = {"weak": "出处里找不到相近表述", "number": "数字对不上出处", "bad_cite": "出处编号不存在",
              "uncited": "有数字但没有出处", "analysis_num": "分析里有资料中没有的数字"}

FIX_NOTE = """上一稿核对发现这些问题，请改写本节并修正（数字以出处原文为准；找不到依据的说法删掉或改成不确定的表述）：
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
    mat = writing.select_material(st["research_task"], sk, st["extra_docs"])
    names = _names()
    st["docs"] = [{k: v for k, v in d.items() if k != "text"} | {"label": writing.source_label(d, names),
                                                                 "chars": len(d["text"])} for d in mat["docs"]]
    st["ranking_version"] = mat["ranking_version"]
    st["facts"] = writing.topic_facts(ctx.topic, sk["material"].get("facts", "all"))
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
                     (f"（已更正，原说法：{f['corrected_from'][:60]}）" if f.get("corrected_from") else "")
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
                    materials=_materials_brief(docs), facts=_facts_text(st["facts"]))}], max_tokens=1800)["data"]
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


def _gap_sources(st) -> list[dict]:
    srcs = [s for s in list_sources() if s["id"] in st["source_ids"]]
    good = [s for s in srcs if s["method"] in ("api", "websearch") or (s["config"] or {}).get("search_feed")]
    return (good or srcs)[:3]


def gap(ctx):
    """补充检索：提纲缺资料或核对找不到依据时，回到信息源检索，新资料加入素材后重新选材。"""
    st = ctx.state
    st["gap_round"] += 1
    queue, st["gap_queue"] = st.get("gap_queue") or [], []
    added = []
    for g in queue:
        log = {**g, "sources": [], "found": []}
        words = [w for w in re.split(r"\s+", g["query"]) if w]
        for s in _gap_sources(st):
            try:
                res = ctx.tool("source_search", source_id=s["id"], query=g["query"], limit=6)
            except Exception as e:  # noqa: BLE001
                log["sources"].append(f"{s['name']} 失败：{str(e)[:40]}")
                continue
            log["sources"].append(f"{s['name']} {res['count']} 条")
            for it in res["items"]:
                blob = f"{it.get('title')} {it.get('summary')}".lower()
                if not any(w.lower() in blob for w in words) and writing.overlap(g["query"], blob) < 0.5:
                    continue
                if it.get("content_kind") != "full":
                    try:
                        art = ctx.tool("page_fetch", url=it["url"])
                        it = {**it, **article_to_item(art, it["url"], base=it)}
                    except Exception:  # noqa: BLE001  抓不到全文就用摘要
                        pass
                it.setdefault("meta", {})["extract_method"] = "gap"
                r = upsert_document(it, ctx.topic, task_id=ctx.task_id)
                if r["doc_id"] not in st["extra_docs"] and r["doc_id"] not in [d["id"] for d in st["docs"]]:
                    st["extra_docs"].append(r["doc_id"])
                    added.append(r["doc_id"])
                    log["found"].append((it.get("title") or it["url"])[:50])
                if len(log["found"]) >= 3:
                    break
            if len(log["found"]) >= 3:
                break
        st["gap_log"].append(log)
    ctx.observe("补充检索：" + "；".join(f"「{g['query']}」→ {len(g['found'])} 篇（{'、'.join(g['sources'])}）"
                                     for g in st["gap_log"][-len(queue):]))
    if st.get("after_gap") == "fix":       # 核对阶段发起的补检：带着新资料去改写
        st["after_gap"] = None
        st["docs"] = [{k: v for k, v in d.items() if k != "text"} | {"label": writing.source_label(d, _names()),
                                                                     "chars": len(d["text"])}
                      for d in writing.select_material(st["research_task"], st["skill"], st["extra_docs"])["docs"]]
        return "fix"
    return "select"


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

def check(ctx):
    st = ctx.state
    docs = _docs(st)
    rep = writing.check(st["sections"], docs)
    fixes = writing.autofix_citations(st["sections"], rep)
    if fixes:
        st["fixes"] += fixes
        rep = writing.check(st["sections"], docs)
    st["check"] = rep
    s = rep["summary"]
    ctx.observe(f"逐句核对：带出处 {s['cited']} 句，出处支持 {s['supported']} 句（{(s['support_rate'] or 0) * 100:.0f}%），"
                f"数字对不上 {s['number']}，表述找不到依据 {s['weak']}，无出处的数字 {s['uncited']}，分析段 {s['analysis']} 段"
                + (f"；自动改正出处 {len(fixes)} 处" if fixes else ""))
    probs = writing.problems(rep, ("number", "bad_cite", "uncited", "analysis_num", "weak"))
    serious = [i for i in probs if i["status"] != "weak" or i.get("overlap", 1) < 0.2]
    if serious and st["fix_round"] == 0:
        st["fix_round"] = 1
        # 分析里编的数字只需改写；来源结论找不到依据才值得回到检索核验
        unresolved = [i for i in serious if not i.get("alt") and i["status"] != "analysis_num"]
        if unresolved and st["gap_round"] < 2:
            # 资料里找不到依据：回到检索核验这些说法
            qs = []
            for i in unresolved[:2]:
                core = " ".join(i.get("numbers", [])[:2] + st["terms"][:2])
                if core.strip():
                    qs.append({"query": core[:30], "why": f"核验「{i['sentence'][:40]}」", "section": i["section"]})
            if qs:
                st["gap_queue"] = qs
                st["after_gap"] = "fix"
                ctx.think(f"{len(unresolved)} 句在现有资料里找不到依据，先补充检索核验，再改写")
                return "gap"
        return "fix"
    return "save"


def fix(ctx):
    st = ctx.state
    docs = _docs(st)
    rep = writing.check(st["sections"], docs)
    by_sec = {}
    for i in writing.problems(rep, ("number", "bad_cite", "uncited", "analysis_num", "weak")):
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
    ctx.observe(f"按核对结果改写 {len(by_sec)} 节：{'、'.join(by_sec)}")
    return "check"


# ---------------------------------------------------------------- save

def save(ctx):
    st = ctx.state
    docs = _docs(st)
    if not st.get("sections") or not all("md" in x for x in st["sections"]):
        raise ValueError("预算耗尽，文章还没写完，无法保存")
    rep = st.get("check") or writing.check(st["sections"], docs)
    secs, refmap = writing.renumber(st["sections"])
    rdocs = [{**d, "n": refmap[d["n"]]} for d in docs if d["n"] in refmap]
    md = writing.assemble(st["title"], secs, rdocs, set(refmap.values()), _names(),
                          note=None if st["draft_by"] == "llm" else "未配置大模型：正文为资料原句摘编，没有改写。")
    sk = st["skill"]
    meta = {"skill": {"id": sk["id"], "name": sk["name"], "version": sk["version"], "fingerprint": sk["fingerprint"]},
            "research_task": st["research_task"], "ranking_version": st.get("ranking_version"),
            "docs": [{**{k: d[k] for k in ("n", "id", "title", "url", "source_id", "label", "why", "kind")},
                      "ref": refmap.get(d["n"])} for d in st["docs"]],
            "refmap": {str(k): v for k, v in refmap.items()},
            "facts": st["facts"], "sections": st["sections"], "check": rep, "fixes": st["fixes"],
            "gap_log": st["gap_log"], "prefs": st["prefs"], "outline_by": st["outline_by"], "draft_by": st["draft_by"]}
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
    nodes=dict(load=load, select=select, outline=outline, gap=gap, draft=draft, check=check, fix=fix, save=save),
    start="load",
    on_budget="save",
))
