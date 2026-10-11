"""长期记忆：两次独立任务（重复资料、新进展、旧结论更正）、偏好影响结果、更正/删除后不再召回。"""
from app import config, db
from app import memory as mem
from app.collect import fetch
from app.harness import create_task, run_task
from tests.test_collect import CollectBase

TOPIC = "开源大模型 长上下文 评测"
PARAMS = {"keywords": ["长上下文"], "en_keywords": ["long context LLM"], "no_llm": True, "max_docs": 30,
          "sources": ["ithome", "36kr", "sspai", "juejin", "arxiv", "hn", "github"]}


class MemoryTest(CollectBase):
    def run_research(self, **extra):
        tid = create_task("research", TOPIC, params={**PARAMS, **extra},
                          budget={"max_tool_calls": 300, "max_steps": 300})
        status = run_task(tid)
        task = db.query_one("SELECT * FROM tasks WHERE id=?", (tid,))
        self.assertEqual(status, "done", task["error"])
        return tid, task

    def facts(self, status="alive"):
        return mem.list_memories(scope="topic", topic=TOPIC, kind="fact", status=status)

    def expire_cache(self):
        fetch.fetcher._robots.clear()
        for p in config.CACHE_DIR.glob("*"):
            p.unlink()

    def test_two_runs_correction_and_forgetting(self):
        # ---- 第一次任务：写入事实与交付
        t1, task1 = self.run_research()
        f1 = self.facts()
        claim128 = [f for f in f1 if "128K" in f["content"] and "宣称支持" in f["content"]]
        self.assertEqual(len(claim128), 1)
        drop = [f for f in f1 if "64K 以上" in f["content"]]
        self.assertEqual(len(drop), 1)
        # 36氪转载了同一篇：证据 2 条，但独立来源只算 1 个，所以仍是“待核实”
        m = mem.get(drop[0]["id"])
        self.assertEqual(m["meta"]["independent"], 1)
        self.assertGreaterEqual(m["meta"]["reprints"], 1)
        self.assertEqual(m["meta"]["verify"], "unverified")
        delivery1 = task1["state"]["delivery_id"]
        self.assertIn(claim128[0]["id"], mem.get(delivery1)["derived_from"])

        # ---- 第二次任务：IT之家更正了数字 + 出现新进展
        self.expire_cache()
        self.web.state.update(corrected=True, extra=True)
        t2, task2 = self.run_research()
        plan_thought = db.query_one("SELECT thought FROM steps WHERE task_id=? AND node='plan'", (t2,))["thought"]
        self.assertIn("召回记忆", plan_thought)                     # 历史记忆影响第二次任务
        self.assertIn("128K", plan_thought)
        rep = task2["state"]["report"]["memory"]
        self.assertEqual(rep["corrected"], 1)
        old = mem.get(claim128[0]["id"])
        self.assertEqual(old["status"], "superseded")             # 旧结论被更正
        new = mem.get(old["superseded_by"])
        self.assertIn("64K", new["content"])
        self.assertIn("来源更正", new["note"])
        self.assertEqual(mem.get(delivery1)["status"], "stale")    # 昨天的交付被标记“需更正”
        self.assertIn("已更正", mem.get(delivery1)["note"])
        stats2 = task2["state"]["stats"]
        self.assertEqual(stats2.get("new"), 1)                     # 只有一篇真正的新资料

        # 第三次召回：被取代的旧结论不再出现
        rec = mem.recall(topic=TOPIC, record_use=False)
        self.assertNotIn(claim128[0]["id"], rec["used"])
        self.assertNotIn("宣称支持 128K", rec["text"])
        self.assertIn(new["id"], rec["used"])

        # ---- 用户删除一条记忆：失效并级联，之后不再召回
        res = mem.delete(drop[0]["id"], reason="用户认为这条复现结论不可靠")
        self.assertIn(task2["state"]["delivery_id"], res["invalidated"])
        rec = mem.recall(topic=TOPIC, record_use=False)
        self.assertNotIn(drop[0]["id"], rec["used"])
        self.assertNotIn(task2["state"]["delivery_id"], rec["used"])
        ev = db.query("SELECT event, actor FROM memory_events WHERE memory_id=?", (drop[0]["id"],))
        self.assertIn(("delete", "user"), [(e["event"], e["actor"]) for e in ev])

        # ---- 用户更正一条记忆：新内容被召回，旧内容不再出现
        r = mem.correct(new["id"], "该模型官方更正后的上下文长度为 64K", reason="用户核对官方公告")
        rec = mem.recall(topic=TOPIC, record_use=False)
        self.assertIn(r["new"], rec["used"])
        self.assertNotIn(new["id"], rec["used"])

    def test_preferences_long_term_vs_this_time(self):
        mem.set_preference("default", "exclude", ["复现"])
        mem.set_preference("default", "focus", ["大海捞针"])
        t1, task1 = self.run_research()
        titles = [d["title"] for d in db.query("SELECT title FROM documents WHERE meta LIKE ?", (f"%{t1}%",))]
        self.assertFalse(any("复现" in (t or "") for t in titles))     # 长期偏好：排除
        self.assertTrue(any("大海捞针" in a["name"] or "大海捞针" in str(a["keywords"])
                            for a in task1["state"]["aspects"]))
        # 本次要求覆盖长期偏好，但不写入长期记忆
        t2, task2 = self.run_research(this_time={"exclude": ["手机"]})
        self.assertEqual(task2["state"]["memory"]["prefs"]["exclude"]["from"], "this_time")
        self.assertTrue(task2["state"]["memory"]["pref_notes"])
        prefs = mem.list_memories(scope="user", kind="preference", status="alive")
        self.assertEqual(sorted(p["meta"]["value"][0] for p in prefs), ["复现", "大海捞针"])
        # 删除偏好后不再生效
        ex = next(p for p in prefs if p["meta"]["type"] == "exclude")
        mem.delete(ex["id"])
        t3, task3 = self.run_research()
        self.assertNotIn("exclude", task3["state"]["memory"]["prefs"])

    def test_budget_limits_recall(self):
        for i in range(30):
            mem.add("topic", "fact", f"测试事实第 {i} 条：" + "很长的内容" * 20, topic=TOPIC,
                    meta={"verify": "unverified"})
        rec = mem.recall(topic=TOPIC, budget_tokens=300, record_use=False)
        self.assertLessEqual(rec["tokens"], 300)
        self.assertGreater(rec["dropped"], 0)


class FactLogicTest(CollectBase):
    def _doc(self, url, text, **kw):
        from app.collect.store import upsert_document
        return upsert_document({"url": url, "title": kw.get("title", url), "text": text,
                                "content_kind": "full", "source_id": kw.get("source", "web"),
                                "primary": kw.get("primary", False)}, "T")["doc_id"]

    def test_reprints_do_not_add_independence_and_generated_ignored(self):
        from app import memory_facts as mf
        root = self._doc("https://a.com/1", "传闻某模型将支持 1M 上下文。" * 30)
        fid = mf.merge_claim("T", {"doc": root, "text": "传闻某模型将支持 1M 上下文", "quote": "传闻某模型将支持 1M 上下文"},
                             "t", {})
        for i in range(5):     # 五家网站原样转载
            d = self._doc(f"https://copy{i}.com/x", "传闻某模型将支持 1M 上下文。" * 30 + f"来源{i}")
            self.assertIsNotNone(db.query_one("SELECT duplicate_of FROM documents WHERE id=?", (d,))["duplicate_of"])
            mf.add_evidence(fid, d, "转载")
        gen = self._doc("https://local/generated", "传闻某模型将支持 1M 上下文（系统生成摘要）" * 10, source="generated")
        mf.add_evidence(fid, gen, "生成摘要")
        meta = mf.verify(fid)
        self.assertEqual(meta["independent"], 1)
        self.assertEqual(meta["reprints"], 5)
        self.assertEqual(meta["generated_ignored"], 1)
        self.assertEqual(meta["verify"], "unverified")

    def test_conflict_then_resolution_by_primary_source(self):
        from app import memory_facts as mf
        a = self._doc("https://news.com/a", "报道称该模型上下文窗口为 128K。" * 20)
        b = self._doc("https://arxiv.org/abs/2410.00001", "The paper states 64K context window." * 20,
                      source="arxiv", primary=True)
        stats = {}
        fa = mf.merge_claim("T", {"doc": a, "text": "该模型上下文窗口为 128K", "quote": "该模型上下文窗口为 128K"}, "t", stats)
        fb = mf.merge_claim("T", {"doc": b, "text": "该模型上下文窗口为 64K", "quote": "64K context window"}, "t", stats)
        self.assertEqual(stats["conflicts"], 1)
        self.assertEqual(mem.get(fa)["meta"]["verify"], "disputed")
        res = mf.resolve_disputes("T", "t")
        self.assertFalse(res[0].get("open"))
        self.assertEqual(mem.get(fb)["meta"]["verify"], "supported")
        self.assertEqual(mem.get(fa)["meta"]["verify"], "refuted")
        rec = mem.recall(topic="T", record_use=False)
        self.assertNotIn(fa, rec["used"])          # 被否定的说法不再被当作事实召回
        self.assertIn(fb, rec["used"])


class PreferenceApiTest(CollectBase):
    def test_reading_minutes_can_be_set_and_changed(self):
        from app.server import create_app
        c = create_app().test_client()
        self.assertEqual(c.post("/api/preferences", json={"type": "reading_minutes", "value": 5}).status_code, 201)
        self.assertEqual(c.post("/api/preferences", json={"type": "reading_minutes", "value": 10}).status_code, 201)
        alive = mem.list_memories(scope="user", kind="preference", status="alive")
        self.assertEqual([p["content"] for p in alive], ["阅读时间（分钟）：10 分钟"])
        dead = mem.list_memories(scope="user", kind="preference", status="superseded")
        self.assertEqual(dead[0]["content"], "阅读时间（分钟）：5 分钟")


class PreferenceValidationTest(CollectBase):
    def test_reject_meaningless(self):
        from app.server import create_app
        c = create_app().test_client()
        for t, v in (("focus", ["5"]), ("style", "q"), ("background", "5"), ("source_prefer", ["q"]),
                     ("reading_minutes", "abc")):
            r = c.post("/api/preferences", json={"type": t, "value": v})
            self.assertEqual(r.status_code, 400, (t, v))
        self.assertEqual(c.post("/api/preferences", json={"type": "source_prefer", "value": ["arXiv"]}).status_code, 201)
        p = mem.list_memories(scope="user", kind="preference", status="alive")[0]
        self.assertEqual(p["meta"]["value"], ["arxiv"])      # 名称自动转成来源 ID


class LegacyPrefTest(CollectBase):
    def test_old_garbage_preferences_not_recalled(self):
        mem.add("user", "preference", "关注重点：12", mkey="focus@all", meta={"type": "focus", "value": ["12"]})
        mem.add("user", "preference", "写作要求：q", mkey="style@all", meta={"type": "style", "value": "q"})
        rec = mem.recall(topic="T", record_use=False)
        self.assertEqual(rec["preferences"], [])
        rows = mem.list_memories(scope="user", kind="preference")
        self.assertTrue(all(r.get("invalid_value") for r in rows))


class DeliveryArchiveTest(CollectBase):
    def test_archive_restore_and_recall(self):
        from app import memory_facts as mf
        f = mem.add("topic", "fact", "128K 时准确率下降到六成", topic="T")
        did = mf.record_delivery("T", "t_1", "default", "检索交付 5 篇", [f], ["d_1"], {})
        self.assertIn(did, mem.recall("default", "T")["used"])
        with self.assertRaises(ValueError):
            mem.archive(f)                                  # 只有交付可以“不再参考”
        mem.archive(did)
        self.assertNotIn(did, mem.recall("default", "T")["used"])          # 不再召回
        self.assertIn(did, [m["id"] for m in mem.list_memories("topic", "T", "delivery", "alive")])  # 仍可见
        mem.correct(f, "64K 时准确率下降到六成")              # 归档期间事实被更正
        self.assertEqual(mem.get(did)["status"], "archived")
        self.assertEqual(mem.restore(did)["status"], "stale")              # 恢复后标为部分结论已过期
        self.assertIn(did, mem.recall("default", "T")["used"])
        imp = mem.delivery_impact(did)
        self.assertEqual((imp["docs"], imp["facts"]), (1, 1))

    def test_minutes_in_wrong_pref_type(self):
        with self.assertRaises(mem.PreferenceError):
            mem.validate_preference("background", "5分钟")
        m = mem.add("user", "preference", "知识背景：5分钟", meta={"type": "background", "value": "5分钟"})
        row = next(r for r in mem.list_memories("user", kind="preference") if r["id"] == m)
        self.assertTrue(row["invalid_value"])
        self.assertEqual(row["suggest"]["value"], 5)


class CompareAndDemoTest(MemoryTest):
    """“与上次相比”和记忆验证演示：两次任务之间删除、更正记忆，第二次任务核对结果。"""

    def test_compare_report(self):
        t1, task1 = self.run_research()
        self.assertTrue(task1["state"]["report"]["compare"]["first"])
        drop = next(f for f in self.facts() if "64K 以上" in f["content"])
        mem.delete(drop["id"], reason="测试删除")
        other = next(f for f in self.facts() if f["id"] != drop["id"] and "宣称支持" not in f["content"])
        fixed = mem.correct(other["id"], "用户更正后的内容", reason="测试更正")
        mem.set_preference("default", "exclude", ["踩坑"])
        self.expire_cache()
        self.web.state.update(corrected=True, extra=True)
        t2, task2 = self.run_research()
        c = task2["state"]["report"]["compare"]
        self.assertEqual(c["prev"]["id"], t1)
        self.assertEqual(c["docs"]["new"], 1)
        self.assertGreaterEqual(c["docs"]["repeated"], 1)
        self.assertEqual(len(c["corrections"]), 1)
        self.assertTrue(all(k["ok"] for k in c["checks"]), c["checks"])
        inv = {x["id"]: x for x in c["invalidated"]}
        self.assertIn(drop["id"], inv)
        self.assertIn(other["id"], inv)
        self.assertFalse(any(x["recalled"] for x in inv.values()))
        self.assertTrue(inv[other["id"]]["replacement_recalled"])
        self.assertIn(fixed["new"], task2["state"]["memory"]["used"])
        ex = next(e for e in c["pref_effects"] if e["type"] == "exclude")
        self.assertIn("排除", ex["effect"])
        self.assertEqual([x["op"] for x in c["pref_changes"]], ["added"])

    def test_demo_api_flow(self):
        from app import memory_demo, replay
        from app.server import create_app
        cl = create_app().test_client()
        self.assertEqual(cl.post("/api/demo/memory/run1").status_code, 400)     # 非回放模式拒绝
        replay.STATE.update(enabled=True, web=self.web)
        try:
            tid = memory_demo.start_run(1)
            self.assertEqual(run_task(tid), "done")
            self.assertEqual(cl.post("/api/demo/memory/sample").status_code, 200)
            st = cl.get("/api/demo/memory").json
            self.assertIn("删除", [e["op"] for e in st["edits"]])
            tid2 = memory_demo.start_run(2)
            self.assertEqual(run_task(tid2), "done")
            st = cl.get("/api/demo/memory").json
            self.assertTrue(all(k["ok"] for k in st["compare"]["checks"]))
            self.assertTrue(st["recall_now"]["dead"])
            self.assertFalse(any(x["recalled"] for x in st["recall_now"]["dead"]))
            r = cl.post("/api/demo/memory/reset").json
            self.assertEqual(r["tasks"], 2)
            self.assertIsNone(cl.get("/api/demo/memory").json["run1"])
        finally:
            replay.STATE.update(enabled=False, web=None)
