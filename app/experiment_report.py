"""实验报告：从实验任务的状态 + 读者盲评 + 外部检测器分数实时汇总（新录入的评分立刻体现在报告里）。"""
from __future__ import annotations

from . import db, evaluate as ev
from .graphs.experiment import BLIND_PAIR, VARIANTS

DIMS = {"accuracy": "准确可信", "information": "信息量", "natural": "表达自然", "easy": "好理解", "overall": "整体更愿意读"}


def _row(r: dict, scores: dict) -> dict:
    m = r.get("metrics") or {}
    f, d, n, c, g = (m.get(k) or {} for k in ("fidelity", "density", "naturalness", "comprehension", "gain"))
    cj = r.get("claim_judge") or {}
    det = {}
    if r.get("aigc"):
        det[_det_name(r["aigc"]["detector"])] = r["aigc"]["ai_prob"]
    for s in scores.get(r["key"], []):
        det[_det_name(s["detector"])] = s["score"]
    return {"key": r["key"], "topic": r["topic"], "research_task": r["research_task"], "variant": r["variant"],
            "name": VARIANTS.get(r["variant"], {}).get("name", r["variant"]), "article_id": r.get("article_id"),
            "version": r.get("version"), "status": r["status"], "error": r.get("error"), "seconds": r.get("seconds"),
            "chars": d.get("chars"), "support_rate": f.get("support_rate"), "number_errors": f.get("number_errors"),
            "number_error_rate": f.get("number_error_rate"), "problems": f.get("problems"),
            "judge_fidelity": cj.get("fidelity"), "judge_bad": cj.get("bad") or [], "judge_n": cj.get("judged"),
            "facts_per_1k": d.get("facts_per_1k"), "caveats_per_1k": d.get("caveats_per_1k"), "sources_cited": d.get("sources_cited"), "redundancy": d.get("redundancy"),
            "filler_ratio": d.get("filler_ratio"), "tropes_per_1k": n.get("tropes_per_1k"), "tropes": n.get("tropes"),
            "sentence_len_cv": n.get("sentence_len_cv"), "connective_start": n.get("connective_start"),
            "dash_per_1k": n.get("dash_per_1k"), "triplets_per_1k": n.get("triplets_per_1k"),
            "avg_sentence_len": c.get("avg_sentence_len"), "long_sentence_ratio": c.get("long_sentence_ratio"),
            "unexplained_terms": c.get("unexplained_terms"), "read_minutes": c.get("read_minutes"),
            "aigc": det, "gain": g, "logs": r.get("logs") or {}, "structure": r.get("structure"),
            "doc_ids": r.get("doc_ids") or [], "cited_doc_ids": r.get("cited_doc_ids") or [],
            "skill_name": _skill_name(r.get("skill")), "excerpt": _excerpt(r.get("text") or "")}


def _det_name(d: str) -> str:
    """同一个本地检测器以前记成“hc3:模型名”，现在叫“本地 HC3 检测器（…）”：合并成一行。"""
    if d.startswith("hc3:"):
        return f"本地 HC3 检测器（{d.split('/')[-1]}）"
    return d


def _skill_name(sid):
    from . import skills
    try:
        return skills.get(sid)["name"]
    except Exception:  # noqa: BLE001
        return sid


def _excerpt(text: str) -> str:
    """正文开头（跳过文章标题和第一个小标题），看不同写法“怎么开头”。"""
    lines = [x for x in text.split("\n") if x.strip()]
    body = [x for x in lines[2:] if len(x) > 20]
    return (body[0] if body else "")[:160]


METRIC_COLS = [  # (键, 名称, 越高好还是越低好, 怎么算的)
    ("chars", "字数", None, "完整正文的字数（去掉配图、图注、阅读说明和参考资料列表，出处编号不计）"),
    ("support_rate", "出处支持率", "up", "带出处的句子里，出处原文能支持的比例（字面相近，或大模型对照原文判断意思一致）"),
    ("number_errors", "数字错误", "down", "带出处的句子里，在出处原文中找不到的数字个数（每篇平均）"),
    ("problems", "待确认问题", "down", "核对没通过的句子数：数字对不上、出处里找不到依据、出处编号不存在、有数字没出处、分析里编了数字"),
    ("judge_fidelity", "独立核验保真度", "up", "另一个大模型抽样最多 16 句，只看出处原文判断是否支持（支持=1，部分支持=0.5）"),
    ("facts_per_1k", "每千字可核对事实", "up", "每千字里出处能支持的句子数：信息密度"),
    ("sources_cited", "引用来源数", "up", "正文引用了几个不同的来源（网站/机构）"),
    ("caveats_per_1k", "限制与争议/千字", "up", "每千字里写了限制条件或争议的句子数（可能、尚未、仅、据称、有争议、样本……）：改写不能把这些“顺”掉"),
    ("redundancy", "重复率", "down", "和前文某句字面重合超过 60% 的句子占比"),
    ("filler_ratio", "空话率", "down", "既没有出处、也没有数字、也不是【分析】的句子占比（不一定是坏句子，但信息少）"),
    ("tropes_per_1k", "套话/千字", "down", "每千字出现的 AI 常用套话次数（首先、值得注意的是、这意味着、从长远来看……）"),
    ("sentence_len_cv", "句长变化", "up", "句子长度的变异系数：越接近 0 句子越整齐划一（像模板），人写的文章长短句交错"),
    ("connective_start", "连接词起句", "down", "以“此外、然而、因此、总之……”开头的句子占比"),
    ("dash_per_1k", "破折号/千字", "down", "每千字破折号个数（大模型中文写作常见的习惯）"),
    ("triplets_per_1k", "三连排比/千字", "down", "每千字“A、B、C 等”式三连列举的次数"),
    ("avg_sentence_len", "平均句长", "down", "平均每句字数：越长越难读"),
    ("long_sentence_ratio", "长句比例", "down", "超过 50 字的句子占比"),
    ("unexplained_terms", "未解释术语", "down", "出现的英文术语/缩写里，没有用括号解释的个数"),
    ("read_minutes", "阅读分钟", None, "按每分钟 400 字估算的阅读时间"),
]


def _avg_by_variant(rows: list[dict], variants: list[str], detectors: list[str]) -> dict:
    out = {}
    for v in variants:
        rs = [r for r in rows if r["variant"] == v and r["status"] == "done"]
        out[v] = {k: ev.mean([r.get(k) for r in rs]) for k, *_ in METRIC_COLS}
        out[v]["aigc"] = {d: ev.mean([r["aigc"].get(d) for r in rs]) for d in detectors}
        out[v]["n"] = len(rs)
    return out


def _delta(a, b):
    return None if a is None or b is None else round(b - a, 3)


def _fmt(x, pct=False):
    if x is None:
        return "—"
    return f"{x * 100:.0f}%" if pct else (f"{round(x, 2):g}" if isinstance(x, (int, float)) else str(x))


def _style_verdict(a, b, detectors) -> dict:
    """降 AI 腔这一步：文风有没有变自然（套话、长句、句长变化、检测器），事实有没有变差（数字错误、待确认问题）。
    文风变好且事实不变差 = 有效；文风变好但事实一项变差 = 部分有效；文风没变好 = 没看到效果。"""
    lt = lambda k: (b.get(k) or 0) < (a.get(k) or 0)          # noqa: E731
    gt = lambda k: (b.get(k) or 0) > (a.get(k) or 0)          # noqa: E731
    style = [n for k, n in (("tropes_per_1k", "套话少了"), ("long_sentence_ratio", "长句少了"), ("avg_sentence_len", "句子短了"),
                            ("dash_per_1k", "破折号少了"), ("triplets_per_1k", "排比少了")) if lt(k)]
    style += ["句长更错落"] if gt("sentence_len_cv") else []
    style += [f"{d} 分数降了" for d in detectors if (b["aigc"].get(d) is not None and a["aigc"].get(d) is not None
                                                     and b["aigc"][d] < a["aigc"][d])]
    worse = [n for k, n in (("number_errors", "数字错误多了"), ("problems", "待确认问题多了")) if gt(k)]
    worse += ["出处支持率降了"] if lt("support_rate") else []
    better_facts = [n for k, n in (("number_errors", "数字错误少了"), ("problems", "待确认问题少了")) if lt(k)]
    if len(style) >= 2 and not worse:
        v = "effective"
    elif len(style) >= 2:
        v = "mixed"
    else:
        v = "none"
    note = ("文风：" + ("、".join(style) or "没有明显变化") + "；事实：" +
            ("、".join(worse + better_facts) or "没有变化"))
    return {"effective": v == "effective", "verdict": v, "note": note}


def _conclusions(avg: dict, rows: list[dict], detectors: list[str], llm_pairs: dict, human: dict) -> dict:
    """消融结论：每一步的作用（有数据才下结论），以及何时失效（来自运行日志的具体案例）。"""
    effects, fails = [], []

    def has(*vs):
        return all(v in avg and avg[v]["n"] for v in vs)
    if has("draft", "checked"):
        a, b = avg["draft"], avg["checked"]
        effects.append({"step": "逐句核对 + 按核对结果改写", "compare": "核对改写 vs 初稿",
                        "text": f"出处支持率 {_fmt(a['support_rate'], True)} → {_fmt(b['support_rate'], True)}；"
                                f"数字错误 {_fmt(a['number_errors'])} → {_fmt(b['number_errors'])} 处/篇；"
                                f"待确认问题 {_fmt(a['problems'])} → {_fmt(b['problems'])}；"
                                f"独立核验保真度 {_fmt(a['judge_fidelity'], True)} → {_fmt(b['judge_fidelity'], True)}",
                        "effective": (b["problems"] or 0) < (a["problems"] or 0) or (b["support_rate"] or 0) > (a["support_rate"] or 0)}
                       | {"verdict": "effective" if ((b["problems"] or 0) < (a["problems"] or 0) or (b["support_rate"] or 0) > (a["support_rate"] or 0)) else "none"})
    if has("checked", "optimized"):
        a, b = avg["checked"], avg["optimized"]
        det = "；".join(f"{d} AI 概率 {_fmt(a['aigc'].get(d), True)} → {_fmt(b['aigc'].get(d), True)}" for d in detectors) or "（还没有检测器分数）"
        effects.append({"step": "自然化改写（降 AI 腔）+ 复核", "compare": "优化稿 vs 核对改写",
                        "text": f"套话 {_fmt(a['tropes_per_1k'])} → {_fmt(b['tropes_per_1k'])} 个/千字；句长变化 "
                                f"{_fmt(a['sentence_len_cv'])} → {_fmt(b['sentence_len_cv'])}；平均句长 {_fmt(a['avg_sentence_len'])} → "
                                f"{_fmt(b['avg_sentence_len'])} 字；{det}；同时出处支持率 {_fmt(a['support_rate'], True)} → "
                                f"{_fmt(b['support_rate'], True)}、数字错误 {_fmt(a['number_errors'])} → {_fmt(b['number_errors'])}",
                        **_style_verdict(a, b, detectors)})
    for abl, label in (("natural_only", "只自然化、不核对"), ("no_recheck", "自然化后不复核")):
        if has(abl, "optimized"):
            a, b = avg[abl], avg["optimized"]
            effects.append({"step": "核对/复核兜底" if abl == "natural_only" else "自然化后的复核与退回",
                            "compare": f"优化稿 vs {label}",
                            "text": f"{label}：数字错误 {_fmt(a['number_errors'])}、待确认问题 {_fmt(a['problems'])}、"
                                    f"独立核验保真度 {_fmt(a['judge_fidelity'], True)}；优化稿：{_fmt(b['number_errors'])}、"
                                    f"{_fmt(b['problems'])}、{_fmt(b['judge_fidelity'], True)}",
                            "effective": (b["problems"] or 0) < (a["problems"] or 0)
                            or (b["number_errors"] or 0) < (a["number_errors"] or 0),
                            "verdict": "effective" if ((b["problems"] or 0) < (a["problems"] or 0)
                                                       or (b["number_errors"] or 0) < (a["number_errors"] or 0)) else "none"})
    # 何时失效：具体案例
    for r in rows:
        lg = r["logs"]
        for x in lg.get("natural") or []:
            if "退回" in x.get("result", "") or "保留原稿" in x.get("result", ""):
                fails.append({"topic": r["topic"], "variant": r["name"], "where": "自然化改写",
                              "what": f"节 {x['section']}：{x['result']}"})
        for g in lg.get("gap") or []:
            if not g.get("skipped") and not g.get("found"):
                fails.append({"topic": r["topic"], "variant": r["name"], "where": "补充检索",
                              "what": f"「{g['query']}」没有找到新资料：缺口只能如实写“资料不足”"})
        if r["variant"] == "optimized" and (r.get("problems") or 0) > 0:
            fails.append({"topic": r["topic"], "variant": r["name"], "where": "核对改写",
                          "what": f"改写一轮后仍有 {r['problems']} 处待确认（通常是素材本身没有依据，只能删掉或人工确认）"})
        for b in r.get("judge_bad") or []:
            if r["variant"] == "optimized":
                fails.append({"topic": r["topic"], "variant": r["name"], "where": "独立核验",
                              "what": f"「{b['sentence'][:50]}」被判为{'与出处矛盾' if b['verdict'] == 'contradicted' else '出处未提到'}：{b.get('why') or ''}"})
    by_topic = {}
    for r in rows:
        by_topic.setdefault(r["research_task"], {})[r["variant"]] = r
    for tid, vs in by_topic.items():
        a, b = vs.get("checked"), vs.get("optimized")
        for d in detectors:
            if a and b and a["aigc"].get(d) is not None and b["aigc"].get(d) is not None and b["aigc"][d] >= a["aigc"][d]:
                fails.append({"topic": b["topic"], "variant": b["name"], "where": "降 AI 率",
                              "what": f"{d}：自然化后 AI 概率没有下降（{_fmt(a['aigc'][d], True)} → {_fmt(b['aigc'][d], True)}）"})
    return {"effects": effects, "failures": fails[:40]}


def _human(exp_id: str, st: dict) -> dict:
    rows = db.query("SELECT pair_id, rater, data, created_at FROM eval_ratings WHERE experiment_id=? ORDER BY id", (exp_id,))
    pair_of = {p["pair_id"]: p for p in st.get("blind") or []}
    variant_of = {r["key"]: r["variant"] for r in st.get("runs") or []}
    tally = {d: {"optimized": 0, "draft": 0, "tie": 0} for d in DIMS}
    order = list(VARIANTS)
    comps = {}

    def comp_of(p):
        vs = sorted({variant_of.get(p["甲"]), variant_of.get(p["乙"])} - {None},
                    key=lambda v: order.index(v) if v in order else 99)
        if len(vs) != 2:
            return None
        k = "|".join(vs)
        if k not in comps:
            comps[k] = {"a": vs[0], "b": vs[1], "names": [VARIANTS.get(v, {}).get("name", v) for v in vs],
                        "pairs": 0, "ratings": 0, "tally": {d: {vs[0]: 0, vs[1]: 0, "tie": 0} for d in DIMS},
                        "ai_guess": {vs[0]: 0, vs[1]: 0, "unsure": 0}}
        return comps[k]
    for p in st.get("blind") or []:
        c = comp_of(p)
        if c:
            c["pairs"] += 1
    guess = {"right": 0, "wrong": 0, "unsure": 0}
    raters, per_rater, comments = set(), {}, []
    for r in rows:
        p = pair_of.get(r["pair_id"])
        if not p:
            continue
        raters.add(r["rater"])
        per_rater.setdefault(r["rater"], set()).add(r["pair_id"])
        if (r["data"] or {}).get("comment"):
            comments.append({"rater": r["rater"], "topic": p["topic"], "text": r["data"]["comment"]})
        data = r["data"] or {}
        c = comp_of(p)
        if c:
            c["ratings"] += 1
        for d in DIMS:
            pick = data.get(d)
            if pick in ("甲", "乙"):
                v = variant_of.get(p[pick], "tie")
                if v in tally[d]:
                    tally[d][v] += 1
                if c:
                    c["tally"][d][v] += 1
            elif pick:
                tally[d]["tie"] += 1
                if c:
                    c["tally"][d]["tie"] += 1
        if c:
            g = data.get("ai_guess")
            c["ai_guess"][variant_of.get(p[g]) if g in ("甲", "乙") else "unsure"] += 1 if g else 0
        g = data.get("ai_guess")                      # 读者认为“更像 AI 写的”是哪篇
        if g in ("甲", "乙"):
            guess["right" if variant_of.get(p[g]) == BLIND_PAIR[0] else "wrong"] += 1
        elif g:
            guess["unsure"] += 1
    return {"ratings": len(rows), "raters": len(raters), "tally": tally, "ai_guess": guess, "comments": comments,
            "comparisons": list(comps.values()),
            "per_rater": [{"rater": k, "done": len(v), "total": len(pair_of)} for k, v in per_rater.items()],
            "note": "ai_guess：读者认为更像 AI 写的是初稿记为“right”（优化稿更不像 AI），认为是优化稿记为“wrong”"}


def _skills_compare(rows: list[dict]) -> dict:
    """同一组资料、不同 Skill：选材重合度（Jaccard）、结构、引用、配图和各项指标。"""
    out = []
    for tid in dict.fromkeys(r["research_task"] for r in rows):
        rs = [r for r in rows if r["research_task"] == tid and r["status"] == "done"]
        mat = []
        for a in rs:
            line = []
            for b in rs:
                sa, sb = set(a["cited_doc_ids"]), set(b["cited_doc_ids"])
                line.append(round(len(sa & sb) / len(sa | sb), 2) if sa | sb else None)
            mat.append(line)
        out.append({"research_task": tid, "topic": rs[0]["topic"] if rs else "", "skills": [r["variant"] for r in rs],
                    "jaccard_cited": mat, "rows": rs})
    return {"groups": out}


def build(exp: dict) -> dict:
    st = exp.get("state") or {}
    scores = {}
    for s in db.query("SELECT run_key, detector, score, note FROM eval_scores WHERE experiment_id=? ORDER BY id", (exp["id"],)):
        lst = scores.setdefault(s["run_key"], [])
        lst[:] = [x for x in lst if x["detector"] != s["detector"]] + [s]      # 同一检测器以最后一次录入为准
    rows = [_row(r, scores) for r in st.get("runs") or []]
    detectors = sorted({d for r in rows for d in r["aigc"]})
    variants = list(dict.fromkeys(r["variant"] for r in rows))
    rep = {"id": exp["id"], "status": exp["status"], "kind": st.get("kind") or exp["params"].get("kind") or "ablation",
           "params": exp["params"], "rows": rows, "variants": [{"id": v, **VARIANTS.get(v, {"name": v, "desc": ""})} for v in variants],
           "detectors": detectors, "metric_cols": [{"key": k, "name": n, "better": b, "desc": d} for k, n, b, d in METRIC_COLS],
           "aigc_note": st.get("aigc_note"), "aigc_error": st.get("aigc_error"), "judge_note": st.get("judge_note"),
           "blind_pairs": len(st.get("blind") or []), "llm_pairs": st.get("llm_pairs") or {}, "fatal": st.get("fatal")}
    rep["avg"] = _avg_by_variant(rows, variants, detectors)
    if rep["kind"] == "skills":
        rep["skills"] = _skills_compare(rows)
    else:
        rep["human"] = _human(exp["id"], st)
        rep["conclusions"] = _conclusions(rep["avg"], rows, detectors, rep["llm_pairs"], rep["human"])
        opt = [r for r in rows if r["variant"] == "optimized" and r["gain"]]
        rep["gain"] = {k: ev.mean([r["gain"].get(k) for r in opt]) for k in
                       ("source_docs", "source_chars", "article_chars", "compression", "minutes_sources", "minutes_article",
                        "minutes_saved", "docs_used", "docs_dropped", "cross_source_paras", "analysis_paras",
                        "disputes_flagged", "disputes_known")} if opt else {}
        rep["gain_rows"] = [{"topic": r["topic"], **r["gain"]} for r in opt]
    return rep
