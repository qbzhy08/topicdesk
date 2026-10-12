"""Lv3 评测实验：同素材的初稿与优化稿对照、步骤消融、多种 Skill 对照。

kind = "ablation"（默认）：每个检索任务（同一批素材）按同一个 Skill 写出几个变体——
    draft        初稿：列提纲后直接成稿（不补检、不核对改写、不自然化）
    checked      +核对改写：逐句核对出处，有问题的节按核对结果改写（含补检）
    optimized    优化稿：核对改写 + 自然化改写（降 AI 腔）+ 自然化后复核、改坏的节退回
    natural_only 只自然化、不核对（消融：没有核对兜底时，降 AI 腔会不会伤事实）
    no_recheck   全流程但自然化后不复核（消融：复核这一步是否必要）
  变体只差步骤开关，素材、Skill、提纲提示词都一样；为了只比较文字，变体不配图。
kind = "skills"：同一个检索任务，用几种 Skill 各写一篇（全流程含配图），比较选材和表达的差异。

节点：plan → write（分批并行，循环到写完）→ measure（确定性指标）→ judge（独立大模型事实核验 + 交换顺序的盲评）
     → detect（AIGC 检测，同一检测器测全部完整正文）→ finish。
读者盲评和外部检测器分数由人录入（eval_ratings / eval_scores），报告在读取时实时汇总。
"""
from __future__ import annotations

import random
import time
from concurrent.futures import ThreadPoolExecutor

from .. import db, evaluate as ev, llm, ranking, skills as skills_mod
from .. import aigc
from ..harness import END, Graph, create_task, register_graph, run_task
from . import write as W

OFF = {"gap": False, "fix": False, "naturalize": False, "natural_recheck": False, "illustrate": False}
VARIANTS = {
    "draft": {"name": "初稿", "desc": "列提纲后直接成稿：不补检、不核对改写、不自然化", "stages": dict(OFF)},
    "checked": {"name": "核对改写", "desc": "初稿 + 逐句核对出处、补检、按核对结果改写",
                "stages": {**OFF, "gap": True, "fix": True}},
    "optimized": {"name": "优化稿", "desc": "核对改写 + 自然化改写（降 AI 腔）+ 自然化后复核，改坏的节退回",
                  "stages": {**OFF, "gap": True, "fix": True, "naturalize": True, "natural_recheck": True}},
    "natural_only": {"name": "只自然化", "desc": "消融：不核对改写，直接自然化，也不复核",
                     "stages": {**OFF, "naturalize": True}},
    "no_recheck": {"name": "自然化不复核", "desc": "消融：全流程，但自然化后不复核、不退回",
                   "stages": {**OFF, "gap": True, "fix": True, "naturalize": True}},
}
DEFAULT_VARIANTS = ["draft", "checked", "optimized", "natural_only"]
BLIND_PAIR = ("draft", "optimized")


def _docs_with_text(meta: dict) -> list[dict]:
    from .. import writing
    rows = writing._doc_rows([d["id"] for d in meta.get("docs") or []])
    out = []
    for d in meta.get("docs") or []:
        r = rows.get(d["id"]) or {}
        out.append({**d, "text": r.get("text") or r.get("summary") or "", "published_at": r.get("published_at"),
                    "primary": bool((r.get("meta") or {}).get("primary"))})
    return out


# ---------------------------------------------------------------- plan

def plan(ctx):
    st, p = ctx.state, ctx.params
    kind = p.get("kind") or "ablation"
    tids = p.get("research_tasks") or []
    if not tids:
        raise ValueError("请至少选一个已完成的检索任务")
    runs = []
    for tid in tids:
        t = db.query_one("SELECT id, topic, status FROM tasks WHERE id=? AND graph='research'", (tid,))
        if not t or t["status"] != "done":
            raise ValueError(f"检索任务 {tid} 不存在或没有完成")
        if not ranking.latest(tid):
            raise ValueError(f"检索任务 {tid} 还没有价值排序，不能写作")
        if kind == "skills":
            for sid in p.get("skills") or []:
                skills_mod.get(sid)
                runs.append({"key": f"{tid}:{sid}", "research_task": tid, "topic": t["topic"], "variant": sid,
                             "skill": sid, "stages": {**W.DEFAULT_STAGES}, "status": "pending"})
        else:
            sid = p.get("skill") or "brief"
            skills_mod.get(sid)
            for v in p.get("variants") or DEFAULT_VARIANTS:
                if v not in VARIANTS:
                    raise ValueError(f"未知变体 {v}")
                runs.append({"key": f"{tid}:{v}", "research_task": tid, "topic": t["topic"], "variant": v,
                             "skill": sid, "stages": VARIANTS[v]["stages"], "status": "pending"})
    if kind == "ablation" and not llm.available() and not p.get("no_llm"):
        raise ValueError("对照实验需要大模型（初稿和优化稿都由大模型写）。请先在设置里配置大模型")
    st.update(kind=kind, runs=runs, started=time.time())
    ctx.observe(f"实验计划：{len(tids)} 组素材 × {len(runs) // max(1, len(tids))} 个变体 = {len(runs)} 篇")
    return "write"


# ---------------------------------------------------------------- write

def _run_one(r, params, user_id):
    wp = {"research_task": r["research_task"], "skill": r["skill"], "stages": r["stages"], "seed_from": r.get("seed_from"),
          "experiment": params["_exp"], "variant": r["variant"], "note": f"实验变体：{r['variant']}"}
    if params.get("no_llm"):
        wp["no_llm"] = True
    wid = create_task("write", r["topic"], params=wp, user_id=user_id, parent_task_id=r["research_task"],
                      budget={"max_tool_calls": 200, "max_steps": 100})
    t0 = time.time()
    status = run_task(wid)
    t = db.query_one("SELECT state, error FROM tasks WHERE id=?", (wid,))
    res = (t["state"] or {}).get("result") or {}
    return {"write_task": wid, "status": status, "error": t.get("error"), "seconds": round(time.time() - t0, 1),
            "article_id": res.get("article_id"), "version": res.get("version")}


FATAL = [(r"HTTP 402|Insufficient Balance|insufficient_quota|余额不足|欠费", "大模型账户余额不足（HTTP 402）：请到大模型服务商充值后，在报告页点“重跑失败的文章”"),
         (r"HTTP 401|invalid api key|Incorrect API key|Unauthorized|未配置 LLM", "大模型的 API Key 无效或没有配置：请到“设置”里检查后重跑"),
         (r"HTTP 403", "大模型服务拒绝访问（HTTP 403）：检查账号权限或模型名称后重跑")]


def friendly_error(err: str | None) -> tuple[str, bool]:
    """把写作失败的原始报错翻译成能看懂的原因；返回 (说明, 是否是继续跑也没用的致命错误)。"""
    import re
    for pat, msg in FATAL:
        if err and re.search(pat, err, re.I):
            return msg, True
    if err and re.search(r"timed? ?out|超时", err, re.I):
        return "大模型响应超时：可以把“同时写几篇”调小后重跑", False
    if err and re.search(r"HTTP 429|rate limit|Too Many Requests", err, re.I):
        return "请求太频繁被限流（HTTP 429）：把“同时写几篇”调成 1 后重跑", False
    return (err or "写作任务失败")[:200], False


def write(ctx):
    st, p = ctx.state, ctx.params
    todo = [r for r in st["runs"] if r["status"] == "pending"]
    if not todo:
        return "measure"
    # 先写各组素材的初稿（对照组）；其他变体等初稿写完，直接用同一份初稿往后做（省时间，也只比较差异步骤）
    drafts = {r["research_task"]: r for r in st["runs"] if r["variant"] == "draft"}
    ready = []
    for r in todo:
        d = drafts.get(r["research_task"])
        if st.get("kind") == "skills" or r["variant"] == "draft" or not d or not p.get("share_draft", True):
            ready.append(r)
        elif d["status"] == "done":
            r["seed_from"] = d.get("write_task")
            ready.append(r)
        elif d["status"] == "failed":
            ready.append(r)
    if not ready:
        ready = todo
    batch = ready[:int(p.get("parallel") or 3)]
    params = {**p, "_exp": ctx.task_id}
    with ThreadPoolExecutor(max_workers=len(batch)) as ex:
        outs = list(ex.map(lambda r: _run_one(r, params, ctx.user_id), batch))
    fatal = None
    for r, o in zip(batch, outs):
        r.update(o)
        r["status"] = "done" if o["status"] == "done" and o.get("article_id") else "failed"
        if r["status"] == "done":
            _measure_one(r)
        if r["status"] == "failed":
            r["raw_error"] = r.get("error")
            r["error"], is_fatal = friendly_error(r.get("error"))
            fatal = fatal or (r["error"] if is_fatal else None)
    if fatal:      # 余额不足、Key 无效：后面的文章写了也是白失败，直接停下，不再浪费时间
        rest = [r for r in st["runs"] if r["status"] == "pending"]
        for r in rest:
            r.update(status="failed", error=f"没有开始写：{fatal}")
        st["fatal"] = f"{fatal}（已停止，剩下 {len(rest)} 篇没有开始写）"
        ctx.observe(st["fatal"])
        return "measure"
    done = sum(1 for r in st["runs"] if r["status"] != "pending")
    ctx.observe(f"已写完 {done}/{len(st['runs'])} 篇：" + "；".join(
        f"{r['topic'][:12]}·{_vname(st, r)} {'✓' if r['status'] == 'done' else '✗ ' + str(r.get('error'))[:40]}（{r['seconds']}s）"
        for r in batch))
    return "write"


def _vname(st, r):
    return VARIANTS.get(r["variant"], {}).get("name") if st.get("kind") != "skills" else r["skill"]


# ---------------------------------------------------------------- measure

def measure(ctx):
    st = ctx.state
    for r in st["runs"]:
        _measure_one(r)
    ctx.observe(f"确定性指标算完 {sum(1 for r in st['runs'] if r.get('metrics'))} 篇")
    return "judge"


def _measure_one(r):
    """一篇写完就算它的确定性指标（报告里边写边出结果，不用等全部写完）。"""
    if r["status"] != "done" or r.get("metrics"):
        return
    v = db.query_one("SELECT markdown, meta FROM article_versions WHERE article_id=? AND version=?",
                     (r["article_id"], r["version"]))
    meta = v["meta"] or {}
    docs = _docs_with_text(meta)
    m = ev.evaluate_version(v["markdown"], meta, docs)
    r["text"] = m.pop("text")
    r["claims_pool"] = [{"sentence": i["sentence"], "cites": i["cites"], "analysis": i.get("analysis")}
                        for i in m.pop("_items") if i.get("cites")]
    r["metrics"] = m
    r["doc_ids"] = [d["id"] for d in meta.get("docs") or []]
    r["cited_doc_ids"] = [d["id"] for d in meta.get("docs") or [] if d.get("ref")]
    r["structure"] = [s.get("heading") for s in meta.get("sections") or []]
    r["logs"] = {"natural": meta.get("natural_log") or [], "fixes": len(meta.get("fixes") or []),
                 "gap": [{"query": g.get("query"), "found": len(g.get("found") or []), "skipped": g.get("skipped")}
                         for g in meta.get("gap_log") or []],
                 "skill_version": (meta.get("skill") or {}).get("version"),
                 "skill_fingerprint": (meta.get("skill") or {}).get("fingerprint"),
                 "figures": len(meta.get("figures") or []), "cover": bool(meta.get("cover")),
                 "ref_check": meta.get("ref_check"), "image_error": meta.get("image_error")}


# ---------------------------------------------------------------- judge

def judge(ctx):
    st, p = ctx.state, ctx.params
    if not W._use_llm(ctx) or p.get("judge") is False:
        st["judge_note"] = "没有大模型评审（未配置大模型或已关闭）"
        return "detect"
    done = [r for r in st["runs"] if r.get("metrics")]
    calls, keys = [], []
    for r in done:
        v = db.query_one("SELECT meta FROM article_versions WHERE article_id=? AND version=?", (r["article_id"], r["version"]))
        docs = _docs_with_text(v["meta"] or {})
        pick, inp = ev.claim_judge_input(r["claims_pool"], docs)
        r["_pick"] = pick
        if pick:
            calls.append(("llm", inp))
            keys.append(("claims", r["key"]))
    pairs = []
    if st["kind"] == "ablation":
        by = {(r["research_task"], r["variant"]): r for r in done}
        for tid in dict.fromkeys(r["research_task"] for r in done):
            x, y = by.get((tid, BLIND_PAIR[0])), by.get((tid, BLIND_PAIR[1]))
            if x and y:
                sk = skills_mod.get(x["skill"])
                for k, inp in enumerate(ev.pair_judge_inputs(x["text"], y["text"], f"目标读者（{sk['reader']}）")):
                    calls.append(("llm", inp))
                    keys.append(("pair", f"{tid}|{k}"))
                pairs.append(tid)
    t0 = time.time()
    res = ctx.tool_parallel(calls, max_workers=6) if calls else []
    out = dict(zip(keys, res))
    for r in done:
        stt, o = out.get(("claims", r["key"]), ("skip", None))
        r["claim_judge"] = ev.claim_judge_score(r.pop("_pick", []), o.get("data") if stt == "ok" and isinstance(o, dict) else None)
    st["llm_pairs"] = {}
    for tid in pairs:
        a = out.get(("pair", f"{tid}|0"))
        b = out.get(("pair", f"{tid}|1"))
        da = a[1].get("data") if a and a[0] == "ok" and isinstance(a[1], dict) else None
        dbb = b[1].get("data") if b and b[0] == "ok" and isinstance(b[1], dict) else None
        st["llm_pairs"][tid] = ev.pair_judge_merge(da, dbb)
    ctx.observe(f"大模型评审：{len(done)} 篇抽样核验事实，{len(pairs)} 组初稿/优化稿盲评（交换顺序各一次），耗时 {time.time() - t0:.0f}s")
    return "detect"


# ---------------------------------------------------------------- detect

def detect(ctx):
    st, p = ctx.state, ctx.params
    det = p.get("detector") or "auto"
    stt = aigc.status()
    if det in ("auto", "hc3") and stt["local_ready"]:
        for r in st["runs"]:
            if r.get("text"):
                try:
                    r["aigc"] = aigc.detect(r["text"])
                except Exception as e:  # noqa: BLE001  模型下载失败等：记录原因，不影响其他结果
                    st["aigc_error"] = f"本地检测器运行失败：{str(e)[:160]}"
                    break
        if not st.get("aigc_error"):
            ctx.observe(f"本地 AIGC 检测器（{stt['model']}）测了 {sum(1 for r in st['runs'] if r.get('aigc'))} 篇完整正文")
    else:
        st["aigc_note"] = ("本地检测器没有安装（需要 torch 和 transformers）：可以安装后点“重新检测”，"
                           "或用同一个外部检测器测每篇完整正文，把分数录入报告页") if det != "manual" else "使用外部检测器，分数在报告页录入"
    return "finish"


def finish(ctx):
    st = ctx.state
    # 读者盲评：每组初稿/优化稿随机分成“甲/乙”，对应关系只存在这里（评分页看不到）
    rnd = random.Random(ctx.task_id)
    keep = {p["research_task"]: p for p in st.get("blind") or []}     # 重跑后已有的分组不变（读者评分还对得上）
    st["blind"] = []
    if st["kind"] == "ablation":
        by = {(r["research_task"], r["variant"]): r for r in st["runs"] if r.get("text")}
        for tid in dict.fromkeys(r["research_task"] for r in st["runs"]):
            x, y = by.get((tid, BLIND_PAIR[0])), by.get((tid, BLIND_PAIR[1]))
            if x and y and tid in keep:
                st["blind"].append(keep[tid])
                continue
            if x and y:
                flip = rnd.random() < 0.5
                st["blind"].append({"pair_id": f"p{len(st['blind']) + len(keep) + 1}", "research_task": tid, "topic": x["topic"],
                                    "甲": (y if flip else x)["key"], "乙": (x if flip else y)["key"]})
    st["finished"] = time.time()
    ctx.observe(f"实验完成：{sum(1 for r in st['runs'] if r['status'] == 'done')} 篇；读者盲评 {len(st['blind'])} 组待评")
    return END


register_graph(Graph(
    name="experiment",
    nodes={"plan": plan, "write": write, "measure": measure, "judge": judge, "detect": detect, "finish": finish},
    start="plan",
    on_budget="finish",
))
