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
