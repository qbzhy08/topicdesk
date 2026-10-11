"""Lv3 写作：Skill 加载、按 Skill 选材、逐句核对、自动改正出处、发现缺口回到检索、按核对结果改写。"""
import json
import re
import tempfile
from pathlib import Path

from app import db, llm, skills, writing
from app.harness import create_task, run_task
from tests.test_collect import CollectBase
from tests import test_memory as _tm
TOPIC = _tm.TOPIC


def _num_of(prompt, needle):
    m = re.search(r"\[(\d+)\] [^\n]*" + needle, prompt)
    return int(m.group(1)) if m else 1


class FakeWriter:
    """模拟大模型：提纲给出一个缺口；成稿故意犯三类错误，看核对能不能抓到并改正。"""

    def __init__(self):
        self.calls = []

    def __call__(self, messages, json_mode=False, max_tokens=1500, **_):
        p = messages[-1]["content"]
        self.calls.append(p)
        if "能否由给出的“出处原文”支持" in p:       # 语义核对：改写过的句子判为支持，其余不支持
            n = len(re.findall(r"^\d+\. 句子：", p, re.M))
            res = [{"i": i + 1, "verdict": "supported" if "差距" in seg else "unsupported", "reason": "测试"}
                   for i, seg in enumerate(re.split(r"^\d+\. 句子：", p, flags=re.M)[1:n + 1])]
            return {"content": "", "_cost": 0, "data": {"results": res}}
        if "找出最多 2 组适合画图" in p:            # 一组真实数字 + 一组编造的数字
            n = _num_of(p, "32K") if "32K" in p else 1
            m = re.search(r"^\[(\d+)\][^\n]*32K[^\n]*64K[^\n]*128K", p, re.M)
            n = int(m.group(1)) if m else n
            return {"content": "", "_cost": 0, "data": {"charts": [
                {"kind": "bar", "title": "实测的上下文长度", "unit": "K", "refs": [n],
                 "items": [{"label": "短", "value": 32}, {"label": "中", "value": 64}, {"label": "长", "value": 128}],
                 "takeaway": "实测覆盖了三档长度"},
                {"kind": "line", "title": "编造的趋势", "unit": "K", "refs": [n],
                 "items": [{"label": "1", "value": 32}, {"label": "2", "value": 999}, {"label": "3", "value": 128}]}]}}
        if "列提纲" in p:
            ids = re.findall(r"- id=(\w+)｜", p)
            return {"content": "", "_cost": 0, "data": {
                "title": "开源大模型长上下文：宣称与复现的差距",
                "sections": [{"id": i, "heading": None, "points": ["要点"], "sources": []} for i in ids],
                "gaps": [{"section": ids[-1], "question": "缺少第三方评测", "query": "长上下文"}]}}
        a = _num_of(p, "成绩公布")          # 官方宣称 128K 的那篇
        b = _num_of(p, "遭质疑")            # 复现发现 64K 以上下降的那篇
        if "上一稿核对发现这些问题" in p:     # 按核对结果改写：去掉编造的数字
            return {"content": f"官方宣称支持 128K 上下文[{a}]。\n\n【分析】宣称和复现之间的差距，说明仅凭单一评测下结论还为时过早。",
                    "_cost": 0}
        if "核心结论" in p or "先说结论" in p:
            return {"content": (f"官方宣称该模型支持 128K 上下文[{a}]。"
                                f"复现评测发现在 64K 以上时检索准确率明显下降[{a}]。\n\n"   # 错误 1：出处引错
                                "官方宣称支持 128K 上下文。\n\n"                           # 错误 2：有数字没出处
                                "【分析】预计明年会扩展到 1000K 上下文。"), "_cost": 0}    # 错误 3：分析里编数字
        return {"content": f"社区开发者的复现结果与官方宣称存在差距[{b}]。", "_cost": 0}


class WriteBase(CollectBase):
    run_research = _tm.MemoryTest.run_research

    def setUp(self):
        super().setUp()
        self.t1, _ = self.run_research()
        self._llm_old = (llm.available, llm.chat)

    def tearDown(self):
        llm.available, llm.chat = self._llm_old
        super().tearDown()

    def write(self, skill, **params):
        w = create_task("write", TOPIC, params={"research_task": self.t1, "skill": skill, **params},
                        budget={"max_tool_calls": 200, "max_steps": 100})
        status = run_task(w)
        task = db.query_one("SELECT * FROM tasks WHERE id=?", (w,))
        self.assertEqual(status, "done", task["error"])
        art = task["state"]["result"]
        v = db.query_one("SELECT * FROM article_versions WHERE article_id=? AND version=?",
                         (art["article_id"], art["version"]))
        return task, v


class WriteTest(WriteBase):
    def test_skills_load_and_validate(self):
        all_ = skills.load_all()
        self.assertEqual(sorted(all_), ["brief", "compare", "explainer"])
        self.assertEqual(skills.load_all.errors, [])
        with self.assertRaises(skills.SkillError):
            skills.parse("---\nid: x\nname: y\n---\n", "bad")
        # 新增 Skill 只需放一个文件
        with tempfile.TemporaryDirectory() as d:
            old = skills.SKILL_DIR
            skills.SKILL_DIR = Path(d)
            try:
                (Path(d) / "news").mkdir()
                (Path(d) / "news" / "SKILL.md").write_text(
                    "---\nid: news\nname: 快讯\nversion: 0.1\nreader: 所有人\nstructure:\n  - heading: 正文\n---\n只写事实。",
                    encoding="utf-8")
                self.assertIn("news", skills.load_all())
                self.assertEqual(skills.get("news")["body"], "只写事实。")
            finally:
                skills.SKILL_DIR = old

    def test_rule_mode_writes_traceable_article(self):
        task, v = self.write("brief", no_llm=True)
        md = v["markdown"]
        self.assertIn("## 参考资料", md)
        self.assertIn("【分析】", md)
        cites = {int(x) for x in re.findall(r"\[(\d+)\]", md.split("## 参考资料")[0])}
        refs = re.findall(r"^(\d+)\. ", md.split("## 参考资料")[1], re.M)
        self.assertEqual(sorted(cites), [int(r) for r in refs])          # 编号连续且都有参考资料
        self.assertEqual(sorted(cites), list(range(1, len(cites) + 1)))
        meta = v["meta"]
        self.assertEqual(meta["skill"]["id"], "brief")
        self.assertTrue(meta["skill"]["fingerprint"])
        self.assertEqual(meta["check"]["summary"]["support_rate"], 1.0)

    def test_skills_change_material(self):
        t_brief, _ = self.write("brief", no_llm=True)
        t_exp, _ = self.write("explainer", no_llm=True)
        b, e = t_brief["state"], t_exp["state"]
        own = [d for d in b["docs"] if not d["why"].startswith("记忆中事实")]
        self.assertLessEqual(len(own), 6)                         # Skill 选材上限（另加记忆中事实的出处）
        self.assertNotEqual([s["heading"] for s in b["sections"]], [s["heading"] for s in e["sections"]])
        self.assertIn("五分钟", b["docs"][0]["why"])
        self.assertIn("三十分钟", e["docs"][0]["why"])

    def test_check_catches_and_fixes(self):
        fake = FakeWriter()
        llm.available, llm.chat = (lambda: True), fake
        task, v = self.write("brief")
        st = task["state"]
        # 提纲发现缺口 → 回到检索
        self.assertGreaterEqual(st["gap_round"], 1)
        self.assertTrue(st["gap_log"])
        self.assertEqual(st["gap_log"][0]["query"], "长上下文")
        # 出处引错 → 自动改到数字真正所在的资料；没出处的数字 → 补上出处
        whys = [f["why"] for f in st["fixes"]]
        self.assertIn("改为数字实际所在的资料", whys)
        self.assertIn("补上出处", whys)
        # 分析里编造的数字 → 按核对结果改写
        self.assertTrue(any(f.get("rewrite") for f in st["fixes"]))
        self.assertTrue(any("上一稿核对发现这些问题" in c for c in fake.calls))
        self.assertNotIn("1000K", v["markdown"])
        s = st["check"]["summary"]
        self.assertEqual(s["number"] + s["uncited"] + s["analysis_num"], 0)
        steps = [r["node"] for r in db.query("SELECT node FROM steps WHERE task_id=? ORDER BY id", (task["id"],))]
        self.assertIn("gap", steps)
        self.assertIn("fix", steps)
        self.assertEqual(steps[-1], "save")

    def test_check_unit(self):
        docs = [{"n": 1, "text": "官方宣称支持 128K 上下文，并在多个评测中领先。"},
                {"n": 2, "text": "复现发现在 64K 以上时检索准确率明显下降。"}]
        secs = [{"id": "a", "md": "复现发现 64K 以上准确率下降[1]。\n\n官方宣称支持 128K 上下文[1]。\n\n【分析】差距可能来自测试条件。"}]
        rep = writing.check(secs, docs)
        st = [i["status"] for i in rep["items"]]
        self.assertEqual(st, ["number", "supported", "analysis"])
        self.assertEqual(rep["items"][0]["alt"], [2])
        fixes = writing.autofix_citations(secs, rep)
        self.assertEqual(len(fixes), 1)
        self.assertIn("下降[2]。", secs[0]["md"])

    def test_semantic_and_tables(self):
        docs = [{"n": 1, "text": "监管层表示，将对程序化交易实施更严格的报备管理。"}]
        secs = [{"id": "a", "md": "监管部门准备收紧对程序化交易的备案要求[1]。\n\n"
                                  "| 维度 | 一方 | 另一方 |\n|---|---|---|\n| 监管 | 收紧程序化交易报备管理[1] | 目前没有找到公开的反驳 |\n\n"
                                  "【分析】这意味着高频策略成本上升[1]。"}]
        rep = writing.check(secs, docs)
        st = {i["sentence"]: i["status"] for i in rep["items"]}
        self.assertEqual(st["监管部门准备收紧对程序化交易的备案要求[1]。"], "weak")
        self.assertNotIn("| 监管 | 收紧程序化交易报备管理[1] | 目前没有找到公开的反驳 |", st)   # 表格按单元格核对
        self.assertIn("收紧程序化交易报备管理[1]", st)
        self.assertNotIn("维度", " ".join(st))                                            # 表头不核对
        self.assertEqual(st["【分析】这意味着高频策略成本上升[1]。"], "analysis")          # 分析段只核数字
        v = {"监管部门准备收紧对程序化交易的备案要求[1]。": {"verdict": "supported", "reason": "同义"}}
        rep2 = writing.check(secs, docs, v)
        self.assertEqual({i["sentence"]: i["status"] for i in rep2["items"]}["监管部门准备收紧对程序化交易的备案要求[1]。"],
                         "semantic")
        self.assertGreater(rep2["summary"]["support_rate"], rep["summary"]["support_rate"])

    def test_gap_relevance_filter(self):
        from app.graphs.write import _relevant
        st = {"terms": ["量化", "A股"]}
        self.assertFalse(_relevant(st, "量化 成交占比 数据", {"title": "CAE技术在汽车轻量化设计中的应用", "summary": ""}))
        self.assertTrue(_relevant(st, "量化 成交占比 数据", {"title": "A股量化成交占比降至两成", "summary": "量化成交数据"}))


    def test_problem_sentences_marked_in_article(self):
        from app import mdrender
        from app.server import _mark_problems
        md = "# 标题\n\n## 一\n\n官方宣称支持 256K 上下文[1]。另一句正常[2]。\n\n## 参考资料\n\n1. [a](http://a)\n2. [b](http://b)\n"
        meta = {"refmap": {"5": 1, "7": 2},
                "check": {"items": [{"status": "number", "sentence": "官方宣称支持 256K 上下文[5]。"},
                                    {"status": "supported", "sentence": "另一句正常[7]。"}]}}
        html = mdrender.render(_mark_problems(md, meta))
        self.assertIn('<mark class="chk-mark" data-chk="0">官方宣称支持 256K 上下文', html)
        self.assertEqual(html.count("chk-mark"), 1)
        self.assertIn('id="ref-1"', html)

    def test_strip_meta_text(self):
        md, removed = writing.strip_meta("官方宣称支持 128K[1]。关于这两项数据，目前公开资料未能确认其具体出处，本节暂不引用。\n\n"
                                         "司美格鲁肽减重 15%（记忆中的事实）。\n\n| a | b |\n|---|---|\n| x | 素材中 |")
        self.assertEqual(len(removed), 2)
        self.assertIn("官方宣称支持 128K[1]。", md)
        self.assertNotIn("本节暂不引用", md)
        self.assertIn("| x | 素材中 |", md)                        # 表格不动

    def test_memory_facts_bring_their_sources(self):
        task, v = self.write("brief", no_llm=True)
        st = task["state"]
        with_refs = [f for f in st["facts"] if f.get("refs")]
        self.assertTrue(with_refs)                                  # 记忆中的事实能标出处编号
        n_ids = {d["n"]: d["id"] for d in st["docs"]}
        for f in with_refs:
            self.assertTrue(set(n_ids[n] for n in f["refs"]) & set(f["docs"]))



class IllustrateTest(WriteBase):
    def test_article_has_cover_and_body_images(self):
        task, v = self.write("brief", no_llm=True)
        meta = v["meta"]
        self.assertGreaterEqual(len(meta["figures"]), 2)
        self.assertTrue(meta["cover"])
        self.assertTrue(all(c["ok"] for c in meta["image_checks"]), meta["image_checks"])
        md = v["markdown"]
        body_imgs = [l for l in md.split("\n") if l.startswith("![")]
        self.assertEqual(len(body_imgs), len(meta["figures"]) + 1)          # 正文图 + 封面
        self.assertEqual(len(set(body_imgs)), len(body_imgs))               # 不重复
        for f in meta["figures"]:
            self.assertIn("图源", f["caption"]) if f["kind"] == "photo" else self.assertIn("示意图", f["caption"])
        from app import mdrender
        html = mdrender.render(md)
        self.assertEqual(html.count("<figure>"), len(body_imgs))
        self.assertIn("<figcaption>", html)

    def test_quality_detects_blur_and_thumbnails(self):
        from PIL import Image, ImageFilter
        from app import config, illustrate as ill
        sharp = Image.open(__import__("io").BytesIO(fixtures_png(900, 500)))
        p1 = config.IMAGE_DIR / "sharp.png"
        sharp.save(p1)
        p2 = config.IMAGE_DIR / "blur.png"
        sharp.filter(ImageFilter.GaussianBlur(8)).save(p2)
        ok = ill.quality("images/sharp.png", 900, 500, "https://x.com/a.png")
        self.assertTrue(ok["ok"], ok)
        bad = ill.quality("images/blur.png", 900, 500, "https://x.com/a.png")
        self.assertFalse(bad["ok"])
        self.assertIn("模糊", "".join(bad["issues"]))
        th = ill.quality("images/sharp.png", 900, 500, "https://x.com/a_thumb_120x80.png")
        self.assertIn("缩略图", "".join(th["issues"]))

    def test_chart_values_must_be_in_sources(self):
        from app import illustrate as ill
        docs = [{"n": 1, "text": "量化成交占比从高峰期的 30% 左右降至 20% 左右，小微盘中占比超过 50%。"}]
        good = {"kind": "bar", "unit": "%", "refs": [1],
                "items": [{"label": "高峰期", "value": 30}, {"label": "目前", "value": 20}, {"label": "小微盘", "value": 50}]}
        self.assertIsNone(ill.verify_chart(good, docs))
        bad = {**good, "items": good["items"][:2] + [{"label": "编的", "value": 65}]}
        self.assertIn("65", ill.verify_chart(bad, docs))
        svg = ill.bar_svg("占比", "%", [{"label": "a", "value": 30.0}, {"label": "b", "value": 20.0}], "来源")
        self.assertIn("示意图", svg)

    def test_final_check_finds_placeholders(self):
        from app import illustrate as ill
        res = {c["name"]: c for c in ill.final_check("正文 {{img:cover}} 还有【此处插入图片】", [], None, 2)}
        self.assertFalse(res["占位符残留"]["ok"])
        self.assertFalse(res["正文配图数量"]["ok"])
        self.assertFalse(res["封面"]["ok"])


def fixtures_png(w, h):
    from tests import fixtures
    return fixtures.png(w, h)


class ImageClarityTest(WriteBase):
    def test_hires_upgrade(self):
        from app.collect import images
        small, big = "https://mmbiz.qpic.cn/mmbiz_png/abc/640?wx_fmt=png", "https://mmbiz.qpic.cn/mmbiz_png/abc/0?wx_fmt=png"
        self.web.images[small] = fixtures_png(640, 360)
        self.web.images[big] = fixtures_png(1600, 900)
        self.assertEqual(images.hires_candidates(small), [big])
        r = images.download_best(small)
        self.assertEqual((r["width"], r["url"], r["upgraded_from"]), (1600, big, "640×360"))
        self.assertEqual(images.hires_candidates("https://img.ithome.com/a.png"), [])

    def test_chartlike_not_cover(self):
        from PIL import Image, ImageDraw
        from app import config, illustrate as ill
        im = Image.new("RGB", (1200, 700), "white")
        d = ImageDraw.Draw(im)
        for y in range(40, 680, 18):                 # 满屏小字一样的细线
            for x in range(40, 1160, 60):
                d.text((x, y), "量化 12%", fill="black")
        im.save(config.IMAGE_DIR / "chart_text.png")
        q = ill.quality("images/chart_text.png", 1200, 700, "")
        self.assertTrue(q["chartlike"], q)
        cand = {"id": "x", "local_path": "images/chart_text.png", "width": 1200, "height": 700, "url": "", "phash": None,
                "caption": "人形机器人下游应用场景", "context_text": "", "doc_title": "人形机器人"}
        self.assertIsNone(ill.pick_cover([cand], set(), ["人形机器人"]))

    def test_numbers_next_to_chinese(self):
        nums = writing.numbers("去年全年产能已超1000台，截至2026年5月累计下线约11,000台，占比30%")
        self.assertIn("1000台", nums)
        self.assertIn("11,000台", nums)
        self.assertIn("30%", nums)
        self.assertTrue(writing.num_in("11,000台", writing._norm("累计下线约11000台")))

    def test_charts_from_verified_numbers(self):
        llm.available, llm.chat = (lambda: True), FakeWriter()
        task, v = self.write("brief")
        meta = v["meta"]
        bars = [f for f in meta["figures"] if f.get("diagram") == "bar"]
        self.assertEqual(len(bars), 1, meta["image_rejected"])                                   # 真实数字画出来了
        self.assertIn("实测的上下文长度", bars[0]["caption"])
        self.assertTrue(any("编造的趋势" in r["doc"] and "999" in r["why"] for r in meta["image_rejected"]))  # 编造的被拒
        self.assertFalse(any(f.get("decorative") for f in meta["figures"]))

    def test_classify_info_vs_photo(self):
        from app import illustrate as ill
        q_chart = {"chartlike": True, "colors": 300, "white": 0.6}
        q_photo = {"chartlike": False, "colors": 5000, "white": 0.05}
        self.assertTrue(ill.classify({"caption": "图表：2026年各品牌销量对比"}, q_chart)["info"])
        self.assertFalse(ill.classify({"caption": "图/IC"}, q_photo)["info"])
        self.assertFalse(ill.classify({"caption": "小米 YU7 发布会现场"}, q_photo)["info"])

