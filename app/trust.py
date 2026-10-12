"""可信度与可追溯：针对一次检索（和由它写出的文章），用实际数据回答四个问题——
资料是否可靠、筛选是否有价值、改写是否帮助理解、整个过程是否可追溯可复现；
并回答“通关后的思考”里的四个问题（每个回答都落到这次任务的具体数字和具体条目上）。"""
from __future__ import annotations

import re

from . import db, evaluate as ev, ranking, writing

HEDGE = writing.HEDGE          # 限制条件与争议：改写时最容易被“顺”掉的那类话


def caveats(text: str) -> int:
    return sum(1 for s in re.split(r"(?<=[。！？；])", text or "") if HEDGE.search(s))


def _pct(a, b):
    return round(a / b, 3) if b else None


def reliability(task_id: str, items: list[dict], facts: list[dict]) -> dict:
    """资料是否可靠：来源构成、同一事件有几个独立来源、被转载/重复的有多少、事实核实状态。"""
    docs = ranking.task_pool(task_id)
    total = len(docs)
    dups = sum(1 for d in docs if d["duplicate_of"])
    primary = sum(1 for d in docs if (d["meta"] or {}).get("primary"))
    sources = len({d["source_id"] for d in docs})
    events = {}
    # 只看和主题相关的资料：被判“相关度低”的不进事件统计，也不拿来举例
    rel_items = [it for it in items if "相关度" not in (it.get("excluded") or "")]
    for it in rel_items:
        e = it["event_id"] or it["id"]
        x = events.setdefault(e, {"docs": 0, "independent": 0, "title": it["title"], "disputed": False, "primary": False})
        x["docs"] += 1
        x["independent"] = max(x["independent"], it["independent"])
        x["disputed"] |= it["disputed"]
        x["primary"] |= it["primary"]
    multi = [e for e in events.values() if e["independent"] >= 2]
    single = [e for e in events.values() if e["independent"] <= 1 and not e["primary"]]
    top_reprint = max(events.values(), key=lambda e: e["docs"], default=None)
    vc = {}
    for f in facts:
        vc[f.get("verify") or "unverified"] = vc.get(f.get("verify") or "unverified", 0) + 1
    return {"docs": total, "duplicates": dups, "sources": sources, "primary": primary, "primary_rate": _pct(primary, total),
            "events": len(events), "events_multi": len(multi), "events_single": len(single),
            "relevant": len(rel_items), "off_topic": len(items) - len(rel_items),
            "top_reprint": top_reprint, "facts": vc,
            "examples_single": [e["title"][:50] for e in single[:3]]}


def selection(rk: dict) -> dict:
    """筛选是否有价值：资料池 → 五分钟/三十分钟清单，剔除了什么、为什么；和“按时间”“按热度”排序比。"""
    items = rk.get("items") or []
    excl = {}
    for it in items:
        if it.get("excluded"):
            k = "转载/重复" if "转载" in it["excluded"] else "相关度低" if "相关度" in it["excluded"] else "其他"
            excl[k] = excl.get(k, 0) + 1
    l5, l30 = (rk.get("lists") or {}).get("5") or {}, (rk.get("lists") or {}).get("30") or {}
    why_out = {}
    for x in l5.get("excluded") or []:
        k = "同一事件只留一篇" if "同一事件" in x["why"] else "时间预算不够" if "预算" in x["why"] else \
            "转载/重复" if "转载" in x["why"] else "相关度低" if "相关度" in x["why"] else "其他"
        why_out[k] = why_out.get(k, 0) + 1
    m = rk.get("metrics_auto") or {}
    by_id = {it["id"]: it for it in items}
    top5 = [{"rank": s["rank"], "title": by_id.get(s["id"], {}).get("title", s["id"])[:60], "why": s["why"][:160],
             "minutes": s["cost"]} for s in (l5.get("selected") or [])]
    return {"pool": len(items), "excluded": excl, "list5": len(l5.get("selected") or []), "list5_used": l5.get("used"),
            "list30": len(l30.get("selected") or []), "list5_out": why_out, "top5": top5,
            "metrics": {k: v for k, v in m.items() if not k.startswith("_")}, "problems": rk.get("problems") or [],
            "version": rk.get("version")}


def understanding(research_task: str) -> dict:
    """改写是否帮助理解：这批资料写出的文章，对比原始资料（时间、交叉印证、判断、可读性），并检查限制条件和争议有没有被“顺”掉。"""
    from .graphs.experiment import _docs_with_text
    arts = db.query("SELECT a.id, a.title, a.current_version, v.markdown, v.meta FROM articles a JOIN article_versions v "
                    "ON v.article_id=a.id AND v.version=a.current_version ORDER BY a.updated_at DESC")
    arts = [a for a in arts if (a["meta"] or {}).get("research_task") == research_task and not (a["meta"] or {}).get("experiment")]
    out = []
    for a in arts[:6]:
        meta = a["meta"] or {}
        docs = _docs_with_text(meta)
        m = ev.evaluate_version(a["markdown"], meta, docs)
        text = m["text"]
        src_caveats = sum(caveats(d.get("text") or "") for d in docs)
        disputes = [f for f in meta.get("facts") or [] if f.get("verify") == "disputed"]
        flagged = [f for f in disputes if writing.overlap(f["content"], text) > 0.3]
        heads = [s.get("heading") for s in meta.get("sections") or []]
        aud = writing.hedge_audit(meta.get("sections") or [], writing.doc_flags(research_task, meta.get("docs") or []))
        out.append({"id": a["id"], "title": a["title"], "version": a["current_version"],
                    "skill": (meta.get("skill") or {}).get("name"), "gain": m["gain"], "comprehension": m["comprehension"],
                    "fidelity": m["fidelity"], "caveats": caveats(text), "caveats_in_sources": src_caveats,
                    "disputes_known": len(disputes), "disputes_kept": len(flagged),
                    "has_uncertain_section": any(writing.UNCERTAIN_HEAD.search(h or "") for h in heads),
                    "hedge_needed": aud["needed"], "hedge_missing": aud["missing"],
                    "corroborations": [{"claim": x.get("claim"), "result": x.get("result"), "url": (x.get("doc") or {}).get("url"),
                                        "found": bool(x.get("doc"))} for x in meta.get("corroborations") or []][:6],
                    "hedge_examples": [writing._short_claim(x["sentence"]) for x in aud["missing_items"][:3]],
                    "analysis_marked": m["gain"]["analysis_paras"]})
    exps = db.query("SELECT id, topic, status, state, created_at FROM tasks WHERE graph='experiment' ORDER BY created_at DESC")
    rel = []
    for x in exps:
        st = x["state"] or {}
        if any(r.get("research_task") == research_task for r in st.get("runs") or []):
            rel.append({"id": x["id"], "topic": x["topic"], "status": x["status"], "created_at": x["created_at"],
                        "runs": len(st.get("runs") or [])})
    return {"articles": out, "experiments": rel[:5]}


def traceability(task_id: str) -> dict:
    """可追溯、可复现：每一步、每次工具调用、检查点、资料版本、排序版本、Skill 版本和指纹、文章版本都有记录。"""
    q = lambda sql, *a: (db.query_one(sql, a) or {}).get("n", 0)      # noqa: E731
    steps = q("SELECT COUNT(*) n FROM steps WHERE task_id=?", task_id)
    tools = q("SELECT COUNT(*) n FROM tool_calls WHERE task_id=?", task_id)
    cached = q("SELECT COUNT(*) n FROM tool_calls WHERE task_id=? AND status='cached'", task_id)
    cks = q("SELECT COUNT(*) n FROM checkpoints WHERE task_id=?", task_id)
    rks = q("SELECT COUNT(*) n FROM rankings WHERE task_id=?", task_id)
    writes = db.query("SELECT id, status FROM tasks WHERE graph='write' AND (parent_task_id=? OR "
                      "json_extract(params, '$.research_task')=?) AND json_extract(params, '$.experiment') IS NULL",
                      (task_id, task_id))
    arts = db.query("SELECT id, title, current_version, skill_version FROM articles WHERE task_id IN "
                    f"({','.join('?' * len(writes)) or 'NULL'})", [w["id"] for w in writes]) if writes else []
    mem_ev = q("SELECT COUNT(*) n FROM memory_events WHERE task_id=?", task_id)
    return {"steps": steps, "tool_calls": tools, "cached_calls": cached, "checkpoints": cks, "ranking_versions": rks,
            "writes": len(writes), "articles": [{"id": a["id"], "title": a["title"], "versions": a["current_version"],
                                                 "skill_version": a["skill_version"]} for a in arts],
            "memory_events": mem_ev}


def build(task_id: str) -> dict:
    t = db.query_one("SELECT id, topic, graph, status, state, params, created_at FROM tasks WHERE id=?", (task_id,))
    if not t or t["graph"] != "research":
        raise ValueError("请从一次主题检索打开")
    st = t["state"] or {}
    rep = st.get("report") or {}
    rk = ranking.latest(task_id) or {}
    facts = ((rep.get("memory") or {}).get("facts")) or []
    rel = reliability(task_id, rk.get("items") or [], facts)
    sel = selection(rk)
    und = understanding(task_id)
    tr = traceability(task_id)
    mem = rep.get("memory") or {}
    compare = rep.get("compare") or {}
    return {"task": {"id": t["id"], "topic": t["topic"], "status": t["status"], "created_at": t["created_at"]},
            "reliability": rel, "selection": sel, "understanding": und, "trace": tr,
            "memory": {"recalled": len(mem.get("recalled") or []), "corrected": mem.get("corrected", 0),
                       "corrections": (mem.get("corrections") or [])[:5], "disputes": (mem.get("disputes") or [])[:5],
                       "conflicts": mem.get("conflicts", 0), "new_facts": mem.get("new_facts", 0),
                       "stale": (mem.get("stale_from_updates") or [])[:5], "compare": compare,
                       "prefs": mem.get("pref_notes") or []},
            "quality": rep.get("quality") or {},
            "primary_search": _primary_search(t)}


def _primary_search(t) -> dict:
    """这次检索有没有专门找一手资料（“官方与一手披露”来源需要搜索 API）。"""
    from . import websearch
    used = (t.get("state") or {}).get("source_ids") or []
    return {"websearch": websearch.available(), "official_used": "official" in used}
