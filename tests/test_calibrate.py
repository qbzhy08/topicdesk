"""判断校准：洗稿识别、同一事件（标题较像 + 共享数字）、网页更新去噪、按人工标注调阈值（交叉验证才采用）。"""
from app import calibrate as cal
from app import db
from app.collect.store import describe_change, upsert_document
from tests.helpers import DBTestCase

ORIG = ("国家电影局10月8日发布数据，2026年国庆档全国电影票房11.65亿元，观影人次3199.59万。"
        "国产影片票房占比78.7%，平均票价36.41元。票房排名前五的影片中，《神探之痕迹》票房4.23亿元居首。"
        "业内人士认为，今年国庆档单日票房连续七天过亿，市场整体平稳。"
        "截至10月7日，2026年全年总票房达307.46亿元，观影人次7.84亿。"
        "多家院线表示，假期后半段家庭观众明显增多，动画电影的场次占比也有所提高。")
# 洗稿：逐句换说法、调语序，关键数字不变
WASH = ("据国家电影局8日公布的数据，今年国庆档电影票房达到11.65亿元，观影人次为3199.59万。"
        "其中国产影片的票房占比为78.7%，平均票价36.41元。前五名影片里，《神探之痕迹》以4.23亿元票房排在第一。"
        "有业内人士表示，今年国庆档单日票房连续7天超过1亿元，整体市场较为平稳。"
        "截至10月7日，2026年总票房已达307.46亿元，观影人次达到7.84亿。"
        "不少院线称，假期后半段带孩子来看电影的家庭明显变多，动画片的排片比例也有提升。")
OTHER = ("今年国庆假期，多地文旅部门推出夜游、演出等活动。某市一家剧场负责人说，假期七天场场爆满，"
         "不少家庭专门从外地赶来。与此同时，部分景区出现排队时间较长的情况，管理部门增派了工作人员。"
         "专家建议，景区应提前公布预约信息，引导游客错峰出行。"
         "记者在现场看到，不少游客选择傍晚入园，避开中午的高峰；也有商户延长了营业时间，夜间消费明显增加。")


class CalibrateTest(DBTestCase):
    def _doc(self, url, title, text, pub="2026-10-08T10:00:00+08:00"):
        return upsert_document({"url": url, "title": title, "text": text, "content_kind": "full",
                                "source_id": "web", "published_at": pub}, "T")

    def test_rewrite_detected_as_not_independent(self):
        a = self._doc("https://a.com/1", "国庆档票房11.65亿元", ORIG)
        f = cal.reprint_features({"text": ORIG}, {"text": WASH})
        self.assertGreater(f["hamming"], 6)                      # 指纹认不出来
        self.assertGreaterEqual(f["align"], 0.6)                 # 但句子一一对应
        b = self._doc("https://b.com/2", "今年国庆档电影票房达11.65亿", WASH)
        self.assertEqual(b["relation"], "rewrite")
        row = db.query_one("SELECT duplicate_of FROM documents WHERE id=?", (b["doc_id"],))
        self.assertEqual(row["duplicate_of"], a["doc_id"])       # 归到原稿，不算独立来源
        c = self._doc("https://c.com/3", "国庆假期多地夜游火爆", OTHER)
        self.assertNotIn(c["relation"], ("rewrite", "reprint"))

    def test_event_by_shared_numbers(self):
        f = cal.event_features({"title": "国庆档票房11.65亿元", "text": ORIG, "published_at": "2026-10-08"},
                               {"title": "电影局：国产片占比78.7%", "text": WASH, "published_at": "2026-10-09"})
        self.assertLess(f["title_sim"], 0.42)
        self.assertGreaterEqual(f["num_shared"], 2)
        self.assertEqual(cal.decide_event({**f, "title_sim": 0.3}, cal.DEFAULTS["event"]), "same")

    def test_update_ignores_view_counts_and_times(self):
        old = "某公司发布新模型，参数规模700亿。阅读 1.2万 评论 35 发布于 2026-10-08 10:20"
        new = "某公司发布新模型，参数规模700亿。阅读 3.5万 评论 88 发布于 2026-10-08 10:20"
        self.assertFalse(describe_change(old, new)["significant"])
        self.assertTrue(describe_change(old, old.replace("700亿", "720亿"))["significant"])

    def test_fit_from_labels_and_apply(self):
        # 人认为：指纹距离 ≤10 都是转载（现行规则 ≤6 漏判）
        for i in range(16):
            ham = i + 1
            cal.save_label("reprint", f"k{i}", "reprint" if ham <= 10 else "independent",
                           {"hamming": ham, "align": 0.1, "sents": 8, "num_jacc": 0.1}, "", {"title": f"t{i}"})
        r = cal.report("reprint")
        self.assertLess(r["current"]["recall"], 1)
        self.assertIsNotNone(r["fit"])
        self.assertEqual(r["fit"]["params"]["hamming"], 10)
        self.assertTrue(r["fit"]["recommend"])
        cal.apply("reprint")
        self.assertEqual(cal.params("reprint")["hamming"], 10)  # 入库判断实时读取新参数
        cal.reset_params("reprint")
        self.assertEqual(cal.params("reprint")["hamming"], 6)

    def test_too_few_labels_no_fit(self):
        cal.save_label("event", "x", "same", {"title_sim": 0.5, "days": 1, "num_shared": 0}, "same")
        r = cal.report("event")
        self.assertIsNone(r["fit"])
        self.assertIn("再标", r["need"])

    def test_candidates_and_api(self):
        from app.server import create_app
        self._doc("https://a.com/1", "国庆档票房11.65亿元", ORIG)
        self._doc("https://c.com/3", "国庆档票房之外：多地夜游火爆", OTHER)
        c = create_app().test_client()
        items = c.get("/api/calibrate/event/next?n=4").get_json()["items"]
        self.assertTrue(items)
        it = items[0]
        r = c.post("/api/calibrate/event/label", json={"key": it["key"], "label": "different",
                                                        "features": it["features"], "system": it["system"]}).get_json()
        self.assertEqual(r["report"]["labels"], 1)
        self.assertNotIn(it["key"], [x["key"] for x in c.get("/api/calibrate/event/next?n=4").get_json()["items"]])
        ov = c.get("/api/calibrate").get_json()
        self.assertEqual([k["kind"] for k in ov["kinds"]], ["reprint", "event", "update", "dispute"])
        self.assertEqual(c.post("/api/calibrate/event/apply", json={}).status_code, 400)   # 标注不够

    def test_dispute_items_and_resolution_fit(self):
        from app import memory as mem
        fa = mem.add("topic", "fact", "上下文窗口为 128K", topic="T", meta={"verify": "disputed", "primary": True, "independent": 1})
        fb = mem.add("topic", "fact", "上下文窗口为 64K", topic="T", meta={"verify": "disputed", "independent": 2})
        mem.add("topic", "dispute", "说法不一致", topic="T", derived_from=[fa, fb], meta={"facts": [fa, fb]})
        items = cal.dispute_items("T")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["system"], "open")              # 证据分 3 vs 2，差距不够，暂不裁决
        # 人工裁决：一手来源那条总是对的 → 调参后应更看重一手来源、敢于裁决
        for i in range(8):
            cal.save_label("dispute", f"d{i}", "a", {"a": {"primary": True, "independent": 1},
                                                    "b": {"primary": False, "independent": 2}, "conflict": True}, "open")
        for i in range(6):
            cal.save_label("dispute", f"e{i}", "b", {"a": {"primary": False, "independent": 1},
                                                    "b": {"primary": False, "independent": 4}, "conflict": True}, "b")
        r = cal.report("dispute")
        self.assertEqual(r["fit"]["train"]["resolve_accuracy"], 1.0)
        self.assertGreater(r["fit"]["train"]["coverage"], r["current"]["coverage"])

    def test_rule_vs_llm_vs_hybrid(self):
        """同一批人工标注上比较规则、大模型、混合；大模型问两次量一致性。"""
        from app import calibrate_llm as cl, llm
        from app.server import create_app
        a = self._doc("https://a.com/1", "国庆档票房11.65亿元", ORIG)["doc_id"]
        b = self._doc("https://b.com/2", "今年国庆档电影票房达11.65亿", WASH)["doc_id"]
        c = self._doc("https://c.com/3", "国庆假期多地夜游火爆", OTHER)["doc_id"]
        k1, k2 = "|".join(sorted([a, b])), "|".join(sorted([a, c]))
        cal.save_label("reprint", k1, "rewrite", cal.reprint_features({"text": ORIG}, {"text": WASH}), "rewrite")
        cal.save_label("reprint", k2, "independent", cal.reprint_features({"text": ORIG}, {"text": OTHER}), "independent")
        calls = []

        def fake(messages, **kw):          # 假大模型：第一次把两对都判成独立报道，第二次判对（模拟“问两次答案不同”）
            calls.append(1)
            first = sum(1 for _ in calls) <= 2
            return {"data": {"label": "independent" if first else "rewrite", "reason": "测试"}}
        old = llm.available, llm.chat
        llm.available, llm.chat = (lambda: True), fake
        try:
            cl.run("reprint", workers=1)              # 单线程：保证先问完第一次再问第二次
        finally:
            llm.available, llm.chat = old
        cmp_ = create_app().test_client().get("/api/calibrate/reprint/llm").get_json()
        self.assertEqual(cmp_["judged"], 2)
        self.assertEqual(cmp_["rule"]["accuracy"], 1.0)
        self.assertEqual(cmp_["llm"]["calls"], 2)
        self.assertLess(cmp_["llm"]["accuracy"], 1.0)                 # 把洗稿判成了独立报道
        self.assertTrue(cmp_["llm_wrong"])
        self.assertEqual(cmp_["consistency"]["n"], 2)
        self.assertLessEqual(cmp_["hybrid"]["calls"], cmp_["llm"]["calls"])
