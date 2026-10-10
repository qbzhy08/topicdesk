import json
import tempfile
import unittest
from pathlib import Path

from app import config, db
from app.collect import browser, dedup, fetch, sources, store
from app.collect.extract import extract_article
from app.harness import create_task, resolve_hitl, run_task
import app.graphs  # noqa: F401
from tests import fixtures
from tests.helpers import DBTestCase


class CollectBase(DBTestCase):
    def setUp(self):
        super().setUp()
        self._dirs = tempfile.TemporaryDirectory()
        base = Path(self._dirs.name)
        self._old = (config.DATA_DIR, config.IMAGE_DIR, config.CACHE_DIR)
        config.DATA_DIR, config.IMAGE_DIR, config.CACHE_DIR = base, base / "images", base / "cache"
        config.IMAGE_DIR.mkdir()
        config.CACHE_DIR.mkdir()
        self.web = fixtures.FakeWeb()
        fetch.set_transport(self.web)
        fetch.fetcher.min_interval = 0
        fetch.fetcher.host_interval.clear()
        fetch.fetcher._robots.clear()
        fetch.fetcher._last.clear()
        self._render = browser.render
        browser.render = fixtures.fake_render
        sources.seed_sources()

    def tearDown(self):
        fetch.set_transport(None)
        browser.render = self._render
        config.DATA_DIR, config.IMAGE_DIR, config.CACHE_DIR = self._old
        self._dirs.cleanup()
        super().tearDown()


class FetchTest(CollectBase):
    def test_robots_rate_limit_cache_challenge(self):
        with self.assertRaises(fetch.FetchError) as cm:
            fetch.fetcher.get("https://paywall.example.com/a")
        self.assertEqual(cm.exception.kind, "robots")

        u = "https://blog.example.com/long-context"
        self.web.state["rate_limit_once"].add(u)
        r = fetch.fetcher.get(u)
        self.assertEqual(r.status, 200)
        self.assertEqual(self.web.calls.count(u), 2)          # 429 后重试成功
        r2 = fetch.fetcher.get(u)
        self.assertTrue(r2.from_cache)
        self.assertEqual(self.web.calls.count(u), 2)          # 命中缓存不再请求

        with self.assertRaises(fetch.FetchError) as cm:
            fetch.fetcher.get("https://captcha.example.com/x")
        self.assertEqual(cm.exception.kind, "blocked")
        with self.assertRaises(fetch.FetchError) as cm:
            fetch.fetcher.get("https://nowhere.example.com/x")
        self.assertEqual(cm.exception.kind, "not_found")


class ExtractTest(unittest.TestCase):
    def test_lazy_srcset_filter_caption_context(self):
        art = extract_article(fixtures.BLOG, "https://blog.example.com/long-context")
        self.assertEqual(art["content_kind"], "full")
        self.assertTrue(art["published_at"])
        img = art["images"][0]
        self.assertEqual(img["url"], "https://blog.example.com/fig-1600.png")   # 取 srcset 最大
        self.assertTrue(any("github.com/example/longeval" in l["url"] for l in art["links"]))

        frag = extract_article(fixtures.ithome_feed().split("CDATA[")[1].split("]]>")[0],
                               "https://www.ithome.com/0/800/001.htm", is_fragment=True)
        by_url = {i["url"]: i for i in frag["images"]}
        chart = by_url["https://img.ithome.com/a/chart.png"]                    # data-src 懒加载
        self.assertIsNone(chart["reject_reason"])
        self.assertEqual(chart["caption"], "图 1：各模型长上下文评测得分")
        self.assertIn("128K", chart["context_after"])
        self.assertIsNotNone(by_url["https://img.ithome.com/avatar/u1.png"]["reject_reason"])
        self.assertIsNone(by_url["https://img.ithome.com/avatar/u1.png"]["caption"])   # 不能借用下一张图的图注

    def test_spa_main_content_and_noise(self):
        art = extract_article(fixtures.SSPAI_RENDERED, "https://sspai.com/post/1001")
        self.assertEqual(art["title"], "开源大模型长上下文评测实测体验")
        self.assertEqual(art["author"], "少数派作者")
        self.assertNotIn("相关推荐", art["text"])
        self.assertNotIn("菜单", art["text"])
        urls = [i["url"] for i in art["images"]]
        self.assertIn("https://cdn.sspai.com/p/test-1.png", urls)
        self.assertNotIn("https://cdn.sspai.com/ad/promo.png", urls)


class DedupTest(unittest.TestCase):
    def test_canonical_and_simhash(self):
        c = dedup.canonical_url
        self.assertEqual(c("https://www.ithome.com/0/800/001.htm?f=rss&utm_source=x#top"),
                         c("http://ithome.com/0/800/001.htm"))
        self.assertEqual(c("http://arxiv.org/pdf/2410.01234v2"), "https://arxiv.org/abs/2410.01234")
        a = dedup.simhash(fixtures.LONG_B)
        b = dedup.simhash(fixtures.LONG_B + "本文转载自 IT之家。")
        self.assertLessEqual(dedup.hamming(a, b), 6)
        self.assertGreater(dedup.hamming(a, dedup.simhash(fixtures.LONG_A)), 10)
        self.assertEqual(dedup.iso("2026-10-10 15:30:18  +0800"), "2026-10-10T07:30:18+00:00")


class StoreTest(CollectBase):
    def test_versions_reprint_event_background(self):
        base = {"url": "https://x.com/a?utm_source=1", "title": "开源大模型长上下文评测", "text": fixtures.LONG_A,
                "content_kind": "full", "source_id": "web", "published_at": fixtures.NOW.isoformat()}
        r1 = store.upsert_document(base, "T")
        self.assertEqual((r1["action"], r1["novelty"]), ("new", "new"))
        r2 = store.upsert_document(dict(base, url="https://x.com/a"), "T")
        self.assertEqual(r2["action"], "unchanged")
        r3 = store.upsert_document(dict(base, text=fixtures.LONG_B), "T")
        self.assertEqual((r3["action"], r3["novelty"]), ("updated", "update"))
        old = db.query_one("SELECT status FROM documents WHERE id=?", (r1["doc_id"],))
        self.assertEqual(old["status"], "superseded")
        r4 = store.upsert_document(dict(base, url="https://y.com/copy", text=fixtures.LONG_B + "转载"), "T")
        self.assertEqual((r4["novelty"], r4["relation"]), ("duplicate", "reprint"))
        r5 = store.upsert_document({"url": "https://z.com/1", "title": "开源大模型长上下文评测结果",
                                    "summary": "短讯", "source_id": "web",
                                    "published_at": fixtures.NOW.isoformat()}, "T")
        self.assertEqual(r5["relation"], "same_event")
        r6 = store.upsert_document({"url": "https://z.com/old", "title": "上下文窗口科普", "summary": "旧文",
                                    "source_id": "web", "published_at": "2025-01-01T00:00:00Z"}, "T")
        self.assertEqual(r6["novelty"], "background")
        # 摘要 → 全文：补全而不是新版本
        s = store.upsert_document({"url": "https://z.com/2", "title": "另一篇", "summary": "摘要",
                                   "content_kind": "summary", "source_id": "web"}, "T")
        e = store.upsert_document({"url": "https://z.com/2", "title": "另一篇", "text": fixtures.LONG_A * 2,
                                   "content_kind": "full", "source_id": "web"}, "T")
        self.assertEqual((e["action"], e["doc_id"]), ("enriched", s["doc_id"]))


class ResearchGraphTest(CollectBase):
    PARAMS = {"keywords": ["长上下文"], "en_keywords": ["long context LLM"], "no_llm": True,
              "max_docs": 30}

    def _run(self, params=None):
        tid = create_task("research", "开源大模型 长上下文 评测", params=params or self.PARAMS,
                          budget={"max_tool_calls": 200, "max_steps": 200})
        status = run_task(tid)
        return tid, status, db.query_one("SELECT * FROM tasks WHERE id=?", (tid,))

    def test_full_react_loop(self):
        tid, status, task = self._run()
        self.assertEqual(status, "done", task["error"])
        rep = task["state"]["report"]
        tools = db.query("SELECT tool, input, status FROM tool_calls WHERE task_id=?", (tid,))
        searched = {t["input"]["source_id"] for t in tools if t["tool"] == "source_search"}
        self.assertEqual(searched, {"ithome", "36kr", "sspai", "arxiv", "hn", "github"})

        docs = db.query("SELECT * FROM documents WHERE status='active'")
        by_url = {d["canonical_url"]: d for d in docs}
        self.assertNotIn("https://ithome.com/0/800/002.htm", by_url)        # 不相关被过滤
        kr = by_url["https://36kr.com/p/9001"]
        self.assertEqual(kr["novelty"], "duplicate")                        # 转载归并
        self.assertIsNotNone(kr["duplicate_of"])
        self.assertEqual(by_url["https://36kr.com/p/8000"]["novelty"], "background")
        ss = by_url["https://sspai.com/post/1001"]                          # 动态页：浏览器渲染补全文
        self.assertEqual(ss["content_kind"], "full")
        self.assertTrue(ss["meta"]["via_browser"])
        blog = by_url["https://blog.example.com/long-context"]              # HN 只有标题 → 追原文
        self.assertEqual(blog["content_kind"], "full")
        self.assertEqual(blog["locator"]["via"]["source"], "hn")
        self.assertIn("https://arxiv.org/abs/2410.01234", by_url)            # 一手来源
        self.assertIn("arxiv", by_url["https://arxiv.org/abs/2410.01234"]["meta"]["sources_seen"])

        fails = rep["failures"]
        pw = [f for f in fails if "paywall" in (f.get("target") or "")]
        self.assertEqual(pw[0]["kind"], "robots")
        self.assertIn("保留已有摘要", pw[0]["fallback"])

        imgs = db.query("SELECT * FROM images")
        kept = {i["url"] for i in imgs if i["status"] == "kept"}
        self.assertIn("https://img.ithome.com/a/chart.png", kept)
        self.assertIn("https://cdn.sspai.com/p/test-1.png", kept)
        rejected = {i["url"]: i["reject_reason"] for i in imgs if i["status"] == "rejected"}
        self.assertIn("avatar", "".join(rejected))
        self.assertTrue(any("宽高比" in r for r in rejected.values()))
        chart = next(i for i in imgs if i["url"].endswith("chart.png"))
        self.assertTrue((config.DATA_DIR / chart["local_path"]).exists())
        self.assertEqual(chart["caption"], "图 1：各模型长上下文评测得分")

        cov = rep["coverage"]
        self.assertGreaterEqual(cov["primary"]["have"], 2)
        self.assertGreaterEqual(cov["debate"]["have"], 1)
        self.assertTrue(any("RSS" in o for o in rep["omissions"]))
        thoughts = [s["thought"] or "" for s in db.query("SELECT thought FROM steps WHERE task_id=?", (tid,))]
        self.assertTrue(any("追踪原始来源" in t for t in thoughts))
        self.assertTrue(any("浏览器" in t for t in thoughts))

    def test_incremental_second_run_and_source_down(self):
        self._run()
        n1 = db.query_one("SELECT COUNT(*) n FROM documents")["n"]
        fetch.fetcher._robots.clear()
        for p in config.CACHE_DIR.glob("*"):        # 模拟一段时间后缓存过期
            p.unlink()
        self.web.state.update(corrected=True, extra=True, hn_down=True)
        tid, status, task = self._run()
        self.assertEqual(status, "done", task["error"])
        rep = task["state"]["report"]
        docs = db.query("SELECT * FROM documents WHERE canonical_url='https://ithome.com/0/800/001.htm' ORDER BY version")
        self.assertEqual([d["version"] for d in docs], [1, 2])               # 更正 → 新版本
        self.assertEqual(docs[1]["novelty"], "update")
        self.assertEqual(docs[0]["status"], "superseded")
        self.assertIn("128K", docs[1]["meta"]["change"]["numbers_removed"][0])
        new = db.query_one("SELECT novelty FROM documents WHERE canonical_url='https://ithome.com/0/800/010.htm'")
        self.assertEqual(new["novelty"], "new")
        self.assertEqual(task["state"]["stats"].get("new"), 1)              # 只有一篇真正新增
        self.assertIn("hn", task["state"]["disabled"])
        self.assertTrue(any("Hacker News" in o for o in rep["omissions"]))
        steps = db.query("SELECT thought FROM steps WHERE task_id=? AND node='plan'", (tid,))
        self.assertIn("历史记忆", steps[0]["thought"])
        self.assertGreater(db.query_one("SELECT COUNT(*) n FROM documents")["n"], n1)

    def test_rss_source_failure_does_not_lose_progress(self):
        orig = self.web.__call__

        def broken(url, h, t):
            if url == "https://36kr.com/feed":
                raise fetch.FetchError("timeout", "模拟超时", url)
            return orig(url, h, t)
        fetch.set_transport(broken)
        tid, status, task = self._run()
        self.assertEqual(status, "done")
        self.assertIn("36kr", task["state"]["disabled"])
        self.assertGreater(task["state"]["report"]["unique_docs"], 5)


class ImportGraphTest(CollectBase):
    def test_import_blocked_then_human_paste(self):
        tid = create_task("import", "开源大模型 长上下文", params={"url": "https://paywall.example.com/a"})
        self.assertEqual(run_task(tid), "waiting_human")
        req = db.query_one("SELECT * FROM hitl_requests WHERE task_id=?", (tid,))
        self.assertIn("robots", req["prompt"])
        resolve_hitl(req["id"], {"action": "paste", "text": fixtures.LONG_B})
        self.assertEqual(run_task(tid), "done")
        d = db.query_one("SELECT * FROM documents WHERE url=?", ("https://paywall.example.com/a",))
        self.assertEqual(d["meta"]["provided_by"], "human")

    def test_import_ok_with_images(self):
        tid = create_task("import", "开源大模型", params={"url": "https://blog.example.com/long-context"})
        self.assertEqual(run_task(tid), "done")
        imgs = db.query("SELECT * FROM images WHERE status='kept'")
        self.assertEqual(len(imgs), 1)
        self.assertEqual((imgs[0]["width"], imgs[0]["height"]), (1600, 900))


if __name__ == "__main__":
    unittest.main()
