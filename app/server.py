"""Flask API + 静态前端。

前端用轮询读取轨迹（/trace?after=seq），刷新页面后从数据库重建全部进度。
"""
from flask import Flask, abort, jsonify, request, send_from_directory

from . import db, graphs  # noqa: F401  导入即注册编排图
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


def create_app(auto_resume: bool = False):
    db.init_db()
    runner.recover_on_boot(auto_resume=auto_resume)
    return app
