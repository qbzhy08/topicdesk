"""Flask API + 静态前端。

前端用轮询读取轨迹（/trace?after=seq），刷新页面后从数据库重建全部进度。
"""
from __future__ import annotations

import re
import threading
import time

from flask import Flask, abort, jsonify, request, send_from_directory

from . import config, db, graphs, llm  # noqa: F401  导入即注册编排图
from . import memory as mem
from . import domains as domains_mod
from . import ranking, websearch
from .collect import sources as sources_mod
from .collect.dedup import canonical_url
from .config import ROOT
from .harness import GRAPHS, create_task, resolve_hitl, rewind, run_task, runner
from .graphs import experiment as E

app = Flask(__name__, static_folder=str(ROOT / "app" / "static"), static_url_path="/static")


def _task_or_404(tid):
    t = db.query_one("SELECT * FROM tasks WHERE id=?", (tid,))
    if t is None:
        abort(404)
    return t


def _public_state(state: dict) -> dict:
    """前端展示用：隐藏以下划线开头的内部键，但保留人工操作日志。"""
    out = {k: v for k, v in state.items() if not k.startswith("_")}
    for k in ("_budget_exhausted", "_human_log"):
        if k in state:
            out[k] = state[k]
    return out


@app.get("/")
def index():
    # 首页不允许浏览器缓存：否则更新后仍引用旧版 app.js（脚本本身带版本号，可以长期缓存）
    r = send_from_directory(app.static_folder, "index.html", max_age=0)
    r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return r


@app.get("/api/graphs")
def list_graphs():
    return jsonify([{"name": g.name, "nodes": list(g.nodes), "start": g.start}
                    for g in GRAPHS.values()])


@app.post("/api/tasks")
def create():
    body = request.get_json(force=True)
    topic = (body.get("topic") or "").strip()
    if not topic:
        return jsonify(error="topic 不能为空"), 400
    graph = body.get("graph", "demo")
    if graph not in GRAPHS:
        return jsonify(error=f"未知流程 {graph}"), 400
    tid = create_task(graph, topic, params=body.get("params") or {},
                      user_id=body.get("user_id", "default"), budget=body.get("budget"))
    runner.start(tid)
    return jsonify(id=tid), 201


@app.get("/api/tasks")
def list_tasks():
    rows = db.query("SELECT id, graph, topic, status, current_node, usage, error, created_at, parent_task_id, "
                    "json_extract(params, '$.skill') AS skill, json_extract(params, '$.research_task') AS research_task, "
                    "updated_at FROM tasks WHERE graph NOT IN ('revise') AND json_extract(params, '$.experiment') IS NULL "
                    "ORDER BY created_at DESC LIMIT 100")   # 审阅修改记在文章版本里
    return jsonify(rows)


@app.get("/api/tasks/<tid>")
def get_task(tid):
    t = _task_or_404(tid)
    t["state"] = _public_state(t["state"])
    t["hitl"] = db.query("SELECT * FROM hitl_requests WHERE task_id=? ORDER BY created_at",
                         (tid,))
    t["running"] = runner.is_running(tid)
    t["checkpoints"] = db.query_one("SELECT COUNT(*) n FROM checkpoints WHERE task_id=?",
                                    (tid,))["n"]
    return jsonify(t)


@app.get("/api/tasks/<tid>/trace")
def trace(tid):
    _task_or_404(tid)
    after = int(request.args.get("after", 0))
    steps = db.query("SELECT * FROM steps WHERE task_id=? AND id>? ORDER BY id", (tid, after))
    if steps:
        ids = [s["id"] for s in steps]
        marks = ",".join("?" * len(ids))
        tcs = db.query(f"SELECT id, step_id, tool, input, output, status, error, latency_ms, cost "
                       f"FROM tool_calls WHERE step_id IN ({marks}) ORDER BY id", ids)
        by_step: dict[int, list] = {}
        for c in tcs:
            out = c.get("output")
            if isinstance(out, (dict, list)):
                s = db.dumps(out)
                c["output_preview"] = s[:600] + ("…" if len(s) > 600 else "")
                c.pop("output")
            by_step.setdefault(c["step_id"], []).append(c)
        for s in steps:
            s["tool_calls"] = by_step.get(s["id"], [])
    return jsonify(steps)


@app.get("/api/tasks/<tid>/checkpoints")
def checkpoints(tid):
    _task_or_404(tid)
    rows = db.query("SELECT id, step_seq, next_node, usage, created_at FROM checkpoints "
                    "WHERE task_id=? ORDER BY id", (tid,))
    return jsonify(rows)


@app.post("/api/tasks/<tid>/resume")
def resume(tid):
    t = _task_or_404(tid)
    if t["status"] in ("done", "cancelled"):
        return jsonify(error=f"任务已{t['status']}"), 400
    if t["status"] == "waiting_human":
        return jsonify(error="任务在等待人工答复"), 400
    started = runner.start(tid)
    return jsonify(started=started)


@app.post("/api/tasks/<tid>/cancel")
def cancel(tid):
    _task_or_404(tid)
    runner.cancel(tid)
    return jsonify(ok=True)


@app.post("/api/tasks/<tid>/rewind")
def do_rewind(tid):
    _task_or_404(tid)
    if runner.is_running(tid):
        return jsonify(error="任务运行中，请先取消或等待结束"), 409
    body = request.get_json(force=True)
    try:
        rewind(tid, body["node"], body.get("invalidate") or [], body.get("note", ""))
    except KeyError as e:
        return jsonify(error=f"未知节点 {e}"), 400
    runner.start(tid)
    return jsonify(ok=True)


@app.post("/api/hitl/<rid>")
def answer_hitl(rid):
    body = request.get_json(force=True)
    try:
        tid = resolve_hitl(rid, body.get("response"))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    runner.start(tid)
    return jsonify(task_id=tid)


# ---------------------------------------------------------------- 资料库

DOC_LIST_COLS = ("id, url, canonical_url, source_id, topic, title, author, published_at, fetched_at, "
                 "content_kind, novelty, version, duplicate_of, event_id, status, LENGTH(text) chars, "
                 "json_extract(meta,'$.primary') primary_src, json_extract(meta,'$.relevance') relevance, "
                 "json_extract(meta,'$.relation') relation, json_extract(meta,'$.via_browser') via_browser, "
                 "json_extract(meta,'$.judge.reason') judge_reason, json_extract(meta,'$.extract_method') how")


@app.get("/api/topics")
def topics():
    return jsonify(db.query("SELECT topic, COUNT(*) n, MAX(fetched_at) last FROM documents "
                            "WHERE status='active' GROUP BY topic ORDER BY last DESC"))


@app.get("/api/docs")
def list_docs():
    a = request.args
    where, args = ["1=1"], []
    if a.get("topic"):
        where.append("topic=?"); args.append(a["topic"])
    if a.get("task"):
        where.append("meta LIKE ?"); args.append(f"%{a['task']}%")
    if a.get("status", "active") != "all":
        where.append("status=?"); args.append(a.get("status", "active"))
    for k in ("novelty", "source_id", "content_kind"):
        if a.get(k):
            where.append(f"{k}=?"); args.append(a[k])
    if a.get("min_rel"):
        where.append("COALESCE(json_extract(meta,'$.relevance'), 1) >= ?"); args.append(float(a["min_rel"]))
    how = a.get("how")
    if how == "browser":
        where.append("json_extract(meta,'$.via_browser')=1")
    elif how in ("api", "rss", "websearch", "html", "human"):
        where.append("json_extract(meta,'$.extract_method')=?"); args.append(how)
        if how == "html":
            where.append("COALESCE(json_extract(meta,'$.via_browser'),0)=0")
    if a.get("hide_dup") == "1":
        where.append("duplicate_of IS NULL")
    if a.get("q"):
        where.append("(title LIKE ? OR text LIKE ?)"); args += [f"%{a['q']}%"] * 2
    order = {"time": "COALESCE(published_at,'') DESC", "fetched": "fetched_at DESC",
             "relevance": "relevance DESC"}.get(a.get("sort", "time"), "fetched_at DESC")
    rows = db.query(f"SELECT {DOC_LIST_COLS} FROM documents WHERE {' AND '.join(where)} "
                    f"ORDER BY {order} LIMIT 500", args)
    if rows:
        ids = [r["id"] for r in rows]
        cnt = {r["doc_id"]: r for r in db.query(
            f"SELECT doc_id, SUM(status='kept') kept, SUM(status='rejected') rejected FROM images "
            f"WHERE doc_id IN ({','.join('?' * len(ids))}) GROUP BY doc_id", ids)}
        for r in rows:
            c = cnt.get(r["id"]) or {}
            r["images_kept"], r["images_rejected"] = c.get("kept") or 0, c.get("rejected") or 0
    return jsonify(rows)


@app.get("/api/docs/methods")
def doc_methods():
    """各获取方式的资料篇数（范围与资料库当前的主题 / 任务筛选一致），用于筛选框显示数量。"""
    a = request.args
    where, args = ["status='active'"], []
    if a.get("topic"):
        where.append("topic=?"); args.append(a["topic"])
    if a.get("task"):
        where.append("meta LIKE ?"); args.append(f"%{a['task']}%")
    w = " AND ".join(where)
    out = {r["m"] or "other": r["n"] for r in db.query(
        f"SELECT json_extract(meta,'$.extract_method') m, COUNT(*) n FROM documents WHERE {w} GROUP BY 1", args)}
    out["browser"] = db.query_one(f"SELECT COUNT(*) n FROM documents WHERE {w} AND json_extract(meta,'$.via_browser')=1", args)["n"]
    out["html"] = db.query_one(f"SELECT COUNT(*) n FROM documents WHERE {w} AND json_extract(meta,'$.extract_method')='html' "
                               "AND COALESCE(json_extract(meta,'$.via_browser'),0)=0", args)["n"]
    return jsonify(out)


@app.get("/api/docs/<did>")
def get_doc(did):
    d = db.query_one("SELECT * FROM documents WHERE id=?", (did,))
    if d is None:
        abort(404)
    d["images"] = db.query("SELECT * FROM images WHERE doc_id=? ORDER BY status, position", (did,))
    versions = db.query("SELECT id, version, fetched_at, status, novelty, json_extract(meta,'$.change') change "
                        "FROM documents WHERE canonical_url=? ORDER BY version", (d["canonical_url"],))
    for v in versions:
        if isinstance(v.get("change"), str):
            import json as _j
            v["change"] = _j.loads(v["change"])
    d["versions"] = versions
    root = d["duplicate_of"] or d["id"]
    d["reprints"] = db.query(f"SELECT {DOC_LIST_COLS} FROM documents WHERE (duplicate_of=? OR id=?) "
                             "AND id!=? AND status='active'", (root, root, did))
    d["same_event"] = db.query(f"SELECT {DOC_LIST_COLS} FROM documents WHERE event_id=? AND id!=? "
                               "AND status='active' AND (duplicate_of IS NULL OR duplicate_of!=?)",
                               (d["event_id"], did, root))
    src = db.query_one("SELECT id, name, method FROM sources WHERE id=?", (d["source_id"],))
    d["source"] = src
    (d.get("meta") or {}).pop("image_candidates", None)
    return jsonify(d)


@app.post("/api/docs/<did>/paste")
def paste_doc(did):
    """抓取失败时由用户粘贴正文：作为人工提供的全文入库（新版本或补全），并记录来源是人工。"""
    from .collect.store import upsert_document
    d = db.query_one("SELECT * FROM documents WHERE id=?", (did,))
    if d is None:
        abort(404)
    text = ((request.get_json(silent=True) or {}).get("text") or "").strip()
    if len(text) < 50:
        return jsonify(error="正文太短（至少 50 字）"), 400
    meta = {"provided_by": "human", "extract_method": "human",
            "fetch_attempts": ((d["meta"] or {}).get("fetch_attempts") or []) + [{"mode": "human", "ok": True, "chars": len(text)}]}
    r = upsert_document({"url": d["url"], "title": d["title"], "author": d["author"], "published_at": d["published_at"],
                         "text": text, "content_kind": "full", "source_id": d["source_id"],
                         "primary": (d["meta"] or {}).get("primary"), "relevance": (d["meta"] or {}).get("relevance"),
                         "meta": meta}, d["topic"])
    return jsonify(r)


@app.get("/media/<path:p>")
def media(p):
    return send_from_directory(config.DATA_DIR, p)


# ---------------------------------------------------------------- 信息源

@app.get("/api/sources")
def get_sources():
    rows = sources_mod.list_sources(enabled_only=False)
    stats = {r["source_id"]: r for r in db.query(
        "SELECT source_id, COUNT(*) n, MAX(fetched_at) last FROM documents WHERE status='active' "
        "GROUP BY source_id")}
    for r in rows:
        r["docs"] = (stats.get(r["id"]) or {}).get("n", 0)
    return jsonify(rows)


@app.post("/api/sources")
def add_source():
    b = request.get_json(force=True)
    feed = (b.get("feed") or "").strip()
    name = (b.get("name") or "").strip()
    if not feed.startswith("http") or not name:
        return jsonify(error="需要名称和 RSS 地址"), 400
    if not [d for d in b.get("domains") or [] if d in domains_mod.ids()]:
        return jsonify(error="请至少选一个领域"), 400
    sid = "rss_" + canonical_url(feed).split("//")[1].replace("/", "_").replace(".", "_")[:40]
    if db.query_one("SELECT id FROM sources WHERE id=?", (sid,)):
        return jsonify(error="该 RSS 已存在"), 400
    db.insert("sources", dict(id=sid, name=name, method="rss", base_url=feed,
                              config={"feed": feed, "full_text": bool(b.get("full_text")),
                                      "lang": b.get("lang", "zh"),
                                      "domains": b.get("domains") or ["general"],
                                      **({"search_feed": b["search_feed"]} if "{q}" in (b.get("search_feed") or "") else {}),
                                      "detail": "browser" if b.get("browser") else "html"},
                              enabled=1, health={}))
    return jsonify(id=sid), 201


@app.patch("/api/sources/<sid>")
def patch_source(sid):
    b = request.get_json(force=True)
    if "enabled" in b:
        db.update("sources", "id", sid, {"enabled": 1 if b["enabled"] else 0})
    if "domains" in b:
        row = db.query_one("SELECT config FROM sources WHERE id=?", (sid,))
        if not row:
            abort(404)
        valid = domains_mod.ids()
        doms = [d for d in b["domains"] or [] if d in valid]
        if not doms:
            return jsonify(error="至少保留一个领域标签"), 400
        # domains_custom：用户改过，重启时不再用内置默认值覆盖
        db.update("sources", "id", sid, {"config": {**(row["config"] or {}), "domains": doms, "domains_custom": True}})
    return jsonify(ok=True)


# ---------------------------------------------------------------- 领域标签

@app.get("/api/domains")
def list_domains():
    srcs = db.query("SELECT id, config FROM sources WHERE enabled=1")
    out = []
    for d in domains_mod.all_domains():
        n = sum(1 for x in srcs if d["id"] in ((x["config"] or {}).get("domains") or []))
        out.append({**d, "sources": n})
    return jsonify(out)


DEFAULT_TAG_SOURCES = ("zhihu", "xiaohongshu", "douyin", "bilibili")


@app.post("/api/domains")
def add_domain():
    """新建领域标签。sources 不传时，默认挂到知乎、小红书、抖音、B站（全网搜索和公众号本来就适用于所有领域）。"""
    b = request.get_json(force=True)
    try:
        d = domains_mod.add(b.get("name"), b.get("hints"), b.get("desc"))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    targets = b.get("sources")
    if targets is None:
        targets = list(DEFAULT_TAG_SOURCES)
    tagged = []
    for sid in targets:
        row = db.query_one("SELECT config FROM sources WHERE id=?", (sid,))
        if not row:
            continue
        cfg = row["config"] or {}
        doms = list(dict.fromkeys((cfg.get("domains") or []) + [d["id"]]))
        db.update("sources", "id", sid, {"config": {**cfg, "domains": doms, "domains_custom": True}})
        tagged.append(sid)
    return jsonify({**d, "tagged_sources": tagged}), 201


@app.delete("/api/domains/<did>")
def del_domain(did):
    try:
        return jsonify(domains_mod.remove(did))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except KeyError:
        abort(404)


@app.post("/api/sources/<sid>/test")
def test_source(sid):
    from .collect.fetch import FetchError
    q = (request.get_json(silent=True) or {}).get("query", "")
    t0 = time.time()
    try:
        items = sources_mod.search(sid, q or "AI", limit=5, terms=[] if not q else None)
    except FetchError as e:
        return jsonify(ok=False, error=e.to_dict(), ms=int((time.time() - t0) * 1000))
    return jsonify(ok=True, count=len(items), ms=int((time.time() - t0) * 1000),
                   sample=[{"title": i["title"], "url": i["url"], "kind": i["content_kind"]} for i in items[:5]])


# ---------------------------------------------------------------- 导入、复查、设置

@app.post("/api/import")
def import_link():
    b = request.get_json(force=True)
    url = (b.get("url") or "").strip()
    if not url.startswith("http"):
        return jsonify(error="请输入 http(s) 链接"), 400
    tid = create_task("import", (b.get("topic") or "未分类").strip(),
                      params={"url": url, "mode": b.get("mode", "auto")})
    runner.start(tid)
    return jsonify(id=tid), 201


# ---------------------------------------------------------------- Lv2 价值榜单

@app.get("/api/tasks/<tid>/ranking")
def get_ranking(tid):
    """某次任务的价值评估：指定版本（默认最新）+ 全部版本列表 + 按当前人工标注重算的指标。"""
    t = db.query_one("SELECT topic FROM tasks WHERE id=?", (tid,))
    if not t:
        abort(404)
    hist = ranking.history(tid)
    if not hist:
        return jsonify(data=None, versions=[])
    v = request.args.get("version", type=int)
    row = next((h for h in hist if h["version"] == v), hist[-1])
    data = row["data"]
    return jsonify(data=data, eval=ranking.with_labels(tid, t["topic"], data), topic=t["topic"],
                   versions=[{"version": h["version"], "trigger": h["trigger"], "created_at": h["created_at"]}
                             for h in hist])


@app.post("/api/labels")
def post_label():
    """人工相关性标注：label=1 相关，0 不相关，null 取消。"""
    b = request.get_json(silent=True) or {}
    if not b.get("topic") or not b.get("doc_id"):
        return jsonify(error="需要 topic 和 doc_id"), 400
    lab = b.get("label")
    ranking.set_label(b["topic"], b["doc_id"], None if lab is None else int(bool(lab)))
    return jsonify(ok=True)


@app.post("/api/tasks/<tid>/rerank")
def post_rerank(tid):
    """重新评估。verify=true 时先补充核验（联网，后台执行，返回任务 id）；否则同步重排（不联网，立即返回新版本）。"""
    t = db.query_one("SELECT topic, graph FROM tasks WHERE id=?", (tid,))
    if not t or t["graph"] != "research":
        return jsonify(error="只能对主题检索任务重新评估"), 400
    if not ranking.latest(tid):
        return jsonify(error="该任务还没有价值评估（可能是旧版本创建的任务，请重新检索）"), 400
    b = request.get_json(silent=True) or {}
    weights = b.get("weights")
    if weights:
        try:
            weights = {k: float(weights[k]) for k in ranking.WEIGHTS if k in weights}
        except (TypeError, ValueError):
            return jsonify(error="权重必须是数字"), 400
    params = {"task": tid, "verify": bool(b.get("verify")), "doc_ids": b.get("doc_ids") or [],
              "weights": weights or None, "trigger": b.get("trigger")}
    rid = create_task("reassess", t["topic"], params=params, budget={"max_tool_calls": 40, "max_steps": 5},
                      parent_task_id=tid)
    if params["verify"]:
        runner.start(rid)
        return jsonify(task_id=rid, async_=True), 201
    run_task(rid)
    return jsonify(task_id=rid, version=ranking.latest(tid)["version"]), 201


@app.post("/api/recheck")
def start_recheck():
    """手动复查：范围优先级 本次任务的资料 > 主题 > 全部。同一范围正在复查时不重复创建。"""
    b = request.get_json(silent=True) or {}
    topic, params = b.get("topic") or "*", {"limit": b.get("limit", 20), "force": True}
    if b.get("task"):
        t = db.query_one("SELECT topic FROM tasks WHERE id=?", (b["task"],))
        if not t:
            return jsonify(error="任务不存在"), 404
        topic, params["scope_task"] = t["topic"], b["task"]
    for r in db.query("SELECT id, params FROM tasks WHERE graph='recheck' AND topic=? AND "
                      "status IN ('pending','running')", (topic,)):
        if (r["params"] or {}).get("scope_task") == params.get("scope_task"):
            return jsonify(id=r["id"], existing=True), 200
    tid = create_task("recheck", topic, params=params)
    runner.start(tid)
    return jsonify(id=tid), 201


@app.get("/api/settings/llm")
def get_llm():
    return jsonify(llm.public_settings())


@app.post("/api/settings/llm")
def set_llm():
    b = request.get_json(force=True)
    llm.save_settings(b.get("base_url", ""), b.get("api_key", ""), b.get("model", ""))
    return jsonify(llm.public_settings())


@app.post("/api/settings/llm/test")
def test_llm():
    try:
        r = llm.chat([{"role": "user", "content": "只回复 JSON：{\"ok\": true}"}], json_mode=True,
                     max_tokens=20, timeout=30)
        return jsonify(ok=True, reply=r["content"][:200], usage=r["usage"])
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:300])


@app.get("/api/settings/websearch")
def get_websearch():
    return jsonify(websearch.public_settings())


@app.post("/api/settings/websearch")
def set_websearch():
    """保存搜索服务的 key。新格式 {keys: {bocha: "...", tavily: "..."}, primary}；兼容旧格式 {provider, api_key}。"""
    b = request.get_json(force=True)
    try:
        if isinstance(b.get("keys"), dict):
            for prov, key in b["keys"].items():
                websearch.save_settings(prov, key or "", primary=False)
            if b.get("primary"):
                websearch.save_settings(b["primary"], "***", primary=True)
        else:
            websearch.save_settings(b.get("provider", ""), b.get("api_key", ""))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(websearch.public_settings())


@app.post("/api/settings/websearch/test")
def test_websearch():
    """每个已配置的搜索服务 × 每个平台，用同一个查询测试，记录各服务对各平台的收录情况。
    之后检索某个平台时自动选收录好的服务；所有服务都不收录的平台跳过。"""
    from .collect.fetch import FetchError
    from .collect.sources import WEB_PLATFORMS
    q = (request.get_json(silent=True) or {}).get("query") or "大模型"
    s = websearch.settings()
    if not s["keys"]:
        return jsonify(ok=False, error="还没有配置任何搜索服务的 key"), 400
    matrix = {}
    for prov in s["keys"]:
        row, cov = {}, {}
        for sid, (site, name) in WEB_PLATFORMS.items():
            try:
                rows = websearch._search(q, site=site, count=10, since=None, provider=prov)
                hit = [x for x in rows if websearch._host_ok(x["url"], site)]
                if site and not hit and prov != "brave":
                    rows2 = websearch._search(f"{q} site:{site}", site=None, count=10, since=None, provider=prov)
                    rows += rows2
                    hit = [x for x in rows2 if websearch._host_ok(x["url"], site)]
                row[name] = {"ok": True, "raw": len(rows), "count": len(hit), "first": hit[0]["title"] if hit else None}
                if site:
                    cov[site] = len(hit)
            except FetchError as e:
                row[name] = {"ok": False, "error": f"{e.kind}：{e.reason}"}
                if e.kind == "config":
                    break
        if cov:
            websearch.save_coverage(prov, cov)
        matrix[websearch.PROVIDERS[prov]] = row
    routes = {name: [websearch.PROVIDERS[p] for p in websearch.providers_for(site)]
              for sid, (site, name) in WEB_PLATFORMS.items()}
    skipped = [n for n, r in routes.items() if not r]
    return jsonify(ok=True, matrix=matrix, routes=routes, skipped=skipped, query=q)


# ---------------------------------------------------------------- 记忆

@app.get("/api/memories")
def list_mem():
    a = request.args
    return jsonify(mem.list_memories(a.get("scope"), a.get("topic"), a.get("kind"), a.get("status"),
                                     a.get("user_id", "default")))


@app.get("/api/memories/<mid>")
def get_mem(mid):
    try:
        return jsonify(mem.detail(mid))
    except KeyError:
        abort(404)


@app.post("/api/memories/<mid>/correct")
def correct_mem(mid):
    b = request.get_json(force=True)
    content = (b.get("content") or "").strip()
    if not content:
        return jsonify(error="更正内容不能为空"), 400
    try:
        return jsonify(mem.correct(mid, content, actor="user", reason=b.get("reason") or "用户更正"))
    except KeyError:
        abort(404)


@app.delete("/api/memories/<mid>")
def delete_mem(mid):
    b = request.get_json(silent=True) or {}
    try:
        return jsonify(mem.delete(mid, actor="user", reason=b.get("reason") or "用户删除"))
    except KeyError:
        abort(404)


@app.post("/api/memories/<mid>/archive")
def archive_mem(mid):
    try:
        return jsonify(mem.archive(mid, reason=(request.get_json(silent=True) or {}).get("reason") or "用户选择不再参考"))
    except KeyError:
        abort(404)
    except ValueError as e:
        return jsonify(error=str(e)), 400


@app.post("/api/memories/<mid>/restore")
def restore_mem(mid):
    try:
        return jsonify(mem.restore(mid))
    except KeyError:
        abort(404)
    except ValueError as e:
        return jsonify(error=str(e)), 400


@app.get("/api/memories/<mid>/impact")
def impact_mem(mid):
    if not mem.get(mid):
        abort(404)
    return jsonify(mem.delivery_impact(mid))


@app.post("/api/preferences")
def add_pref():
    b = request.get_json(force=True)
    t = b.get("type")
    if t not in mem.PREF_TYPES or b.get("value") in (None, "", []):
        return jsonify(error="偏好类型或内容无效"), 400
    try:
        mid = mem.set_preference(b.get("user_id", "default"), t, b["value"], applies_to=b.get("applies_to") or "all")
    except mem.PreferenceError as e:
        return jsonify(error=str(e)), 400
    return jsonify(id=mid), 201


@app.get("/api/memory/preview")
def preview_recall():
    a = request.args
    rec = mem.recall(a.get("user_id", "default"), a.get("topic"), a.get("purpose", "research"),
                     int(a.get("budget", 1200)), record_use=False)
    return jsonify(rec)


@app.get("/api/memory/topics")
def mem_topics():
    return jsonify(db.query("SELECT topic, COUNT(*) n, SUM(kind='fact') facts, SUM(kind='dispute') disputes, "
                            "MAX(updated_at) last FROM memories WHERE scope='topic' AND status IN "
                            "('active','stale') GROUP BY topic ORDER BY last DESC"))


@app.post("/api/keywords/suggest")
def suggest_keywords():
    from . import keywords
    b = request.get_json(force=True)
    topic = (b.get("topic") or "").strip()
    if not topic:
        return jsonify(error="请先填写主题"), 400
    try:
        return jsonify(keywords.suggest(topic, b.get("keywords") or [], b.get("en_keywords") or []))
    except Exception as e:
        return jsonify(error=str(e)[:200]), 500


# ---------------------------------------------------------------- Lv3：Skill 与文章

@app.get("/api/skills")
def list_skills():
    from . import skills
    allsk = skills.load_all()
    return jsonify(skills=[skills.public(s) for s in allsk.values()], errors=skills.load_all.errors,
                   dir=str(skills.SKILL_DIR))


@app.get("/api/skills/<sid>")
def get_skill(sid):
    from . import skills
    try:
        text = skills.raw(sid)
        v = skills.validate(text)
    except skills.SkillError as e:
        return jsonify(error=str(e)), 404
    used = db.query("SELECT COUNT(*) n FROM articles WHERE skill=?", (sid,))[0]["n"]
    return jsonify(text=text, **v, history=skills.history(sid), articles=used)


@app.get("/api/skills/<sid>/history/<fname>")
def get_skill_history(sid, fname):
    from . import skills
    try:
        return jsonify(text=skills.history_text(sid, fname))
    except skills.SkillError as e:
        return jsonify(error=str(e)), 404


@app.get("/api/skills/<sid>/fingerprint/<fp>")
def get_skill_by_fingerprint(sid, fp):
    """文章里记录的 Skill 指纹 → 写这篇文章时 Skill 的原文。"""
    from . import skills
    text = skills.find_by_fingerprint(sid, fp)
    return jsonify(text=text) if text else (jsonify(error="找不到这个指纹对应的 Skill 原文"), 404)


def _skill_text(b):
    from . import skills
    return b["text"] if isinstance(b.get("text"), str) else skills.from_form(b.get("form") or {})


@app.post("/api/skills/validate")
def validate_skill():
    from . import skills
    b = request.get_json(force=True) or {}
    try:
        text = _skill_text(b)
        return jsonify(ok=True, text=text, **skills.validate(text))
    except (skills.SkillError, ValueError, TypeError) as e:
        return jsonify(ok=False, error=str(e)), 200


@app.post("/api/skills")
def create_skill():
    from . import skills
    b = request.get_json(force=True) or {}
    try:
        return jsonify(skills.save(_skill_text(b))), 201
    except (skills.SkillError, ValueError, TypeError) as e:
        return jsonify(error=str(e)), 400


@app.put("/api/skills/<sid>")
def update_skill(sid):
    from . import skills
    b = request.get_json(force=True) or {}
    try:
        return jsonify(skills.save(_skill_text(b), editing=sid))
    except (skills.SkillError, ValueError, TypeError) as e:
        return jsonify(error=str(e)), 400


@app.delete("/api/skills/<sid>")
def delete_skill(sid):
    from . import skills
    try:
        return jsonify(moved_to=skills.remove(sid))
    except skills.SkillError as e:
        return jsonify(error=str(e)), 404


@app.post("/api/tasks/<tid>/write")
def start_write(tid):
    from . import skills
    t = _task_or_404(tid)
    if t["graph"] != "research" or t["status"] != "done":
        return jsonify(error="只有完成的检索任务可以写作"), 400
    b = request.get_json(force=True) or {}
    try:
        skills.get(b.get("skill") or "")
    except skills.SkillError as e:
        return jsonify(error=str(e)), 400
    wid = create_task("write", t["topic"], params={"research_task": tid, "skill": b["skill"]},
                      user_id=t["user_id"], parent_task_id=tid)
    runner.start(wid)
    return jsonify(id=wid), 201


def _article_row(a):
    v = db.query_one("SELECT version, note, created_at, meta FROM article_versions WHERE article_id=? AND version=?",
                     (a["id"], a["current_version"]))
    meta = (v or {}).get("meta") or {}
    s = ((meta.get("check") or {}).get("summary")) or {}
    return {**a, "note": (v or {}).get("note"), "skill_name": (meta.get("skill") or {}).get("name"),
            "support_rate": s.get("support_rate"), "problems": s.get("problems"),
            "research_task": meta.get("research_task"), "experiment": meta.get("experiment"),
            "variant": meta.get("variant")}


@app.get("/api/articles")
def list_articles():
    a = request.args
    sql, args = "SELECT * FROM articles WHERE 1=1", []
    if a.get("topic"):
        sql += " AND topic=?"
        args.append(a["topic"])
    rows = db.query(sql + " ORDER BY updated_at DESC LIMIT 100", args)
    out = [_article_row(r) for r in rows if r.get("current_version")]   # 写作失败、没有任何版本的不列出
    if a.get("research_task"):
        out = [r for r in out if r["research_task"] == a["research_task"]]
    if not a.get("experiment"):                     # 评测实验写的变体不混进普通文章列表
        out = [r for r in out if not r.get("experiment")]
    return jsonify(out)


@app.get("/api/articles/<aid>")
def get_article(aid):
    from . import mdrender
    a = db.query_one("SELECT * FROM articles WHERE id=?", (aid,))
    if not a:
        abort(404)
    ver = request.args.get("v", type=int) or a["current_version"]
    v = db.query_one("SELECT * FROM article_versions WHERE article_id=? AND version=?", (aid, ver))
    versions = db.query("SELECT version, note, created_at FROM article_versions WHERE article_id=? ORDER BY version", (aid,))
    if v:
        meta = v.get("meta") or {}
        v["html"] = mdrender.render(_mark_problems(_mark_paras(v["markdown"], meta), meta))
        v["review"] = _review_view(meta)
    versions = db.query("SELECT version, note, created_at, json_extract(meta, '$.edit.op') AS op FROM article_versions "
                        "WHERE article_id=? ORDER BY version", (aid,))
    running = db.query_one("SELECT id, status, params FROM tasks WHERE graph IN ('revise','write') AND status IN "
                           "('pending','running') AND json_extract(params, '$.article_id')=? ORDER BY created_at DESC",
                           (aid,))
    return jsonify(article=_article_row(a), version=v, versions=versions, pending=running)


def _review_view(meta: dict) -> dict:
    """审阅用：每节每段（用户看到的出处编号）和配图列表。"""
    from .graphs.revise import to_display_numbers
    from . import illustrate as ill
    secs = [{"id": s["id"], "heading": s.get("heading"),
             "paras": [to_display_numbers(x, meta) for x in ill.paragraphs(s.get("md"))]} for s in meta.get("sections") or []]
    figs = [{"k": k, "src": f.get("src"), "caption": to_display_numbers(f.get("caption") or "", meta),
             "section": f.get("section"), "para": f.get("para"), "kind": f.get("kind"), "diagram": f.get("diagram")}
            for k, f in enumerate(meta.get("figures") or [])]
    return {"sections": secs, "figures": figs, "revisions": meta.get("revisions") or [], "edit": meta.get("edit")}


def _mark_paras(md: str, meta: dict) -> str:
    """在每节每段开头加 ⟦p:节:段⟧，渲染成 data-sec / data-para，文章页上悬停某段就能改这一段。"""
    from .graphs.revise import to_display_numbers
    from . import illustrate as ill
    body, sep, refs = md.partition("\n## 参考资料")
    for s in meta.get("sections") or []:
        start = body.find(f"\n## {s.get('heading')}\n")
        if start < 0:
            continue
        end = body.find("\n## ", start + 4)
        end = len(body) if end < 0 else end
        chunk, pos = body[start:end], 0
        for k, para in enumerate(ill.paragraphs(s.get("md"))):
            t = to_display_numbers(para, meta).strip()
            j = chunk.find(t, pos)
            if j < 0 or not t:
                continue
            mark = f"⟦p:{s['id']}:{k}⟧"
            chunk = chunk[:j] + mark + chunk[j:]
            pos = j + len(mark) + len(t)
        body = body[:start] + chunk + body[end:]
    return body + sep + refs


@app.get("/api/articles/<aid>/export")
def export_article(aid):
    """导出交付：?format=md（zip：Markdown + 图片）或 html（单文件手机阅读版，图片内嵌）；?v= 指定版本。"""
    from flask import Response
    from urllib.parse import quote
    from . import export
    a = db.query_one("SELECT * FROM articles WHERE id=?", (aid,))
    if not a:
        abort(404)
    ver = request.args.get("v", type=int) or a["current_version"]
    v = db.query_one("SELECT * FROM article_versions WHERE article_id=? AND version=?", (aid, ver))
    if not v:
        abort(404)
    meta = v["meta"] or {}
    title = a["title"] or "article"
    name = f"{export.safe_name(title)}_v{ver}"
    sk = meta.get("skill") or {}
    footer = (f"写法：{sk.get('name', '')} v{sk.get('version', '')}（Skill 指纹 {sk.get('fingerprint', '')}）· 第 {ver} 版 · "
              "句末 [n] 为出处，对应文末参考资料；“分析”标记的段落是作者判断。")
    if request.args.get("format") == "html":
        data, mime, fname = export.mobile_html(v["markdown"], title, footer).encode("utf-8"), "text/html; charset=utf-8", f"{name}.html"
    else:
        data, mime, fname = export.markdown_zip(v["markdown"], title), "application/zip", f"{name}_markdown.zip"
    return Response(data, mimetype=mime, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"})


@app.post("/api/uploads/image")
def upload_image():
    """审阅时上传自己的图片：校验是图片、限制大小，统一保存在 data/images/uploads/（文件名是内容哈希）。
    返回 local_path，换图/加图/换封面时作为 upload 参数传入。"""
    import hashlib
    import io
    from PIL import Image, UnidentifiedImageError
    from . import illustrate as ill
    f = request.files.get("file")
    if not f:
        return jsonify(error="没有收到文件"), 400
    data = f.read(15 * 1024 * 1024 + 1)
    if len(data) > 15 * 1024 * 1024:
        return jsonify(error="图片不能超过 15MB"), 400
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except (UnidentifiedImageError, OSError) as e:
        return jsonify(error=f"不是可识别的图片：{str(e)[:80]}"), 400
    fmt = (im.format or "").upper()
    ext = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp", "GIF": "gif"}.get(fmt)
    if not ext:                                     # HEIC/BMP/TIFF 等转成 PNG，浏览器和导出都能用
        buf = io.BytesIO()
        (im.convert("RGBA") if im.mode in ("P", "LA") else im.convert("RGB")).save(buf, "PNG")
        data, ext = buf.getvalue(), "png"
    name = hashlib.sha256(data).hexdigest()[:16] + "." + ext
    d = config.IMAGE_DIR / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(data)
    local = f"images/uploads/{name}"
    q = ill.quality(local, im.size[0], im.size[1], "")
    return jsonify(local_path=local, src="/media/" + local, w=im.size[0], h=im.size[1],
                   warn=(q.get("issues") or []) + (q.get("warn") or [])), 201


@app.post("/api/articles/<aid>/revise")
def revise_article(aid):
    """审阅修改：改标题 / 改写或直接改某段 / 删段 / 配图删除、替换、改图注 / 换封面。异步执行，返回任务 id。"""
    from .graphs.revise import OPS
    a = db.query_one("SELECT * FROM articles WHERE id=?", (aid,))
    if not a:
        abort(404)
    b = request.get_json(force=True) or {}
    if b.get("op") not in OPS:
        return jsonify(error="不支持的修改"), 400
    keep = ("op", "base_version", "title", "section", "para", "instruction", "text", "figure", "caption", "image_id",
            "upload", "remember", "remember_scope", "feedback")
    params = {k: b[k] for k in keep if k in b}
    params["article_id"] = aid
    tid = create_task("revise", a["topic"], params=params, parent_task_id=a["task_id"],
                      budget={"max_tool_calls": 40, "max_steps": 20})
    runner.start(tid)
    return jsonify(id=tid), 201


@app.post("/api/articles/<aid>/rewrite")
def rewrite_article(aid):
    """换选题角度：重列提纲、重写全文（写作任务），结果存为同一篇文章的新版本。"""
    from . import skills
    a = db.query_one("SELECT * FROM articles WHERE id=?", (aid,))
    if not a:
        abort(404)
    v = db.query_one("SELECT meta FROM article_versions WHERE article_id=? AND version=?", (aid, a["current_version"]))
    meta = (v or {}).get("meta") or {}
    b = request.get_json(force=True) or {}
    angle, title = (b.get("angle") or "").strip(), (b.get("title") or "").strip()
    if len(angle) < 4:
        return jsonify(error="请写清楚新的选题角度，例如：从中小企业落地成本的角度写"), 400
    skill = b.get("skill") or (meta.get("skill") or {}).get("id") or a["skill"]
    try:
        skills.get(skill)
    except skills.SkillError as e:
        return jsonify(error=str(e)), 400
    rt = meta.get("research_task")
    if not rt:
        return jsonify(error="找不到这篇文章的检索任务"), 400
    params = {"research_task": rt, "skill": skill, "article_id": aid, "angle": angle,
              "note": f"换角度重写：{angle[:40]}"}
    if title:
        params["title"] = title
    wid = create_task("write", a["topic"], params=params, parent_task_id=rt)
    if b.get("remember"):
        from . import memory as mem
        mem.add("user", "feedback", f"选题角度偏好：{angle}", applies_to=f"topic:{a['topic']}",
                source={"by": "user", "confirmed": True, "article": aid}, meta={"type": "feedback", "from": "换角度重写"},
                actor="user", task_id=wid)
    runner.start(wid)
    return jsonify(id=wid), 201


@app.get("/api/articles/<aid>/images")
def article_image_candidates(aid):
    from .graphs.revise import image_candidates
    a = db.query_one("SELECT current_version FROM articles WHERE id=?", (aid,))
    if not a:
        abort(404)
    ver = request.args.get("v", type=int) or a["current_version"]
    v = db.query_one("SELECT meta FROM article_versions WHERE article_id=? AND version=?", (aid, ver))
    return jsonify(image_candidates((v or {}).get("meta") or {}))


@app.post("/api/articles/<aid>/restore")
def restore_article(aid):
    """回到旧版本：把旧版本复制为新的最新版本（历史不丢）。"""
    from .graphs.write import save_version
    b = request.get_json(force=True) or {}
    v = db.query_one("SELECT * FROM article_versions WHERE article_id=? AND version=?", (aid, b.get("version")))
    if not v:
        abort(404)
    meta = {**(v["meta"] or {}), "edit": {"op": "restore", "summary": f"恢复到第 {v['version']} 版"}}
    m = re.match(r"#\s+(.+)", v["markdown"])
    nv = save_version(aid, v["markdown"], meta, note=f"恢复到第 {v['version']} 版", title=m.group(1).strip() if m else None)
    return jsonify(version=nv)


@app.get("/api/articles/<aid>/diff")
def article_diff(aid):
    """两个版本按段落对比：相同、删除、新增、修改（修改的段落再标出句内差异）。"""
    import difflib
    a, b = request.args.get("a", type=int), request.args.get("b", type=int)
    va = db.query_one("SELECT markdown FROM article_versions WHERE article_id=? AND version=?", (aid, a))
    vb = db.query_one("SELECT markdown FROM article_versions WHERE article_id=? AND version=?", (aid, b))
    if not va or not vb:
        abort(404)

    def paras(md):
        return [p.strip() for p in re.split(r"\n\s*\n", md.split("\n## 参考资料")[0]) if p.strip()]
    pa, pb = paras(va["markdown"]), paras(vb["markdown"])
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, pa, pb, autojunk=False).get_opcodes():
        if op == "equal":
            out += [{"t": "same", "text": x} for x in pa[i1:i2]]
            continue
        olds, news = pa[i1:i2], pb[j1:j2]
        for k in range(max(len(olds), len(news))):
            o = olds[k] if k < len(olds) else None
            n = news[k] if k < len(news) else None
            if o is not None and n is not None:
                sm = difflib.SequenceMatcher(None, o, n, autojunk=False)
                if sm.ratio() >= 0.35:
                    out.append({"t": "mod", "parts": [[tag, o[x1:x2], n[y1:y2]] for tag, x1, x2, y1, y2 in sm.get_opcodes()]})
                else:                     # 同一位置整段重写：新旧放在一起看
                    out.append({"t": "rewrite", "old": o, "new": n})
                continue
            if o is not None:
                out.append({"t": "del", "text": o})
            if n is not None:
                out.append({"t": "add", "text": n})
    stats = {k: sum(1 for x in out if x["t"] == k) for k in ("same", "mod", "rewrite", "add", "del")}
    return jsonify(a=a, b=b, items=out, stats=stats)


# ---------------------------------------------------------------- Lv3：评测实验

@app.post("/api/experiments")
def create_experiment():
    b = request.get_json(force=True) or {}
    kind = b.get("kind") or "ablation"
    tids = [t for t in b.get("research_tasks") or [] if t]
    if kind == "ablation" and len(tids) < 1:
        return jsonify(error="至少选一组素材（检索任务）"), 400
    params = {"kind": kind, "research_tasks": tids, "skill": b.get("skill") or "brief",
              "variants": b.get("variants") or None, "skills": b.get("skills") or [],
              "detector": b.get("detector") or "auto", "parallel": max(1, min(6, int(b.get("parallel") or 3))),
              "judge": b.get("judge", True)}
    if kind == "skills" and len(params["skills"]) < 2:
        return jsonify(error="Skill 对照至少选 2 种写法"), 400
    title = "评测实验：" + ("多种 Skill 对照" if kind == "skills" else f"初稿 vs 优化稿（{len(tids)} 组素材）")
    tid = create_task("experiment", title, params=params,
                      budget={"max_steps": 400, "max_tool_calls": 600, "max_seconds": 6 * 3600, "max_cost": 50})
    runner.start(tid)
    return jsonify(id=tid), 201


@app.get("/api/experiments")
def list_experiments():
    rows = db.query("SELECT id, topic, status, params, state, created_at, updated_at, error FROM tasks WHERE graph='experiment' "
                    "ORDER BY created_at DESC LIMIT 50")
    out = []
    for r in rows:
        st_ = r.pop("state") or {}
        runs = st_.get("runs") or []
        failed = [{"topic": x["topic"], "variant": x["variant"], "error": E.display_error(x.get("error"))[:120],
                   "research_task": x["research_task"]} for x in runs if x.get("status") == "failed"]
        out.append({**r, "runs": len(runs), "done": sum(1 for x in runs if x.get("status") == "done"), "failed": failed,
                    "fatal": st_.get("fatal"), "running": runner.is_running(r["id"]),
                    "pending": sum(1 for x in runs if x.get("status") == "pending")})
    return jsonify(out)


def _exp_or_404(eid):
    t = db.query_one("SELECT * FROM tasks WHERE id=? AND graph='experiment'", (eid,))
    if not t:
        abort(404)
    return t


@app.get("/api/experiments/<eid>")
def get_experiment(eid):
    from . import experiment_report
    from . import evaluate as ev
    t = _exp_or_404(eid)
    runs = (t["state"] or {}).get("runs") or []
    if not runner.is_running(eid) and any(r.get("status") == "done" and r.get("metrics_version") != ev.METRICS_VERSION
                                          for r in runs):
        _remeasure(eid, runs)                      # 指标定义更新过：自动按新定义重算一次
        t = _exp_or_404(eid)
    rep = experiment_report.build(t)
    rep.update(topic=t["topic"], error=E.display_error(t["error"]) or None, current_node=t["current_node"], running=runner.is_running(eid),
               created_at=t["created_at"])
    return jsonify(rep)


@app.post("/api/experiments/<eid>/retry")
def experiment_retry(eid):
    """重跑失败（或没开始）的文章：只把这些标回待写，从“写作”这一步接着跑，写成功的保留不动。"""
    from .harness import patch_state
    t = _exp_or_404(eid)
    if runner.is_running(eid):
        return jsonify(error="实验还在运行"), 409
    runs = (t["state"] or {}).get("runs") or []
    redo = [r for r in runs if r.get("status") in ("failed", "pending")]
    if not redo:
        return jsonify(error="没有失败的文章"), 400
    for r in redo:
        for k in ("error", "raw_error", "write_task", "article_id", "version", "seconds", "metrics", "text", "claim_judge"):
            r.pop(k, None)
        r["status"] = "pending"
    for r in runs:                      # 指标、评审要按全部文章重算
        r.pop("claim_judge", None)
    patch_state(eid, {"runs": runs, "fatal": None, "llm_pairs": {}}, note=f"重跑 {len(redo)} 篇失败的文章")
    rewind(eid, "write", note="重跑失败的文章")
    runner.start(eid)
    return jsonify(retry=len(redo))


def _remeasure(eid: str, runs: list[dict]) -> int:
    from . import evaluate as ev
    from .harness import patch_state
    from .graphs.experiment import _docs_with_text
    n = 0
    for r in runs:
        if r.get("status") != "done":
            continue
        v = db.query_one("SELECT markdown, meta FROM article_versions WHERE article_id=? AND version=?", (r["article_id"], r["version"]))
        if not v:
            continue
        m = ev.evaluate_version(v["markdown"], v["meta"] or {}, _docs_with_text(v["meta"] or {}))
        r["text"] = m.pop("text")
        m.pop("_items", None)
        r["metrics"] = m
        r["metrics_version"] = ev.METRICS_VERSION
        n += 1
    patch_state(eid, {"runs": runs}, note="按新的指标定义重新计算")
    return n


@app.post("/api/experiments/<eid>/remeasure")
def experiment_remeasure(eid):
    """指标定义更新后，用已写好的文章重新计算确定性指标（不重写文章、不调用大模型）。"""
    t = _exp_or_404(eid)
    if runner.is_running(eid):
        return jsonify(error="实验还在运行"), 409
    return jsonify(remeasured=_remeasure(eid, (t["state"] or {}).get("runs") or []))


@app.get("/api/experiments/<eid>/text/<path:key>")
def experiment_text(eid, key):
    t = _exp_or_404(eid)
    r = next((x for x in (t["state"] or {}).get("runs") or [] if x["key"] == key), None)
    if not r or not r.get("text"):
        abort(404)
    return jsonify(key=key, text=r["text"], chars=len(r["text"]))


@app.get("/api/experiments/<eid>/blind")
def experiment_blind(eid):
    """读者盲评：只给“甲/乙”两篇完整正文，不告诉哪篇是初稿/优化稿；出处编号去掉，避免以此判断。"""
    t = _exp_or_404(eid)
    st = t["state"] or {}
    text = {r["key"]: r.get("text") or "" for r in st.get("runs") or []}
    rater = request.args.get("rater") or ""
    mine = {r["pair_id"]: r["data"] for r in db.query(
        "SELECT pair_id, data FROM eval_ratings WHERE experiment_id=? AND rater=? ORDER BY id", (eid, rater))}
    strip = lambda s: re.sub(r"\[\d+\]", "", s)  # noqa: E731
    return jsonify(pairs=[{"pair_id": p["pair_id"], "topic": p["topic"], "甲": strip(text[p["甲"]]), "乙": strip(text[p["乙"]]),
                           "rated": p["pair_id"] in mine, "mine": mine.get(p["pair_id"])} for p in st.get("blind") or []])


@app.post("/api/experiments/<eid>/blind")
def experiment_rate(eid):
    _exp_or_404(eid)
    b = request.get_json(force=True) or {}
    rater = (b.get("rater") or "").strip()[:30]
    if not rater:
        return jsonify(error="请填一个称呼（只用来区分不同读者，可以是化名）"), 400
    ans = {k: v for k, v in (b.get("answers") or {}).items()
           if k in ("accuracy", "information", "natural", "easy", "overall", "ai_guess") and v in ("甲", "乙", "相当", "说不准")}
    ans["comment"] = str((b.get("answers") or {}).get("comment") or "")[:300]
    if len(ans) < 6:
        return jsonify(error="每一项都要选"), 400
    db.execute("DELETE FROM eval_ratings WHERE experiment_id=? AND pair_id=? AND rater=?", (eid, b.get("pair_id"), rater))
    db.insert("eval_ratings", dict(experiment_id=eid, pair_id=b.get("pair_id"), rater=rater, data=ans, created_at=db.now()))
    return jsonify(ok=True)


@app.post("/api/experiments/<eid>/blind/pairs")
def experiment_add_pairs(eid):
    """新增盲评组：任选一组素材（或全部素材）里的两个版本，让读者盲评。甲乙随机分配。"""
    import random
    from .harness import patch_state
    t = _exp_or_404(eid)
    if runner.is_running(eid):
        return jsonify(error="实验还在运行，跑完再加"), 409
    b = request.get_json(force=True) or {}
    va, vb = b.get("a"), b.get("b")
    if not va or not vb or va == vb:
        return jsonify(error="请选两个不同的版本"), 400
    st = t["state"] or {}
    runs = {(r["research_task"], r["variant"]): r for r in st.get("runs") or [] if r.get("status") == "done" and r.get("text")}
    tids = list(dict.fromkeys(r["research_task"] for r in st.get("runs") or []))
    if b.get("research_task") not in (None, "", "all"):
        tids = [b["research_task"]]
    blind = st.get("blind") or []
    have = {(p["research_task"], frozenset((p["甲"], p["乙"]))) for p in blind}
    nxt = max([int(p["pair_id"][1:]) for p in blind if p["pair_id"][1:].isdigit()] or [0]) + 1
    added, skipped = [], []
    for tid in tids:
        x, y = runs.get((tid, va)), runs.get((tid, vb))
        if not x or not y:
            skipped.append(f"{tid}：缺少写成功的版本")
            continue
        if (tid, frozenset((x["key"], y["key"]))) in have:
            skipped.append(f"{x['topic']}：这两个版本已经有盲评组")
            continue
        flip = random.random() < 0.5
        blind.append({"pair_id": f"p{nxt}", "research_task": tid, "topic": x["topic"],
                      "甲": (y if flip else x)["key"], "乙": (x if flip else y)["key"], "added_by": "user"})
        added.append(f"p{nxt}")
        nxt += 1
    if not added:
        return jsonify(error="没有新增：" + "；".join(skipped)), 400
    patch_state(eid, {"blind": blind}, note=f"新增盲评组 {len(added)} 个")
    return jsonify(added=added, skipped=skipped)


@app.delete("/api/experiments/<eid>/blind/pairs/<pid>")
def experiment_del_pair(eid, pid):
    """删除一个盲评组（连同这组的评分）。"""
    from .harness import patch_state
    t = _exp_or_404(eid)
    blind = [p for p in (t["state"] or {}).get("blind") or [] if p["pair_id"] != pid]
    patch_state(eid, {"blind": blind}, note=f"删除盲评组 {pid}")
    db.execute("DELETE FROM eval_ratings WHERE experiment_id=? AND pair_id=?", (eid, pid))
    return jsonify(ok=True)


@app.post("/api/experiments/<eid>/scores")
def experiment_score(eid):
    """录入外部 AIGC 检测器的结果（同一个检测器测同一份完整正文）。分数填 0-100 或 0-1 都可以。"""
    _exp_or_404(eid)
    b = request.get_json(force=True) or {}
    det = (b.get("detector") or "").strip()[:40]
    try:
        sc = float(b.get("score"))
    except (TypeError, ValueError):
        return jsonify(error="分数要是数字"), 400
    sc = sc / 100 if sc > 1 else sc
    if not det or not 0 <= sc <= 1:
        return jsonify(error="请填检测器名称，分数在 0-100 之间"), 400
    db.insert("eval_scores", dict(experiment_id=eid, run_key=b.get("run_key"), detector=det, score=sc,
                                  note=str(b.get("note") or "")[:200], created_at=db.now()))
    return jsonify(ok=True)


@app.post("/api/experiments/<eid>/detect")
def experiment_detect(eid):
    """用本地检测器（重新）测这次实验的全部完整正文，结果写入分数表。"""
    from . import aigc
    t = _exp_or_404(eid)
    if not aigc.status()["deps"]:
        return jsonify(error="本地检测器没有安装：双击 topicdesk 文件夹里的“安装AIGC检测器.command”，装好后重启主题编辑台"), 400
    n = 0
    try:
        for r in (t["state"] or {}).get("runs") or []:
            if r.get("text"):
                d = aigc.detect(r["text"])
                db.insert("eval_scores", dict(experiment_id=eid, run_key=r["key"], detector=d["detector"], score=d["ai_prob"],
                                              note="本地检测", created_at=db.now()))
                n += 1
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        return jsonify(error=f"检测失败（{type(e).__name__}）：{str(e)[:300]}"), 500
    return jsonify(detected=n)


@app.delete("/api/experiments/<eid>/blind")
def experiment_rate_clear(eid):
    """清除某位读者的全部盲评（比如试填的数据）。"""
    _exp_or_404(eid)
    rater = (request.args.get("rater") or "").strip()
    if not rater:
        return jsonify(error="要指定读者"), 400
    n = db.execute("DELETE FROM eval_ratings WHERE experiment_id=? AND rater=?", (eid, rater))
    return jsonify(deleted=n)


@app.get("/api/llm/stats")
def llm_stats():
    from . import llm
    return jsonify(llm.stats())


@app.get("/api/aigc")
def aigc_status():
    from . import aigc
    return jsonify(aigc.status())


@app.get("/api/aigc/selfcheck")
def aigc_selfcheck():
    """检测器自检：用一段人写、一段 AI 腔的参考文字看检测器能不能区分。"""
    from . import aigc
    if not aigc.status()["deps"]:
        return jsonify(error="本地检测器没有安装"), 400
    try:
        return jsonify(aigc.selfcheck())
    except Exception as e:  # noqa: BLE001
        return jsonify(error=f"自检失败（{type(e).__name__}）：{str(e)[:300]}"), 500


@app.post("/api/aigc")
def aigc_settings():
    from . import aigc
    b = request.get_json(force=True) or {}
    aigc.save_settings(b.get("model"), b.get("hf_endpoint"))
    return jsonify(aigc.status())


@app.get("/api/trust/<tid>")
def trust_report(tid):
    """可信度与可追溯：资料是否可靠、筛选是否有价值、改写是否帮助理解、过程是否可追溯可复现。"""
    from . import trust
    try:
        return jsonify(trust.build(tid))
    except ValueError as e:
        return jsonify(error=str(e)), 400


@app.get("/api/flow")
def get_flow():
    """同一主题的流程链：整理资料（检索任务）→ 写作任务 → 文章。任务页、文章页顶部的流程条用它互相跳转。"""
    a = request.args
    rid, here_write, here_article = None, None, a.get("article")
    if here_article:
        art = db.query_one("SELECT task_id FROM articles WHERE id=?", (here_article,))
        if not art:
            abort(404)
        here_write = art["task_id"]
    tid = a.get("task") or here_write
    t = db.query_one("SELECT id, graph, parent_task_id, params FROM tasks WHERE id=?", (tid,)) if tid else None
    if not t:
        abort(404)
    if t["graph"] == "write":
        here_write = t["id"]
        rid = (t.get("params") or {}).get("research_task") or t.get("parent_task_id")
    elif t["graph"] == "research":
        rid = t["id"]
    else:
        return jsonify(research=None, writes=[])
    r = db.query_one("SELECT id, topic, status, state FROM tasks WHERE id=?", (rid,)) if rid else None
    research = None
    if r:
        rep = ((r.get("state") or {}).get("report") or {})
        research = {"id": r["id"], "topic": r["topic"], "status": r["status"],
                    "docs": rep.get("unique_docs") or rep.get("total_docs")}
    writes = []
    if rid:
        rows = db.query("SELECT id, status, params, state, created_at FROM tasks WHERE graph='write' "
                        "AND (parent_task_id=? OR json_extract(params, '$.research_task')=?) "
                        "AND json_extract(params, '$.experiment') IS NULL ORDER BY created_at",
                        (rid, rid))
        for w in rows:
            st = w.get("state") or {}
            art = db.query_one("SELECT id, title, current_version FROM articles WHERE task_id=?", (w["id"],))
            if not art and (st.get("result") or {}).get("article_id"):
                art = db.query_one("SELECT id, title, current_version FROM articles WHERE id=?", (st["result"]["article_id"],))
            writes.append({"task_id": w["id"], "status": w["status"], "created_at": w["created_at"],
                           "skill": (st.get("skill") or {}).get("name") or (w.get("params") or {}).get("skill"),
                           "article_id": art["id"] if art else None,
                           "title": (art or {}).get("title") or st.get("title"),
                           "version": (art or {}).get("current_version")})
    return jsonify(research=research, writes=writes, here_write=here_write, here_article=here_article)


PROBLEM_STATUS = {"weak", "number", "bad_cite", "uncited", "analysis_num"}


def _mark_problems(md: str, meta: dict) -> str:
    """把核对有问题的句子在正文里标出来（⟦m序号⟧…⟦/m⟧，渲染为下划线），右栏点一下就能定位到原句。
    核对记录用的是素材编号，正文用的是重排后的出处编号，先换算再查找。"""
    import re as _re
    refmap = meta.get("refmap") or {}
    body, sep, refs = md.partition("\n## 参考资料")
    items = (meta.get("check") or {}).get("items") or []
    for k, it in enumerate(items):
        if it.get("status") not in PROBLEM_STATUS:
            continue
        sent = _re.sub(r"\[(\d+)\]", lambda m: f"[{refmap.get(m.group(1), m.group(1))}]", it["sentence"])
        if sent and sent in body:
            body = body.replace(sent, f"⟦m{k}⟧{sent}⟦/m⟧", 1)
    return body + sep + refs


# ---------------------------------------------------------------- 记忆验证演示（回放模式）

@app.get("/api/demo/memory")
def memory_demo_status():
    from . import memory_demo
    return jsonify(memory_demo.status())


@app.post("/api/demo/memory/<action>")
def memory_demo_action(action):
    from . import memory_demo
    try:
        if action in ("run1", "run2"):
            tid = memory_demo.start_run(1 if action == "run1" else 2)
            runner.start(tid)
            return jsonify(id=tid)
        if action == "sample":
            return jsonify(memory_demo.apply_sample())
        if action == "reset":
            return jsonify(memory_demo.reset())
    except (RuntimeError, ValueError) as e:
        return jsonify(error=str(e)), 400
    abort(404)


@app.get("/api/replay")
def replay_info():
    from . import replay
    return jsonify(replay.info())


@app.post("/api/replay/day")
def replay_day():
    from . import replay
    if not replay.STATE["enabled"]:
        return jsonify(error="未启用回放模式（启动时加 --replay）"), 400
    replay.set_day(int((request.get_json(force=True) or {}).get("day", 1)))
    return jsonify(replay.info())


def _recheck_loop(interval_min: float):
    while True:
        time.sleep(interval_min * 60)
        try:
            due = db.query_one("SELECT COUNT(*) n FROM documents WHERE status='active' AND "
                               "recheck_at IS NOT NULL AND recheck_at<=?", (db.now(),))["n"]
            busy = db.query_one("SELECT COUNT(*) n FROM tasks WHERE graph='recheck' AND "
                                "status IN ('pending','running')")["n"]
            if due and not busy:
                runner.start(create_task("recheck", "*", params={"limit": 20}))
        except Exception:
            pass
        finally:
            db.reset()


def create_app(auto_resume: bool = False, recheck_every: float = 0):
    db.init_db()
    sources_mod.seed_sources()
    runner.recover_on_boot(auto_resume=auto_resume)
    if recheck_every > 0:
        threading.Thread(target=_recheck_loop, args=(recheck_every,), daemon=True).start()
    return app
