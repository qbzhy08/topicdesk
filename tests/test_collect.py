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
        self._render, self._raw = browser.render, browser.fetch_raw
        browser.render, browser.fetch_raw = fixtures.fake_render, fixtures.fake_fetch_raw
        sources.seed_sources()

    def tearDown(self):
        fetch.set_transport(None)
        browser.render, browser.fetch_raw = self._render, self._raw
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
              "max_docs": 30, "days": 365,
              "sources": ["ithome", "36kr", "sspai", "qbitai", "juejin", "leiphone", "arxiv", "hn", "github"]}

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
        self.assertEqual(searched, {"ithome", "36kr", "sspai", "qbitai", "juejin", "leiphone",
                                    "arxiv", "hn", "github"})
        jj = db.query_one("SELECT * FROM documents WHERE url='https://juejin.cn/post/7001'")
        self.assertEqual((jj["content_kind"], jj["author"]), ("full", "掘金作者"))   # 站内搜索 → 抓全文
        self.assertIn("leiphone", task["state"]["disabled"])        # 不存在的 RSS：记录并停用，不死循环

        # 站内搜索：标题不含关键词，抓全文后确认相关，并且只追正文里的论文链接，不追导航链接
        qb = db.query_one("SELECT * FROM documents WHERE url='https://www.qbitai.com/2026/10/502364.html'")
        self.assertEqual(qb["content_kind"], "full")
        fetched = [t["input"]["url"] for t in tools if t["tool"] == "page_fetch"]
        self.assertNotIn("https://huggingface.co/docs", fetched)
        self.assertNotIn("https://github.com/features", fetched)

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

    def test_source_diversity_when_cap_reached(self):
        # 资料上限很小：第一个来源就能占满，但其他来源仍各检索一次
        tid, status, task = self._run(dict(self.PARAMS, max_docs=3, per_source=2))
        self.assertEqual(status, "done", task["error"])
        searched = {t["input"]["source_id"] for t in db.query(
            "SELECT input FROM tool_calls WHERE task_id=? AND tool='source_search'", (tid,))}
        self.assertTrue({"arxiv", "hn", "github", "juejin", "ithome"} <= searched)
        self.assertGreaterEqual(len(task["state"]["report"]["by_source"]), 4)

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
        # 第二次运行：已处理过配图的资料直接复用，报告里仍统计到这些配图
        self.assertGreaterEqual(rep["image_stats"]["kept"], 2)
        self.assertGreaterEqual(rep["image_stats"]["reused_docs"], 1)

    def test_rss_source_failure_does_not_lose_progress(self):
        orig = self.web.__call__

        def broken(url, h, t, **kw):
            if url == "https://36kr.com/feed":
                raise fetch.FetchError("timeout", "模拟超时", url)
            return orig(url, h, t, **kw)
        fetch.set_transport(broken)
        tid, status, task = self._run()
        self.assertEqual(status, "done")
        self.assertIn("36kr", task["state"]["disabled"])
        self.assertGreater(task["state"]["report"]["unique_docs"], 5)


class DomainTest(CollectBase):
    def test_finance_topic_selects_finance_sources(self):
        tid = create_task("research", "股票趋势测评", params={"no_llm": True, "en_keywords": ["stock trend prediction"],
                                                          "max_docs": 20}, budget={"max_tool_calls": 200})
        self.assertEqual(run_task(tid), "done")
        task = db.query_one("SELECT * FROM tasks WHERE id=?", (tid,))
        self.assertEqual(task["state"]["domains"], ["finance"])
        ids = task["state"]["source_ids"]
        self.assertIn("wallstcn", ids)
        self.assertIn("openalex", ids)
        self.assertNotIn("ithome", ids)          # 科技媒体与金融主题不匹配
        self.assertNotIn("juejin", ids)
        wd = db.query_one("SELECT * FROM documents WHERE source_id='wallstcn'")
        self.assertEqual(wd["content_kind"], "full")
        oa = db.query_one("SELECT * FROM documents WHERE source_id='openalex'")
        self.assertIn("evaluate stock trend", oa["text"])   # 倒排索引还原摘要
        self.assertTrue(oa["meta"]["primary"])


class WafTest(CollectBase):
    def test_waf_feed_falls_back_to_browser(self):
        self.web.state["waf_36kr"] = True
        items = sources.search("36kr", "长上下文 大模型", terms=["长上下文", "大模型"])
        self.assertTrue(items)
        self.assertEqual(items[0]["meta"]["feed_via"], "browser")
        with self.assertRaises(fetch.FetchError) as cm:
            fetch.fetcher.get("https://36kr.com/feed", cache_ttl=0)
        self.assertEqual(cm.exception.kind, "blocked")


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


class TranslateTest(CollectBase):
    def test_dictionary_translation(self):
        from app import keywords
        r = keywords.suggest("大模型长上下文", use_llm=False)
        self.assertIn("长上下文", r["terms_zh"])
        self.assertTrue(any("long context" in q.lower() for q in r["queries_en"]))
        r2 = keywords.suggest("long context LLM benchmark", use_llm=False)
        self.assertIn("长上下文", r2["terms_zh"])
        self.assertEqual(r2["queries_en"], ["long context LLM benchmark"])

    def test_research_fills_missing_language(self):
        tid = create_task("research", "开源大模型 长上下文 评测",
                          params={"no_llm": True, "max_docs": 10, "sources": ["ithome", "arxiv"]},
                          budget={"max_tool_calls": 100})
        self.assertEqual(run_task(tid), "done")
        task = db.query_one("SELECT * FROM tasks WHERE id=?", (tid,))
        q = task["state"]["plan"]["queries_en"]
        self.assertTrue(any("long context" in x.lower() for x in q))     # 没填英文也会自动补
        searched = [t["input"] for t in db.query("SELECT input FROM tool_calls WHERE task_id=? AND tool='source_search'", (tid,))]
        self.assertTrue(any(i["source_id"] == "arxiv" for i in searched))
        plan = db.query_one("SELECT thought FROM steps WHERE task_id=? AND node='plan'", (tid,))["thought"]
        self.assertIn("自动翻译", plan)

    def test_user_keywords_are_kept(self):
        tid = create_task("research", "开源大模型 长上下文 评测",
                          params={"no_llm": True, "max_docs": 5, "en_keywords": ["my custom query"],
                                  "sources": ["arxiv"]}, budget={"max_tool_calls": 60})
        run_task(tid)
        task = db.query_one("SELECT * FROM tasks WHERE id=?", (tid,))
        self.assertEqual(task["state"]["plan"]["queries_en"], ["my custom query"])   # 用户填写的优先，不被覆盖



class EarlyStopTest(CollectBase):
    def test_stops_soon_after_full_coverage(self):
        tid = create_task("research", "开源大模型 长上下文 评测",
                          params={"no_llm": True, "max_docs": 200, "max_fetch": 50, "enrich_after_cover": 1,
                                  "en_keywords": ["long context LLM"],
                                  "sources": ["ithome", "36kr", "sspai", "juejin", "arxiv", "hn", "github"]},
                          budget={"max_tool_calls": 300, "max_steps": 300})
        self.assertEqual(run_task(tid), "done")
        task = db.query_one("SELECT * FROM tasks WHERE id=?", (tid,))
        rep = task["state"]["report"]
        self.assertIsNotNone(rep["covered_at"])
        fetched_after = task["state"]["fetches"] - rep["covered_at"]["fetches"]
        self.assertLessEqual(fetched_after, 1)                 # 覆盖后最多再补抓 1 篇
        thoughts = " ".join(s["thought"] or "" for s in db.query("SELECT thought FROM steps WHERE task_id=?", (tid,)))
        self.assertIn("所有子问题已覆盖", thoughts)


class GapFillTest(CollectBase):
    """缺口补检：先扩充同义词用已有资料判断；仍不足的只做少量定向检索；补不上就标注，不反复全量检索。"""

    def _fake_chat(self, messages, **kw):
        text = messages[-1]["content"]
        if "信息检索规划助手" in text:
            data = {"domains": ["tech"], "core_terms_zh": ["长上下文"], "queries_en": ["long context LLM"],
                    "aspects": [
                        {"name": "上下文窗口扩展与位置外推技术", "keywords": ["位置外推"],
                         "search_en": "long context RoPE extrapolation", "search_zh": "位置外推"},
                        {"name": "长上下文评测方法新进展", "keywords": ["评测方法论"]},
                        {"name": "监管政策", "keywords": ["监管"]}], "notes": "test"}
        elif "还没有覆盖下面这些子问题" in text:
            self.gap_prompts.append(text)
            data = {"why": "补同义词", "gaps": [
                {"id": "llm0", "keywords": ["YaRN", "RoPE scaling"]},
                {"id": "llm1", "keywords": ["needle in a haystack", "multi-hop benchmark"]},
                {"id": "llm2", "keywords": ["合规"], "search_en": "long context regulation"}]}
        else:
            raise RuntimeError("translate: not needed in test")
        return {"content": json.dumps(data), "data": data, "usage": {}, "_cost": 0}

    def test_targeted_gap_fill(self):
        from app import llm
        self.gap_prompts = []
        old = (llm.available, llm.chat)
        llm.available, llm.chat = (lambda: True), self._fake_chat
        try:
            tid = create_task("research", "开源大模型 长上下文 评测",
                              params={"max_docs": 40, "en_keywords": ["long context LLM"],
                                      "sources": ["ithome", "juejin", "arxiv", "hn", "github"]},
                              budget={"max_tool_calls": 300, "max_steps": 300})
            self.assertEqual(run_task(tid), "done")
        finally:
            llm.available, llm.chat = old
        task = db.query_one("SELECT * FROM tasks WHERE id=?", (tid,))
        rep = task["state"]["report"]
        gaps = {g["id"]: g for g in rep["gaps"]}
        # 评测方法：补充同义词后，已抓到的博客（needle in a haystack）就能覆盖，不额外检索
        self.assertEqual(gaps["llm1"]["status"], "closed")
        self.assertEqual(gaps["llm1"]["queries"], [])
        # 位置外推：定向检索 arXiv 找到 YaRN 论文后补上；检索数量很少
        self.assertEqual(gaps["llm0"]["status"], "closed")
        self.assertLessEqual(len(gaps["llm0"]["queries"]), 4)
        self.assertTrue(any("RoPE extrapolation" in q for q in gaps["llm0"]["queries"]))
        self.assertGreaterEqual(rep["coverage"]["llm0"]["have"], 1)
        # 监管：找不到就标注“暂无足够资料”，只补检一次
        self.assertEqual(gaps["llm2"]["status"], "insufficient")
        self.assertTrue(any("暂无足够资料" in o for o in rep["omissions"]))
        self.assertEqual(len(self.gap_prompts), 1)
        searches = db.query("SELECT input FROM tool_calls WHERE task_id=? AND tool='source_search'", (tid,))
        self.assertLessEqual(len(searches), 5 * 2 + 4 * 3)     # 初始检索 + 每个缺口 ≤4 个定向检索

    def test_kw_hit(self):
        from app.graphs.research import _kw_hit
        blob = "we use rope scaling for position interpolation of llms；外推位置编码的方法".lower()
        self.assertTrue(_kw_hit("position interpolation", blob))
        self.assertTrue(_kw_hit("interpolation of position", blob))      # 多词不要求相邻
        self.assertTrue(_kw_hit("位置外推", blob))                        # 中文二元组覆盖
        self.assertFalse(_kw_hit("rag", "average storage"))              # 不做子串误判（短词仍是原文匹配）
        self.assertFalse(_kw_hit("x", blob))


class FetchCostTest(CollectBase):
    """避免白等：最近抓过的全文直接复用；PubMed 摘要走接口；Gradio 应用页不开浏览器。"""

    def test_second_run_reuses_recent_full_text(self):
        params = {"no_llm": True, "max_docs": 30, "en_keywords": ["long context LLM"],
                  "sources": ["hn", "github", "arxiv"]}
        t1 = create_task("research", "开源大模型 长上下文 评测", params=params)
        self.assertEqual(run_task(t1), "done")
        first = self.web.calls.count("https://blog.example.com/long-context")
        self.assertEqual(first, 1)
        t2 = create_task("research", "开源大模型 长上下文 评测", params=params)
        self.assertEqual(run_task(t2), "done")
        self.assertEqual(self.web.calls.count("https://blog.example.com/long-context"), 1)   # 第二次没联网
        doc = db.query_one("SELECT meta FROM documents WHERE url='https://blog.example.com/long-context'")
        self.assertIn(t2, doc["meta"]["seen_by_tasks"])                                    # 但计入本次资料
        obs = " ".join(s["summary"] or "" for s in db.query("SELECT summary FROM steps WHERE task_id=?", (t2,)))
        self.assertIn("复用库中", obs)

    def test_pubmed_abstract_via_api(self):
        items = sources.search_pubmed({"id": "pubmed", "config": {}}, "long context LLM")
        self.assertIn("dropped beyond 32K", items[0]["text"])
        self.assertFalse(any("pubmed.ncbi.nlm.nih.gov/42211289" in c for c in self.web.calls))

    def test_gradio_space_skips_browser(self):
        called = []
        browser.render = lambda *a, **k: called.append(a) or fixtures.fake_render(*a, **k)
        art = sources.fetch_page("https://huggingface.co/spaces/snap-stanford/stark-leaderboard")
        self.assertEqual(called, [])
        self.assertFalse(art["via_browser"])


class QualityTest(CollectBase):
    """时效：默认 90 天窗口过滤旧资料；相关性：短语匹配、泛词不计分、零散命中封顶。"""

    def test_time_window_filters_old_items(self):
        tid = create_task("research", "开源大模型 长上下文 评测",
                          params={"no_llm": True, "max_docs": 30, "keywords": ["长上下文"],
                                  "en_keywords": ["long context LLM"], "sources": ["36kr", "hn"]})
        self.assertEqual(run_task(tid), "done")
        task = db.query_one("SELECT * FROM tasks WHERE id=?", (tid,))
        self.assertIsNone(db.query_one("SELECT id FROM documents WHERE canonical_url='https://36kr.com/p/8000'"))
        q = task["state"]["report"]["quality"]
        self.assertGreaterEqual(q["dropped_old"], 1)
        self.assertEqual(q["window_days"], 90)
        hn_calls = [c for c in self.web.calls if c.startswith("https://hn.algolia.com/")]
        self.assertTrue(all("numericFilters=created_at_i" in c for c in hn_calls))

    def test_relevance_v2(self):
        from app.graphs.research import _rel
        st = {"plan": {"terms_zh": ["长上下文", "大模型"], "queries_en": ["long context LLM"]}}
        self.assertGreaterEqual(_rel(st, "Long-context evals for open LLMs are misleading"), 0.85)
        # 只零散出现 long、context，讲的是别的事
        scattered = _rel(st, "A long weekend", "The context of the policy debate was about trade and LLM chips.")
        self.assertLess(scattered, 0.4)
        # 只命中泛词“大模型”不算相关
        self.assertLess(_rel(st, "大模型价格战再起", "多家厂商下调大模型 API 价格"), 0.4)
        self.assertGreaterEqual(_rel(st, "长上下文评测踩坑", ""), 0.9)
        self.assertGreaterEqual(_rel(st, "如何评测", "本文讨论长上下文能力"), 0.6)
        st2 = {"plan": {"terms_zh": [], "queries_en": ["stock trend prediction"]}}
        self.assertGreaterEqual(_rel(st2, "Predicting stock trends with transformers"), 0.4)

    def test_llm_judge_demotes_keyword_coincidence(self):
        from app import llm

        def fake_chat(messages, **kw):
            text = messages[-1]["content"]
            if "逐条判断与主题的相关程度" not in text:
                raise RuntimeError("only judge in this test")
            scores = []
            for line in text.splitlines():
                m = __import__("re").match(r"\[(\d+)\] (.*)", line)
                if m:
                    bad = "Paywalled" in m.group(2)
                    scores.append({"i": int(m.group(1)), "score": 0 if bad else 3,
                                   "reason": "关键词巧合" if bad else "直接讨论长上下文评测"})
            data = {"scores": scores}
            return {"content": json.dumps(data), "data": data, "usage": {}, "_cost": 0}

        old = (llm.available, llm.chat)
        llm.available, llm.chat = (lambda: True), fake_chat
        try:
            tid = create_task("research", "开源大模型 长上下文 评测",
                              params={"max_docs": 30, "keywords": ["长上下文"], "en_keywords": ["long context LLM"],
                                      "sources": ["hn", "github", "arxiv"], "auto_translate": False})
            self.assertEqual(run_task(tid), "done")
        finally:
            llm.available, llm.chat = old
        rep = db.query_one("SELECT state FROM tasks WHERE id=?", (tid,))["state"]["report"]
        q = rep["quality"]
        self.assertEqual(q["judge"]["by"], "llm")
        self.assertGreaterEqual(q["judge"]["judged"], 3)
        pw = db.query_one("SELECT id, meta FROM documents WHERE url='https://paywall.example.com/a'")
        self.assertEqual(pw["meta"]["judge"]["score"], 0)
        self.assertLess(pw["meta"]["relevance"], 0.4)
        self.assertNotIn(pw["id"], rep["delivered_docs"])
        blog = db.query_one("SELECT meta FROM documents WHERE url='https://blog.example.com/long-context'")
        self.assertGreaterEqual(blog["meta"]["relevance"], 0.7)


class RecheckScopeTest(CollectBase):
    """手动复查：只查当前任务的资料，标题显示主题，结果写入报告；没有待复查页面时说明原因。"""

    def test_scoped_manual_recheck(self):
        params = {"no_llm": True, "max_docs": 30, "en_keywords": ["long context LLM"],
                  "sources": ["hn", "github", "arxiv"]}
        t1 = create_task("research", "开源大模型 长上下文 评测", params=params)
        self.assertEqual(run_task(t1), "done")
        other = store.upsert_document({"url": "https://github.com/other/repo", "title": "other",
                                       "text": "x" * 50, "content_kind": "full", "source_id": "web"}, "别的主题")
        from app import server
        c = server.app.test_client()
        from app.harness import runner
        started = []
        old = runner.start
        runner.start = started.append
        try:
            r = c.post("/api/recheck", json={"task": t1})
            r2 = c.post("/api/recheck", json={"task": t1})          # 重复点击：复用同一个任务
        finally:
            runner.start = old
        rid = r.json["id"]
        self.assertEqual(r2.json["id"], rid)
        task = db.query_one("SELECT * FROM tasks WHERE id=?", (rid,))
        self.assertEqual(task["topic"], "开源大模型 长上下文 评测")
        self.assertEqual(run_task(rid), "done")
        rep = db.query_one("SELECT state FROM tasks WHERE id=?", (rid,))["state"]["report"]
        self.assertEqual(rep["kind"], "recheck")
        self.assertTrue(rep["force"])
        urls = {i["url"] for i in rep["items"]}
        self.assertIn("https://github.com/example/longeval", urls)      # 仓库属于可能变化的页面
        self.assertNotIn("https://github.com/other/repo", urls)         # 其他主题的资料不在范围内
        self.assertTrue(all(i["outcome"] in ("unchanged", "updated", "enriched", "failed") for i in rep["items"]))

    def test_nothing_to_recheck(self):
        tid = create_task("recheck", "空主题", params={"force": True})
        self.assertEqual(run_task(tid), "done")
        rep = db.query_one("SELECT state FROM tasks WHERE id=?", (tid,))["state"]["report"]
        self.assertEqual(rep["checked"], 0)

    def test_recheck_uses_status_apis(self):
        params = {"no_llm": True, "max_docs": 30, "en_keywords": ["long context LLM"],
                  "sources": ["hn", "github", "arxiv"]}
        t1 = create_task("research", "开源大模型 长上下文 评测", params=params)
        self.assertEqual(run_task(t1), "done")
        r1 = create_task("recheck", "开源大模型 长上下文 评测", params={"force": True, "scope_task": t1})
        self.assertEqual(run_task(r1), "done")
        calls = db.query("SELECT tool, input FROM tool_calls WHERE task_id=?", (r1,))
        fetched = [c["input"]["url"] for c in calls if c["tool"] == "page_fetch"]
        self.assertFalse(any("github.com/example/longeval" in u or "arxiv.org" in u for u in fetched))
        self.assertEqual(sum(c["tool"] == "github_status" for c in calls), 1)
        rep = db.query_one("SELECT state FROM tasks WHERE id=?", (r1,))["state"]["report"]
        gh = next(i for i in rep["items"] if "github.com/example/longeval" in i["url"])
        self.assertEqual(gh["outcome"], "unchanged")
        # 仓库有新提交 → 才打开页面看具体变化
        old = fixtures.GH_JSON
        fixtures.GH_JSON = old.replace("2026-10-05T00:00:00Z", "2026-10-10T00:00:00Z")
        try:
            r2 = create_task("recheck", "开源大模型 长上下文 评测", params={"force": True, "scope_task": t1})
            self.assertEqual(run_task(r2), "done")
        finally:
            fixtures.GH_JSON = old
        rep2 = db.query_one("SELECT state FROM tasks WHERE id=?", (r2,))["state"]["report"]
        gh2 = next(i for i in rep2["items"] if "github.com/example/longeval" in i["url"])
        self.assertIn("有新提交", gh2["note"])
        fetched2 = [c["input"]["url"] for c in db.query("SELECT input FROM tool_calls WHERE task_id=? AND tool='page_fetch'", (r2,))]
        self.assertIn("https://github.com/example/longeval", fetched2)


class TitleTest(CollectBase):
    def test_overlong_title_truncated(self):
        long = "GitHub - a/b: # Application card: " + "GitHub Copilot Chat responsibly. " * 200
        r = store.upsert_document({"url": "https://github.com/a/b", "title": long, "text": "x" * 100,
                                   "content_kind": "full", "source_id": "web"}, "T")
        t = db.query_one("SELECT title FROM documents WHERE id=?", (r["doc_id"],))["title"]
        self.assertLessEqual(len(t), 161)
        self.assertTrue(t.endswith("…"))
        self.assertFalse(t.startswith("GitHub - "))


class RobustnessTest(CollectBase):
    def test_bad_gzip_and_unexpected_errors_do_not_fail_task(self):
        """服务器声明 gzip 但内容损坏、或处理某个页面时出现意外异常：只记一条失败，任务继续完成。"""
        from app.collect import tools as _tools  # noqa: F401
        from app.harness.core import TOOLS
        orig = TOOLS["page_fetch"].fn

        def flaky(url, mode="auto", fresh=False):
            if "blog.example.com" in url:
                raise ValueError("Received response with content-encoding: gzip, but failed to decode it.")
            return orig(url, mode=mode, fresh=fresh)
        TOOLS["page_fetch"].fn = flaky
        try:
            tid = create_task("research", "开源大模型 长上下文 评测",
                              params={"no_llm": True, "max_docs": 30, "en_keywords": ["long context LLM"],
                                      "sources": ["hn", "github", "arxiv"], "verify_top": False})
            self.assertEqual(run_task(tid), "done")
        finally:
            TOOLS["page_fetch"].fn = orig
        st = db.query_one("SELECT state FROM tasks WHERE id=?", (tid,))["state"]
        self.assertTrue(any("gzip" in (f.get("reason") or "") for f in st["failures"]))
