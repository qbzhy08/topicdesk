"""资料入库：版本化、转载归并、事件聚类、新进展/更新/背景/重复分类、复查时间。"""
from __future__ import annotations

import difflib
from collections import Counter
import re
import time
from datetime import datetime, timedelta, timezone

from .. import db
from .dedup import (canonical_url, content_hash, hamming, iso, parse_date, simhash,
                    title_sim)

KIND_RANK = {"summary": 0, "excerpt": 1, "full": 2}
NEAR_DUP = 6           # SimHash 海明距离 ≤6 视为转载/近似全文
EVENT_TITLE_SIM = 0.42  # 标题相似度阈值：同一事件的不同报道
EVENT_WINDOW = timedelta(days=4)
BACKGROUND_AGE = timedelta(days=30)


_NUM = re.compile(r"\d+(?:\.\d+)?\s*[%KkMmBb万亿]?")


def describe_change(old: str, new: str) -> dict:
    """比较两个版本：相似度、增删的数字（按出现次数）、增删的句子。
    用于判断是否实质变化，并向用户说明“哪里变了”。"""
    from ..calibrate import NOISE, params
    pu = params("update")
    a, b = NOISE.sub(" ", old[:30000]), NOISE.sub(" ", new[:30000])   # 阅读数、时间戳不参与比较
    ratio = difflib.SequenceMatcher(None, a, b, autojunk=False).quick_ratio() if a and b else 0.0
    if ratio > 0.9:
        ratio = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    # 阅读数、点赞数、时间戳、“3 分钟前”这类数字每次抓取都会变，不算内容变化
    ca, cb = Counter(_NUM.findall(a)), Counter(_NUM.findall(b))
    sa = {x.strip() for x in re.split(r"[。！？!?\n]", a) if len(x.strip()) > 6}
    sb = {x.strip() for x in re.split(r"[。！？!?\n]", b) if len(x.strip()) > 6}
    # 摘要末尾常被截断（“……查看全文”），在新文本里能找到前半句的不算删除
    removed = [x for x in sa - sb if x.rstrip("…. 查看全文阅读原文") not in b]
    return {"similarity": round(ratio, 4),
            "numbers_removed": sorted((ca - cb).elements())[:10],
            "numbers_added": sorted((cb - ca).elements())[:10],
            "sentences_added": sorted(sb - sa, key=len)[:5],
            "sentences_removed": sorted(removed, key=len)[:5],
            "significant": ratio < pu["similarity"] or
            sum(((ca - cb) + (cb - ca)).values()) >= pu["num_changed"]}


def _basis(item: dict) -> str:
    return item.get("text") or item.get("summary") or item.get("title") or ""


def _recheck_at(item: dict, url: str) -> float | None:
    """可能变化的页面安排复查：代码仓库、论文新版本、新近新闻。"""
    now = time.time()
    if "github.com" in url:
        return now + 2 * 86400
    if "arxiv.org" in url:
        return now + 7 * 86400
    pub = parse_date(item.get("published_at"))
    if pub and datetime.now(timezone.utc) - pub < timedelta(days=3):
        return now + 86400        # 刚发布的新闻 24 小时后复查是否有更正
    return None


def topic_last_run(topic: str, exclude_task: str | None = None) -> float | None:
    row = db.query_one("SELECT MAX(updated_at) t FROM tasks WHERE topic=? AND graph='research' "
                       "AND status='done' AND id!=?", (topic, exclude_task or ""))
    return row["t"] if row and row["t"] else None


TITLE_MAX = 160


def clean_title(t: str | None) -> str | None:
    """标题清洗：合并空白；去掉 GitHub 页面标题前缀；超长截断。
    有的仓库把整篇 README 填进“简介”，GitHub 页面标题会带上它，不截断就会变成几千字的标题。"""
    if not t:
        return t
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"^GitHub - ", "", t)
    if len(t) > TITLE_MAX:
        cut = t[:TITLE_MAX]
        # 尽量在句读或空格处截断
        m = re.search(r"^(.{60,})[。.!！?？;；:：,，\s]", cut)
        t = (m.group(1) if m else cut).rstrip(" ,，:：;；-—#*") + "…"
    return t


def upsert_document(item: dict, topic: str, *, task_id: str | None = None,
                    last_run_at: float | None = None) -> dict:
    """返回 {doc_id, action, novelty, relation, related_id}。
    action: new / unchanged / enriched / updated
    novelty: new(新进展) / update(内容更新) / background(背景资料) / duplicate(重复报道)
    """
    if item.get("title"):
        item = {**item, "title": clean_title(item["title"])}
    url = item["url"]
    canon = canonical_url(url)
    basis = _basis(item)
    chash, sh = content_hash(basis), simhash(basis)
    kind = item.get("content_kind") or "summary"
    pub = iso(item.get("published_at")) or item.get("published_at")
    meta_in = dict(item.get("meta") or {})
    from .sources import is_primary, self_archive   # 一手站点（政府、监管、论文、官方新闻稿……）上的页面一律标为一手资料
    meta_in.update(primary=(bool(item.get("primary")) or is_primary(url)) and not self_archive(url),
                   unreviewed=self_archive(url) or None, relevance=item.get("relevance"),
                   last_task=task_id)
    if item.get("images"):
        meta_in["image_candidates"] = item["images"]

    old = db.query_one("SELECT * FROM documents WHERE canonical_url=? AND status='active' "
                       "ORDER BY version DESC LIMIT 1", (canon,))
    if old:
        meta = {**(old["meta"] or {}), **{k: v for k, v in meta_in.items() if v is not None}}
        meta["primary"] = bool((old["meta"] or {}).get("primary") or meta_in.get("primary")) and not self_archive(url)
        meta["sources_seen"] = sorted(set((old["meta"] or {}).get("sources_seen", [old["source_id"]]))
                                      | {item.get("source_id") or "web"})
        meta.setdefault("seen_by_tasks", [])
        if task_id and task_id not in meta["seen_by_tasks"]:
            meta["seen_by_tasks"].append(task_id)
        if old["content_hash"] == chash or (KIND_RANK.get(kind, 0) < KIND_RANK.get(old["content_kind"], 0)):
            db.update("documents", "id", old["id"], {"meta": meta, "fetched_at": db.now()})
            return {"doc_id": old["id"], "action": "unchanged", "novelty": old["novelty"],
                    "relation": "seen_before", "related_id": None}
        change = describe_change(old["text"] or "", basis)
        title_only = (old["text"] or "").strip() == (old["title"] or "").strip()
        # 摘要（编辑写的导语）→ 全文：视为补全；节选 → 全文：新文本须包含旧文本，否则可能是更正
        extends_old = title_only or old["content_kind"] == "summary" or \
            (not change["numbers_removed"] and not change["sentences_removed"])
        if KIND_RANK.get(kind, 0) > KIND_RANK.get(old["content_kind"], 0) and \
                old["content_kind"] != "full" and extends_old:
            # 摘要 → 全文：新文本包含旧文本，只是补全，不算内容更新
            db.update("documents", "id", old["id"], dict(
                text=basis, content_kind=kind, content_hash=chash, simhash=sh, meta=meta,
                title=item.get("title") or old["title"], author=item.get("author") or old["author"],
                published_at=old["published_at"] or pub, fetched_at=db.now()))
            return {"doc_id": old["id"], "action": "enriched", "novelty": old["novelty"],
                    "relation": "enriched", "related_id": None}
        # 抽取方式不同（RSS 全文 vs 网页正文）时，文本天然有出入，放宽阈值避免误判为“更新”；
        # 抽取方式相同时，哪怕只改了一个数字也算实质变化（更正往往就是改数字）
        same_method = (old["meta"] or {}).get("extract_method") == meta_in.get("extract_method")
        if (not same_method and old["simhash"] and hamming(old["simhash"], sh) <= 14) or \
                (same_method and not change["significant"]):
            if change["similarity"] < 1:
                # 记下这次“判为不重要”的变化：判断校准页拿它请人确认“是不是真的不重要”
                meta["minor_changes"] = ((meta.get("minor_changes") or []) + [{"at": round(db.now()), "change": change}])[-3:]
            db.update("documents", "id", old["id"], {"meta": meta, "fetched_at": db.now()})
            return {"doc_id": old["id"], "action": "unchanged", "novelty": old["novelty"],
                    "relation": "minor_change", "related_id": None}
        # 内容实质变化 → 新版本，旧版本保留
        new_id = db.new_id("d_")
        db.insert("documents", dict(
            id=new_id, url=url, canonical_url=canon, source_id=item.get("source_id") or old["source_id"],
            topic=old["topic"], title=item.get("title") or old["title"],
            author=item.get("author") or old["author"], published_at=pub or old["published_at"],
            fetched_at=db.now(), content_kind=kind, text=basis, summary=item.get("summary"),
            content_hash=chash, simhash=sh, version=old["version"] + 1, prev_version_id=old["id"],
            duplicate_of=old["duplicate_of"], event_id=old["event_id"], novelty="update",
            locator={**(old["locator"] or {}), "url": url, "canonical": canon},
            meta={**meta, "changed_from": old["id"], "change": change},
            recheck_at=_recheck_at(item, canon), status="active"))
        db.update("documents", "id", old["id"], {"status": "superseded"})
        return {"doc_id": new_id, "action": "updated", "novelty": "update",
                "relation": "new_version", "related_id": old["id"]}

    # ---- 全新 URL：判断转载 / 同一事件 / 新进展 / 背景
    novelty, relation, related, dup_of, event_id = None, None, None, None, None
    pub_dt = parse_date(pub)
    peers = db.query("SELECT id, title, simhash, published_at, event_id, duplicate_of, "
                     "content_kind, LENGTH(text) n FROM documents WHERE topic=? AND status='active'",
                     (topic,))
    from ..calibrate import decide_event, decide_reprint, event_features, params, reprint_features
    pr, pe = params("reprint"), params("event")
    me = {"title": item.get("title") or "", "text": basis, "simhash": sh, "published_at": pub}
    if len(basis) >= 120:
        for p in peers:
            if len(basis) >= 200 and p["simhash"] and (p["n"] or 0) >= 200 and hamming(p["simhash"], sh) <= pr["hamming"]:
                dup_of = p["duplicate_of"] or p["id"]
                event_id, novelty, relation, related = p["event_id"], "duplicate", "reprint", p["id"]
                break
        if not dup_of:
            # 洗稿：逐句改写、调换语序，SimHash 认不出来；只对标题相近或指纹较近的候选逐句比对（省时间）
            near = [p for p in peers if (p["n"] or 0) >= 120 and p["simhash"] and
                    (hamming(p["simhash"], sh) <= 24 or title_sim(me["title"], p["title"] or "") >= 0.3)]
            for p in near[:30]:
                full = db.query_one("SELECT text FROM documents WHERE id=?", (p["id"],))
                f = reprint_features(me, {**p, "text": (full or {}).get("text") or ""})
                if decide_reprint(f, pr) == "rewrite":
                    dup_of = p["duplicate_of"] or p["id"]
                    event_id, novelty, relation, related = p["event_id"], "duplicate", "rewrite", p["id"]
                    meta_in["rewrite_of"] = {"doc": p["id"], **f}
                    break
    if not dup_of and item.get("title"):
        best, best_s = None, 0.0
        for p in peers:
            s = title_sim(item["title"], p["title"] or "")
            if s < min(pe["title"], pe["title_lo"]) or s <= best_s:
                continue
            f = {"title_sim": s, "days": None, "num_shared": 0}
            pd = parse_date(p["published_at"])
            if pub_dt and pd:
                f["days"] = abs(pub_dt - pd).total_seconds() / 86400
            if s < pe["title"]:
                # 标题只是有点像：再看两篇是否共享关键数字（同一件事的报道通常引同一组数字）
                full = db.query_one("SELECT title, text, published_at FROM documents WHERE id=?", (p["id"],))
                f = {**event_features(me, full or {}), "days": f["days"]}
            if decide_event(f, pe) != "same":
                continue
            best, best_s = p, s
        if best:
            event_id, novelty, relation, related = best["event_id"], "duplicate", "same_event", best["id"]

    doc_id = db.new_id("d_")
    if novelty is None:
        if pub_dt and (datetime.now(timezone.utc) - pub_dt > BACKGROUND_AGE or
                       (last_run_at and pub_dt.timestamp() < last_run_at - 86400)):
            novelty = "background"
        else:
            novelty = "new"
    meta_in.update(relation=relation, related_id=related, sources_seen=[item.get("source_id") or "web"],
                   seen_by_tasks=[task_id] if task_id else [])
    db.insert("documents", dict(
        id=doc_id, url=url, canonical_url=canon, source_id=item.get("source_id"), topic=topic,
        title=item.get("title"), author=item.get("author"), published_at=pub,
        fetched_at=db.now(), content_kind=kind, text=basis, summary=item.get("summary"),
        content_hash=chash, simhash=sh, version=1, duplicate_of=dup_of,
        event_id=event_id or doc_id, novelty=novelty,
        locator={"url": url, "canonical": canon, **({"via": meta_in["via"]} if meta_in.get("via") else {})},
        meta=meta_in, recheck_at=_recheck_at(item, canon), status="active"))
    return {"doc_id": doc_id, "action": "new", "novelty": novelty, "relation": relation,
            "related_id": related}


def article_to_item(art: dict, url: str, source_id: str = "web", base: dict | None = None) -> dict:
    """把 fetch_page 的结果转成入库 Item，保留原条目（如 RSS/HN）中的已知字段。"""
    base = base or {}
    meta = dict(base.get("meta") or {})
    meta.update(via_browser=art.get("via_browser"), fetch_attempts=art.get("attempts"),
                site_name=art.get("site_name"), license=art.get("license"),
                cover=art.get("cover"), out_links=art.get("links", [])[:30])
    if art.get("doc_type"):
        meta["doc_type"] = art["doc_type"]
    before = base.get("text") or base.get("summary") or ""
    if base.get("url") and before is not None:
        # 抓取前已有的内容（RSS / 搜索结果里的摘要），用于说明“抓全文带来了什么”
        meta["before_fetch"] = {"kind": base.get("content_kind") or ("summary" if before else "title"),
                                "chars": len(before), "source": base.get("source_id")}
    return {
        "url": base.get("url") or art.get("final_url") or url,
        "title": art.get("title") or base.get("title"),
        "author": art.get("author") or base.get("author"),
        "published_at": base.get("published_at") or art.get("published_at"),
        "summary": base.get("summary") or (art.get("description") or art["text"][:300]),
        "text": art["text"] or base.get("text"), "content_kind": art["content_kind"],
        "images": art.get("images") or [], "source_id": base.get("source_id") or source_id,
        "primary": base.get("primary", False), "relevance": base.get("relevance"), "meta": meta,
    }
