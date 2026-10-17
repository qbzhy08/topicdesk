import unittest

from app import db
from app.graphs import demo
from app.harness import GRAPHS, create_task, last_checkpoint, resolve_hitl, rewind, run_task, runner
from tests.helpers import DBTestCase


def steps(tid):
    return db.query("SELECT * FROM steps WHERE task_id=? ORDER BY seq, id", (tid,))


def calls(tid, tool="demo_search"):
    return db.query("SELECT * FROM tool_calls WHERE task_id=? AND tool=? ORDER BY id", (tid, tool))


class HarnessTest(DBTestCase):

    def test_hitl_pause_and_resume(self):
        tid = create_task("demo", "测试主题")
        self.assertEqual(run_task(tid), "waiting_human")
        req = db.query_one("SELECT * FROM hitl_requests WHERE task_id=?", (tid,))
        self.assertEqual(req["status"], "open")
        self.assertEqual(len(req["payload"]["options"]), 5)

        # 再跑一次不会越过人工节点
        self.assertEqual(run_task(tid), "waiting_human")
        self.assertEqual(db.query_one("SELECT COUNT(*) n FROM hitl_requests WHERE task_id=?",
                                      (tid,))["n"], 1)

        resolve_hitl(req["id"], {"keep": ["官方公告 C", "独立复现 D"]})
        self.assertEqual(run_task(tid), "done")
        task = db.query_one("SELECT * FROM tasks WHERE id=?", (tid,))
        self.assertEqual(task["state"]["result"]["items"], ["官方公告 C", "独立复现 D"])
        # 决策依据写入了轨迹：assess 节点说明了为什么继续检索
        thoughts = [s["thought"] for s in steps(tid) if s["node"] == "assess"]
        self.assertTrue(any("缺口仍在" in t for t in thoughts))
        self.assertTrue(any("覆盖足够" in t for t in thoughts))

    def test_crash_and_resume_from_checkpoint(self):
        tid = create_task("demo", "崩溃恢复", params={"hitl": False, "need": 5})
        real_search = GRAPHS["demo"].nodes["search"]
        n = {"i": 0}

        def crashing_search(ctx):
            n["i"] += 1
            out = real_search(ctx)
            if n["i"] == 2:
                raise KeyboardInterrupt("模拟进程被杀")   # BaseException，Harness 不会吞掉
            return out

        GRAPHS["demo"].nodes["search"] = crashing_search
        try:
            with self.assertRaises(KeyboardInterrupt):
                run_task(tid)
        finally:
            GRAPHS["demo"].nodes["search"] = real_search

        self.assertEqual(db.query_one("SELECT status FROM tasks WHERE id=?", (tid,))["status"],
                         "running")
        self.assertEqual(runner.recover_on_boot(), [tid])
        self.assertEqual(db.query_one("SELECT status FROM tasks WHERE id=?", (tid,))["status"],
                         "interrupted")

        self.assertEqual(run_task(tid), "done")
        task = db.query_one("SELECT * FROM tasks WHERE id=?", (tid,))
        self.assertEqual(len(task["state"]["result"]["items"]), 5)
        # 崩溃那一步的工具调用在重跑时命中缓存，没有重复执行
        official = [c for c in calls(tid) if c["input"]["query"] == "official"]
        self.assertEqual([c["status"] for c in official], ["ok", "cached"])

    def test_tool_failure_retry(self):
        demo._flaky_calls["n"] = 0
        tid = create_task("demo", "重试", params={"queries": ["flaky", "overview"], "hitl": False,
                                                 "need": 2})
        self.assertEqual(run_task(tid), "done")
        st = [c["status"] for c in calls(tid) if c["input"]["query"] == "flaky"]
        self.assertEqual(st, ["error", "ok"])
        search_steps = [s["status"] for s in steps(tid) if s["node"] == "search"]
        self.assertEqual(search_steps[:2], ["failed", "ok"])

    def test_permanent_errors_fail_fast(self):
        """确定性错误（输入不合法、代码 bug）重跑结果一样：不重试，直接失败并说明；暂时性错误（超时）重试一次。"""
        from app.harness import Graph, error_kind, register_graph
        n = {"calls": 0}

        def bad(ctx):
            n["calls"] += 1
            raise ValueError("没有可用的素材")
        register_graph(Graph(name="t_perm", nodes={"bad": bad}, start="bad"))
        tid = create_task("t_perm", "x")
        self.assertEqual(run_task(tid), "failed")
        self.assertEqual(n["calls"], 1)                       # 没有白白重试
        self.assertIn("重试也不会变", db.query_one("SELECT error FROM tasks WHERE id=?", (tid,))["error"])
        self.assertEqual(error_kind(TimeoutError()), "transient")
        self.assertEqual(error_kind(KeyError("x")), "permanent")

    def test_budget_exhausted_goes_to_finalize(self):
        tid = create_task("demo", "预算", params={"hitl": False, "need": 99},
                          budget={"max_tool_calls": 1})
        self.assertEqual(run_task(tid), "done")
        task = db.query_one("SELECT * FROM tasks WHERE id=?", (tid,))
        self.assertIn("工具调用达到上限", task["state"]["result"]["budget_note"])
        self.assertEqual(len([c for c in calls(tid) if c["status"] == "ok"]), 1)
        self.assertTrue(any(s["node"] == "__harness__" for s in steps(tid)))

    def test_rewind_reuses_intermediate_results(self):
        tid = create_task("demo", "回退", params={"hitl": False})
        run_task(tid)
        n_calls = len(calls(tid))
        rewind(tid, "review", note="用户想重新挑选")
        task = db.query_one("SELECT * FROM tasks WHERE id=?", (tid,))
        self.assertEqual(task["status"], "pending")
        self.assertEqual(run_task(tid), "done")
        self.assertEqual(len(calls(tid)), n_calls)   # 只重跑审阅和收尾，不重新检索
        self.assertEqual(last_checkpoint(tid)["next_node"], "__end__")

    def test_cancel(self):
        tid = create_task("demo", "取消")
        db.update("tasks", "id", tid, {"status": "cancelling"})
        self.assertEqual(run_task(tid), "cancelled")


if __name__ == "__main__":
    unittest.main()


class SharedToolTest(unittest.TestCase):
    """同一次实验里的相同检索只真正执行一次；不同实验、独立任务各自执行。"""

    def test_scope(self):
        import threading
        from app.harness import core
        n = {"c": 0}

        def slow(q):
            n["c"] += 1
            import time
            time.sleep(0.2)
            return {"q": q}
        spec = core.ToolSpec("t_share", slow, "", 0.0, True, 60)
        h = core._hash({"q": "x"})
        outs = []
        ths = [threading.Thread(target=lambda: outs.append(core._invoke(spec, {"q": "x"}, h, "exp1"))) for _ in range(3)]
        [t.start() for t in ths]
        [t.join() for t in ths]
        self.assertEqual(n["c"], 1)                                      # 同时发起：只执行一次
        self.assertEqual(sorted(s for _, s in outs), [False, True, True])
        core._invoke(spec, {"q": "x"}, h, "exp2")
        core._invoke(spec, {"q": "x"}, h, None)
        self.assertEqual(n["c"], 3)                                      # 别的实验、独立任务不复用
