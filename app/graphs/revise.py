"""Lv3 人工审阅：在已写好的文章上做一处修改，重新核对，存为新版本。

一次修改 = 一个 revise 任务（有轨迹、可重跑）：
  open    读出基础版本（节、素材、配图、封面……都在版本 meta 里）
  edit    执行修改：改标题 / 局部改写（大模型按要求改一段或一节）/ 直接改文字 / 删段 / 配图删除、替换、改图注
  recheck 逐句核对；改动处如果引入了数字对不上、找不到出处等问题，大模型再改一轮
  save    存为新版本；勾选了“以后都这样”的修改意见写入长期记忆（写作反馈），下次写作自动用上

修改“选题角度”需要重列提纲、重写全文，不走这里，走写作任务（params.angle），结果仍存为同一篇文章的新版本。
"""
from __future__ import annotations

import re
import time

from .. import db, llm, skills as skills_mod, writing
from .. import illustrate as ill
from .. import memory as mem
from ..harness import END, Graph, register_graph
from . import write as W

REWRITE_PROMPT = """你在修改文章《{title}》（按 Skill「{name}」写作；读者：{reader}；风格：{style}）。
{prefs}
要修改的是「{heading}」一节里的{what}。
用户的修改要求（必须做到）：{instruction}

{target_label}：
{target}
{context}
可用素材（只能使用这些内容；方括号编号就是出处编号）：
{materials}
{fix}
规则：
1. 来自素材的事实、数字、引语，在句末保留或加上出处编号，例如“……扩展到 64K[2]。”。
2. 你自己的推断、评价单独成段，段首写“【分析】”；分析里不要出现素材里没有的数字。
3. 素材里没有的信息不要写，不要编造；用户要求补充的内容如果素材里没有，就如实写“目前公开资料未能确认……”。
4. 只输出改好后的{what}本身（Markdown），不要解释改了什么，不要重复小标题。"""

OPS = ("title", "rewrite", "text", "delete_para", "figure_remove", "figure_caption", "figure_swap", "cover_swap")


def _inv_refmap(meta) -> dict:
    return {int(v): int(k) for k, v in (meta.get("refmap") or {}).items()}


def to_material_numbers(text: str, meta: dict) -> str:
    """用户看到的是重排后的出处编号；内部（核对、素材）用素材编号。"""
    inv = _inv_refmap(meta)
    return writing.CITE.sub(lambda m: f"[{inv.get(int(m.group(1)), int(m.group(1)) + 1000)}]", text or "")


def to_display_numbers(text: str, meta: dict) -> str:
    rm = {int(k): int(v) for k, v in (meta.get("refmap") or {}).items()}
    return writing.CITE.sub(lambda m: f"[{rm.get(int(m.group(1)), m.group(1))}]", text or "")


def _sec(st, sid):
    sec = next((s for s in st["sections"] if s["id"] == sid), None)
    if not sec:
        raise ValueError(f"文章里没有这一节：{sid}")
    return sec


def _set_para(st, sec, idx: int, new_md: str | None):
    """替换（new_md 为 None 时删除）第 idx 段；同节后面的配图位置跟着挪。"""
    paras = ill.paragraphs(sec.get("md"))
    if not 0 <= idx < len(paras):
        raise ValueError(f"「{sec['heading']}」没有第 {idx + 1} 段")
    new = ill.paragraphs(new_md) if new_md else []
    shift = len(new) - 1
    paras[idx:idx + 1] = new
    sec["md"] = "\n\n".join(paras)
    for f in st.get("figures") or []:
        if f.get("section") == sec["id"]:
            if f.get("para", 0) > idx:
                f["para"] = max(0, f["para"] + shift)
            elif f.get("para", 0) == idx and not new:
                f["para"] = max(0, idx - 1)


# ---------------------------------------------------------------- open

def open_(ctx):
    st, p = ctx.state, ctx.params
    a = db.query_one("SELECT * FROM articles WHERE id=?", (p["article_id"],))
    if not a:
        raise ValueError("文章不存在")
    base = int(p.get("base_version") or a["current_version"])
    v = db.query_one("SELECT * FROM article_versions WHERE article_id=? AND version=?", (a["id"], base))
    if not v:
        raise ValueError(f"文章没有第 {base} 版")
    m = v["meta"] or {}
    if base != a["current_version"]:
        ctx.think(f"基于第 {base} 版修改（当前最新是第 {a['current_version']} 版）")
    try:
        sk = skills_mod.get(m["skill"]["id"])
        skill = {**skills_mod.public(sk), "body": sk["body"]}
    except skills_mod.SkillError:
        skill = {**m["skill"], "body": "", "reader": "", "style": [], "images": {"body_min": 0}}
    st.update(article_id=a["id"], base_version=base, title=a["title"] or "", skill=skill,
              research_task=m.get("research_task"), ranking_version=m.get("ranking_version"),
              docs=[{k: d.get(k) for k in ("n", "id", "title", "url", "source_id", "label", "why", "kind")}
                    for d in m.get("docs") or []],
              facts=m.get("facts") or [], sections=m.get("sections") or [], fixes=[], gap_log=m.get("gap_log") or [],
              prefs=m.get("prefs") or [], figures=m.get("figures") or [], cover=m.get("cover"),
              image_rejected=m.get("image_rejected") or [], chart_log=m.get("chart_log") or [],
              outline_by=m.get("outline_by"), draft_by=m.get("draft_by"), write_task=m.get("write_task"),
              revisions=m.get("revisions") or [], verdicts={}, terms=[], base_meta_refmap=m.get("refmap") or {},
              changed=[], prev_check=m.get("check"))
    if not st["sections"]:
        raise ValueError("这个版本没有保存分节内容，无法局部修改")
    # 上一版已经让大模型判断过的句子不再重复判断（只核新改的句子）
    for it in (m.get("check") or {}).get("items") or []:
        if it["status"] == "semantic":
            st["verdicts"][it["sentence"]] = {"verdict": "supported", "reason": ""}
        elif it["status"] == "weak" and (it.get("note") or "").startswith("大模型对照原文判断不支持"):
            st["verdicts"][it["sentence"]] = {"verdict": "unsupported", "reason": ""}
    if p.get("op") not in OPS:
        raise ValueError(f"不支持的修改：{p.get('op')}")
    return "edit"


# ---------------------------------------------------------------- edit

def _rewrite_input(st, sec, idx, instruction, docs, fix_items=None):
    sk = st["skill"]
    paras = ill.paragraphs(sec.get("md"))
    if idx is None:
        what, target_label, target = "这一节的正文", "这一节现在的正文", sec.get("md") or ""
        context = ""
    else:
        what, target_label, target = "这一段", "这一段现在的内容", paras[idx]
        others = [x for k, x in enumerate(paras) if k != idx]
        context = ("同一节的其他段落（不要改，只供衔接参考）：\n" + "\n\n".join(others)[:1500] + "\n") if others else ""
    fix = W.FIX_NOTE.format(items="\n".join(f"- 「{i['sentence'][:80]}」：{i.get('note') or i['status']}" for i in fix_items)) \
        + f"\n你上一次改出来的是：\n{target}" if fix_items else ""
    sec_view = {**sec, "goal": sec.get("goal") or "", "points": sec.get("points") or [], "sources": sec.get("sources") or []}
    return {"json_mode": False, "max_tokens": 1600, "messages": [{"role": "user", "content": REWRITE_PROMPT.format(
        title=st["title"], name=sk.get("name", ""), reader=sk.get("reader", ""), style="；".join(sk.get("style") or []),
        prefs=W._prefs_text(st), heading=sec["heading"], what=what, instruction=instruction,
        target_label=target_label, target=target, context=context,
        materials=W._section_materials(sec_view, docs), fix=fix)}]}


def _llm_rewrite(ctx, sec, idx, instruction, docs, fix_items=None) -> str:
    if not W._use_llm(ctx):
        raise ValueError("按要求改写需要大模型（设置页配置）。也可以选择“直接改文字”自己改。")
    r = ctx.tool("llm", **_rewrite_input(ctx.state, sec, idx, instruction, docs, fix_items))
    text, removed = writing.strip_meta(re.sub(r"^#+\s*" + re.escape(sec["heading"]) + r"\s*\n", "", (r["content"] or "").strip()))
    if not text.strip():
        raise ValueError("大模型没有给出改写结果，请换个说法再试")
    return text


def edit(ctx):
    st, p = ctx.state, ctx.params
    op = p["op"]
    docs = W._docs(st)
    before, after, summary = "", "", ""
    if op == "title":
        new = (p.get("title") or "").strip()
        if not 2 <= len(new) <= 60:
            raise ValueError("标题请写 2 到 60 个字")
        before, after, st["title"] = st["title"], new, new
        summary = f"标题改为《{new}》"
    elif op in ("rewrite", "text", "delete_para"):
        sec = _sec(st, p["section"])
        idx = p.get("para")
        idx = None if idx in (None, "", "all") else int(idx)
        paras = ill.paragraphs(sec.get("md"))
        before = sec["md"] if idx is None else (paras[idx] if 0 <= idx < len(paras) else "")
        if op == "delete_para":
            if idx is None:
                raise ValueError("请指定要删除的段落")
            _set_para(st, sec, idx, None)
            summary = f"删除「{sec['heading']}」第 {idx + 1} 段"
        else:
            if op == "rewrite":
                ins = (p.get("instruction") or "").strip()
                if len(ins) < 2:
                    raise ValueError("请写清楚想怎么改，例如：更口语一点、补上和去年的对比")
                new = _llm_rewrite(ctx, sec, idx, ins, docs)
                summary = f"按要求改写「{sec['heading']}」" + (f"第 {idx + 1} 段" if idx is not None else "整节") + f"：{ins[:40]}"
            else:
                new = to_material_numbers((p.get("text") or "").strip(), {"refmap": st["base_meta_refmap"]})
                if not new:
                    raise ValueError("内容为空；要删掉这段请用“删除这段”")
                summary = f"直接修改「{sec['heading']}」" + (f"第 {idx + 1} 段" if idx is not None else "整节")
            if idx is None:
                sec["md"] = new
            else:
                _set_para(st, sec, idx, new)
        after = "" if op == "delete_para" else new
        st["changed"].append({"section": sec["id"], "para": idx, "text": after})
    elif op in ("figure_remove", "figure_caption", "figure_swap"):
        k = int(p.get("figure", -1))
        figs = st["figures"]
        if not 0 <= k < len(figs):
            raise ValueError("没有这张配图")
        f = figs[k]
        before = f.get("caption") or ""
        if op == "figure_remove":
            figs.pop(k)
            summary = f"删除配图：{before[:30]}"
        elif op == "figure_caption":
            cap = (p.get("caption") or "").strip()
            if len(cap) < 2:
                raise ValueError("图注太短")
            f["caption"] = to_material_numbers(cap, {"refmap": st["base_meta_refmap"]})
            f["alt"] = re.sub(r"[（(].*$", "", cap)[:30] or f.get("alt")
            f.setdefault("warn", [])
            f["warn"] = [w for w in f["warn"] if "图注" not in w]
            after, summary = cap, f"修改图注：{cap[:30]}"
        else:
            nf = _fig_from_image(int(p["image_id"]), f.get("section"), f.get("para", 0))
            nf["why"] = "用户在审阅时换上的图"
            figs[k] = nf
            after, summary = nf["caption"], f"替换配图为：{nf['caption'][:30]}"
    elif op == "cover_swap":
        nf = _fig_from_image(int(p["image_id"]), None, 0)
        before = (st.get("cover") or {}).get("caption", "")
        nf["why"] = "用户在审阅时换上的封面"
        st["cover"] = nf
        after, summary = nf["caption"], f"更换封面：{nf['caption'][:30]}"
    st["edit"] = {"op": op, "before": before, "after": after, "summary": summary,
                  "instruction": (p.get("instruction") or "").strip()}
    ctx.observe(summary)
    return "recheck"


def _fig_from_image(image_id: int, section, para) -> dict:
    im = db.query_one("SELECT i.*, d.title AS doc_title, d.source_id, d.url AS doc_url FROM images i "
                      "JOIN documents d ON d.id=i.doc_id WHERE i.id=?", (image_id,))
    if not im or not im.get("local_path"):
        raise ValueError("候选图片不存在或还没下载")
    q = ill.quality(im["local_path"], im.get("width"), im.get("height"), im.get("url"))
    cls = ill.classify(im, q)
    cap = (im.get("caption") or "").strip()
    return {"kind": "photo", "image_id": im["id"], "src": "/media/" + im["local_path"], "url": im["url"],
            "section": section, "para": para, "doc_id": im["doc_id"], "doc_title": im["doc_title"],
            "source_id": im["source_id"], "doc_url": im["doc_url"], "orig_caption": im.get("caption"),
            "license": im.get("license"), "w": q.get("w"), "h": q.get("h"), "sharpness": q.get("sharpness"),
            "warn": (q.get("warn") or []) + ([] if cap else ["原文没有图注，请确认图片内容与这段一致"]),
            "info": bool(cls.get("info")),
            "caption": (cap or "原文配图") + f"（图源：{W._names_short(im['source_id'])}《{im['doc_title'][:24]}》）",
            "alt": cap[:30] or im["doc_title"][:30]}


def image_candidates(meta: dict) -> list[dict]:
    """可以换上的图：文章素材里质检合格、还没用上的原图。信息图排前面。"""
    used = {f.get("image_id") for f in (meta.get("figures") or []) + [meta.get("cover") or {}]}
    out = []
    for c in ill.candidates([d["id"] for d in meta.get("docs") or []]):
        if c["id"] in used:
            continue
        q = ill.quality(c["local_path"], c.get("width"), c.get("height"), c.get("url"))
        if not q["ok"]:
            continue
        cls = ill.classify(c, q)
        out.append({"image_id": c["id"], "src": "/media/" + c["local_path"], "caption": c.get("caption") or "",
                    "doc_title": c["doc_title"], "w": q["w"], "h": q["h"], "info": bool(cls.get("info")),
                    "why": cls.get("why")})
    out.sort(key=lambda x: (not x["info"], -(x["w"] or 0)))
    return out[:24]


# ---------------------------------------------------------------- recheck

def recheck(ctx):
    st, p = ctx.state, ctx.params
    docs = W._docs(st)
    rep = W._check(ctx, docs)
    fixes = writing.autofix_citations(st["sections"], rep)
    if fixes:
        st["fixes"] += fixes
        rep = W._check(ctx, docs)
    # 改动处引入了问题：按要求改写的，让大模型按核对结果再改一轮
    ch = st["changed"][-1] if st["changed"] else None
    if ch and p["op"] == "rewrite" and W._use_llm(ctx):
        sec = _sec(st, ch["section"])
        new_units = {u for u, _ in writing._units(ch["text"])}
        bad = [i for i in W._serious(ctx, rep) if i["section"] == sec["id"] and i["sentence"] in new_units]
        if bad:
            ctx.think(f"改写后有 {len(bad)} 句没通过核对，按核对结果再改一轮")
            idx = ch["para"]
            new = _llm_rewrite(ctx, sec, idx, st["edit"]["instruction"], docs, fix_items=bad)
            if idx is None:
                sec["md"] = new
            else:
                n_old = len(ill.paragraphs(ch["text"]))
                paras = ill.paragraphs(sec["md"])
                paras[idx:idx + n_old] = ill.paragraphs(new)
                sec["md"] = "\n\n".join(paras)
            ch["text"] = new
            st["edit"]["after"] = new
            st["fixes"].append({"section": sec["id"], "rewrite": True, "items": [
                {"status": i["status"], "sentence": i["sentence"]} for i in bad], "why": "审阅改写后按核对结果再改"})
            rep = W._check(ctx, docs)
    st["check"] = rep
    old = (st.get("prev_check") or {}).get("summary") or {}
    s = rep["summary"]
    st["edit"]["check_delta"] = {"problems": [old.get("problems"), s["problems"]],
                                 "support_rate": [old.get("support_rate"), s["support_rate"]]}
    ctx.observe(f"重新核对：遗留问题 {old.get('problems', '—')} → {s['problems']}，出处支持率 "
                f"{_pct(old.get('support_rate'))} → {_pct(s['support_rate'])}")
    return "save"


def _pct(x):
    return "—" if x is None else f"{x * 100:.0f}%"


# ---------------------------------------------------------------- save

def save(ctx):
    st, p = ctx.state, ctx.params
    e = st["edit"]
    fb_id = None
    if p.get("remember"):
        text = (p.get("feedback") or e.get("instruction") or "").strip()
        if len(text) >= 4:
            scope = f"skill:{st['skill']['id']}" if p.get("remember_scope") == "skill" else "all"
            dup = db.query_one("SELECT id FROM memories WHERE scope='user' AND kind='feedback' AND content=? "
                               "AND status IN ('active','stale')", (text,))
            fb_id = dup["id"] if dup else mem.add(
                "user", "feedback", text, user_id=ctx.user_id, applies_to=scope,
                source={"by": "user", "confirmed": True, "article": st["article_id"], "base_version": st["base_version"]},
                meta={"type": "feedback", "from": "审阅修改"}, actor="user", task_id=ctx.task_id)
            ctx.observe(f"修改意见写入长期记忆（{'只用于 Skill「' + st['skill']['name'] + '」' if scope != 'all' else '所有写作'}）：{text}")
    st["revisions"] = (st.get("revisions") or []) + [{
        "op": p["op"], "summary": e["summary"], "instruction": e.get("instruction"), "base_version": st["base_version"],
        "task_id": ctx.task_id, "at": time.time(), "feedback_memory": fb_id, "check_delta": e.get("check_delta")}]
    v, md, rep = W.build_and_save(st, note=e["summary"][:60], task_id=None,
                                  extra_meta={"edit": {k: e.get(k) for k in ("op", "summary", "instruction", "check_delta")}})
    st["result"] = {"article_id": st["article_id"], "version": v, "feedback_memory": fb_id}
    ctx.observe(f"存为第 {v} 版（基于第 {st['base_version']} 版）")
    return END


register_graph(Graph(
    name="revise",
    nodes={"open": open_, "edit": edit, "recheck": recheck, "save": save},
    start="open",
    on_budget="save",
))
