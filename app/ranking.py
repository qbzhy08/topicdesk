"""Lv2：价值评估与重排序。

一篇资料“值不值得读”由五个维度决定，每个维度 0-1，并给出可读的理由：

    相关度 relevance     规则相关度（短语匹配、泛词降权），配置了大模型时为复核后的分数
    可信度 credibility   一手来源、同一事件的独立来源数、正文完整度、支撑的事实是否已核验 / 有争议
    新颖性 novelty       新进展 > 内容更新 > 背景资料 > 重复报道（相对于该主题上次运行）
    时效性 timeliness    按发布时间指数衰减，半衰期 14 天
    阅读成本 cost        按字数估算的阅读分钟数（中文 400 字/分钟，英文 220 词/分钟）

价值分 = Σ 权重 × 维度分（成本不进价值分，而是作为“预算”约束榜单）。

榜单是“在时间预算内选出价值最高、彼此不重复的一组资料”：
    5 分钟速览   每篇按“读标题+摘要”计 0.6 分钟，适合快速掌握动态
    30 分钟深读  每篇按全文阅读时间计（超长文按 15 分钟封顶，只读核心部分）
    偏好时长     用户在记忆中设置了阅读时间（如 10 分钟）时额外生成
同一事件只保留价值最高的一篇（信息增益：重复报道不增加新信息）。

基线：按发布时间倒序、按热度（点赞/星标/浏览/引用）倒序，在同一资料池上排序，不做去重。
指标：P@5 / P@10（人工标注优先，否则用自动标注“银标”并注明）、子问题覆盖率、重复率、7 天内占比、一手来源占比。
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from . import db
from .collect.dedup import parse_date

WEIGHTS = {"relevance": 0.35, "credibility": 0.25, "novelty": 0.2, "timeliness": 0.2}
FACTOR_NAME = {"relevance": "相关度", "credibility": "可信度", "novelty": "新颖性", "timeliness": "时效性"}
NOVELTY_SCORE = {"new": 1.0, "update": 0.85, "background": 0.35, "duplicate": 0.0}
NOVELTY_NAME = {"new": "新进展", "update": "内容有更新", "background": "背景资料", "duplicate": "重复报道"}
HALF_LIFE_DAYS = 14
SKIM_MIN = 0.6
DEEP_CAP_MIN = 15
UNVERIFIED_PENALTY = 0.85        # 补充核验后仍找不到第二个来源：价值打折并标注“待核实”


# ---------------------------------------------------------------- 阅读成本

def reading_minutes(text: str | None) -> float:
    t = text or ""
    cjk = len(re.findall(r"[一-鿿]", t))
    words = len(re.findall(r"[A-Za-z]+", t))
    return round(max(0.3, cjk / 400 + words / 220), 1)


def _host(url: str) -> str:
    h = urlsplit(url or "").netloc.lower()
    return h[4:] if h.startswith("www.") else h


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def popularity_raw(meta: dict) -> float | None:
    """热度原始值：HN 分数、GitHub 星标、掘金浏览/点赞、论文被引。没有热度信号返回 None。"""
    vals = [_num(meta.get(k)) for k in ("points", "stars", "views", "likes", "cited_by")]
    vals = [v for v in vals if v is not None]
    return max(vals) if vals else None


# ---------------------------------------------------------------- 资料池与信号

def task_pool(task_id: str) -> list[dict]:
    return db.query("SELECT * FROM documents WHERE status='active' AND meta LIKE ?", (f"%{task_id}%",))


def _signals(pool: list[dict]) -> dict:
    """批量预计算：每个事件的独立来源（按站点去重、不含转载）、每篇资料支撑的事实及其核验状态。"""
    topics = {d["topic"] for d in pool}
    events: dict[str, set] = {}
    if topics:
        rows = db.query(f"SELECT event_id, url FROM documents WHERE status='active' AND duplicate_of IS NULL "
                        f"AND topic IN ({','.join('?' * len(topics))})", list(topics))
        for r in rows:
            if r["event_id"]:
                events.setdefault(r["event_id"], set()).add(_host(r["url"]))
    facts: dict[str, list] = {}
    ids = [d["id"] for d in pool]
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        for r in db.query(f"SELECT e.doc_id, m.content, m.meta FROM evidence e JOIN memories m ON m.id=e.claim_id "
                          f"WHERE e.stance='support' AND m.status IN ('active','stale') "
                          f"AND e.doc_id IN ({','.join('?' * len(chunk))})", chunk):
            facts.setdefault(r["doc_id"], []).append(r)
    pops: dict[str, float] = {}
    for d in pool:
        p = popularity_raw(d["meta"] or {})
        if p is not None:
            pops[d["source_id"]] = max(pops.get(d["source_id"], 0), p)
    return {"events": events, "facts": facts, "pop_max": pops}


def _credibility(d: dict, sig: dict) -> tuple[float, list[str], dict]:
    meta = d["meta"] or {}
    reasons, info = [], {}
    score = 0.35
    if meta.get("primary"):
        score += 0.3
        reasons.append("一手来源（论文/仓库/官方）")
    n = len(sig["events"].get(d["event_id"], set()) | {_host(d["url"])}) if d["event_id"] else 1
    info["independent"] = n
    if n >= 2:
        score += 0.25 * min(1, (n - 1) / 2)
        reasons.append(f"{n} 个独立站点报道同一事件")
    if d["content_kind"] == "full":
        score += 0.1
    fs = sig["facts"].get(d["id"], [])
    statuses = [(f["meta"] or {}).get("verify") for f in fs]
    info["facts"] = len(fs)
    if "supported" in statuses:
        score += 0.15
        reasons.append(f"支撑 {statuses.count('supported')} 条已核验事实")
    if "disputed" in statuses or "refuted" in statuses:
        score -= 0.25
        bad = next(f for f in fs if (f["meta"] or {}).get("verify") in ("disputed", "refuted"))
        reasons.append(f"涉及有争议的事实：「{bad['content'][:30]}」")
        info["disputed"] = True
    if meta.get("verify_note"):
        reasons.append(meta["verify_note"])
    return round(max(0.0, min(1.0, score)), 3), reasons, info


def _timeliness(d: dict, now: datetime) -> tuple[float, str]:
    dt = parse_date(d["published_at"])
    if not dt:
        return 0.3, "发布时间未知"
    age = max(0.0, (now - dt).total_seconds() / 86400)
    s = max(0.05, 0.5 ** (age / HALF_LIFE_DAYS))
    txt = "今天发布" if age < 1 else f"{int(age)} 天前发布"
    return round(s, 3), txt


def score_pool(pool: list[dict], *, min_rel: float = 0.4, weights: dict | None = None,
               flags: dict | None = None) -> list[dict]:
    """给资料池中每篇资料打分。flags: {doc_id: {"unverified": True, "note": "..."}} 来自补充核验。"""
    w = {**WEIGHTS, **(weights or {})}
    tot = sum(w.values()) or 1
    sig = _signals(pool)
    now = datetime.now(timezone.utc)
    flags = flags or {}
    items = []
    for d in pool:
        meta = d["meta"] or {}
        rel = meta.get("relevance")
        rel = 0.5 if rel is None else float(rel)
        cred, cred_reasons, cinfo = _credibility(d, sig)
        nov = NOVELTY_SCORE.get(d["novelty"], 0.35)
        tim, tim_txt = _timeliness(d, now)
        f = {"relevance": round(rel, 3), "credibility": cred, "novelty": nov, "timeliness": tim}
        value = sum(w[k] * f[k] for k in f) / tot
        fl = flags.get(d["id"]) or {}
        if fl.get("unverified"):
            value *= UNVERIFIED_PENALTY
        pop = popularity_raw(meta)
        pmax = sig["pop_max"].get(d["source_id"])
        judge = meta.get("judge") or {}
        reasons = []
        if judge.get("reason"):
            reasons.append(f"相关度 {rel:.2f}（大模型：{judge['reason']}）")
        else:
            reasons.append(f"相关度 {rel:.2f}")
        if fl.get("note"):          # 补充核验的结论放在前面，重新评估时一眼能看到变化原因
            reasons.append(fl["note"])
        reasons += cred_reasons
        reasons.append(NOVELTY_NAME.get(d["novelty"], d["novelty"]))
        reasons.append(tim_txt)
        excluded = None
        if d["duplicate_of"]:
            excluded = "转载/重复报道，已归并到原文"
        elif rel < min_rel:
            excluded = f"相关度 {rel:.2f} 低于门槛 {min_rel}" + (f"（大模型：{judge['reason']}）" if judge.get("reason") else "")
        text = d["text"] or d["summary"] or ""
        items.append({
            "id": d["id"], "title": d["title"] or d["url"], "url": d["url"], "source_id": d["source_id"],
            "published_at": d["published_at"], "event_id": d["event_id"], "duplicate_of": d["duplicate_of"],
            "novelty": d["novelty"], "content_kind": d["content_kind"], "primary": bool(meta.get("primary")),
            "features": f, "value": round(value, 4), "reasons": reasons,
            "independent": cinfo.get("independent", 1), "disputed": bool(cinfo.get("disputed")),
            "unverified": bool(fl.get("unverified")),
            "read_min": min(reading_minutes(text), DEEP_CAP_MIN), "skim_min": SKIM_MIN,
            "popularity": pop, "popularity_norm": round(math.log1p(pop) / math.log1p(pmax), 3) if pop and pmax else 0,
            "excluded": excluded,
        })
    # 解释用“突出项”：相对资料池平均水平高出最多的维度（权重加权），而不是绝对值最大的维度——
    # 否则相关度权重最大，几乎每篇都会写成“主要因为相关度高”
    if items:
        mean = {k: sum(it["features"][k] for it in items) / len(items) for k in WEIGHTS}
        for it in items:
            gains = {k: w[k] * (it["features"][k] - mean[k]) for k in WEIGHTS}
            it["top_factor"] = max(gains, key=gains.get)
            it["weak_factor"] = min(gains, key=gains.get)
    return items


# ---------------------------------------------------------------- 榜单

def ours_order(items: list[dict]) -> list[str]:
    """我们的排序：去掉低相关和转载，按价值分降序，同一事件只保留价值最高的一篇。"""
    out, seen = [], set()
    for it in sorted(items, key=lambda x: -x["value"]):
        if it["excluded"]:
            continue
        ev = it["event_id"] or it["id"]
        if ev in seen:
            continue
        seen.add(ev)
        out.append(it["id"])
    return out


def build_list(items: list[dict], budget: float, mode: str) -> dict:
    """在阅读时间预算内贪心选取：按价值分从高到低，同一事件只取一篇，放不下的跳过并说明。
    返回 {"budget", "mode", "selected": [{id, rank, cost, why}], "excluded": [{id, why}], "used"}。"""
    by_value = sorted(items, key=lambda x: -x["value"])
    used, sel, exc, ev_first = 0.0, [], [], {}
    for it in by_value:
        cost = it["skim_min"] if mode == "skim" else max(it["read_min"], 1.0)
        if it["excluded"]:
            exc.append({"id": it["id"], "why": it["excluded"]})
            continue
        ev = it["event_id"] or it["id"]
        if ev in ev_first:
            exc.append({"id": it["id"], "why": f"与第 {ev_first[ev]} 条是同一事件，信息重复（保留价值更高的那篇）"})
            continue
        if used + cost > budget + 1e-6:
            exc.append({"id": it["id"], "why": f"时间预算不够：需 {cost:.1f} 分钟，只剩 {max(0, budget - used):.1f} 分钟"
                                              f"（价值分 {it['value']:.2f}，低于已入选的资料）"})
            continue
        used += cost
        ev_first[ev] = len(sel) + 1
        top = [r for r in it["reasons"] if r]
        sel.append({"id": it["id"], "rank": len(sel) + 1, "cost": round(cost, 1),
                    "why": f"价值分 {it['value']:.2f}，突出在{FACTOR_NAME[it['top_factor']]}"
                           f"（短板：{FACTOR_NAME[it['weak_factor']]}）；" + "；".join(top[:4])})
    return {"budget": budget, "mode": mode, "used": round(used, 1), "selected": sel, "excluded": exc}


def baseline_orders(items: list[dict]) -> dict:
    """基线：不做价值评估、不去重。按时间：发布时间倒序；按热度：热度（来源内归一化）倒序。"""
    def ts(it):
        dt = parse_date(it["published_at"])
        return dt.timestamp() if dt else 0
    return {"time": [it["id"] for it in sorted(items, key=lambda x: -ts(x))],
            "popularity": [it["id"] for it in sorted(items, key=lambda x: (-(x["popularity_norm"] or 0), -ts(x)))]}


# ---------------------------------------------------------------- 评测

def silver_label(it: dict, doc_meta: dict) -> int:
    """自动标注（银标）：大模型复核分 ≥2 视为相关；没有大模型分时用规则相关度 ≥0.6。
    注意：银标与排序共用相关度信号，会偏向我们的方法，因此报告中优先展示人工标注结果。"""
    j = (doc_meta or {}).get("judge") or {}
    if "score" in j:
        return 1 if j["score"] >= 2 else 0
    return 1 if it["features"]["relevance"] >= 0.6 else 0


def compute_metrics(orders: dict, items: dict, labels: dict, aspect_hits: dict, aspect_ids: list | None = None,
                    ks=(5, 10)) -> dict:
    """orders: {name: [doc_id...]}；items: {id: item}；labels: {doc_id: 0/1 人工}；
    aspect_hits: {doc_id: set(aspect_id)}；返回 {name: {P@5, P@10, coverage@10, dup@10, fresh@10, primary@10, labeled}}。"""
    all_aspects = set(aspect_ids or []) or (set().union(*aspect_hits.values()) if aspect_hits else set())
    n_aspects = len(all_aspects)
    now = datetime.now(timezone.utc)
    out = {}
    for name, order in orders.items():
        m = {}
        for k in ks:
            top = order[:k]
            if not top:
                m[f"P@{k}"] = None
                continue
            lab = [labels[i] if i in labels else items[i]["silver"] for i in top]
            m[f"P@{k}"] = round(sum(lab) / len(top), 3)
            m[f"human@{k}"] = sum(1 for i in top if i in labels)
        top = order[:10]
        covered = set().union(*(aspect_hits.get(i, set()) for i in top)) if top else set()
        m["coverage@10"] = round(len(covered & all_aspects) / max(len(all_aspects), 1), 3) if all_aspects else None
        seen_ev, dup = set(), 0
        for i in top:
            it = items[i]
            ev = it["event_id"] or it["id"]
            if it["duplicate_of"] or ev in seen_ev:
                dup += 1
            seen_ev.add(ev)
        m["dup@10"] = round(dup / len(top), 3) if top else None
        fresh = 0
        for i in top:
            dt = parse_date(items[i]["published_at"])
            if dt and (now - dt).days <= 7:
                fresh += 1
        m["fresh@10"] = round(fresh / len(top), 3) if top else None
        m["primary@10"] = round(sum(items[i]["primary"] for i in top) / len(top), 3) if top else None
        out[name] = m
    out["_n_aspects"] = n_aspects
    return out


def topic_labels(topic: str) -> dict:
    return {r["doc_id"]: int(r["label"]) for r in db.query("SELECT doc_id, label FROM labels WHERE topic=?", (topic,))}


def set_label(topic: str, doc_id: str, label: int | None):
    if label is None:
        db.execute("DELETE FROM labels WHERE topic=? AND doc_id=?", (topic, doc_id))
    else:
        db.execute("INSERT INTO labels(topic, doc_id, label, updated_at) VALUES (?,?,?,?) "
                   "ON CONFLICT(topic, doc_id) DO UPDATE SET label=excluded.label, updated_at=excluded.updated_at",
                   (topic, doc_id, 1 if label else 0, db.now()))


# ---------------------------------------------------------------- 发现问题

def find_problems(data: dict, top_n: int = 5, limit: int = 3) -> list[dict]:
    """检查速览榜前几名是否存在可信度问题：只有单一来源且不是一手来源、涉及有争议的事实。"""
    items = {it["id"]: it for it in data["items"]}
    out = []
    for s in data["lists"]["5"]["selected"][:top_n]:
        it = items[s["id"]]
        issue = None
        if it["disputed"]:
            issue = "涉及有争议的事实，需要找更多来源核对"
        elif not it["primary"] and it["independent"] <= 1 and not it["unverified"]:
            issue = "只有单一来源报道、不是一手来源，缺少交叉印证"
        if issue:
            out.append({"id": it["id"], "title": it["title"], "issue": issue, "rank": s["rank"],
                        "credibility": it["features"]["credibility"], "value": it["value"]})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 评估入口

def evaluate(task_id: str, topic: str, *, aspects: list[dict], min_rel: float = 0.4, pref_minutes=None,
             weights: dict | None = None, flags: dict | None = None, trigger: str = "初次评估",
             extra: dict | None = None, store: bool = True, prev: dict | None = None) -> dict:
    from .graphs.research import _doc_hits      # 避免循环导入
    pool = task_pool(task_id)
    items = score_pool(pool, min_rel=min_rel, weights=weights, flags=flags)
    metas = {d["id"]: d["meta"] or {} for d in pool}
    for it in items:
        it["silver"] = silver_label(it, metas[it["id"]])
    lists = {"5": build_list(items, 5, "skim"), "30": build_list(items, 30, "deep")}
    try:
        pm = int(float(pref_minutes)) if pref_minutes not in (None, "") else None
    except (TypeError, ValueError):
        pm = None
    if pm and pm not in (5, 30):
        lists[str(pm)] = build_list(items, pm, "skim" if pm <= 10 else "deep")
    orders = {"ours": ours_order(items), **baseline_orders(items)}
    hits = {d["id"]: {a["id"] for a in aspects if a.get("kind") not in ("any",) and _doc_hits(d, a)} for d in pool}
    data = {"trigger": trigger, "weights": {**WEIGHTS, **(weights or {})}, "min_rel": min_rel,
            "pref_minutes": pm, "items": items, "lists": lists, "orders": orders,
            "aspects": [{"id": a["id"], "name": a["name"]} for a in aspects if a.get("kind") != "any"],
            "aspect_hits": {k: sorted(v) for k, v in hits.items() if v},
            "flags": flags or {}, **(extra or {})}
    data["metrics_auto"] = compute_metrics(orders, {it["id"]: it for it in items}, {}, hits,
                                           [a["id"] for a in data["aspects"]])
    data["problems"] = find_problems(data)
    if prev:
        data["diff_from"] = prev.get("version")
        data["diff"] = {k: diff(prev, data, k) for k in data["lists"] if k in prev["lists"]}
    if store:
        v = (db.query_one("SELECT MAX(version) v FROM rankings WHERE task_id=?", (task_id,))["v"] or 0) + 1
        data["version"] = v
        db.insert("rankings", dict(id=db.new_id("rk_"), task_id=task_id, version=v, trigger=trigger,
                                   data=data, created_at=db.now()))
    return data


def latest(task_id: str) -> dict | None:
    r = db.query_one("SELECT * FROM rankings WHERE task_id=? ORDER BY version DESC LIMIT 1", (task_id,))
    return r["data"] if r else None


def history(task_id: str) -> list[dict]:
    return db.query("SELECT version, trigger, created_at, data FROM rankings WHERE task_id=? ORDER BY version",
                    (task_id,))


def diff(old: dict, new: dict, key: str = "5") -> list[dict]:
    """两个版本之间榜单的变化：新进、移出、名次升降、价值分变化。"""
    o = {s["id"]: s["rank"] for s in old["lists"][key]["selected"]}
    n = {s["id"]: s["rank"] for s in new["lists"][key]["selected"]}
    ov = {it["id"]: it for it in old["items"]}
    nv = {it["id"]: it for it in new["items"]}
    out = []
    for i in set(o) | set(n):
        a, b = o.get(i), n.get(i)
        if a == b:
            continue
        it = nv.get(i) or ov.get(i)
        out.append({"id": i, "title": it["title"], "from": a, "to": b,
                    "value_from": ov[i]["value"] if i in ov else None, "value_to": nv[i]["value"] if i in nv else None,
                    "cred_from": ov[i]["features"]["credibility"] if i in ov else None,
                    "cred_to": nv[i]["features"]["credibility"] if i in nv else None})
    out.sort(key=lambda x: (x["to"] is None, x["to"] or 99))
    return out


def with_labels(task_id: str, topic: str, data: dict) -> dict:
    """用当前的人工标注重新计算指标（不改排序）。人工标注覆盖的条目优先，其余用银标。"""
    labels = topic_labels(topic)
    items = {it["id"]: it for it in data["items"]}
    hits = {k: set(v) for k, v in (data.get("aspect_hits") or {}).items()}
    for i in items:
        hits.setdefault(i, set())
    m = compute_metrics(data["orders"], items, labels, hits, [a["id"] for a in data.get("aspects") or []])
    pool_ids = set()
    for o in data["orders"].values():
        pool_ids |= set(o[:10])
    return {"metrics": m, "labels": {i: labels[i] for i in items if i in labels},
            "pool": sorted(pool_ids), "labeled_in_pool": sum(1 for i in pool_ids if i in labels)}
