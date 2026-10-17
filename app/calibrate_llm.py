"""规则 vs 大模型 vs 混合：在同一批人工标注上比较三种做法。

  规则：calibrate.py 里的阈值规则（现行参数）；
  大模型：把两篇资料 / 一处网页变化 / 两条说法原样交给大模型判断；
  混合：规则判断明确的直接用规则，只有落在阈值附近（规则拿不准）的才问大模型。

同时量两件“可靠性”的事：
  一致性：同一条样本、同样的设置，问大模型两次，答案一样的比例；
  成本：每种做法需要调用大模型多少次。
大模型的判断和理由存进 llm_judgments 表，已经判过的不重复调用（标注样本不变，结果可复查）。
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from . import calibrate as cal
from . import db, llm

PROMPTS = {
    "reprint": """判断两篇中文资料的关系，只能选一个：
reprint = 原样转载（同一篇稿子，最多改了标题、删了几句）；
rewrite = 洗稿/改写（同一篇稿子逐句换说法、调语序，信息和结构基本一样）；
independent = 独立报道（各自采写，即使说的是同一件事、引用了同样的数字）。
只输出 JSON：{{"label": "reprint|rewrite|independent", "reason": "一句话理由（30 字内）"}}

资料 A：{a}

资料 B：{b}""",
    "event": """判断两篇报道说的是不是同一件新闻事件（同一个发布、同一次事故、同一份数据通报……）。
同一系列里的不同事件（比如同一场比赛的前后两轮、同一公司的两次发布）、后续进展报道，都算 different。
例子：“国庆档票房11.65亿”和“电影局：国产片占比78.7%”引用同一份通报 → same；
“苹果发布 HomeView”和“苹果下月将发布新 iPad” → different（两次发布）。
只输出 JSON：{{"label": "same|different", "reason": "一句话理由（30 字内）"}}

报道 A：{a}

报道 B：{b}""",
    "update": """同一个网页前后两次抓取，下面是变化的部分。判断这次变化是不是实质更新：
significant = 改了事实、数字、结论，增删了实质内容（包括更正）；
minor = 阅读数、时间、广告、导航、格式这类和内容无关的变化。
只输出 JSON：{{"label": "significant|minor", "reason": "一句话理由（30 字内）"}}

网页：{a}
{b}""",
    "dispute": """下面两条说法被系统认为互相矛盾。请判断：
1) 它们是真的矛盾，还是其实说的不是一回事（比较基准、时间、口径、对象不同）；
2) 如果真的矛盾，你认为哪条是对的（只能根据下面给出的信息判断，不要用你自己记得的知识；判断不了就选 unsure）。
例子：“入境游客比2024年增长19%”和“比2025年增长6.5%” → not_conflict（比较基准不同）；
“上下文窗口128K（官网）”和“实测64K以上准确率下降（评测文章）” → not_conflict（一个是宣称上限，一个是实测表现）。
出处层级只是参考：一手/官方的说法通常更可靠，但如果另一条是官方后来发布的更正，以更正为准。
只输出 JSON：{{"label": "a|b|not_conflict|unsure", "reason": "一句话理由（40 字内）"}}

第一条（a）：{a}

第二条（b）：{b}""",
}


def _doc_text(doc_id: str) -> str:
    d = db.query_one("SELECT title, url, published_at, text FROM documents WHERE id=?", (doc_id,))
    if not d:
        return "（资料已删除）"
    from .writing import site_name
    return f"《{d['title']}》（{site_name(d['url'])}，{str(d['published_at'] or '')[:10]}）\n{(d['text'] or '')[:1500]}"


def _material(kind: str, row: dict) -> tuple[str, str] | None:
    key = row["key"]
    if kind in ("reprint", "event"):
        if "|" not in key:
            return None
        a, b = key.split("|", 1)
        return _doc_text(a), _doc_text(b)
    if kind == "update":
        doc_id, tag = key.split("@", 1)
        d = db.query_one("SELECT title, url, meta FROM documents WHERE id=?", (doc_id,))
        if not d:
            return None
        meta = d["meta"] or {}
        ch = meta.get("change") if tag.startswith("v") else next(
            (m.get("change") for m in meta.get("minor_changes") or [] if f"minor{int(m.get('at') or 0)}" == tag), None)
        if not ch:
            return None
        lines = [f"全文相似度 {ch.get('similarity')}",
                 f"数字变化：{'、'.join(ch.get('numbers_removed') or []) or '无'} → {'、'.join(ch.get('numbers_added') or []) or '无'}"]
        lines += [f"删掉：{x[:120]}" for x in ch.get("sentences_removed") or []]
        lines += [f"新增：{x[:120]}" for x in ch.get("sentences_added") or []]
        return f"《{d['title']}》{d['url']}", "\n".join(lines)
    if kind == "dispute":
        ids = None
        m = db.query_one("SELECT meta, derived_from FROM memories WHERE id=?", (key,))
        if m:
            meta = m["meta"] if isinstance(m["meta"], dict) else json.loads(m["meta"] or "{}")
            ids = meta.get("facts") or m["derived_from"]
        if not ids or len(ids) < 2:
            return None
        fa, fb = cal._fact_info(ids[0]), cal._fact_info(ids[1])
        if not fa or not fb:
            return None

        def show(f):
            src = "；".join(f"{s['site']}《{(s['title'] or '')[:30]}》" for s in f["sources"]) or "无"
            return f"{f['content']}\n出处：{src}\n（一手来源：{'有' if f['primary'] else '无'}；独立来源 {f['independent']} 个）"
        return show(fa), show(fb)
    return None


def judge(kind: str, row: dict, run: int = 1) -> dict | None:
    mat = _material(kind, row)
    if not mat:
        return None
    r = llm.chat([{"role": "system", "content": "你只输出 JSON。"},
                  {"role": "user", "content": PROMPTS[kind].format(a=mat[0], b=mat[1])}],
                 json_mode=True, max_tokens=300, timeout=90)
    d = r.get("data") or {}
    label = str(d.get("label") or "").strip()
    valid = set(cal.LABELS[kind]) | ({"unsure"} if kind == "dispute" else set())
    if label not in valid:
        label = "invalid"
    out = {"kind": kind, "key": row["key"], "run": run, "label": label, "reason": str(d.get("reason") or "")[:200],
           "created_at": db.now()}
    db.execute("DELETE FROM llm_judgments WHERE kind=? AND key=? AND run=?", (kind, row["key"], run))
    db.insert("llm_judgments", out)
    return out


def judgments(kind: str) -> dict:
    out = {}
    for r in db.query("SELECT * FROM llm_judgments WHERE kind=?", (kind,)):
        out.setdefault(r["key"], {})[r["run"]] = r
    return out


def run(kind: str, limit: int = 30, repeat: int = 10, workers: int = 4) -> dict:
    """让大模型判一遍已标注、还没判过的样本；前 repeat 条再问第二次，用来量一致性。"""
    if not llm.available():
        raise ValueError("没有配置大模型")
    have = judgments(kind)
    rows = cal.labels(kind)
    jobs = [(r, 1) for r in rows if 1 not in have.get(r["key"], {})][:limit]
    with_second = [r for r in rows if 2 in have.get(r["key"], {})]
    need2 = max(0, repeat - len(with_second))
    jobs += [(r, 2) for r in rows if 2 not in have.get(r["key"], {})][:need2]
    done, failed = 0, 0

    def one(job):
        try:
            return judge(kind, job[0], job[1])
        except Exception:  # noqa: BLE001
            return None
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for res in ex.map(one, jobs):
            done += res is not None
            failed += res is None
    return {"called": len(jobs), "ok": done, "failed": failed}


def borderline(kind: str, f: dict, p: dict) -> bool:
    """规则拿不准的样本：特征落在阈值附近。混合做法只在这些样本上问大模型。"""
    if kind == "reprint":
        return (p["hamming"] < f["hamming"] <= p["hamming"] + 14) and (f["align"] >= p["align"] - 0.35)
    if kind == "event":
        return abs(f["title_sim"] - p["title"]) <= 0.15 or (p["title_lo"] - 0.1 <= f["title_sim"] < p["title"])
    if kind == "update":
        return 0.9 <= f["similarity"] <= 0.997 or 1 <= f.get("num_changed", 0) <= 2
    return True


def _binary(kind, rows, predict):
    tp = fp = fn = tn = 0
    for r in rows:
        y = predict(r)
        if y is None:
            continue
        t = cal._truth(kind, r["label"])
        tp += t and y
        fp += y and not t
        fn += t and not y
        tn += (not t) and (not y)
    n = tp + fp + fn + tn
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / (tp + fn) if tp + fn else None
    f1 = 2 * prec * rec / (prec + rec) if prec and rec else 0.0
    return {"n": n, "accuracy": round((tp + tn) / n, 3) if n else None,
            "precision": round(prec, 3) if prec is not None else None,
            "recall": round(rec, 3) if rec is not None else None, "f1": round(f1, 3)}


def compare(kind: str) -> dict:
    p = cal.params(kind)
    have = judgments(kind)
    rows = [r for r in cal.labels(kind) if 1 in have.get(r["key"], {}) and have[r["key"]][1]["label"] != "invalid"]
    out = {"kind": kind, "labeled": len(cal.labels(kind)), "judged": len(rows), "llm_available": llm.available()}
    pairs = [(have[k][1]["label"], have[k][2]["label"]) for k in have if 1 in have[k] and 2 in have[k]]
    out["consistency"] = {"n": len(pairs), "same": sum(1 for a, b in pairs if a == b),
                          "rate": round(sum(1 for a, b in pairs if a == b) / len(pairs), 3) if pairs else None}
    if not rows:
        return out
    llm_label = {r["key"]: have[r["key"]][1]["label"] for r in rows}
    if kind == "dispute":
        def conflict_acc(pred):
            rs = [r for r in rows if r["label"] != "unsure"]
            ok = sum(1 for r in rs if (r["label"] != "not_conflict") == pred(r))
            return round(ok / len(rs), 3) if rs else None

        def resolve(pred):
            rs = [r for r in rows if r["label"] in ("a", "b")]
            dec = [(r, pred(r)) for r in rs if pred(r) in ("a", "b")]
            right = sum(1 for r, d in dec if d == r["label"])
            return {"judged": len(rs), "decided": len(dec), "right": right,
                    "accuracy": round(right / len(dec), 3) if dec else None,
                    "coverage": round(len(dec) / len(rs), 3) if rs else None}
        rule_c = lambda r: bool(r["features"].get("conflict", True))          # noqa: E731
        llm_c = lambda r: llm_label[r["key"]] != "not_conflict"               # noqa: E731
        rule_w = lambda r: cal.decide_dispute({**r["features"], "conflict": True}, p)   # noqa: E731
        out["rule"] = {"conflict_accuracy": conflict_acc(rule_c), "resolve": resolve(rule_w), "calls": 0}
        out["llm"] = {"conflict_accuracy": conflict_acc(llm_c), "resolve": resolve(lambda r: llm_label[r["key"]]),
                      "calls": len(rows)}
        out["hybrid"] = {"conflict_accuracy": conflict_acc(llm_c), "resolve": resolve(rule_w), "calls": len(rows),
                         "note": "大模型判断是不是真矛盾，谁对仍按证据（一手来源、独立来源数）裁决"}
    else:
        rule_p = lambda r: cal._pred(kind, cal.DECIDE[kind](r["features"], p))     # noqa: E731
        llm_p = lambda r: cal._pred(kind, llm_label[r["key"]])                      # noqa: E731
        hyb_p = lambda r: llm_p(r) if borderline(kind, r["features"], p) else rule_p(r)   # noqa: E731
        out["rule"] = {**_binary(kind, rows, rule_p), "calls": 0}
        out["llm"] = {**_binary(kind, rows, llm_p), "calls": len(rows)}
        out["hybrid"] = {**_binary(kind, rows, hyb_p), "calls": sum(1 for r in rows if borderline(kind, r["features"], p)),
                         "note": "规则判断明确的直接用规则，阈值附近的才问大模型"}
    dis = []
    for r in rows:
        lab = llm_label[r["key"]]
        if lab != r["label"] and not (kind == "reprint" and {lab, r["label"]} <= {"reprint", "rewrite"}):
            dis.append({"key": r["key"], "human": r["label"], "llm": lab, "reason": have[r["key"]][1]["reason"],
                        **(r.get("detail") or {})})
    out["llm_wrong"] = dis[:10]
    out["flips"] = [{"key": k, "first": have[k][1]["label"], "second": have[k][2]["label"],
                     "reasons": [have[k][1]["reason"], have[k][2]["reason"]]}
                    for k in have if 1 in have[k] and 2 in have[k] and have[k][1]["label"] != have[k][2]["label"]][:6]
    return out
