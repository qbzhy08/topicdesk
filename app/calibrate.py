"""判断校准：用人的判断检验、并调整系统的四类自动判断。

  reprint  转载 / 洗稿：两篇资料是不是同一篇稿子（原样转载，或逐句改写的“洗稿”）——决定“独立来源”怎么数；
  event    同一事件：两篇报道说的是不是同一件事——决定事件归并、多来源印证；
  update   网页更新：同一网址前后两次抓到的内容，变化是不是实质更新（改了数字、增删了句子），
           还是阅读数、时间戳这类无关紧要的变化——决定要不要生成新版本、提示“来源更正”；
  dispute  争议裁决：两条说法是不是真的矛盾；如果矛盾，哪条对——决定事实的核验状态。

流程：系统挑出“最值得人看”的样本（系统判为是的 + 差一点就判为是的边界样本）→ 人在“判断校准”页逐条标注 →
用标注算现行规则的准确率、精确率、召回率 → 在参数网格上搜索最符合人工判断的阈值（5 折交叉验证，防止只是记住了标注）
→ 新参数在交叉验证上明显更好时才建议采用；采用后写进 data/calibration.json，入库和裁决时实时读取。

这不是训练神经网络：样本只有几十到几百条，可解释的阈值规则 + 交叉验证更稳，也能说清“为什么判为转载”。
"""
from __future__ import annotations

import itertools
import json
import random
import re
import time
from datetime import timedelta

from . import config, db

KINDS = {"reprint": "转载 / 洗稿", "event": "同一事件", "update": "网页更新", "dispute": "争议裁决"}

DEFAULTS = {
    # 转载：SimHash 海明距离 ≤ hamming；洗稿：较短一篇里 ≥ align 比例的句子能在另一篇找到改写对应句，
    # 且两篇的关键数字重合度 ≥ num_jacc（两篇都没有数字时不看这一条），句子数 ≥ min_sents
    "reprint": {"hamming": 6, "align": 0.6, "num_jacc": 0.4, "min_sents": 4},
    # 同一事件：发布时间相差 ≤ days 天，并且 标题相似度 ≥ title，或 标题相似度 ≥ title_lo 且共享 ≥ num_shared 个关键数字
    "event": {"title": 0.42, "days": 4, "title_lo": 0.25, "num_shared": 2},
    # 网页更新：全文相似度 < similarity，或关键数字（去掉阅读数、时间这类噪声）变化 ≥ num_changed 个
    "update": {"similarity": 0.985, "num_changed": 1},
    # 争议裁决：证据分 = 一手来源 × primary_w + 独立来源数 + 来源更正 × corrected_w；分差 ≥ gap 才裁决
    "dispute": {"primary_w": 2, "corrected_w": 2, "gap": 2},
}

GRID = {
    "reprint": {"hamming": [3, 4, 6, 8, 10, 12, 14], "align": [0.4, 0.5, 0.6, 0.7, 0.8, 1.01],
                "num_jacc": [0.0, 0.2, 0.4, 0.6], "min_sents": [3, 4, 6]},
    "event": {"title": [0.3, 0.35, 0.42, 0.5, 0.6], "days": [1, 2, 4, 7, 10],
              "title_lo": [0.15, 0.2, 0.25, 0.3, 1.01], "num_shared": [1, 2, 3]},
    "update": {"similarity": [0.9, 0.95, 0.97, 0.985, 0.995, 1.01], "num_changed": [1, 2, 3, 99]},
    "dispute": {"primary_w": [0, 1, 2, 3, 4], "corrected_w": [0, 1, 2, 3], "gap": [1, 2, 3, 4]},
}

LABELS = {
    "reprint": {"reprint": "原样转载", "rewrite": "洗稿 / 改写自同一稿", "independent": "独立报道"},
    "event": {"same": "同一事件", "different": "不是同一事件"},
    "update": {"significant": "实质更新", "minor": "无关紧要的变化"},
    "dispute": {"a": "第一条对", "b": "第二条对", "not_conflict": "不矛盾（说的不是一回事）", "unsure": "说不准"},
}


# ---------------------------------------------------------------- 参数（标注调过的优先）

def _path():
    return config.DB_PATH.parent / "calibration.json"


_cache = {"mtime": None, "data": {}}


def _load() -> dict:
    p = _path()
    try:
        mt = p.stat().st_mtime
    except OSError:
        return {}
    if _cache["mtime"] != (str(p), mt):
        try:
            _cache["data"] = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _cache["data"] = {}
        _cache["mtime"] = (str(p), mt)
    return _cache["data"]


def params(kind: str) -> dict:
    saved = (_load().get(kind) or {}).get("params") or {}
    return {**DEFAULTS[kind], **{k: v for k, v in saved.items() if k in DEFAULTS[kind]}}


def save_params(kind: str, new: dict, metrics: dict | None = None, note: str = ""):
    data = dict(_load())
    old = data.get(kind) or {}
    hist = (old.get("history") or [])[-9:]
    if old.get("params"):
        hist.append({"params": old["params"], "at": old.get("at"), "metrics": old.get("metrics")})
    data[kind] = {"params": {k: new[k] for k in DEFAULTS[kind] if k in new}, "at": time.time(),
                  "metrics": metrics, "note": note, "history": hist}
    _path().write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    _cache["mtime"] = None


def reset_params(kind: str):
    data = dict(_load())
    data.pop(kind, None)
    _path().write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    _cache["mtime"] = None


# ---------------------------------------------------------------- 特征

HARD_NUM = re.compile(r"\d+(?:\.\d+)?\s*(?:%|％|万亿|亿元|万元|亿|万|倍|元|人次|美元|个百分点)|\d+\.\d+|\d{3,}")
NOISE = re.compile(
    r"\d+(?:\.\d+)?\s*[万wW]?\+?\s*(?:次?阅读|次?浏览|人看过|次播放|评论|点赞|收藏|分享|转发|人参与|人关注)"
    r"|(?:阅读|浏览|评论|点赞|收藏|分享|转发|播放)(?:量|数|次数)?\s*[:：]?\s*\d+(?:\.\d+)?\s*[万wW]?"
    r"|\d{1,2}:\d{2}(?::\d{2})?"
    r"|(?:19|20)\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?"
    r"|\d+\s*(?:分钟|小时|天)前")
SENT = re.compile(r"(?<=[。！？!?；;])|\n+")


def hard_numbers(text: str, denoise: bool = True) -> set:
    t = NOISE.sub(" ", text or "") if denoise else (text or "")
    return {re.sub(r"\s+", "", n) for n in HARD_NUM.findall(t)}


def _grams(s: str) -> set:
    s = re.sub(r"[\s\W_]+", "", (s or "").lower())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _sents(text: str, limit: int = 80) -> list[str]:
    out = [s.strip() for s in SENT.split(NOISE.sub(" ", text or "")) if len(s.strip()) >= 10]
    return out[:limit]


def align_ratio(a: str, b: str) -> tuple[float, int]:
    """较短一篇的句子里，有多大比例能在另一篇找到“改写对应句”（字二元组重合 ≥ 0.5）。洗稿的典型特征：
    逐句换词、调语序，但句子一一对应、关键信息不变。返回 (比例, 较短一篇的句子数)。"""
    sa, sb = _sents(a), _sents(b)
    if len(sa) > len(sb):
        sa, sb = sb, sa
    if not sa or not sb:
        return 0.0, len(sa)
    gb = [_grams(s) for s in sb]
    hit = 0
    for s in sa:
        g = _grams(s)
        if g and max(len(g & x) / len(g) for x in gb) >= 0.5:
            hit += 1
    return round(hit / len(sa), 3), len(sa)


def _jacc(a: set, b: set) -> float | None:
    if not a and not b:
        return None
    return round(len(a & b) / len(a | b), 3) if (a | b) else None


def reprint_features(a: dict, b: dict) -> dict:
    from .collect.dedup import hamming, simhash, title_sim
    ta, tb = (a.get("text") or "")[:12000], (b.get("text") or "")[:12000]
    ha, hb = a.get("simhash") or simhash(ta), b.get("simhash") or simhash(tb)
    al, n = align_ratio(ta, tb)
    return {"hamming": hamming(ha, hb), "align": al, "sents": n,
            "num_jacc": _jacc(hard_numbers(ta), hard_numbers(tb)),
            "title_sim": round(title_sim(a.get("title") or "", b.get("title") or ""), 3)}


def decide_reprint(f: dict, p: dict) -> str:
    if f["hamming"] <= p["hamming"]:
        return "reprint"
    nj = f.get("num_jacc")
    if f["align"] >= p["align"] and f["sents"] >= p["min_sents"] and (nj is None or nj >= p["num_jacc"]):
        return "rewrite"
    return "independent"


def _days(a, b):
    from .collect.dedup import parse_date
    da, dbb = parse_date(a), parse_date(b)
    return round(abs((da - dbb).total_seconds()) / 86400, 2) if da and dbb else None


def event_features(a: dict, b: dict) -> dict:
    from .collect.dedup import title_sim
    na = hard_numbers((a.get("title") or "") + " " + (a.get("text") or "")[:1500])
    nb = hard_numbers((b.get("title") or "") + " " + (b.get("text") or "")[:1500])
    return {"title_sim": round(title_sim(a.get("title") or "", b.get("title") or ""), 3),
            "days": _days(a.get("published_at"), b.get("published_at")),
            "num_shared": len(na & nb),
            "lead_sim": round(title_sim((a.get("text") or "")[:200], (b.get("text") or "")[:200]), 3)}


def decide_event(f: dict, p: dict) -> str:
    if f.get("days") is not None and f["days"] > p["days"]:
        return "different"
    if f["title_sim"] >= p["title"]:
        return "same"
    if f["title_sim"] >= p["title_lo"] and f["num_shared"] >= p["num_shared"]:
        return "same"
    return "different"


def update_features(change: dict) -> dict:
    nums = [n for n in (change.get("numbers_added") or []) + (change.get("numbers_removed") or [])]
    return {"similarity": change.get("similarity", 1.0), "num_changed": len(nums),
            "sent_changed": len(change.get("sentences_added") or []) + len(change.get("sentences_removed") or [])}


def decide_update(f: dict, p: dict) -> str:
    return "significant" if f["similarity"] < p["similarity"] or f["num_changed"] >= p["num_changed"] else "minor"


def dispute_score(f: dict, p: dict) -> float:
    return (p["primary_w"] if f.get("primary") else 0) + (f.get("independent") or 0) + \
        (p["corrected_w"] if f.get("corrected") else 0)


def decide_dispute(f: dict, p: dict) -> str:
    """f = {"a": {...}, "b": {...}, "conflict": 系统是否判为冲突}。返回 a / b / open / not_conflict。"""
    if not f.get("conflict", True):
        return "not_conflict"
    sa, sb = dispute_score(f["a"], p), dispute_score(f["b"], p)
    if abs(sa - sb) < p["gap"]:
        return "open"
    return "a" if sa > sb else "b"


DECIDE = {"reprint": decide_reprint, "event": decide_event, "update": decide_update, "dispute": decide_dispute}


# ---------------------------------------------------------------- 候选样本（最值得人看的）

def _doc_rows(topic: str | None, limit: int = 160) -> list[dict]:
    sql = ("SELECT id, url, title, text, simhash, published_at, source_id, duplicate_of, event_id, topic, meta "
           "FROM documents WHERE status='active' AND LENGTH(text) >= 120")
    args = []
    if topic:
        sql += " AND topic=?"
        args.append(topic)
    sql += " ORDER BY fetched_at DESC LIMIT ?"
    args.append(limit)
    return db.query(sql, args)


def labeled_keys(kind: str) -> set:
    return {r["key"] for r in db.query("SELECT key FROM judgment_labels WHERE kind=?", (kind,))}


def _brief(d: dict) -> dict:
    from .writing import site_name
    return {"id": d["id"], "title": d.get("title") or d.get("url"), "url": d.get("url"),
            "site": site_name(d.get("url") or ""), "published_at": d.get("published_at"),
            "excerpt": re.sub(r"\s+", " ", (d.get("text") or ""))[:260]}


def _pairs(topic: str | None):
    from .collect.dedup import hamming, title_sim
    docs = _doc_rows(topic)
    by_topic = {}
    for d in docs:
        by_topic.setdefault(d["topic"], []).append(d)
    for ds in by_topic.values():
        for a, b in itertools.combinations(ds, 2):
            yield a, b, (hamming(a["simhash"], b["simhash"]) if a["simhash"] and b["simhash"] else 64), \
                title_sim(a["title"] or "", b["title"] or "")


def candidates(kind: str, topic: str | None = None, n: int = 8, seed: int | None = None) -> list[dict]:
    """挑样本：一半是系统判为“是”的，一半是离阈值最近、差一点就判为“是”的（最能暴露阈值的问题）。"""
    done = labeled_keys(kind)
    p = params(kind)
    rnd = random.Random(seed if seed is not None else int(time.time()))
    out = []
    if kind in ("reprint", "event"):
        pool = []
        for a, b, ham, ts in _pairs(topic):
            key = "|".join(sorted([a["id"], b["id"]]))
            if key in done:
                continue
            if kind == "reprint" and not (ham <= 24 or ts >= 0.3):
                continue
            if kind == "event" and ts < 0.15:
                continue
            f = reprint_features(a, b) if kind == "reprint" else event_features(a, b)
            sysd = DECIDE[kind](f, p)
            # 离判定边界有多近（越小越值得看）
            if kind == "reprint":
                margin = min(abs(f["hamming"] - p["hamming"]) / 8, abs(f["align"] - p["align"]))
            else:
                margin = min(abs(f["title_sim"] - p["title"]), abs(f["title_sim"] - p["title_lo"]) + (0 if f["num_shared"] >= p["num_shared"] else 0.3))
            pool.append((margin, key, a, b, f, sysd))
            if len(pool) >= 600:
                break
        pos = [x for x in pool if x[5] != ("independent" if kind == "reprint" else "different")]
        neg = sorted([x for x in pool if x not in pos], key=lambda x: x[0])
        rnd.shuffle(pos)
        pick = pos[: (n + 1) // 2] + neg[: n - min(len(pos), (n + 1) // 2)]
        for _, key, a, b, f, sysd in pick[:n]:
            out.append({"kind": kind, "key": key, "a": _brief(a), "b": _brief(b), "features": f, "system": sysd})
    elif kind == "update":
        rows = db.query("SELECT id, url, title, meta, version, prev_version_id, published_at FROM documents "
                        "WHERE (prev_version_id IS NOT NULL OR meta LIKE '%minor_changes%') ORDER BY fetched_at DESC LIMIT 300")
        pool = []
        for d in rows:
            meta = d["meta"] or {}
            items = []
            if d["prev_version_id"] and meta.get("change"):
                items.append((f"{d['id']}@v{d['version']}", meta["change"]))
            for k, mc in enumerate(meta.get("minor_changes") or []):
                items.append((f"{d['id']}@minor{int(mc.get('at') or k)}", mc.get("change") or {}))
            for key, ch in items:
                if key in done:
                    continue
                f = update_features(ch)
                pool.append((abs(f["similarity"] - p["similarity"]), key, d, ch, f, decide_update(f, p)))
        pos = [x for x in pool if x[5] == "significant"]
        neg = sorted([x for x in pool if x[5] != "significant"], key=lambda x: x[0])
        rnd.shuffle(pos)
        pick = pos[: (n + 1) // 2] + neg[: n - min(len(pos), (n + 1) // 2)]
        for _, key, d, ch, f, sysd in pick[:n]:
            out.append({"kind": kind, "key": key, "a": _brief(d), "change": ch, "features": f, "system": sysd})
    elif kind == "dispute":
        out = [x for x in dispute_items(topic) if x["key"] not in done][:n]
    return out


def _fact_info(mid: str) -> dict | None:
    from . import memory as mem
    f = mem.get(mid)
    if not f:
        return None
    m = f["meta"] or {}
    docs = db.query("SELECT d.id, d.url, d.title, d.published_at FROM evidence e JOIN documents d ON d.id=e.doc_id "
                    "WHERE e.claim_id=? AND e.stance='support' LIMIT 4", (mid,))
    from .writing import site_name
    return {"id": mid, "content": f["content"], "verify": m.get("verify"), "primary": bool(m.get("primary")),
            "independent": m.get("independent") or 0, "corrected": bool(m.get("corrected_doc")),
            "sources": [{"title": x["title"], "url": x["url"], "site": site_name(x["url"])} for x in docs]}


def dispute_items(topic: str | None = None) -> list[dict]:
    sql = "SELECT id, topic, status, meta, derived_from FROM memories WHERE kind='dispute'"
    args = []
    if topic:
        sql += " AND topic=?"
        args.append(topic)
    out = []
    for r in db.query(sql + " ORDER BY created_at DESC LIMIT 200", args):
        meta = r["meta"] if isinstance(r["meta"], dict) else json.loads(r["meta"] or "{}")
        ids = meta.get("facts") or (r["derived_from"] if isinstance(r["derived_from"], list) else json.loads(r["derived_from"] or "[]"))
        if len(ids) < 2:
            continue
        a, b = _fact_info(ids[0]), _fact_info(ids[1])
        if not a or not b:
            continue
        f = {"a": {k: a[k] for k in ("primary", "independent", "corrected")},
             "b": {k: b[k] for k in ("primary", "independent", "corrected")},
             "conflict": not meta.get("misjudged")}
        out.append({"kind": "dispute", "key": r["id"], "topic": r["topic"], "fa": a, "fb": b, "features": f,
                    "system": decide_dispute(f, params("dispute")), "note": meta.get("misjudged")})
    return out


# ---------------------------------------------------------------- 标注

def save_label(kind: str, key: str, label: str, features: dict, system: str, detail: dict | None = None):
    if kind not in KINDS or label not in LABELS[kind] and label != "skip":
        raise ValueError("未知的标注")
    db.execute("DELETE FROM judgment_labels WHERE kind=? AND key=?", (kind, key))
    db.insert("judgment_labels", dict(kind=kind, key=key, label=label, features=json.dumps(features, ensure_ascii=False),
                                      system=system, detail=json.dumps(detail or {}, ensure_ascii=False),
                                      created_at=db.now()))


def labels(kind: str) -> list[dict]:
    out = []
    for r in db.query("SELECT * FROM judgment_labels WHERE kind=? AND label!='skip' ORDER BY created_at", (kind,)):
        r = dict(r)
        for k in ("features", "detail"):
            if isinstance(r.get(k), str):
                try:
                    r[k] = json.loads(r[k] or "{}")
                except ValueError:
                    r[k] = {}
        out.append(r)
    return out


# ---------------------------------------------------------------- 评估与调参

def _truth(kind: str, label: str):
    """人工标注 → 二分类真值（dispute 另算）。"""
    return {"reprint": label in ("reprint", "rewrite"), "event": label == "same",
            "update": label == "significant"}.get(kind)


def _pred(kind: str, d: str) -> bool:
    return {"reprint": d in ("reprint", "rewrite"), "event": d == "same", "update": d == "significant"}[kind]


def score(kind: str, rows: list[dict], p: dict) -> dict:
    if kind == "dispute":
        return _score_dispute(rows, p)
    tp = fp = fn = tn = 0
    wrong = []
    for r in rows:
        t, y = _truth(kind, r["label"]), _pred(kind, DECIDE[kind](r["features"], p))
        if t and y:
            tp += 1
        elif y and not t:
            fp += 1
            wrong.append({"key": r["key"], "type": "误判为是", "label": r["label"], **(r.get("detail") or {})})
        elif t and not y:
            fn += 1
            wrong.append({"key": r["key"], "type": "漏判", "label": r["label"], **(r.get("detail") or {})})
        else:
            tn += 1
    n = len(rows)
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / (tp + fn) if tp + fn else None
    f1 = 2 * prec * rec / (prec + rec) if prec and rec else 0.0
    return {"n": n, "tp": tp, "fp": fp, "fn": fn, "tn": tn, "accuracy": round((tp + tn) / n, 3) if n else None,
            "precision": round(prec, 3) if prec is not None else None, "recall": round(rec, 3) if rec is not None else None,
            "f1": round(f1, 3), "wrong": wrong[:12]}


def _score_dispute(rows: list[dict], p: dict) -> dict:
    """冲突识别：系统判为冲突的里，人也认为矛盾的比例；裁决：系统做了裁决的里，和人一致的比例 + 裁决覆盖率。"""
    conf_rows = [r for r in rows if r["label"] != "unsure"]
    conflict_ok = sum(1 for r in conf_rows if (r["label"] != "not_conflict") == bool(r["features"].get("conflict", True)))
    decided = right = wrong_n = 0
    wrong = []
    for r in rows:
        if r["label"] not in ("a", "b"):
            continue
        d = decide_dispute({**r["features"], "conflict": True}, p)
        if d == "open":
            continue
        decided += 1
        if d == r["label"]:
            right += 1
        else:
            wrong_n += 1
            wrong.append({"key": r["key"], "type": "裁决错误", "label": r["label"], **(r.get("detail") or {})})
    judged = sum(1 for r in rows if r["label"] in ("a", "b"))
    util = (right - 2 * wrong_n) / judged if judged else 0.0     # 裁决错比不裁决代价大：错一个扣两分
    return {"n": len(rows), "conflict_accuracy": round(conflict_ok / len(conf_rows), 3) if conf_rows else None,
            "judged": judged, "decided": decided, "right": right,
            "resolve_accuracy": round(right / decided, 3) if decided else None,
            "coverage": round(decided / judged, 3) if judged else None, "f1": round(util, 3), "wrong": wrong[:12]}


def _objective(kind, rows, p):
    s = score(kind, rows, p)
    return s["f1"] if kind != "dispute" else s["f1"]


def _closeness(kind, p):
    """离默认值多远（同分时选改动最小的参数，避免为了几条样本大幅改规则）。"""
    d = 0.0
    for k, v in DEFAULTS[kind].items():
        grid = GRID[kind][k]
        try:
            d += abs(grid.index(p[k]) - grid.index(min(grid, key=lambda g: abs(g - v))))
        except ValueError:
            d += 1
    return d


def fit(kind: str, rows: list[dict]) -> dict:
    """网格搜索：在标注上找最符合人工判断的参数。返回 {params, train}。"""
    keys = list(GRID[kind])
    best, best_key = None, None
    for combo in itertools.product(*[GRID[kind][k] for k in keys]):
        p = dict(zip(keys, combo))
        if kind == "event" and p["title_lo"] > p["title"] and p["title_lo"] < 1:
            continue
        obj = _objective(kind, rows, p)
        k = (obj, -_closeness(kind, p))
        if best_key is None or k > best_key:
            best, best_key = p, k
    return {"params": best, "train": score(kind, rows, best)}


def cross_validate(kind: str, rows: list[dict], folds: int = 5) -> dict:
    """5 折交叉验证：每次用 4/5 的标注调参，在剩下 1/5 上检验。衡量“调出来的参数在没见过的样本上好不好”。"""
    if len(rows) < folds * 2:
        folds = max(2, len(rows) // 3)
    idx = list(range(len(rows)))
    random.Random(7).shuffle(idx)
    preds = []
    for k in range(folds):
        test = [rows[i] for i in idx[k::folds]]
        train = [rows[i] for i in idx if i not in idx[k::folds]]
        if not train or not test:
            continue
        p = fit(kind, train)["params"]
        preds += [(r, p) for r in test]
    if kind == "dispute":
        # 每条用它所在折调出的参数
        agg = {"right": 0, "decided": 0, "judged": 0, "wrong": 0}
        for r, p in preds:
            if r["label"] not in ("a", "b"):
                continue
            agg["judged"] += 1
            d = decide_dispute({**r["features"], "conflict": True}, p)
            if d != "open":
                agg["decided"] += 1
                agg["right" if d == r["label"] else "wrong"] += 1
        util = (agg["right"] - 2 * agg["wrong"]) / agg["judged"] if agg["judged"] else 0.0
        return {"f1": round(util, 3), "resolve_accuracy": round(agg["right"] / agg["decided"], 3) if agg["decided"] else None,
                "coverage": round(agg["decided"] / agg["judged"], 3) if agg["judged"] else None}
    tp = fp = fn = tn = 0
    for r, p in preds:
        t, y = _truth(kind, r["label"]), _pred(kind, DECIDE[kind](r["features"], p))
        tp += t and y
        fp += y and not t
        fn += t and not y
        tn += (not t) and (not y)
    prec = tp / (tp + fp) if tp + fp else 0
    rec = tp / (tp + fn) if tp + fn else 0
    n = tp + fp + fn + tn
    return {"f1": round(2 * prec * rec / (prec + rec), 3) if prec and rec else 0.0,
            "precision": round(prec, 3), "recall": round(rec, 3), "accuracy": round((tp + tn) / n, 3) if n else None}


MIN_LABELS = 12


def report(kind: str) -> dict:
    rows = labels(kind)
    cur = params(kind)
    out = {"kind": kind, "name": KINDS[kind], "labels": len(rows), "params": cur, "defaults": DEFAULTS[kind],
           "label_counts": {k: sum(1 for r in rows if r["label"] == k) for k in LABELS[kind]},
           "current": score(kind, rows, cur) if rows else None,
           "applied": (_load().get(kind) or {}).get("at")}
    if kind == "dispute":
        both = len([r for r in rows if r["label"] in ("a", "b")]) >= 6
    else:
        pos = sum(1 for r in rows if _truth(kind, r["label"]))
        both = pos >= 3 and len(rows) - pos >= 3
    if len(rows) < MIN_LABELS or not both:
        out["fit"] = None
        out["need"] = (f"再标 {MIN_LABELS - len(rows)} 条" if len(rows) < MIN_LABELS else
                       "“是”和“不是”两类都至少要 3 条" if kind != "dispute" else "至少 6 条要判断出哪条对")
        return out
    f = fit(kind, rows)
    cv_new = cross_validate(kind, rows)
    cur_f1 = out["current"]["f1"]
    changed = {k: [cur[k], v] for k, v in f["params"].items() if cur.get(k) != v}
    better = cv_new["f1"] >= cur_f1 + 0.02 and bool(changed)
    out["fit"] = {"params": f["params"], "train": {k: v for k, v in f["train"].items() if k != "wrong"},
                  "cv": cv_new, "changed": changed, "recommend": better,
                  "why": ("新参数在没参与调参的样本上（交叉验证）更符合人工判断" if better else
                          "现行参数已经和人工判断一致" if not changed else
                          "新参数只在调参用的样本上更好，交叉验证没有明显提升，先不改（可能是过拟合）")}
    return out


def apply(kind: str) -> dict:
    r = report(kind)
    if not r.get("fit"):
        raise ValueError(r.get("need") or "标注不够")
    save_params(kind, r["fit"]["params"], {"cv": r["fit"]["cv"], "labels": r["labels"],
                                           "before": {k: v for k, v in (r["current"] or {}).items() if k != "wrong"}},
                note="按人工标注调参")
    return r
