"""成稿“像人写的”那一层：限制说明怎么写、争议按句子判断、来源写网站名、AI 腔句式识别、写作说明清理。"""
import unittest

from app import writing


def _flags(**kw):
    base = {1: {"risky": False, "disputed": True, "label": "文化和旅游部"},
            2: {"risky": True, "disputed": False, "label": "知乎"},
            "_disputed": ["入境游客比2024年增长19.0%"]}
    base.update(kw)
    return base


class StyleTest(unittest.TestCase):
    def test_dispute_is_sentence_level(self):
        """资料里有一条说法有争议，不代表引用它的每句话都有争议：官方总量数字不该被标“各方说法不一”。"""
        secs = [{"id": "a", "md": "7天国内出游8.26亿人次，花费7383.75亿元[1]。\n\n入境游客比2024年增长19.0%[1]。"}]
        aud = writing.hedge_audit(secs, _flags())
        kinds = {x["sentence"][:6]: x["kind"] for x in aud["items"]}
        self.assertNotIn("7天国内出游", kinds)
        self.assertEqual(kinds.get("入境游客比2"), "disputed")

    def test_attribution_counts_as_hedge_and_once_per_claim(self):
        """单一来源的说法，交代了“谁说的”就够了；同一组数字第二次出现不再补说明。"""
        secs = [{"id": "a", "md": "众信旅游称，赴欧人数同比增长140%[2]。\n\n赴北美增长480%[2]。\n\n赴北美增长480%，增速最高[2]。"}]
        r = writing.ensure_hedges(secs, _flags())
        md = secs[0]["md"]
        self.assertNotIn("众信旅游称，赴欧人数同比增长140%（", md)          # 已交代出处方，不再加括注
        self.assertEqual(md.count("（仅见于知乎）"), 1)                    # 同一个说法只补一次
        self.assertNotIn("尚待证实", md)
        self.assertEqual(r["added"], 1)
        r2 = writing.ensure_hedges(secs, _flags())                         # 幂等
        self.assertEqual(secs[0]["md"], md)
        self.assertEqual(r2["added"], 1)

    def test_old_verbose_notes_are_replaced(self):
        secs = [{"id": "a", "md": "赴北美增长480%（目前只有全网搜索报道，尚待证实）[2]。"}]
        writing.ensure_hedges(secs, _flags())
        self.assertEqual(secs[0]["md"].split("\n")[0], "赴北美增长480%（仅见于知乎）[2]。")

    def test_publisher_label_not_channel(self):
        d = {"source_id": "web_search", "label": "全网搜索，2026-10-09", "url": "https://news.ifeng.com/c/8wrsd"}
        self.assertEqual(writing.publisher_label(d), "凤凰网")
        self.assertEqual(writing.site_name("https://www.mct.gov.cn/whzx/x.htm"), "文化和旅游部")
        self.assertEqual(writing.site_name("https://www.nxshgzb.gov.cn/x"), "政府网站")
        self.assertIn("凤凰网", writing.source_label({**d, "published_at": "2026-10-01"}, {"web_search": "全网搜索"}))
        self.assertEqual(writing.publisher_label({"source_id": "zhihu", "label": "知乎", "url": "https://zhuanlan.zhihu.com/p/1"}), "知乎")

    def test_trope_sentences_and_meta_clauses(self):
        md = ("增长动力正在换挡。人次涨了，不是爆发而是温和。把这三条放在一起看，结论很清楚。"
              "赴欧140%来自众信[11]，引用时必须标注来源。普通的一句话[1]。")
        whys = [x["why"] for x in writing.trope_sentences(md)]
        self.assertEqual(len(whys), 4)
        clean, removed = writing.strip_meta(md)
        self.assertIn("赴欧140%来自众信[11]。", clean)
        self.assertNotIn("引用时", clean)
        self.assertTrue(removed)

    def test_titles_and_headings(self):
        self.assertEqual(writing.clean_title("共庆华诞#"), "共庆华诞")
        self.assertEqual(writing.clean_heading("【分析】增长不只在人次"), "增长不只在人次")
        self.assertEqual(writing.clean_heading("先说结论：人次涨了"), "人次涨了")



class BenchMetricsTest(unittest.TestCase):
    def test_metrics_and_compare(self):
        from app import stylebench as sb
        sys_md = ("# 标题\n\n## 先说结论\n\n全国出游8.26亿人次，增长6.3%[1]。这意味着市场在换挡。\n\n"
                  "## 数字\n\n8.26亿人次、增长6.3%，是按可比口径算的[1]。\n\n【分析】总量扩张。\n\n【分析】结构变化。")
        human = ("全国国内出游8.26亿人次，按可比口径同比增长6.3%。“长线游明显增多。”中国旅游研究院研究员黄璜说。"
                 "入境游客298.3万人次，比上年多了不少，酒店预订也跟着涨。")
        m = sb.metrics(__import__("app.evaluate", fromlist=["x"]).plain_text(sys_md), sys_md)
        h = sb.metrics(human)
        self.assertGreater(m["extra_number_mentions_per_1k"], 0)
        self.assertEqual(h["quotes"], 1)
        self.assertEqual(m["template_headings"], ["先说结论"])
        self.assertTrue(m["lead_concrete"])
        keys = {g["key"] for g in sb.compare(m, [h])["gaps"]}
        self.assertTrue({"extra_number_mentions_per_1k", "trope_sentences_per_1k", "quotes_per_1k", "template_headings"} <= keys)
        agg = sb.aggregate([{"topic": "t", "compare": sb.compare(m, [h])}])
        self.assertEqual(agg["compared"], 1)



class SourceQualityTest(unittest.TestCase):
    def test_self_archive_not_primary(self):
        """Zenodo 这类自存档论文：DOI 看着像期刊论文，但任何人都能上传，不算一手资料，正文里要交代“未经同行评审”。"""
        from app.collect.sources import is_primary, self_archive
        z = "https://doi.org/10.5281/zenodo.23298560"
        self.assertTrue(self_archive(z))
        self.assertFalse(is_primary(z))
        self.assertTrue(is_primary("https://doi.org/10.1038/s41586-026-0001"))
        self.assertEqual(writing.publisher_label({"url": z, "source_id": "openalex"}), "一篇未经同行评审的自存档论文")
        self.assertIn("未经同行评审", writing.source_label({"url": z, "source_id": "openalex"}, {"openalex": "OpenAlex 学术"}))

    def test_undash_and_headings(self):
        self.assertEqual(writing.undash("几组数字——普吉岛增78%——目前只有单一报道。"), "几组数字，普吉岛增78%，目前只有单一报道。")
        self.assertIn("——", writing.undash("他说：“这是——好事。”"))            # 原话不动
        self.assertEqual(writing.clean_heading("我们的判断：诺奖实至名归"), "诺奖实至名归")

    def test_auto_topic_filters(self):
        import sys
        sys.path.insert(0, "tools")
        import style_bench as B
        self.assertTrue(B.JUNK_TITLE.search("统计数据_阿巴嘎旗人民政府"))
        self.assertFalse(B.JUNK_TITLE.search("国庆档票房11.65亿元 国产片占比78.7%"))
        self.assertFalse(B.mainstream("https://www.163.com/dy/article/X.html"))     # 网易号是自媒体
        self.assertTrue(B.mainstream("https://www.chinanews.com.cn/gj/2026/10-05/1.shtml"))


    def test_source_tier_and_date(self):
        from app.llm import _with_date
        self.assertEqual(writing.source_tier({"url": "https://www.mct.gov.cn/x"}), "一手/官方")
        self.assertEqual(writing.source_tier({"url": "https://www.163.com/dy/article/x"}), "自媒体/社区，个人说法")
        self.assertEqual(writing.source_tier({"url": "https://news.ifeng.com/c/x"}), "媒体报道")
        msgs = _with_date([{"role": "system", "content": "你只输出 JSON。"}, {"role": "user", "content": "x"}])
        self.assertIn("今天是", msgs[0]["content"])
        self.assertEqual(len(msgs), 2)


    def test_semantic_verdict_needs_real_evidence(self):
        src = ["官方宣称该模型支持 128K 上下文，开发者可通过接口调用。"]
        ok = writing.checked_verdict({"verdict": "supported", "evidence": "官方宣称该模型支持 128K 上下文"}, src)
        self.assertEqual(ok["verdict"], "supported")
        bad = writing.checked_verdict({"verdict": "supported", "evidence": "官方确认上下文扩展到 256K"}, src)
        self.assertEqual(bad["verdict"], "unsupported")       # 编出来的依据：降为不支持
        none = writing.checked_verdict({"verdict": "partial"}, src)
        self.assertEqual(none["verdict"], "unsupported")
        doc = ["文化和旅游部数据中心测算，国庆假期7天全国国内出游8.26亿人次，按可比口径（日均）比2025年增长6.3%。"]
        # 漏抄几个字、全半角不同、用省略号：仍算找到
        self.assertTrue(writing.evidence_ok("国内出游8.26亿人次，按可比口径比2025年增长6.3%", doc, "增长6.3%[1]。"))
        self.assertTrue(writing.evidence_ok("文化和旅游部数据中心测算……国内出游8.26亿人次", doc, "出游8.26亿人次[1]。"))
        # 原文各处的字拼起来的“依据”、真实但和句子数字无关的依据：不算
        self.assertFalse(writing.evidence_ok("全国游客总量达8.26亿，同比大增", doc, "出游8.26亿人次[1]。"))
        self.assertFalse(writing.evidence_ok("国庆假期7天全国国内出游", doc, "增长6.3%[1]。"))


if __name__ == "__main__":
    unittest.main()
