"""用几组不同的测试数据各起一个主题编辑台，再跑页面巡检（开发自测用，不连网、不用真大模型）。

场景：
  empty   空库：什么都没有时每个页面能不能正常显示
  full    完整：检索任务、多种写法的文章（含审阅修改出的多个版本）、初稿/优化稿实验（含 1 篇失败、盲评、外部检测分数）、
          多种 Skill 对照实验、写作偏好记忆
用法：python tools/ui_scenarios.py --out /tmp/ui
"""
from __future__ import annotations

import argparse
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def serve(port):
    from werkzeug.serving import make_server
    from app.server import create_app
    from app import server
    server.runner.start = lambda tid: None
    srv = make_server("127.0.0.1", port, create_app(), threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def build_full(tc):
    from app import llm, db
    from app.harness import run_task
    from app.graphs import experiment as E
    from tests.test_write import JudgeWriter
    llm.available, llm.chat = (lambda: True), JudgeWriter()
    from app.server import create_app
    c = create_app().test_client()
    arts = []
    for sk in ("brief", "timeline", "faq"):
        t, _ = tc.write(sk)
        arts.append(t["state"]["result"]["article_id"])
    aid = arts[0]
    for body in ({"op": "title", "title": "长上下文评测：宣称与复现的差距（修订）"},
                 {"op": "rewrite", "section": "lead", "para": 0, "instruction": "更口语一点", "remember": True}):
        tid = c.post(f"/api/articles/{aid}/revise", json=body).get_json()["id"]
        run_task(tid)
    orig = E._run_one
    E._run_one = lambda r, p, u: {"write_task": None, "status": "failed", "seconds": 1, "article_id": None, "version": None,
                                  "error": "节点 draft 失败：timed out"} if r["variant"] == "natural_only" else orig(r, p, u)
    eid = c.post("/api/experiments", json={"research_tasks": [tc.t1], "skill": "brief", "detector": "manual"}).get_json()["id"]
    run_task(eid)
    E._run_one = orig
    rep = c.get(f"/api/experiments/{eid}").get_json()
    for r, sc in zip([x for x in rep["rows"] if x["status"] == "done"], (91, 80, 63)):
        c.post(f"/api/experiments/{eid}/scores", json={"run_key": r["key"], "detector": "外部检测器X", "score": sc})
    pair = c.get(f"/api/experiments/{eid}/blind?rater=a").get_json()["pairs"][0]
    c.post(f"/api/experiments/{eid}/blind", json={"pair_id": pair["pair_id"], "rater": "读者A", "answers": {
        "accuracy": "甲", "information": "乙", "natural": "相当", "easy": "甲", "overall": "乙", "ai_guess": "说不准",
        "comment": "乙更好读，但甲的数字更具体"}})
    sid = c.post("/api/experiments", json={"kind": "skills", "research_tasks": [tc.t1], "skills": ["brief", "timeline", "compare"]}).get_json()["id"]
    run_task(sid)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/ui_check")
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    from tests.test_write import WriteBase
    from tools.ui_check import run
    results = {}
    for name in ("empty", "full"):
        if a.only and name != a.only:
            continue

        class T(WriteBase):
            def runTest(self):
                pass
        tc = T()
        if name == "empty":
            from tests.test_collect import CollectBase
            tc = type("E", (CollectBase,), {"runTest": lambda self: None})()
        tc.setUp()
        try:
            if name == "full":
                build_full(tc)
            srv = serve(8790)
            try:
                results[name] = run("http://127.0.0.1:8790", f"{a.out}/{name}", 4)
            finally:
                srv.shutdown()
        finally:
            tc.tearDown()
    for name, s in results.items():
        print(f"== 场景 {name}：检查 {s['checks']} 次，{s['with_issues']} 次有问题")
        for x in s["issues"]:
            print(f"- [{x['view']}] {x['page']}：{'；'.join(x['issues'])[:400]}")


if __name__ == "__main__":
    main()
