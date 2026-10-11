"""主题事实记忆：抽取 → 合并（支持/冲突/更正）→ 核验 → 交付记录。

关键原则
- 事实必须有原文证据：抽取时要求给出原文引句，引句在原文中找不到就丢弃（防止模型编造）。
- 独立来源计数：转载（duplicate_of）只算一个来源；系统生成内容永远不算证据。
  所以“一百篇文章转述同一条未经证实的消息”只算 1 个独立来源，仍是“待核实”。
- 更正优先：同一 URL 的新版本推翻了旧数字 → 旧事实被更正（superseded），派生的交付记录标记“需更正”。
- 冲突核验：两条说法数字不同且无法判定谁是更正 → 双方标“有争议”，建立争议记忆；
  依据一手来源与独立来源数量裁决，裁决不了就保留争议，并在下次检索时优先补充核验。
"""
from __future__ import annotations

import re
import time

from . import db, llm
from . import memory as mem
from .collect.dedup import title_sim
from .collect.sources import is_primary

NUM = re.compile(r"\d+(?:\.\d+)?\s*(?:%|K|k|M|B|万|亿|倍|个|款|项|篇|tokens?|token)?")
SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])\s*|\n+")


ORDINAL = re.compile(r"(第\s*\d+\s*[段章节条部步个]|(?:paragraph|part|section|step|fig\.?|figure|table|图|表)\s*\d+)", re.I)


def numbers(text: str) -> list[str]:
    """“有意义的数字”：带单位的数、小数、三位以上的数。序号（第 2 段、Figure 3）不算，避免误判冲突。"""
    t = ORDINAL.sub(" ", text or "")
    out = set()
    for n in NUM.findall(t):
        n = re.sub(r"\s+", "", n)
        if not re.search(r"\d", n):
            continue
        if re.search(r"[^\d.]", n) or "." in n or len(n) >= 3:
            out.add(n)
    return sorted(out)


def skeleton(text: str) -> str:
    """去掉数字和标点后的骨架，用来判断两句话是否在说同一件事。"""
    s = NUM.sub("#", ORDINAL.sub("", text or ""))
    return re.sub(r"[\s\W_]+", "", s.lower())


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


# ---------------------------------------------------------------- 抽取

EXTRACT_PROMPT = """从下面几篇资料中抽取与主题「{topic}」直接相关、可核验的关键事实（每篇 1-3 条）。
要求：
- 只抽取资料里明确写出的事实（发布、数字、评测结果、结论、更正），不要推断，不要合并多篇。
- text 用一句中文客观复述；quote 必须是原文中连续出现的原句片段（10-60 字，原样复制）。
- time_sensitive：数字、排名、“最新/目前”类结论为 true。
- subject：这条事实在说什么对象/指标（2-8 个字），用于判断不同资料是否在说同一件事。
只输出 JSON：{{"claims":[{{"doc":"资料编号","text":"...","quote":"...","subject":"...","time_sensitive":true}}]}}

{docs}"""


def _rule_claims(doc: dict, terms: list[str], limit: int = 3) -> list[dict]:
    out, seen = [], set()
    for s in SENT_SPLIT.split(doc.get("text") or ""):
        s = s.strip().lstrip("#").strip().rstrip("。；;")
        if skeleton(s) in seen:          # 同一篇里几乎一样的句子只取一次
            continue
        seen.add(skeleton(s))
        if not (12 <= len(s) <= 120) or not numbers(s):
            continue
        low = s.lower()
        if terms and not any(t.lower() in low for t in terms):
            continue
        out.append({"doc": doc["id"], "text": s, "quote": s, "subject": skeleton(s)[:8],
                    "time_sensitive": True, "by": "rules"})
        if len(out) >= limit:
            break
    return out


def extract_claims(ctx, docs: list[dict], terms: list[str]) -> list[dict]:
    """有 LLM 时批量抽取，并校验引句真实存在；否则用规则（含数字且命中主题词的句子）。"""
    claims = []
    if llm.available() and not ctx.params.get("no_llm"):
        for i in range(0, len(docs), 5):
            batch = docs[i:i + 5]
            body = "\n\n".join(f"[{d['id']}] 标题：{d['title']}\n{(d['text'] or '')[:1800]}" for d in batch)
            try:
                res = ctx.tool("llm", messages=[
                    {"role": "system", "content": "你只输出 JSON。"},
                    {"role": "user", "content": EXTRACT_PROMPT.format(topic=ctx.topic, docs=body)}],
                    max_tokens=1800)
                for c in (res.get("data") or {}).get("claims", []):
                    d = next((x for x in batch if x["id"] == c.get("doc")), None)
                    q = (c.get("quote") or "").strip()
                    if not d or not c.get("text") or len(q) < 6 or _norm(q) not in _norm(d["text"]):
                        continue      # 找不到原文引句：可能是模型编造，丢弃
                    claims.append({**c, "by": "llm"})
            except Exception as e:
                ctx.observe(f"LLM 抽取失败（{str(e)[:60]}），该批改用规则")
                for d in batch:
                    claims += _rule_claims(d, terms)
    else:
        for d in docs:
            claims += _rule_claims(d, terms)
    return claims


# ---------------------------------------------------------------- 核验

def _doc(doc_id):
    return db.query_one("SELECT id, url, source_id, duplicate_of, prev_version_id, version, status, "
                        "published_at, meta FROM documents WHERE id=?", (doc_id,))


def verify(mid: str, task_id=None) -> dict:
    """重新计算一条事实的证据强度：独立来源数、是否有一手来源、转载数。"""
    m = mem.get(mid)
    ev = db.query("SELECT doc_id, stance FROM evidence WHERE claim_id=?", (mid,))
    roots, primary, reprints, generated, refute = set(), False, 0, 0, 0
    for e in ev:
        d = _doc(e["doc_id"])
        if not d:
            continue
        meta = d["meta"] or {}
        if d["source_id"] == "generated" or meta.get("generated"):
            generated += 1          # 系统生成内容不能当证据
            continue
        if e["stance"] == "refute":
            refute += 1
            continue
        root = d["duplicate_of"] or d["id"]
        if root in roots or d["duplicate_of"]:
            reprints += 1          # 转载：同一个根来源只算一次
        roots.add(root)
        primary = primary or bool(meta.get("primary")) or is_primary(d["url"])
    n = len(roots)
    meta = dict(m["meta"] or {})
    old_verify = meta.get("verify")
    if meta.get("verify") not in ("disputed", "refuted"):
        meta["verify"] = "supported" if (primary or n >= 2) else "unverified"
    meta.update(independent=n, primary=primary, reprints=reprints, generated_ignored=generated,
                refuting=refute, evidence_total=len(ev))
    conf = round(1 - 0.5 ** (n + (1 if primary else 0)), 3)
    mem.update(mid, {"meta": meta, "confidence": conf}, "verify",
               {"independent": n, "primary": primary, "verify": meta["verify"], "was": old_verify},
               task_id=task_id)
    return meta


def add_evidence(mid: str, doc_id: str, snippet: str, stance: str = "support") -> bool:
    if db.query_one("SELECT 1 FROM evidence WHERE claim_id=? AND doc_id=?", (mid, doc_id)):
        return False
    db.insert("evidence", dict(claim_id=mid, doc_id=doc_id, snippet=snippet[:300], stance=stance,
                               created_at=db.now()))
    return True


def _is_newer_version(new_doc_id: str, old_mid: str) -> str | None:
    """new_doc 是否是旧事实某个证据文档的后续版本（同一 URL 的更正）。返回旧文档 id。"""
    new = _doc(new_doc_id)
    seen, cur = set(), new
    olds = {e["doc_id"] for e in db.query("SELECT doc_id FROM evidence WHERE claim_id=?", (old_mid,))}
    while cur and cur.get("prev_version_id") and cur["prev_version_id"] not in seen:
        seen.add(cur["prev_version_id"])
        if cur["prev_version_id"] in olds:
            return cur["prev_version_id"]
        cur = _doc(cur["prev_version_id"])
    return None


def _same_root(doc_id: str, mid: str) -> bool:
    d = _doc(doc_id)
    if not d:
        return False
    root = d["duplicate_of"] or d["id"]
    for e in db.query("SELECT doc_id FROM evidence WHERE claim_id=? AND stance='support'", (mid,)):
        o = _doc(e["doc_id"])
        if o and (o["duplicate_of"] or o["id"]) == root:
            return True
    return False


# ---------------------------------------------------------------- 合并

def merge_claim(topic: str, c: dict, task_id: str, stats: dict) -> str:
    sk, nums = skeleton(c["text"]), numbers(c["text"])
    facts = [m for m in mem.list_memories(scope="topic", topic=topic, kind="fact", status="alive")]
    best, best_s = None, 0.0
    for f in facts:
        s = title_sim(sk, skeleton(f["content"]))
        if s > best_s:
            best, best_s = f, s
    recheck = time.time() + 7 * 86400 if c.get("time_sensitive") else None
    if best and best_s >= 0.55:
        fnums = numbers(best["content"])
        if not nums or not fnums or set(nums) == set(fnums):
            if add_evidence(best["id"], c["doc"], c["quote"]):
                mem.event(best["id"], "support", {"doc": c["doc"]}, task_id)
                stats["supported"] = stats.get("supported", 0) + 1
            if best["status"] == "stale" and set(nums) == set(fnums):
                mem.update(best["id"], {"status": "active", "note": "复查后仍成立",
                                        "recheck_at": recheck}, "recheck_ok", {"doc": c["doc"]},
                           task_id=task_id)
            verify(best["id"], task_id)
            return best["id"]
        old_doc = _is_newer_version(c["doc"], best["id"])
        same_src = _same_root(c["doc"], best["id"])
        if not old_doc and same_src:
            # 同一篇资料（或其转载）里的不同数字是在说不同的点，不构成冲突：作为新事实保存
            nid = mem.add("topic", "fact", c["text"], topic=topic, mkey=sk[:40],
                          source={"doc": c["doc"], "task": task_id, "by": c.get("by")},
                          time_sensitive=bool(c.get("time_sensitive")), recheck_at=recheck,
                          meta={"verify": "unverified", "subject": c.get("subject")}, task_id=task_id)
            add_evidence(nid, c["doc"], c["quote"])
            verify(nid, task_id)
            stats["new_facts"] = stats.get("new_facts", 0) + 1
            return nid
        if old_doc:
            # 同一来源发布了更正：旧事实被更正，派生的交付标记需更正
            r = mem.correct(best["id"], c["text"], actor="system",
                            reason=f"来源更正：{'、'.join(sorted(set(fnums) - set(nums)))} → "
                                   f"{'、'.join(sorted(set(nums) - set(fnums)))}",
                            meta_patch={"verify": "unverified", "subject": c.get("subject"),
                                        "corrected_doc": c["doc"], "old_doc": old_doc},
                            task_id=task_id)
            db.execute("DELETE FROM evidence WHERE claim_id=? AND doc_id=?", (r["new"], old_doc))
            add_evidence(r["new"], old_doc, "旧版本（已被更正）", "refute")
            add_evidence(r["new"], c["doc"], c["quote"])
            verify(r["new"], task_id)
            stats["corrected"] = stats.get("corrected", 0) + 1
            stats.setdefault("corrections", []).append(
                {"old": best["content"], "new": c["text"], "old_id": best["id"], "new_id": r["new"],
                 "stale_deliveries": r["stale"]})
            return r["new"]
        # 不同来源说法不同：冲突
        nid = mem.add("topic", "fact", c["text"], topic=topic, mkey=sk[:40],
                      source={"doc": c["doc"], "task": task_id, "by": c.get("by")},
                      time_sensitive=bool(c.get("time_sensitive")), recheck_at=recheck,
                      meta={"verify": "disputed", "subject": c.get("subject")}, task_id=task_id)
        add_evidence(nid, c["doc"], c["quote"])
        meta = dict(best["meta"] or {})
        meta["verify"] = "disputed"
        mem.update(best["id"], {"meta": meta}, "conflict", {"with": nid}, task_id=task_id)
        pair = sorted([best["id"], nid])
        mem.add("topic", "dispute", f"「{best['content']}」与「{c['text']}」说法不一致",
                topic=topic, mkey="|".join(pair), derived_from=pair,
                meta={"facts": pair, "subject": c.get("subject") or best["meta"].get("subject"),
                      "query": re.sub(r"\d.*$", "", c["text"])[:16] or c["text"][:16]},
                task_id=task_id)
        verify(nid, task_id)
        verify(best["id"], task_id)
        stats["conflicts"] = stats.get("conflicts", 0) + 1
        return nid
    nid = mem.add("topic", "fact", c["text"], topic=topic, mkey=sk[:40],
                  source={"doc": c["doc"], "task": task_id, "by": c.get("by")},
                  time_sensitive=bool(c.get("time_sensitive")), recheck_at=recheck,
                  meta={"verify": "unverified", "subject": c.get("subject")}, task_id=task_id)
    add_evidence(nid, c["doc"], c["quote"])
    verify(nid, task_id)
    stats["new_facts"] = stats.get("new_facts", 0) + 1
    return nid


def resolve_disputes(topic: str, task_id: str) -> list[dict]:
    """裁决争议：一手来源 + 独立来源数 + 是否为更正后的说法。差距足够大才裁决，否则保留争议。"""
    out = []
    for dsp in mem.list_memories(scope="topic", topic=topic, kind="dispute", status="alive"):
        ids = (dsp["meta"] or {}).get("facts") or dsp["derived_from"]
        facts = [mem.get(i) for i in ids]
        facts = [f for f in facts if f and f["status"] in ("active", "stale")]
        if len(facts) < 2:
            mem.update(dsp["id"], {"status": "resolved", "note": "一方已失效"}, "resolve", {}, task_id=task_id)
            continue

        def score(f):
            m = f["meta"] or {}
            return (2 if m.get("primary") else 0) + m.get("independent", 0) + (2 if m.get("corrected_doc") else 0)
        a, b = sorted(facts, key=score, reverse=True)[:2]
        sa, sb = score(a), score(b)
        if sa - sb >= 2:
            for f, v in ((a, "supported"), (b, "refuted")):
                meta = dict(f["meta"] or {})
                meta["verify"] = v
                mem.update(f["id"], {"meta": meta}, "verify",
                           {"by": "dispute_resolution", "score": score(f)}, task_id=task_id)
            mem.update(dsp["id"], {"status": "resolved",
                                   "note": f"采信「{a['content'][:40]}」（证据分 {sa} vs {sb}）"},
                       "resolve", {"winner": a["id"], "loser": b["id"]}, task_id=task_id)
            out.append({"dispute": dsp["id"], "winner": a["content"], "loser": b["content"],
                        "score": [sa, sb]})
        else:
            out.append({"dispute": dsp["id"], "open": True, "content": dsp["content"], "score": [sa, sb]})
    return out


def propagate_doc_updates(topic: str, task_id: str) -> list[dict]:
    """资料出现新版本：旧版本支撑的事实，如果新版本里已找不到对应数字，标记需复查（等待更正合并）。"""
    out = []
    rows = db.query("SELECT id, prev_version_id, text, meta FROM documents WHERE topic=? AND "
                    "status='active' AND prev_version_id IS NOT NULL AND meta LIKE ?",
                    (topic, f"%{task_id}%"))
    for d in rows:
        for e in db.query("SELECT claim_id FROM evidence WHERE doc_id=? AND stance='support'",
                          (d["prev_version_id"],)):
            f = mem.get(e["claim_id"])
            if not f or f["status"] not in ("active",):
                continue
            missing = [n for n in numbers(f["content"]) if _norm(n) not in _norm(d["text"])]
            if missing:
                change = (d["meta"] or {}).get("change") or {}
                mem.update(f["id"], {"status": "stale",
                                     "note": f"来源已更新，{'、'.join(missing)} 不再出现于新版本"},
                           "stale", {"doc": d["id"], "change": change}, task_id=task_id)
                affected = mem.cascade(f["id"], "stale", "所依据的事实来源已更新", task_id=task_id)
                out.append({"fact": f["content"], "missing": missing, "doc": d["id"], "stale": affected})
            else:
                add_evidence(f["id"], d["id"], "新版本仍支持")
    return out


def record_delivery(topic: str, task_id: str, user_id: str, summary: str, fact_ids: list[str],
                    doc_ids: list[str], meta: dict) -> str:
    return mem.add("topic", "delivery", summary, user_id=user_id, topic=topic,
                   source={"task": task_id}, derived_from=fact_ids + doc_ids[:60],
                   meta={**meta, "task": task_id, "docs": doc_ids}, task_id=task_id)
