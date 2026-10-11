"""记忆验证演示（固定样例回放）：同一主题的两次独立任务，验证长期记忆。

步骤：
  1. 第 1 天跑第一次任务：写入事实、历史交付；
  2. 修改记忆：删除 / 更正一条事实，设置长期偏好（可一键套用示例改动）；
  3. 切到第 2 天（IT之家把 128K 更正为 64K、出现一篇 256K 新进展、HN 不可用）跑第二次任务；
  4. 看第二次任务的“与上次相比”：重复资料、新进展、旧结论更正、偏好影响、失效记忆是否还被召回；
     再做一次不计数的召回，逐条确认失效内容不出现。
只在回放模式（独立数据库 data/replay.db、模拟网站）下可用，不影响真实数据。
"""
from __future__ import annotations

from . import db, replay
from . import memory as mem

TOPIC = "开源大模型 长上下文 评测"
PARAMS = {"keywords": ["长上下文"], "en_keywords": ["long context LLM"], "max_docs": 30,
          "sources": ["ithome", "36kr", "sspai", "juejin", "arxiv", "hn", "github"]}
BUDGET = {"max_tool_calls": 300, "max_steps": 300}
# 一键示例改动：删除一条事实 + 三条长期偏好
SAMPLE_DELETE = "64K 以上"
SAMPLE_PREFS = [("focus", ["大海捞针"]), ("exclude", ["踩坑"]), ("reading_minutes", 10)]


def _tasks():
    rows = db.query("SELECT id, status, params, created_at, updated_at, error FROM tasks "
                    "WHERE topic=? AND graph='research' ORDER BY created_at", (TOPIC,))
    out = {}
    for r in rows:
        step = (r["params"] or {}).get("demo_step")
        if step in (1, 2):
            out[step] = r          # 同一步跑了多次时取最新的
    return out


def _edits(after: float | None, before: float | None) -> list[dict]:
    if not after:
        return []
    rows = db.query("SELECT e.memory_id, e.event, e.actor, e.detail, e.created_at, m.kind, m.content, m.status, m.meta "
                    "FROM memory_events e JOIN memories m ON m.id=e.memory_id WHERE e.actor='user' "
                    "AND e.event IN ('delete','supersede','create','update','archive') AND e.created_at>? "
                    "AND e.created_at<=? AND (m.topic=? OR m.scope='user') ORDER BY e.created_at",
                    (after, before or db.now() + 1, TOPIC))
    names = {"delete": "删除", "supersede": "更正", "create": "新建", "update": "修改", "archive": "不再参考"}
    return [{"id": r["memory_id"], "op": names.get(r["event"], r["event"]), "kind": r["kind"],
             "content": r["content"][:100], "status": r["status"], "at": r["created_at"]} for r in rows
            # 更正会新建一条接替的记忆，那条“新建”不单独列出（“更正”已经说明了）
            if not (r["event"] == "create" and "corrected_from" in str(r["meta"] or ""))]


def status() -> dict:
    t = _tasks()
    t1, t2 = t.get(1), t.get(2)
    facts = mem.list_memories(scope="topic", topic=TOPIC, kind="fact", status="alive") if t1 else []
    prefs = mem.list_memories(scope="user", kind="preference", status="alive")
    out = {"enabled": replay.STATE["enabled"], "day": replay.info().get("day", 1), "topic": TOPIC,
           "run1": t1 and {"id": t1["id"], "status": t1["status"], "error": t1["error"]},
           "run2": t2 and {"id": t2["id"], "status": t2["status"], "error": t2["error"]},
           "facts": [{"id": f["id"], "content": f["content"], "verify": (f["meta"] or {}).get("verify")}
                     for f in facts][:12],
           "prefs": [{"id": p["id"], "content": p["content"]} for p in prefs],
           "edits": _edits(t1 and t1["status"] == "done" and t1["updated_at"],
                           t2 and t2["created_at"]) if t1 else []}
    if t2 and t2["status"] == "done":
        full = db.query_one("SELECT state FROM tasks WHERE id=?", (t2["id"],))["state"]
        out["compare"] = (full.get("report") or {}).get("compare")
        # 现在再召回一次（不计使用次数），逐条确认失效的记忆不出现
        rec = mem.recall(topic=TOPIC, record_use=False, budget_tokens=4000)
        dead = db.query("SELECT id, kind, content, status FROM memories WHERE (topic=? OR scope='user') "
                        "AND status IN ('deleted','superseded','invalid','archived')", (TOPIC,))
        out["recall_now"] = {"lines": rec["text"].splitlines(),
                             "dead": [{"id": d["id"], "kind": d["kind"], "content": d["content"][:100],
                                       "status": d["status"], "recalled": d["id"] in rec["used"]} for d in dead]}
    return out


def _need_replay():
    if not replay.STATE["enabled"]:
        raise RuntimeError("需要在回放模式下运行：双击“演示回放.command”，或 bash start.command --replay")


def start_run(step: int) -> str:
    """第 1 次任务用第 1 天的样例，第 2 次用第 2 天的样例。"""
    from .harness import create_task
    _need_replay()
    if step == 2 and not (_tasks().get(1) or {}).get("status") == "done":
        raise ValueError("先完成第一次任务")
    replay.set_day(step)
    return create_task("research", TOPIC, params={**PARAMS, "demo_step": step, "no_llm": True},
                       budget=BUDGET)


def apply_sample() -> dict:
    """示例改动：删除“64K 以上检索准确率下降”这条事实，设置关注重点 / 排除内容 / 阅读时间三条长期偏好。"""
    _need_replay()
    done = []
    f = next((x for x in mem.list_memories(scope="topic", topic=TOPIC, kind="fact", status="alive")
              if SAMPLE_DELETE in x["content"]), None)
    if f:
        r = mem.delete(f["id"], reason="演示：认为这条复现结论不可靠")
        done.append(f"删除事实「{f['content'][:30]}…」，连带失效 {len(r['invalidated'])} 条派生记忆")
    for k, v in SAMPLE_PREFS:
        mem.set_preference("default", k, v)
        done.append(f"设置长期偏好 {mem.PREF_TYPES[k]}：{v if not isinstance(v, list) else '、'.join(v)}")
    return {"done": done}


def reset() -> dict:
    """清空演示主题的任务、资料和记忆（只在回放数据库里），以及用户偏好，从头再演示。"""
    _need_replay()
    ids = [r["id"] for r in db.query("SELECT id FROM memories WHERE topic=? OR scope='user'", (TOPIC,))]
    for mid in ids:
        db.execute("DELETE FROM evidence WHERE claim_id=?", (mid,))
        db.execute("DELETE FROM memory_events WHERE memory_id=?", (mid,))
    db.execute("DELETE FROM memories WHERE topic=? OR scope='user'", (TOPIC,))
    tids = [r["id"] for r in db.query("SELECT id FROM tasks WHERE topic=?", (TOPIC,))]
    for tid in tids:
        for tbl in ("steps", "tool_calls", "checkpoints", "hitl_requests", "rankings", "labels"):
            try:
                db.execute(f"DELETE FROM {tbl} WHERE task_id=?", (tid,))
            except Exception:  # noqa: BLE001  表不存在时忽略
                pass
    db.execute("DELETE FROM tasks WHERE topic=?", (TOPIC,))
    for d in db.query("SELECT id FROM documents WHERE topic=?", (TOPIC,)):
        for tbl, col in (("images", "doc_id"), ("evidence", "doc_id"), ("labels", "doc_id")):
            try:
                db.execute(f"DELETE FROM {tbl} WHERE {col}=?", (d["id"],))
            except Exception:  # noqa: BLE001
                pass
    db.execute("DELETE FROM documents WHERE topic=?", (TOPIC,))
    replay.set_day(1)
    return {"tasks": len(tids), "memories": len(ids)}
