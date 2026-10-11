"""Lv2：价值评估、榜单、基线与指标、发现问题—补充核验—重新评估。"""
from app import db, ranking
from app import memory as mem
from app.harness import create_task, run_task
from tests.test_collect import CollectBase

TOPIC = "开源大模型 长上下文 评测"
PARAMS = {"keywords": ["长上下文"], "en_keywords": ["long context LLM"], "no_llm": True, "max_docs": 30,
          "days": 365, "sources": ["ithome", "36kr", "sspai", "qbitai", "juejin", "arxiv", "hn", "github"]}


def _item(i, value, ev=None, read=2.0, excluded=None):
    return {"id": i, "title": i, "value": value, "event_id": ev or i, "excluded": excluded, "read_min": read,
            "skim_min": ranking.SKIM_MIN, "reasons": ["r"], "top_factor": "relevance", "weak_factor": "timeliness"}


class ListTest(CollectBase):
    def test_budget_event_dedup_and_reasons(self):
        items = [_item("a", 0.9, "e1", 12), _item("b", 0.8, "e1", 3), _item("c", 0.7, read=10),
                 _item("d", 0.6, read=9), _item("e", 0.5, excluded="相关度 0.2 低于门槛 0.4")]
        deep = ranking.build_list(items, 30, "deep")
        self.assertEqual([s["id"] for s in deep["selected"]], ["a", "c"])      # 12+10=22，再加 d(9) 超出 30
        why = {e["id"]: e["why"] for e in deep["excluded"]}
        self.assertIn("同一事件", why["b"])
        self.assertIn("时间预算不够", why["d"])
        self.assertIn("低于门槛", why["e"])
        skim = ranking.build_list(items, 5, "skim")
        self.assertEqual([s["id"] for s in skim["selected"]], ["a", "c", "d"])
        self.assertLessEqual(skim["used"], 5)

    def test_reading_minutes(self):
        self.assertAlmostEqual(ranking.reading_minutes("字" * 4000), 10.0)
        self.assertAlmostEqual(ranking.reading_minutes("word " * 2200), 10.0)


class FlowTest(CollectBase):
    def _run(self, **extra):
        self.web.state["corroborate"] = True
        tid = create_task("research", TOPIC, params={**PARAMS, **extra}, budget={"max_tool_calls": 200, "max_steps": 200})
        self.assertEqual(run_task(tid), "done")
        return tid

    def test_rank_verify_rerank(self):
        tid = self._run()
        hist = ranking.history(tid)
        self.assertEqual([h["trigger"] for h in hist], ["初次评估", "补充核验后重新评估"])
        v1, v2 = hist[0]["data"], hist[1]["data"]
        # 价值排序去重：前 10 篇没有重复；按时间的基线有重复
        m = v1["metrics_auto"]
        self.assertEqual(m["ours"]["dup@10"], 0)
        self.assertGreater(m["time"]["dup@10"], 0)
        for k in ("P@5", "P@10", "coverage@10", "fresh@10", "primary@10"):
            self.assertIn(k, m["ours"])
        # 发现问题 → 核验：掘金找到同一事件的另一篇报道 → 可信度上升；找不到的标注待核实、价值打折
        self.assertTrue(v1["problems"])
        recs = {r["title"]: r for r in v2["verify"]}
        corr = recs["开源大模型长上下文能力遭质疑：复现评测结果不及预期"]
        self.assertEqual(corr["outcome"], "corroborated")
        self.assertGreater(len(corr["hosts_after"]), len(corr["hosts_before"]))
        it1 = {i["id"]: i for i in v1["items"]}
        it2 = {i["id"]: i for i in v2["items"]}
        self.assertGreater(it2[corr["id"]]["features"]["credibility"], it1[corr["id"]]["features"]["credibility"])
        unv = [r for r in v2["verify"] if r["outcome"] == "unverified"]
        self.assertTrue(unv)
        self.assertTrue(it2[unv[0]["id"]]["unverified"])
        self.assertLess(it2[unv[0]["id"]]["value"], it1[unv[0]["id"]]["value"])
        self.assertTrue(v2["diff"]["5"])                      # 榜单有变化
        self.assertEqual(v2["triggered_by"], v1["problems"])
        rep = db.query_one("SELECT state FROM tasks WHERE id=?", (tid,))["state"]["report"]
        self.assertEqual(rep["ranking"]["versions"], [1, 2])

    def test_pref_minutes_list(self):
        mem.set_preference("default", "reading_minutes", 10)
        tid = self._run(verify_top=False)
        d = ranking.latest(tid)
        self.assertEqual(d["pref_minutes"], 10)
        self.assertIn("10", d["lists"])
        self.assertEqual(len(ranking.history(tid)), 1)       # 关闭核验时只有一版


class ApiTest(CollectBase):
    def test_ranking_labels_rerank(self):
        from app import server
        c = server.app.test_client()
        tid = create_task("research", TOPIC, params={**PARAMS, "verify_top": False},
                          budget={"max_tool_calls": 200, "max_steps": 200})
        self.assertEqual(run_task(tid), "done")
        r = c.get(f"/api/tasks/{tid}/ranking").json
        self.assertEqual(r["data"]["version"], 1)
        top = r["data"]["orders"]["ours"][:5]
        # 人工把价值排序前 5 中的 2 篇标为不相关 → P@5 用人工标注重算
        for i in top[:2]:
            self.assertEqual(c.post("/api/labels", json={"topic": TOPIC, "doc_id": i, "label": 0}).status_code, 200)
        r2 = c.get(f"/api/tasks/{tid}/ranking").json
        self.assertEqual(r2["eval"]["metrics"]["ours"]["human@5"], 2)
        self.assertLessEqual(r2["eval"]["metrics"]["ours"]["P@5"], 0.6)
        c.post("/api/labels", json={"topic": TOPIC, "doc_id": top[0], "label": None})
        self.assertEqual(c.get(f"/api/tasks/{tid}/ranking").json["eval"]["metrics"]["ours"]["human@5"], 1)
        # 调整权重同步重排 → 新版本，并有与上一版的差异
        rr = c.post(f"/api/tasks/{tid}/rerank", json={"weights": {"timeliness": 0.8, "relevance": 0.1}})
        self.assertEqual(rr.status_code, 201)
        r3 = c.get(f"/api/tasks/{tid}/ranking").json
        self.assertEqual(r3["data"]["version"], 2)
        self.assertEqual(r3["data"]["weights"]["timeliness"], 0.8)
        self.assertEqual(r3["data"]["diff_from"], 1)
        self.assertEqual(len(r3["versions"]), 2)
        self.assertEqual(c.get(f"/api/tasks/{tid}/ranking?version=1").json["data"]["version"], 1)
        # 只能对主题检索任务重排
        other = create_task("recheck", TOPIC, params={"force": True})
        self.assertEqual(c.post(f"/api/tasks/{other}/rerank", json={}).status_code, 400)
        self.assertEqual(c.post(f"/api/tasks/{tid}/rerank", json={"weights": {"relevance": "x"}}).status_code, 400)
