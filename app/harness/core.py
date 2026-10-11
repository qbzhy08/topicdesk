"""Harness 核心：图式编排 + 追踪 + Checkpoint + 预算 + HITL。

一张 Graph 由若干节点函数组成。节点签名 `fn(ctx) -> str`，返回下一个节点名
（或 END）。节点内部通过 ctx 做三件事：

- ctx.tool(name, **input)   调用已注册工具：记录输入/输出/耗时/成本，计入预算，
                            同一任务内相同输入的成功结果会被缓存——崩溃后重跑
                            节点不会重复抓取。
- ctx.human(key, ...)       需要人工判断时调用：已有答复则直接返回，否则抛出
                            Interrupt，任务进入 waiting_human 并保存 checkpoint。
- ctx.think(...) / ctx.observe(...)  写入本步的“决策依据”和“观察摘要”，
                            前端时间线据此展示“已有结果怎样影响下一步”。

每个节点结束后写一次 checkpoint（state + usage + next_node）。任何时候中断
（进程崩溃、服务重启、人工暂停），都从最后一个 checkpoint 继续。
"""
from __future__ import annotations

import hashlib
import json
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import db
from ..config import DEFAULT_BUDGET

END = "__end__"


# ---------------------------------------------------------------- 异常与注册表

class Interrupt(Exception):
    """节点请求人工介入。"""

    def __init__(self, key: str, kind: str, prompt: str, payload: dict | None = None):
        super().__init__(prompt)
        self.key, self.kind, self.prompt, self.payload = key, kind, prompt, payload or {}


class BudgetExceeded(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Cancelled(Exception):
    pass


@dataclass
class ToolSpec:
    name: str
    fn: Callable[..., Any]
    description: str = ""
    cost: float = 0.0           # 每次调用的估计成本（LLM 工具可在结果里返回实际成本）
    cacheable: bool = True


TOOLS: dict[str, ToolSpec] = {}


def tool(name: str, description: str = "", cost: float = 0.0, cacheable: bool = True):
    """注册工具的装饰器。工具返回值必须可 JSON 序列化。"""
    def deco(fn):
        TOOLS[name] = ToolSpec(name, fn, description or (fn.__doc__ or "").strip(), cost, cacheable)
        return fn
    return deco


@dataclass
class Graph:
    name: str
    nodes: dict[str, Callable[["Ctx"], str]]
    start: str
    on_budget: str | None = None     # 预算耗尽时跳转的收尾节点
    max_retries: int = 1             # 节点异常后的自动重试次数
    init_state: Callable[[dict], dict] = field(default=lambda task: {})


GRAPHS: dict[str, Graph] = {}


def register_graph(g: Graph):
    GRAPHS[g.name] = g
    return g


# ---------------------------------------------------------------- 运行上下文

def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
                          .encode()).hexdigest()[:24]


class Ctx:
    def __init__(self, task: dict, state: dict, usage: dict, budget: dict, step_id: int,
                 node: str, grace: bool = False):
        self.task = task
        self.task_id = task["id"]
        self.topic = task["topic"]
        self.user_id = task["user_id"]
        self.params = task["params"]
        self.state = state
        self.usage = usage
        self.budget = budget
        self.step_id = step_id
        self.node = node
        self.grace = grace          # 收尾节点允许在预算耗尽后少量调用
        self._thoughts: list[str] = []
        self._obs: list[str] = []

    # ---- 轨迹
    def think(self, text: str):
        self._thoughts.append(text)

    def observe(self, text: str):
        self._obs.append(text)

    # ---- 预算
    def budget_left(self) -> dict:
        b, u = self.budget, self.usage
        return {
            "steps": b["max_steps"] - u.get("steps", 0),
            "tool_calls": b["max_tool_calls"] - u.get("tool_calls", 0),
            "seconds": b["max_seconds"] - u.get("seconds", 0),
            "cost": round(b["max_cost"] - u.get("cost", 0), 4),
        }

    def check_budget(self):
        if self.grace:
            return
        reason = budget_reason(self.budget, self.usage)
        if reason:
            raise BudgetExceeded(reason)

    def charge(self, cost: float):
        self.usage["cost"] = round(self.usage.get("cost", 0) + cost, 6)

    # ---- 工具调用
    def tool(self, name: str, _cache: bool = True, **inp):
        spec = TOOLS.get(name)
        if spec is None:
            raise KeyError(f"unknown tool: {name}")
        h = _hash(inp)
        if _cache and spec.cacheable:
            hit = db.query_one(
                "SELECT output FROM tool_calls WHERE task_id=? AND tool=? AND input_hash=? "
                "AND status='ok' ORDER BY id DESC LIMIT 1", (self.task_id, name, h))
            if hit is not None:
                db.insert("tool_calls", dict(task_id=self.task_id, step_id=self.step_id, tool=name,
                                             input=inp, input_hash=h, output=hit["output"],
                                             status="cached", latency_ms=0, cost=0,
                                             created_at=db.now()))
                return hit["output"]
        self.check_budget()
        _check_cancel(self.task_id)
        t0 = time.time()
        try:
            out = spec.fn(**inp)
        except Exception as e:  # 工具失败也要留痕，由节点决定降级还是上抛
            self.usage["tool_calls"] = self.usage.get("tool_calls", 0) + 1
            db.insert("tool_calls", dict(task_id=self.task_id, step_id=self.step_id, tool=name,
                                         input=inp, input_hash=h, status="error",
                                         error=f"{type(e).__name__}: {e}"[:2000],
                                         latency_ms=int((time.time() - t0) * 1000),
                                         cost=0, created_at=db.now()))
            raise
        cost = spec.cost
        if isinstance(out, dict) and "_cost" in out:
            cost = out.pop("_cost")
        self.usage["tool_calls"] = self.usage.get("tool_calls", 0) + 1
        self.charge(cost)
        db.insert("tool_calls", dict(task_id=self.task_id, step_id=self.step_id, tool=name,
                                     input=inp, input_hash=h, output=out, status="ok",
                                     latency_ms=int((time.time() - t0) * 1000), cost=cost,
                                     created_at=db.now()))
        return out

    def tool_parallel(self, calls: list[tuple[str, dict]], max_workers: int = 6) -> list[tuple[str, object]]:
        """并行执行多个工具调用（不同站点互不等待）。返回与 calls 等长的 [("ok", 输出) | ("error", 异常)]。
        缓存命中、预算、取消检查与逐条留痕都与 tool() 一致；只是真正的网络请求并发进行。"""
        from concurrent.futures import ThreadPoolExecutor
        results: list = [None] * len(calls)
        todo = []
        for i, (name, inp) in enumerate(calls):
            spec = TOOLS.get(name)
            if spec is None:
                results[i] = ("error", KeyError(f"unknown tool: {name}"))
                continue
            h = _hash(inp)
            hit = db.query_one(
                "SELECT output FROM tool_calls WHERE task_id=? AND tool=? AND input_hash=? "
                "AND status='ok' ORDER BY id DESC LIMIT 1", (self.task_id, name, h)) if spec.cacheable else None
            if hit is not None:
                db.insert("tool_calls", dict(task_id=self.task_id, step_id=self.step_id, tool=name,
                                             input=inp, input_hash=h, output=hit["output"],
                                             status="cached", latency_ms=0, cost=0, created_at=db.now()))
                results[i] = ("ok", hit["output"])
            else:
                todo.append((i, name, inp, h, spec))
        if todo:
            self.check_budget()
            _check_cancel(self.task_id)

            def run(item):
                i, name, inp, h, spec = item
                t0 = time.time()
                try:
                    return i, "ok", spec.fn(**inp), int((time.time() - t0) * 1000)
                except Exception as e:
                    return i, "error", e, int((time.time() - t0) * 1000)
                finally:
                    db.reset()          # 工作线程的数据库连接用完即关
            with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(todo)))) as ex:
                done = list(ex.map(run, todo))
            meta = {i: (name, inp, h, spec) for i, name, inp, h, spec in todo}
            for i, status, out, ms in done:
                name, inp, h, spec = meta[i]
                self.usage["tool_calls"] = self.usage.get("tool_calls", 0) + 1
                if status == "error":
                    db.insert("tool_calls", dict(task_id=self.task_id, step_id=self.step_id, tool=name,
                                                 input=inp, input_hash=h, status="error",
                                                 error=f"{type(out).__name__}: {out}"[:2000], latency_ms=ms,
                                                 cost=0, created_at=db.now()))
                    results[i] = ("error", out)
                    continue
                cost = spec.cost
                if isinstance(out, dict) and "_cost" in out:
                    cost = out.pop("_cost")
                self.charge(cost)
                db.insert("tool_calls", dict(task_id=self.task_id, step_id=self.step_id, tool=name,
                                             input=inp, input_hash=h, output=out, status="ok",
                                             latency_ms=ms, cost=cost, created_at=db.now()))
                results[i] = ("ok", out)
        return results

    # ---- 人工介入
    def human(self, key: str, kind: str, prompt: str, payload: dict | None = None):
        answers = self.state.setdefault("_human", {})
        if key in answers:
            return answers[key]
        raise Interrupt(key, kind, prompt, payload)


def budget_reason(budget: dict, usage: dict) -> str | None:
    if usage.get("steps", 0) >= budget["max_steps"]:
        return f"步数达到上限 {budget['max_steps']}"
    if usage.get("tool_calls", 0) >= budget["max_tool_calls"]:
        return f"工具调用达到上限 {budget['max_tool_calls']}"
    if usage.get("seconds", 0) >= budget["max_seconds"]:
        return f"运行时间达到上限 {budget['max_seconds']}s"
    if usage.get("cost", 0) >= budget["max_cost"]:
        return f"成本达到上限 ${budget['max_cost']}"
    return None


def _check_cancel(task_id: str):
    row = db.query_one("SELECT status FROM tasks WHERE id=?", (task_id,))
    if row and row["status"] == "cancelling":
        raise Cancelled()


# ---------------------------------------------------------------- 任务生命周期

def create_task(graph: str, topic: str, params: dict | None = None, user_id: str = "default",
                budget: dict | None = None, parent_task_id: str | None = None) -> str:
    if graph not in GRAPHS:
        raise KeyError(f"unknown graph: {graph}")
    tid = db.new_id("t_")
    b = {**DEFAULT_BUDGET, **(budget or {})}
    db.insert("tasks", dict(id=tid, graph=graph, topic=topic, user_id=user_id, status="pending",
                            params=params or {}, state={}, budget=b, usage={},
                            parent_task_id=parent_task_id, created_at=db.now(),
                            updated_at=db.now()))
    return tid


def last_checkpoint(task_id: str) -> dict | None:
    return db.query_one("SELECT * FROM checkpoints WHERE task_id=? ORDER BY id DESC LIMIT 1",
                        (task_id,))


def save_checkpoint(task_id: str, seq: int, next_node: str, state: dict, usage: dict):
    db.insert("checkpoints", dict(task_id=task_id, step_seq=seq, next_node=next_node,
                                  state=state, usage=usage, created_at=db.now()))
    db.update("tasks", "id", task_id, dict(state=state, usage=usage, current_node=next_node,
                                           updated_at=db.now()))


def _set_status(task_id: str, status: str, error: str | None = None):
    db.update("tasks", "id", task_id, dict(status=status, error=error, updated_at=db.now()))


def run_task(task_id: str) -> str:
    """同步执行任务直到结束、等待人工、失败或取消。返回最终状态。可重复调用以恢复。"""
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if task is None:
        raise KeyError(task_id)
    if task["status"] in ("done", "cancelled"):
        return task["status"]
    if task["status"] == "cancelling":
        _set_status(task_id, "cancelled")
        return "cancelled"
    graph = GRAPHS[task["graph"]]

    ck = last_checkpoint(task_id)
    if ck:
        node, state, usage, seq = ck["next_node"], ck["state"], ck["usage"], ck["step_seq"]
    else:
        node, state, usage, seq = graph.start, graph.init_state(task), {}, 0
        save_checkpoint(task_id, 0, node, state, usage)

    _set_status(task_id, "running")
    budget = task["budget"]

    while node != END:
        # 外部取消
        if db.query_one("SELECT status FROM tasks WHERE id=?", (task_id,))["status"] == "cancelling":
            _set_status(task_id, "cancelled")
            return "cancelled"

        grace = False
        reason = budget_reason(budget, usage)
        if reason:
            if graph.on_budget and not state.get("_budget_exhausted"):
                state["_budget_exhausted"] = reason
                _record_system_step(task_id, seq := seq + 1, node,
                                    f"预算耗尽：{reason}，跳过 {node}，转入收尾节点 {graph.on_budget}",
                                    graph.on_budget)
                node = graph.on_budget
                save_checkpoint(task_id, seq, node, state, usage)
            # 进入收尾路径后（on_budget 节点及其后续节点）允许少量超支，但设硬上限防止失控
            if graph.on_budget and state.get("_budget_exhausted") and \
                    usage.get("steps", 0) < budget["max_steps"] + 15:
                grace = True
            else:
                _set_status(task_id, "failed", f"预算耗尽：{reason}")
                return "failed"

        seq += 1
        step_id = db.insert("steps", dict(task_id=task_id, seq=seq, node=node, status="running",
                                          started_at=db.now()))
        # 节点开始前的状态快照：节点没有正常结束时回滚到这里，避免“半执行”的修改被重复应用
        snapshot = json.loads(json.dumps(state, default=str))
        ctx = Ctx(task, state, usage, budget, step_id, node, grace=grace)
        t0 = time.time()
        try:
            nxt = graph.nodes[node](ctx)
            if nxt is None:
                nxt = END
            if nxt != END and nxt not in graph.nodes:
                raise ValueError(f"node {node} returned unknown next node {nxt}")
        except Interrupt as it:
            ctx.observe(f"等待人工：{it.prompt}")
            _finish_step(step_id, ctx, "interrupted", node, t0, usage)
            _open_hitl(task_id, node, it)
            state = snapshot
            save_checkpoint(task_id, seq, node, state, usage)   # 恢复后重跑本节点
            _set_status(task_id, "waiting_human")
            return "waiting_human"
        except BudgetExceeded as be:
            _finish_step(step_id, ctx, "failed", graph.on_budget or END, t0, usage,
                         error=f"预算耗尽：{be.reason}")
            state = snapshot
            if graph.on_budget and not state.get("_budget_exhausted"):
                state["_budget_exhausted"] = be.reason
                node = graph.on_budget
                save_checkpoint(task_id, seq, node, state, usage)
                continue
            save_checkpoint(task_id, seq, node, state, usage)
            _set_status(task_id, "failed", f"预算耗尽：{be.reason}")
            return "failed"
        except Cancelled:
            _finish_step(step_id, ctx, "failed", node, t0, usage, error="用户取消")
            state = snapshot
            save_checkpoint(task_id, seq, node, state, usage)
            _set_status(task_id, "cancelled")
            return "cancelled"
        except Exception as e:
            tb = traceback.format_exc(limit=6)
            state = snapshot
            retries = state.setdefault("_retries", {})
            retries[node] = retries.get(node, 0) + 1
            _finish_step(step_id, ctx, "failed", node, t0, usage, error=f"{e}\n{tb}")
            save_checkpoint(task_id, seq, node, state, usage)
            if retries[node] <= graph.max_retries:
                time.sleep(min(2 ** retries[node], 8) * 0.1)
                continue  # 从同一节点重试，工具缓存保证已完成的调用不重复
            _set_status(task_id, "failed", f"节点 {node} 失败：{e}")
            return "failed"

        _finish_step(step_id, ctx, "ok", nxt, t0, usage)
        state.get("_retries", {}).pop(node, None)
        node = nxt
        save_checkpoint(task_id, seq, node, state, usage)

    _set_status(task_id, "done")
    return "done"


def _finish_step(step_id, ctx: Ctx, status, next_node, t0, usage, error=None):
    usage["steps"] = usage.get("steps", 0) + 1
    usage["seconds"] = round(usage.get("seconds", 0) + (time.time() - t0), 3)
    db.update("steps", "id", step_id, dict(
        status=status, thought="\n".join(ctx._thoughts) or None,
        summary="\n".join(ctx._obs) or None, next_node=next_node, error=error,
        ended_at=db.now()))


def _record_system_step(task_id, seq, node, text, next_node):
    db.insert("steps", dict(task_id=task_id, seq=seq, node="__harness__", status="ok",
                            thought=text, next_node=next_node, started_at=db.now(),
                            ended_at=db.now()))


def _open_hitl(task_id: str, node: str, it: Interrupt) -> str:
    existing = db.query_one("SELECT id FROM hitl_requests WHERE task_id=? AND node=? AND "
                            "status='open' AND json_extract(payload,'$._key')=?",
                            (task_id, node, it.key))
    if existing:
        return existing["id"]
    rid = db.new_id("h_")
    db.insert("hitl_requests", dict(id=rid, task_id=task_id, node=node, kind=it.kind,
                                    prompt=it.prompt, payload={**it.payload, "_key": it.key},
                                    status="open", created_at=db.now()))
    return rid


def resolve_hitl(req_id: str, response: Any) -> str:
    """写入人工答复，并生成一个新的 checkpoint（状态更新），返回 task_id。"""
    req = db.query_one("SELECT * FROM hitl_requests WHERE id=?", (req_id,))
    if req is None or req["status"] != "open":
        raise ValueError("HITL 请求不存在或已处理")
    task_id = req["task_id"]
    ck = last_checkpoint(task_id)
    state = ck["state"]
    state.setdefault("_human", {})[req["payload"]["_key"]] = response
    state.setdefault("_human_log", []).append(
        {"key": req["payload"]["_key"], "node": req["node"], "response": response, "at": db.now()})
    save_checkpoint(task_id, ck["step_seq"], ck["next_node"], state, ck["usage"])
    db.update("hitl_requests", "id", req_id, dict(status="resolved", response=db.dumps(response),
                                                  resolved_at=db.now()))
    _set_status(task_id, "pending")
    return task_id


def patch_state(task_id: str, patch: dict, note: str = "manual patch"):
    """人工直接修改任务状态（如修改选题、替换图片）。生成新 checkpoint 留痕。"""
    ck = last_checkpoint(task_id)
    state = ck["state"]
    state.update(patch)
    state.setdefault("_human_log", []).append({"patch": list(patch), "note": note, "at": db.now()})
    save_checkpoint(task_id, ck["step_seq"], ck["next_node"], state, ck["usage"])


def rewind(task_id: str, node: str, invalidate: list[str] | None = None, note: str = ""):
    """把任务退回到某个节点重新执行（局部重跑）。invalidate 指定要清空的 state 键，
    其余中间结果保留复用。"""
    ck = last_checkpoint(task_id)
    graph = GRAPHS[db.query_one("SELECT graph FROM tasks WHERE id=?", (task_id,))["graph"]]
    if node not in graph.nodes:
        raise KeyError(node)
    state = ck["state"]
    for k in invalidate or []:
        state.pop(k, None)
    state.pop("_budget_exhausted", None)
    # 回退到的节点上之前给过的人工答复也作废，重跑时会再次询问
    stale = {l["key"] for l in state.get("_human_log", []) if l.get("node") == node and "key" in l}
    for k in stale:
        state.get("_human", {}).pop(k, None)
    state.setdefault("_human_log", []).append(
        {"rewind_to": node, "invalidated": invalidate or [], "note": note, "at": db.now()})
    _record_system_step(task_id, ck["step_seq"] + 1, node,
                        f"人工回退到 {node}，失效中间结果：{invalidate or '无'}。{note}", node)
    save_checkpoint(task_id, ck["step_seq"] + 1, node, state, ck["usage"])
    _set_status(task_id, "pending")
