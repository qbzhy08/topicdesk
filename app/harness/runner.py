"""后台执行器：每个任务一个线程，防止重复运行；服务启动时处理上次中断的任务。"""
import threading

from .. import db
from .core import run_task

_lock = threading.Lock()
_running: dict[str, threading.Thread] = {}


def _worker(task_id: str):
    try:
        run_task(task_id)
    except Exception as e:  # 防御：执行器本身异常也要落库
        db.update("tasks", "id", task_id, dict(status="failed", error=f"runner: {e}",
                                               updated_at=db.now()))
    finally:
        with _lock:
            _running.pop(task_id, None)
        db.reset()


def start(task_id: str) -> bool:
    """在后台启动或恢复任务。已在运行则返回 False。"""
    with _lock:
        t = _running.get(task_id)
        if t and t.is_alive():
            return False
        t = threading.Thread(target=_worker, args=(task_id,), daemon=True, name=f"task-{task_id}")
        _running[task_id] = t
        t.start()
        return True


def is_running(task_id: str) -> bool:
    with _lock:
        t = _running.get(task_id)
        return bool(t and t.is_alive())


def cancel(task_id: str):
    row = db.query_one("SELECT status FROM tasks WHERE id=?", (task_id,))
    if row is None:
        return
    if is_running(task_id):
        db.update("tasks", "id", task_id, dict(status="cancelling", updated_at=db.now()))
    elif row["status"] not in ("done",):
        db.update("tasks", "id", task_id, dict(status="cancelled", updated_at=db.now()))


def recover_on_boot(auto_resume: bool = False) -> list[str]:
    """进程重启后：上次处于 running/cancelling 的任务标记为 interrupted。
    auto_resume=True 时直接从 checkpoint 继续。"""
    rows = db.query("SELECT id, status FROM tasks WHERE status IN ('running','cancelling','pending')")
    ids = []
    for r in rows:
        if r["status"] == "cancelling":
            db.update("tasks", "id", r["id"], dict(status="cancelled"))
            continue
        db.update("tasks", "id", r["id"], dict(status="interrupted", updated_at=db.now()))
        ids.append(r["id"])
        if auto_resume:
            start(r["id"])
    return ids


def wait(task_id: str, timeout: float = 30):
    with _lock:
        t = _running.get(task_id)
    if t:
        t.join(timeout)
