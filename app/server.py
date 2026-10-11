"""Flask API + 静态前端。

前端用轮询读取轨迹（/trace?after=seq），刷新页面后从数据库重建全部进度。
"""
from __future__ import annotations

import threading
import time

from flask import Flask, abort, jsonify, request, send_from_directory

from . import config, db, graphs, llm  # noqa: F401  导入即注册编排图
from . import memory as mem
from . import ranking, websearch
from .collect import sources as sources_mod
from .collect.dedup import canonical_url
from .config import ROOT
from .harness import GRAPHS, create_task, resolve_hitl, rewind, run_task, runner

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
                 "json_extract(meta,'$.relation') relation, json_extract(meta,'$.via_browser') via_browser, "
                 "json_extract(meta,'$.judge.reason') judge_reason")


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
    b = request.get_json(force=True)
    try:
        websearch.save_settings(b.get("provider", ""), b.get("api_key", ""))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(websearch.public_settings())


@app.post("/api/settings/websearch/test")
def test_websearch():
    """用同一个查询逐个平台测试，显示服务返回条数和其中属于该平台的条数（判断该服务对各平台的收录情况）。"""
    from .collect.fetch import FetchError
    from .collect.sources import WEB_PLATFORMS
    q = (request.get_json(silent=True) or {}).get("query") or "大模型"
    out = {}
    for sid, (site, name) in WEB_PLATFORMS.items():
        st = {}
        try:
            rows = websearch.search(q, site=site, count=10, stats=st)
            out[name] = {"ok": True, "count": len(rows), "raw": st.get("raw", 0), "retried": st.get("retried"),
                         "first": (rows[0]["title"] if rows else None)}
        except FetchError as e:
            out[name] = {"ok": False, "error": f"{e.kind}：{e.reason}"}
            if e.kind == "config":
                break
    return jsonify(ok=all(v["ok"] for v in out.values()), results=out, query=q)


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
