"""Flask API + 静态前端。

前端用轮询读取轨迹（/trace?after=seq），刷新页面后从数据库重建全部进度。
"""
from __future__ import annotations

import threading
import time

from flask import Flask, abort, jsonify, request, send_from_directory

from . import config, db, graphs, llm  # noqa: F401  导入即注册编排图
from .collect import sources as sources_mod
from .collect.dedup import canonical_url
from .config import ROOT
from .harness import GRAPHS, create_task, resolve_hitl, rewind, runner

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
    return send_from_directory(app.static_folder, "index.html")


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
    rows = db.query("SELECT id, graph, topic, status, current_node, usage, error, created_at, "
                    "updated_at FROM tasks ORDER BY created_at DESC LIMIT 100")
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
                 "json_extract(meta,'$.relation') relation, json_extract(meta,'$.via_browser') via_browser")


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
    sid = "rss_" + canonical_url(feed).split("//")[1].replace("/", "_").replace(".", "_")[:40]
    if db.query_one("SELECT id FROM sources WHERE id=?", (sid,)):
        return jsonify(error="该 RSS 已存在"), 400
    db.insert("sources", dict(id=sid, name=name, method="rss", base_url=feed,
                              config={"feed": feed, "full_text": bool(b.get("full_text")),
                                      "lang": b.get("lang", "zh"),
                                      "detail": "browser" if b.get("browser") else "html"},
                              enabled=1, health={}))
    return jsonify(id=sid), 201


@app.patch("/api/sources/<sid>")
def patch_source(sid):
    b = request.get_json(force=True)
    if "enabled" in b:
        db.update("sources", "id", sid, {"enabled": 1 if b["enabled"] else 0})
    return jsonify(ok=True)


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


@app.post("/api/recheck")
def start_recheck():
    b = request.get_json(silent=True) or {}
    tid = create_task("recheck", b.get("topic") or "*", params={"limit": b.get("limit", 20)})
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
