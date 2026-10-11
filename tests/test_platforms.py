"""社区与内容平台：搜索 API（公众号/知乎/贴吧/小红书/抖音/全网）、B站、搜狗微信。"""
from app import db, ranking, websearch
from app.collect import fetch, sources
from app.collect.sources import bili_sign, _MIXIN, _BILI
from app.harness import create_task, run_task
from tests.test_collect import CollectBase

TOPIC = "极创工作室 男娘"
COMMUNITY = ["bilibili", "sogou_wechat", "web_search", "wechat", "zhihu", "tieba", "xiaohongshu", "douyin"]


class SignTest(CollectBase):
    def test_wbi_reference_vector(self):
        img, sub = "7cd084941338484aae1ad9425b84077c", "4932caff0ff746eab6f01bf08b70ac45"
        mixin = "".join((img + sub)[i] for i in _MIXIN)[:32]
        self.assertEqual(mixin, "ea1db124af3c7062474693fa704f4ff8")
        self.assertEqual(bili_sign({"foo": "114", "bar": "514", "zab": 1919810}, mixin, wts=1702204169),
                         "bar=514&foo=114&wts=1702204169&zab=1919810&w_rid=8f6f2b5b3d485fe1886cec6a0be8c5d4")


class PlatformTest(CollectBase):
    def setUp(self):
        super().setUp()
        _BILI.update(t=0, mixin="", cookie="")

    def test_bilibili_search(self):
        items = sources.search("bilibili", "极创工作室")
        self.assertEqual(items[0]["url"], "https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertEqual(items[0]["title"], "极创工作室男娘幕后花絮")         # 去掉了 <em> 高亮
        self.assertEqual(items[0]["meta"]["views"], 52000)
        self.assertTrue(items[0]["images"][0]["url"].startswith("https://i0.hdslb.com/"))

    def test_sogou_wechat_and_captcha(self):
        items = sources.search("sogou_wechat", "极创工作室")
        self.assertEqual(items[0]["author"], "二次元观察")
        self.assertTrue(items[0]["url"].startswith("https://weixin.sogou.com/link?url="))
        self.assertIsNotNone(items[0]["published_at"])
        self.web.state["sogou_captcha"] = True
        fetch.fetcher._robots.clear()
        with self.assertRaises(fetch.FetchError) as cm:
            sources.search("sogou_wechat", "另一个查询")
        self.assertEqual(cm.exception.kind, "blocked")

    def test_websearch_not_configured_is_not_a_source_failure(self):
        with self.assertRaises(fetch.FetchError) as cm:
            sources.search("wechat", "极创工作室")
        self.assertEqual(cm.exception.kind, "config")
        self.assertFalse((db.query_one("SELECT health FROM sources WHERE id='wechat'")["health"] or {}).get("consecutive_fail"))

    def test_bad_key(self):
        websearch.save_settings("bocha", "wrong")
        with self.assertRaises(fetch.FetchError) as cm:
            websearch.search("x", site="zhihu.com")
        self.assertEqual(cm.exception.kind, "config")
        self.assertIn("key", cm.exception.reason)

    def _run(self):
        tid = create_task("research", TOPIC, params={"no_llm": True, "max_docs": 30, "verify_top": False},
                          budget={"max_tool_calls": 150, "max_steps": 150})
        self.assertEqual(run_task(tid), "done")
        return tid, db.query_one("SELECT state FROM tasks WHERE id=?", (tid,))["state"]

    def test_community_topic_without_key(self):
        tid, st = self._run()
        self.assertIn("community", st["domains"])
        self.assertIn("bilibili", st["source_ids"])
        self.assertTrue(st.get("no_websearch"))
        self.assertTrue(any("未配置搜索 API" in o for o in st["report"]["omissions"]))
        self.assertIsNotNone(db.query_one("SELECT id FROM documents WHERE url LIKE 'https://www.bilibili.com/video/%'"))

    def test_community_topic_with_key(self):
        websearch.save_settings("bocha", "test-key")
        tid, st = self._run()
        docs = {d["url"]: d for d in db.query("SELECT * FROM documents WHERE status='active'")}
        # 公众号：搜索 API 找到 → 抓到全文、账号名、发布时间
        a = docs["https://mp.weixin.qq.com/s/aaa"]
        self.assertEqual((a["content_kind"], a["author"]), ("full", "某某动漫"))
        # 小红书/抖音/知乎：只保留摘要，不去抓需要登录的页面
        x = docs["https://www.xiaohongshu.com/explore/123"]
        self.assertEqual(x["content_kind"], "summary")
        self.assertTrue(x["meta"]["summary_only"])
        self.assertFalse(self.web.state.get("login_wall_hits"))
        self.assertIn("https://www.zhihu.com/question/789", docs)
        self.assertIn("https://fans.example.com/jichuang", docs)          # 全网搜索
        self.assertTrue(any("只有搜索摘要" in o for o in st["report"]["omissions"]))
        # 全网搜索里碰巧带关键词的无关页面（股票行情）：不抓全文
        self.assertNotIn("https://stock.example.com/300857", self.web.calls)
        self.assertGreaterEqual(st["stats"].get("skip_fetch_irrelevant", 0), 1)
        # 社区话题：子问题换成当事人说法 / 社区讨论 / 相关作品，不再要求论文类一手来源
        ids = [a["id"] for a in st["aspects"]]
        self.assertIn("official", ids)
        self.assertIn("discussion", ids)
        self.assertNotIn("primary", ids)
        self.assertGreaterEqual(st["report"]["coverage"]["discussion"]["have"], 2)
        # 不同公众号账号算不同的独立来源
        self.assertNotEqual(ranking.source_key(a["url"], a["author"]),
                            ranking.source_key("https://mp.weixin.qq.com/s/bbb", "二次元观察"))
        d = ranking.latest(tid)
        self.assertTrue(d["lists"]["5"]["selected"])

    def test_settings_api(self):
        from app import server
        c = server.app.test_client()
        self.assertFalse(c.get("/api/settings/websearch").json["available"])
        self.assertEqual(c.post("/api/settings/websearch", json={"provider": "nope", "api_key": "k"}).status_code, 400)
        r = c.post("/api/settings/websearch", json={"provider": "bocha", "api_key": "test-key"}).json
        self.assertEqual((r["available"], r["api_key"]), (True, "***"))
        c.post("/api/settings/websearch", json={"provider": "bocha", "api_key": "***"})   # 不改 key
        self.assertEqual(websearch.settings()["api_key"], "test-key")
        t = c.post("/api/settings/websearch/test", json={}).json
        self.assertTrue(t["ok"], t)


class EmptyReasonTest(CollectBase):
    def test_site_filter_retry_and_empty_reason(self):
        websearch.save_settings("bocha", "test-key")
        self.web.state["ignore_include"] = True
        items = sources.search("zhihu", "极创工作室")                      # include 被忽略 → 用 site: 重试
        self.assertEqual([i["url"] for i in items], ["https://www.zhihu.com/question/789"])
        items = sources.search("tieba", "极创工作室")                      # 该平台确实没有结果
        self.assertEqual(items, [])
        h = db.query_one("SELECT health FROM sources WHERE id='tieba'")["health"]
        self.assertEqual(h["last_count"], 0)
        self.assertIn("tieba.baidu.com", h["last_note"])
        h2 = db.query_one("SELECT health FROM sources WHERE id='zhihu'")["health"]
        self.assertEqual(h2["last_count"], 1)
        self.assertIsNone(h2["last_note"])


class CoverageTest(CollectBase):
    def test_uncovered_platform_is_skipped_after_test(self):
        from app import server
        websearch.save_settings("bocha", "test-key")
        c = server.app.test_client()
        r = c.post("/api/settings/websearch/test", json={"query": "极创工作室"}).json
        self.assertIn("百度贴吧", r["skipped"])                     # 测试数据里贴吧没有任何结果
        self.assertNotIn("小红书", r["skipped"])
        self.assertTrue(websearch.not_covered("tieba.baidu.com"))
        websearch.save_settings("bocha", "***")                    # 改设置不丢收录记录
        self.assertTrue(websearch.not_covered("tieba.baidu.com"))
        tid = create_task("research", "极创工作室 男娘", params={"no_llm": True, "max_docs": 30, "verify_top": False},
                          budget={"max_tool_calls": 150, "max_steps": 150})
        self.assertEqual(run_task(tid), "done")
        st = db.query_one("SELECT state FROM tasks WHERE id=?", (tid,))["state"]
        self.assertIn("百度贴吧", st["not_covered"])
        calls = db.query("SELECT input FROM tool_calls WHERE task_id=? AND tool='source_search'", (tid,))
        self.assertFalse(any(c["input"]["source_id"] == "tieba" for c in calls))
        self.assertTrue(any("不收录" in o for o in st["report"]["omissions"]))
