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
        self.assertEqual(sorted(all_), ["background", "brief", "compare", "data", "exec", "explainer", "faq", "social", "timeline"])
        for sk in all_.values():          # 每个 Skill 都声明了读者、结构、风格、参考资料和配图要求
            self.assertTrue(sk["reader"] and sk["structure"] and sk["style"])
            self.assertGreaterEqual(sk["references"]["min_sources"], 2)
            self.assertGreaterEqual(sk["images"]["body_min"], 1)
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

    def test_reference_rules(self):
        """参考资料要求在选材和成稿检查里生效：同一来源最多几篇、一手资料要求、成稿引用了几个来源。"""
        sk = dict(skills.get("brief"))
        sk["references"] = {**sk["references"], "max_per_source": 1, "min_primary": 1}
        mat = writing.select_material(self.t1, sk)
        srcs = [d["source_id"] for d in mat["docs"]]
        self.assertEqual(len(srcs), len(set(srcs)))                      # 每个来源最多 1 篇
        task, v = self.write("brief", no_llm=True)
        rc = v["meta"]["ref_check"]
        self.assertEqual([i["name"] for i in rc["items"]], ["不同来源", "一手资料", "参考资料日期"])
        self.assertTrue(rc["sources"])
        self.assertEqual(v["meta"]["skill"]["version"], "1.1.0")

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
        self.assertEqual((r["width"], r["url"]), (1600, big))
        self.assertTrue(r["upgraded_from"])
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
        steps = db.query("SELECT node, summary, thought, error FROM steps WHERE task_id=? AND node='illustrate'", (task["id"],))
        self.assertEqual(len(bars), 1, (meta.get("chart_log"), steps))                                   # 真实数字画出来了
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
        # 图片前后的正文提到“售价/价格”，但图本身是照片：不是信息图
        self.assertFalse(ill.classify({"caption": None, "context_text": "YU7 售价 32.99 万元起，价格战再起"}, q_photo)["info"])
        self.assertFalse(ill.classify({"caption": "", "context_text": "碳酸锂价格上涨"}, {"chartlike": False, "colors": 1500, "white": 0.1})["info"])
        # 没有图注、但图片本身像图表：是信息图
        self.assertTrue(ill.classify({"caption": "", "context_text": "销量对比如下"}, q_chart)["info"])



class FlowTest(WriteBase):
    """流程条：检索任务、写作任务、文章三处都能拿到同一条链，互相跳转。"""

    def test_flow_links_research_write_article(self):
        from app.server import create_app
        llm.available, llm.chat = (lambda: False), llm.chat
        t1, _ = self.write("brief")
        t2, _ = self.write("explainer")
        c = create_app().test_client()
        f = c.get(f"/api/flow?task={self.t1}").get_json()
        self.assertEqual(f["research"]["id"], self.t1)
        self.assertEqual([w["task_id"] for w in f["writes"]], [t1["id"], t2["id"]])
        self.assertTrue(all(w["article_id"] and w["status"] == "done" for w in f["writes"]))
        g = c.get(f"/api/flow?task={t2['id']}").get_json()
        self.assertEqual(g["research"]["id"], self.t1)
        self.assertEqual(g["here_write"], t2["id"])
        aid = t1["state"]["result"]["article_id"]
        h = c.get(f"/api/flow?article={aid}").get_json()
        self.assertEqual((h["research"]["id"], h["here_write"], h["here_article"]), (self.t1, t1["id"], aid))
        self.assertEqual(c.get("/api/flow?task=nope").status_code, 404)


class ReviewWriter(FakeWriter):
    """审阅改写：第一次故意写一个出处里没有的数字，核对不过后第二次改对。"""

    def __call__(self, messages, json_mode=False, max_tokens=1500, **kw):
        p = messages[-1]["content"]
        if "用户的修改要求（必须做到）" in p:
            self.calls.append(p)
            a = _num_of(p, "成绩公布")
            if "上一稿核对发现这些问题" in p:
                return {"content": f"官方宣称该模型支持 128K 上下文[{a}]，这一点在发布时就已说明。", "_cost": 0}
            return {"content": f"简单说：官方宣称它能处理 999K 上下文[{a}]。", "_cost": 0}
        return super().__call__(messages, json_mode, max_tokens, **kw)


class ReviewTest(WriteBase):
    """Lv3 人工审阅：每次修改存新版本、重新核对；修改意见可写入长期记忆，下次写作用上。"""

    def setUp(self):
        super().setUp()
        from app.server import create_app
        from app import server
        self.c = create_app().test_client()
        self._start = server.runner.start
        server.runner.start = lambda tid: None          # 测试里同步执行
        self.server = server

    def tearDown(self):
        self.server.runner.start = self._start
        super().tearDown()

    def revise(self, aid, **body):
        r = self.c.post(f"/api/articles/{aid}/revise", json=body)
        self.assertEqual(r.status_code, 201, r.get_json())
        tid = r.get_json()["id"]
        status = run_task(tid)
        t = db.query_one("SELECT * FROM tasks WHERE id=?", (tid,))
        return status, t

    def art(self, aid):
        return self.c.get(f"/api/articles/{aid}").get_json()

    def test_title_text_delete_and_versions(self):
        task, v1 = self.write("brief", no_llm=True)
        aid = task["state"]["result"]["article_id"]
        d = self.art(aid)
        self.assertIn('data-para="0"', d["version"]["html"])               # 正文段落带定位标记
        secs = d["version"]["review"]["sections"]
        sec = next(s for s in secs if s["paras"])
        # 改标题
        st, t = self.revise(aid, op="title", title="长上下文评测：宣称与复现")
        self.assertEqual(st, "done", t["error"])
        d = self.art(aid)
        self.assertEqual(d["article"]["title"], "长上下文评测：宣称与复现")
        self.assertEqual(d["article"]["current_version"], 2)
        self.assertIn("# 长上下文评测：宣称与复现", d["version"]["markdown"])
        self.assertEqual(d["article"]["task_id"], task["id"])            # 文章仍指向写作任务（流程条不变）
        # 直接改文字：用户看到的是重排后的编号，内部换回素材编号再核对
        old = sec["paras"][0]
        n = re.findall(r"\[(\d+)\]", old)[0]
        new_text = f"这是用户自己改写的一句话，保留出处[{n}]。"
        st, t = self.revise(aid, op="text", section=sec["id"], para=0, text=new_text)
        self.assertEqual(st, "done", t["error"])
        d = self.art(aid)
        self.assertIn(new_text.replace(f"[{n}]", "["), d["version"]["markdown"].replace(f"[{n}]", "["))
        self.assertEqual(d["version"]["meta"]["edit"]["op"], "text")
        self.assertEqual(len(d["version"]["meta"]["revisions"]), 2)
        # 版本对比
        df = self.c.get(f"/api/articles/{aid}/diff?a=2&b=3").get_json()
        self.assertGreaterEqual(df["stats"]["mod"] + df["stats"]["rewrite"] + df["stats"]["add"], 1)
        self.assertGreater(df["stats"]["same"], 0)
        self.assertTrue(any(x["t"] in ("mod", "rewrite", "add") and "用户自己改写" in json.dumps(x, ensure_ascii=False)
                            for x in df["items"]))
        # 删段
        n_before = len(self.art(aid)["version"]["review"]["sections"][secs.index(sec)]["paras"])
        st, t = self.revise(aid, op="delete_para", section=sec["id"], para=0)
        self.assertEqual(st, "done", t["error"])
        self.assertEqual(len(self.art(aid)["version"]["review"]["sections"][secs.index(sec)]["paras"]), n_before - 1)
        # 回到第 1 版（复制成新版本，历史不丢）
        r = self.c.post(f"/api/articles/{aid}/restore", json={"version": 1}).get_json()
        self.assertEqual(r["version"], 5)
        d = self.art(aid)
        self.assertEqual(d["version"]["markdown"], v1["markdown"])
        self.assertEqual(d["article"]["title"], v1["markdown"].split("\n")[0][2:])
        self.assertEqual(len(d["versions"]), 5)

    def test_rewrite_rechecks_and_remembers_feedback(self):
        fake = ReviewWriter()
        llm.available, llm.chat = (lambda: True), fake
        task, v1 = self.write("brief")
        aid = task["state"]["result"]["article_id"]
        sec = next(s for s in self.art(aid)["version"]["review"]["sections"] if s["paras"])
        st, t = self.revise(aid, op="rewrite", section=sec["id"], para=0, instruction="更口语一点，开头用“简单说”",
                            remember=True, remember_scope="skill")
        self.assertEqual(st, "done", t["error"])
        rs = t["state"]
        self.assertEqual(sum("用户的修改要求" in c for c in fake.calls), 2)        # 第一次编了 999K，核对不过再改
        self.assertTrue(any(f.get("why") == "审阅改写后按核对结果再改" for f in rs["fixes"]))
        md = self.art(aid)["version"]["markdown"]
        self.assertNotIn("999K", md)
        self.assertIn("128K", md)
        # 修改意见进入长期记忆，下次用同一 Skill 写作时召回
        fb = db.query_one("SELECT * FROM memories WHERE kind='feedback' AND content LIKE '%简单说%'")
        self.assertIsNotNone(fb)
        self.assertEqual(fb["applies_to"], "skill:brief")
        t2, _ = self.write("brief")
        self.assertTrue(any("简单说" in x for x in t2["state"]["prefs"]))
        t3, _ = self.write("explainer")
        self.assertFalse(any("简单说" in x for x in t3["state"]["prefs"]))   # 只对这个 Skill 生效

    def test_rewrite_needs_llm(self):
        task, _ = self.write("brief", no_llm=True)
        aid = task["state"]["result"]["article_id"]
        llm.available = lambda: False
        sec = next(s for s in self.art(aid)["version"]["review"]["sections"] if s["paras"])
        st, t = self.revise(aid, op="rewrite", section=sec["id"], para=0, instruction="更短")
        self.assertEqual(st, "failed")
        self.assertIn("需要大模型", t["error"])
        self.assertEqual(self.art(aid)["article"]["current_version"], 1)      # 失败不产生新版本

    def test_figures_edit(self):
        task, v1 = self.write("brief", no_llm=True)
        aid = task["state"]["result"]["article_id"]
        figs = self.art(aid)["version"]["review"]["figures"]
        if not figs:
            self.skipTest("测试素材没有配图")
        st, t = self.revise(aid, op="figure_caption", figure=0, caption="新的图注（测试）")
        self.assertEqual(st, "done", t["error"])
        self.assertIn("*新的图注（测试）*", self.art(aid)["version"]["markdown"])
        cands = self.c.get(f"/api/articles/{aid}/images").get_json()
        if cands:
            st, t = self.revise(aid, op="figure_swap", figure=0, image_id=cands[0]["image_id"])
            self.assertEqual(st, "done", t["error"])
            self.assertIn(cands[0]["src"], self.art(aid)["version"]["markdown"])
        n = len(self.art(aid)["version"]["review"]["figures"])
        st, t = self.revise(aid, op="figure_remove", figure=0)
        self.assertEqual(st, "done", t["error"])
        self.assertEqual(len(self.art(aid)["version"]["review"]["figures"]), n - 1)

    def test_change_angle_rewrites_same_article(self):
        task, v1 = self.write("brief", no_llm=True)
        aid = task["state"]["result"]["article_id"]
        r = self.c.post(f"/api/articles/{aid}/rewrite", json={"angle": "从开发者选型的角度写", "title": "选长上下文模型前要看什么"})
        self.assertEqual(r.status_code, 201, r.get_json())
        wid = r.get_json()["id"]
        w = db.query_one("SELECT params FROM tasks WHERE id=?", (wid,))
        w["params"]["no_llm"] = True
        db.update("tasks", "id", wid, {"params": w["params"]})
        self.assertEqual(run_task(wid), "done")
        d = self.art(aid)
        self.assertEqual(d["article"]["current_version"], 2)
        self.assertEqual(d["article"]["title"], "选长上下文模型前要看什么")
        self.assertIn("本次选题角度（用户指定）：从开发者选型的角度写", d["version"]["meta"]["prefs"])
        self.assertEqual(self.c.post(f"/api/articles/{aid}/rewrite", json={"angle": "短"}).status_code, 400)


class NaturalWriter(FakeWriter):
    """自然化改写：一节正常改写；一节把数字改错（应退回原稿）；其余丢了出处（应保留原稿）。"""

    def __call__(self, messages, json_mode=False, max_tokens=1500, **kw):
        p = messages[-1]["content"]
        if "改得更像一位懂行的编辑写的" in p:
            self.calls.append(p)
            text = p.split("原文：\n", 1)[1]
            if "128K" in text and "64K" not in text:
                return {"content": text.replace("128K", "256K"), "_cost": 0}          # 把数字改错
            if "差距" in text:
                return {"content": text.replace("存在差距", "对不上"), "_cost": 0}      # 正常改写，保留出处
            return {"content": "这一节说的是长上下文。", "_cost": 0}                    # 丢了出处
        return super().__call__(messages, json_mode, max_tokens, **kw)


class StagesTest(WriteBase):
    """消融开关与自然化改写：改坏的节退回，丢出处的节不采用；关掉步骤时对应节点不执行。"""

    def test_naturalize_reverts_bad_sections(self):
        llm.available, llm.chat = (lambda: True), NaturalWriter()
        task, v = self.write("brief")
        log = task["state"]["natural_log"]
        res = " ".join(x["result"] for x in log)
        self.assertIn("已改写", res)
        self.assertTrue("退回原稿" in res or "保留原稿" in res)
        self.assertNotIn("256K", v["markdown"])
        self.assertIn("对不上", v["markdown"])
        self.assertEqual(v["meta"]["check"]["summary"]["number"], 0)

    def test_stages_off_gives_raw_draft(self):
        fake = NaturalWriter()
        llm.available, llm.chat = (lambda: True), fake
        task, v = self.write("brief", stages={"gap": False, "fix": False, "naturalize": False, "illustrate": False})
        st = task["state"]
        self.assertEqual(st["gap_round"], 0)
        self.assertEqual(st["fix_round"], 0)
        self.assertFalse(st.get("natural_log"))
        self.assertFalse(any("改得更像" in c for c in fake.calls))
        self.assertGreater(v["meta"]["check"]["summary"]["problems"], 0)          # 初稿的问题原样保留，供对照
        self.assertEqual(v["meta"]["stages"]["fix"], False)
        nodes = [r["node"] for r in db.query("SELECT node FROM steps WHERE task_id=?", (task["id"],))]
        self.assertNotIn("fix", nodes)
        self.assertNotIn("illustrate", nodes)


class SkillWorkshopTest(WriteBase):
    """页面里 DIY Skill：新建、校验、编辑自动升版本并留历史、按指纹找回、删除；不改业务代码就能用于写作。"""

    def setUp(self):
        super().setUp()
        import shutil
        from app.server import create_app
        self._skdir = tempfile.TemporaryDirectory()
        self._old_dir = skills.SKILL_DIR
        shutil.copytree(self._old_dir, Path(self._skdir.name) / "skills", ignore=shutil.ignore_patterns("history", ".trash"))
        skills.SKILL_DIR = Path(self._skdir.name) / "skills"
        self.c = create_app().test_client()

    def tearDown(self):
        skills.SKILL_DIR = self._old_dir
        self._skdir.cleanup()
        super().tearDown()

    def form(self, **kw):
        f = {"id": "weekly", "name": "团队周报", "reader": "团队成员", "length": "600-900 字", "author": "我",
             "structure": [{"id": "news", "heading": "本周进展", "goal": "列出关键事实，带出处", "words": 400},
                           {"id": "take", "heading": "我们的判断", "goal": "以【分析】开头给出判断", "words": 200}],
             "style": "短句\n不用套话", "references": {"min_sources": 2, "min_primary": 0, "max_per_source": 2},
             "images": {"cover": 1, "body_min": 1}, "body": "写给团队看的周报。"}
        f.update(kw)
        return f

    def test_create_edit_version_delete(self):
        r = self.c.post("/api/skills/validate", json={"form": self.form(id="Bad Id")}).get_json()
        self.assertFalse(r["ok"])
        r = self.c.post("/api/skills", json={"form": self.form()})
        self.assertEqual(r.status_code, 201, r.get_json())
        self.assertEqual(self.c.post("/api/skills", json={"form": self.form()}).status_code, 400)   # id 重复
        lst = self.c.get("/api/skills").get_json()["skills"]
        mine = next(x for x in lst if x["id"] == "weekly")
        self.assertTrue(mine["custom"])
        # 新 Skill 直接用于写作（没有改任何业务代码）
        task, v = self.write("weekly", no_llm=True)
        # Skill 定义的结构原样使用；只有单一来源的说法时，系统在文末补“还不确定的”一节
        self.assertEqual([s["heading"] for s in task["state"]["sections"] if not s.get("auto")], ["本周进展", "我们的判断"])
        fp1 = v["meta"]["skill"]["fingerprint"]
        # 编辑：内容变了没改版本号 → 自动升版本，旧版存历史
        d = self.c.get("/api/skills/weekly").get_json()
        r = self.c.put("/api/skills/weekly", json={"form": {**self.form(summary="新简介"), "base": d["text"]}}).get_json()
        self.assertTrue(r["bumped"])
        self.assertEqual(r["skill"]["version"], "1.0.1")
        self.assertEqual(len(self.c.get("/api/skills/weekly").get_json()["history"]), 1)
        # 文章记录的指纹能找回写作时的 Skill 原文
        old = self.c.get(f"/api/skills/weekly/fingerprint/{fp1}").get_json()["text"]
        self.assertIn("version: 1.0.0", old)
        self.assertNotIn("新简介", old)
        # 源码编辑同样可以
        txt = self.c.get("/api/skills/weekly").get_json()["text"].replace("version: 1.0.1", "version: 2.0.0")
        r = self.c.put("/api/skills/weekly", json={"text": txt}).get_json()
        self.assertEqual((r["skill"]["version"], r["bumped"]), ("2.0.0", False))
        # 编辑内置 Skill 时表单里没有的字段保留
        b = self.c.get("/api/skills/compare").get_json()
        k = b["skill"]
        r = self.c.put("/api/skills/compare", json={"form": {**k, "summary": "改了简介", "base": b["text"]}}).get_json()
        self.assertTrue(skills.get("compare")["references"].get("both_sides"))
        # 删除：移到回收站，不再出现
        self.assertEqual(self.c.delete("/api/skills/weekly").status_code, 200)
        self.assertNotIn("weekly", skills.load_all())


class JudgeWriter(NaturalWriter):
    """实验用：在写作之外还要回答独立事实核验和盲评。"""

    def __call__(self, messages, json_mode=False, max_tokens=1500, **kw):
        p = messages[-1]["content"]
        if "你是事实核查员" in p:
            n = len(re.findall(r"^\d+\. 文章中的说法", p, re.M))
            return {"content": "", "_cost": 0, "data": {"results": [
                {"i": i + 1, "verdict": "supported" if i % 4 else "partial", "why": "测试"} for i in range(n)]}}
        if "同一主题、同一批资料写成的两篇文章" in p:
            a = p.split("【文章 A】", 1)[1].split("【文章 B】", 1)[0]
            better = "A" if "对不上" in a else "B"          # 优化稿里有“对不上”（自然化改写的痕迹）
            dim = {"winner": better, "A": 4 if better == "A" else 3, "B": 4 if better == "B" else 3}
            return {"content": "", "_cost": 0, "data": {d: dict(dim) for d in ("accuracy", "information", "natural", "easy", "overall")}
                    | {"reason": "测试"}}
        return super().__call__(messages, json_mode, max_tokens, **kw)


class ExperimentTest(WriteBase):
    """评测实验：同素材初稿/优化稿/消融变体、确定性指标、独立核验、交换顺序的盲评、读者盲评、外部检测器分数、Skill 对照。"""

    def setUp(self):
        super().setUp()
        from app.server import create_app
        from app import server
        self.c = create_app().test_client()
        self._start = server.runner.start
        server.runner.start = lambda tid: None
        self.server = server
        llm.available, llm.chat = (lambda: True), JudgeWriter()

    def tearDown(self):
        self.server.runner.start = self._start
        super().tearDown()

    def run_exp(self, **body):
        r = self.c.post("/api/experiments", json=body)
        self.assertEqual(r.status_code, 201, r.get_json())
        eid = r.get_json()["id"]
        self.assertEqual(run_task(eid), "done", db.query_one("SELECT error FROM tasks WHERE id=?", (eid,))["error"])
        return eid, self.c.get(f"/api/experiments/{eid}").get_json()

    def test_ablation_report(self):
        eid, rep = self.run_exp(research_tasks=[self.t1], skill="brief", detector="manual")
        rows = {r["variant"]: r for r in rep["rows"]}
        self.assertEqual(set(rows), {"draft", "checked", "optimized", "natural_only"})
        self.assertTrue(all(r["status"] == "done" for r in rows.values()))
        # 初稿保留了问题；核对改写后问题减少
        self.assertGreater(rows["draft"]["problems"], rows["checked"]["problems"])
        self.assertIsNotNone(rows["optimized"]["judge_fidelity"])
        self.assertIn("对不上", self.c.get(f"/api/experiments/{eid}/text/{rows['optimized']['key']}").get_json()["text"])
        # 大模型盲评：交换顺序两次一致才算胜
        lp = rep["llm_pairs"][self.t1]
        self.assertEqual(lp["overall"]["winner"], "Y")
        self.assertTrue(lp["overall"]["consistent"])
        # 结论：每一步的作用 + 何时失效
        steps = [e["step"] for e in rep["conclusions"]["effects"]]
        self.assertIn("逐句核对 + 按核对结果改写", steps)
        self.assertIn("自然化改写（降 AI 腔）+ 复核", steps)
        self.assertTrue(any(f["where"] == "自然化改写" for f in rep["conclusions"]["failures"]))
        # 外部检测器分数录入（同一检测器），进入报告
        for v, sc in (("draft", 92), ("optimized", 61)):
            self.assertEqual(self.c.post(f"/api/experiments/{eid}/scores",
                                         json={"run_key": rows[v]["key"], "detector": "外部检测器X", "score": sc}).status_code, 200)
        rep = self.c.get(f"/api/experiments/{eid}").get_json()
        self.assertEqual(rep["detectors"], ["外部检测器X"])
        self.assertEqual(rep["avg"]["optimized"]["aigc"]["外部检测器X"], 0.61)
        # 读者盲评：看不到哪篇是哪篇；评分后按变体汇总
        bl = self.c.get(f"/api/experiments/{eid}/blind?rater=小王").get_json()["pairs"]
        self.assertEqual(len(bl), 1)
        self.assertFalse(re.search(r"\[\d+\]", bl[0]["甲"]))
        exp = db.query_one("SELECT state FROM tasks WHERE id=?", (eid,))["state"]
        opt_side = "甲" if exp["blind"][0]["甲"].endswith(":optimized") else "乙"
        ans = {d: opt_side for d in ("accuracy", "information", "natural", "easy", "overall")}
        ans["ai_guess"] = "乙" if opt_side == "甲" else "甲"
        r = self.c.post(f"/api/experiments/{eid}/blind", json={"pair_id": bl[0]["pair_id"], "rater": "小王", "answers": ans})
        self.assertEqual(r.status_code, 200, r.get_json())
        h = self.c.get(f"/api/experiments/{eid}").get_json()["human"]
        self.assertEqual(h["tally"]["overall"]["optimized"], 1)
        self.assertEqual(h["ai_guess"]["right"], 1)
        # 增益：省下的阅读时间、跨来源印证
        g = rep["gain"]
        self.assertGreater(g["minutes_sources"], g["minutes_article"])
        # 实验写的变体不混进普通列表
        names = [t["graph"] for t in self.c.get("/api/tasks").get_json()]
        self.assertEqual(names.count("write"), 0)

    def test_skills_compare(self):
        eid, rep = self.run_exp(kind="skills", research_tasks=[self.t1], skills=["brief", "timeline", "faq"])
        g = rep["skills"]["groups"][0]
        self.assertEqual(g["skills"], ["brief", "timeline", "faq"])
        self.assertEqual(len(g["jaccard_cited"]), 3)
        self.assertEqual(g["jaccard_cited"][0][0], 1.0)
        heads = {r["variant"]: r["structure"] for r in g["rows"]}
        self.assertNotEqual(heads["brief"], heads["timeline"])
        self.assertTrue(all(r["logs"]["cover"] for r in g["rows"]))           # Skill 对照是全流程（含配图）

    def test_metrics_unit(self):
        from app import evaluate as ev
        t = "首先，模型很好。其次，值得注意的是它很快。综上所述，它很重要。"
        n = ev.naturalness(t)
        self.assertGreater(n["tropes_per_1k"], 50)
        self.assertEqual(n["connective_start"], 1.0)                             # 三句都以连接词开头
        md = "# 标题\n\n![a](/media/x.png)\n*图注*\n\n> 阅读说明：……\n\n## 一\n\n正文[1]。\n\n## 参考资料\n\n1. x"
        self.assertEqual(ev.plain_text(md), "标题\n\n一\n\n正文[1]。")
        m = ev.pair_judge_merge({"overall": {"winner": "A", "A": 4, "B": 2}}, {"overall": {"winner": "B", "A": 3, "B": 5}})
        self.assertEqual(m["overall"]["winner"], "X")
        self.assertEqual(m["overall"]["x"], 4.5)
        m = ev.pair_judge_merge({"overall": {"winner": "A"}}, {"overall": {"winner": "A"}})
        self.assertEqual(m["overall"]["winner"], "tie")                         # 两次不一致（位置偏差）算相当


class AigcTest(WriteBase):
    def test_detect_chunks_and_labels(self):
        from app import aigc
        calls = []

        def fake_pipe(parts):
            calls.append(parts)
            return [[{"label": "ChatGPT", "score": 0.8}, {"label": "Human", "score": 0.2}] if "AI" in p else
                    [{"label": "ChatGPT", "score": 0.1}, {"label": "Human", "score": 0.9}] for p in parts]
        old = aigc._pipe
        aigc._pipe = lambda: fake_pipe
        try:
            text = ("AI" + "字" * 498 + "\n") + ("人" * 500)
            r = aigc.detect(text)
        finally:
            aigc._pipe = old
        self.assertEqual(len(calls[0]), 2)                     # 按 ≤500 字切块
        self.assertAlmostEqual(r["ai_prob"], 0.45, places=2)   # 按字数加权
        self.assertTrue(r["detector"].startswith("本地 HC3 检测器"))
        self.assertIn("local_ready", aigc.status())


class ExportTest(WriteBase):
    def test_export_md_zip_and_mobile_html(self):
        import io
        import zipfile
        from app.server import create_app
        task, v = self.write("brief", no_llm=True)
        aid = task["state"]["result"]["article_id"]
        c = create_app().test_client()
        r = c.get(f"/api/articles/{aid}/export?format=md")
        self.assertEqual(r.status_code, 200)
        z = zipfile.ZipFile(io.BytesIO(r.data))
        md = z.read("article.md").decode()
        imgs = [n for n in z.namelist() if n.startswith("images/")]
        self.assertTrue(imgs)
        self.assertIn(f"]({imgs[0]})", md)                       # 图片改成包内相对路径
        self.assertNotIn("/media/", md)
        self.assertIn("## 参考资料", md)
        r = c.get(f"/api/articles/{aid}/export?format=html")
        h = r.data.decode()
        self.assertIn('name="viewport"', h)
        self.assertIn("data:image/", h)                          # 图片内嵌，单文件可转发
        self.assertNotIn('src="/media/', h)
        self.assertIn('class="cite"', h)
        self.assertIn("attachment", r.headers["Content-Disposition"])


class ReviewUploadTest(ReviewTest):
    """改整节也能自己改；上传自己的图片：换图、在某段后加图、换封面。"""

    def test_whole_section_text_and_uploads(self):
        import io
        from tests.fixtures import png
        task, v1 = self.write("brief", no_llm=True)
        aid = task["state"]["result"]["article_id"]
        sec = next(s for s in self.art(aid)["version"]["review"]["sections"] if len(s["paras"]) >= 1)
        new = "整节由用户重写的第一段。\n\n【分析】用户自己写的判断，不含数字。"
        st, t = self.revise(aid, op="text", section=sec["id"], para="all", text=new)
        self.assertEqual(st, "done", t["error"])
        paras = next(s for s in self.art(aid)["version"]["review"]["sections"] if s["id"] == sec["id"])["paras"]
        self.assertEqual(paras, ["整节由用户重写的第一段。", "【分析】用户自己写的判断，不含数字。"])
        # 上传：不是图片 → 拒绝；是图片 → 保存
        r = self.c.post("/api/uploads/image", data={"file": (io.BytesIO(b"not an image"), "a.txt")},
                        content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        r = self.c.post("/api/uploads/image", data={"file": (io.BytesIO(png(1200, 700, (10, 120, 200))), "my.png")},
                        content_type="multipart/form-data")
        self.assertEqual(r.status_code, 201, r.get_json())
        up = r.get_json()
        self.assertTrue(up["local_path"].startswith("images/uploads/"))
        # 在这一节第 1 段后加图（带图注）
        n = len(self.art(aid)["version"]["review"]["figures"])
        st, t = self.revise(aid, op="figure_add", section=sec["id"], para=0, upload=up["local_path"], caption="我自己做的对比图")
        self.assertEqual(st, "done", t["error"])
        d = self.art(aid)
        self.assertEqual(len(d["version"]["review"]["figures"]), n + 1)
        self.assertIn("*我自己做的对比图（图源：用户上传）*", d["version"]["markdown"])
        # 用上传的图换掉第一张图、换封面
        st, t = self.revise(aid, op="figure_swap", figure=0, upload=up["local_path"])
        self.assertEqual(st, "done", t["error"])
        st, t = self.revise(aid, op="cover_swap", upload=up["local_path"], caption="封面")
        self.assertEqual(st, "done", t["error"])
        self.assertIn(up["src"], self.art(aid)["version"]["markdown"].split("## ")[0])
        # 伪造路径不接受
        st, t = self.revise(aid, op="cover_swap", upload="../../etc/passwd")
        self.assertEqual(st, "failed")


class AigcDownloadTest(WriteBase):
    def test_download_falls_back_to_mirror(self):
        """官方地址不通时换国内镜像；地址作为参数传入（不依赖环境变量）；成功的地址记住。"""
        import sys
        import types
        from app import aigc
        calls = []

        def fake_snapshot(repo_id, local_dir, endpoint=None, allow_patterns=None):
            calls.append(endpoint)
            if endpoint is None:
                raise OSError("connection timed out")
            p = Path(local_dir)
            p.mkdir(parents=True, exist_ok=True)
            (p / "config.json").write_text("{}")
            (p / "pytorch_model.bin").write_bytes(b"x")
        sys.modules["huggingface_hub"] = types.SimpleNamespace(snapshot_download=fake_snapshot)
        try:
            self.assertFalse(aigc.status()["model_ready"])
            d = aigc.download(log=lambda *_: None)
        finally:
            del sys.modules["huggingface_hub"]
        self.assertEqual(calls, [None, "https://hf-mirror.com"])
        self.assertTrue(aigc.status()["model_ready"])
        self.assertEqual(aigc.settings()["hf_endpoint"], "https://hf-mirror.com")
        self.assertIn("models", d)


class ExperimentFailTest(ExperimentTest):
    def test_failed_runs_marked(self):
        from app.graphs import experiment as E
        orig = E._run_one

        def flaky(r, params, user_id):
            if r["variant"] == "natural_only":
                return {"write_task": None, "status": "failed", "error": "大模型超时（测试）", "seconds": 1,
                        "article_id": None, "version": None}
            return orig(r, params, user_id)
        E._run_one = flaky
        try:
            eid, rep = self.run_exp(research_tasks=[self.t1], skill="brief", detector="manual")
        finally:
            E._run_one = orig
        bad = [r for r in rep["rows"] if r["status"] == "failed"]
        self.assertEqual([r["variant"] for r in bad], ["natural_only"])
        self.assertIn("超时", bad[0]["error"])                                     # 报错翻译成看得懂的原因
        self.assertEqual(rep["avg"]["natural_only"]["n"], 0)                       # 失败的不计入统计
        lst = next(x for x in self.c.get("/api/experiments").get_json() if x["id"] == eid)
        self.assertEqual(lst["failed"][0]["variant"], "natural_only")
        self.assertEqual((lst["done"], lst["runs"]), (3, 4))

    def test_fatal_error_stops_and_retry(self):
        from app.graphs import experiment as E
        orig = E._run_one
        E._run_one = lambda r, p, u: {"write_task": None, "status": "failed", "seconds": 1, "article_id": None, "version": None,
                                      "error": '节点 draft 失败：HTTP 402: {"error":{"message":"Insufficient Balance"}}'}
        try:
            eid, rep = self.run_exp(research_tasks=[self.t1], skill="brief", detector="manual", parallel=1)
        finally:
            E._run_one = orig
        self.assertIn("余额不足", rep["fatal"])
        self.assertTrue(all(r["status"] == "failed" for r in rep["rows"]))
        self.assertIn("没有开始写", rep["rows"][-1]["error"])                    # 第一篇失败后就停了
        # 充值后重跑：只重写失败的
        r = self.c.post(f"/api/experiments/{eid}/retry", json={})
        self.assertEqual(r.get_json()["retry"], 4)
        self.assertEqual(run_task(eid), "done")
        rep = self.c.get(f"/api/experiments/{eid}").get_json()
        self.assertTrue(all(x["status"] == "done" for x in rep["rows"]))
        self.assertIsNone(rep["fatal"])
        self.assertEqual(rep["blind_pairs"], 1)

    def test_remeasure(self):
        eid, rep = self.run_exp(research_tasks=[self.t1], skill="brief", detector="manual")
        r = self.c.post(f"/api/experiments/{eid}/remeasure", json={}).get_json()
        self.assertEqual(r["remeasured"], 4)
        rep = self.c.get(f"/api/experiments/{eid}").get_json()
        self.assertIsNotNone(rep["rows"][0]["dash_per_1k"])

    def test_add_blind_pairs(self):
        eid, rep = self.run_exp(research_tasks=[self.t1], skill="brief", detector="manual")
        r = self.c.post(f"/api/experiments/{eid}/blind/pairs", json={"research_task": "all", "a": "checked", "b": "optimized"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.c.post(f"/api/experiments/{eid}/blind/pairs",
                                     json={"research_task": "all", "a": "optimized", "b": "checked"}).status_code, 400)   # 重复
        pairs = self.c.get(f"/api/experiments/{eid}/blind?rater=甲").get_json()["pairs"]
        self.assertEqual(len(pairs), 2)
        new = pairs[1]
        ans = {d: "甲" for d in ("accuracy", "information", "natural", "easy", "overall", "ai_guess")}
        self.c.post(f"/api/experiments/{eid}/blind", json={"pair_id": new["pair_id"], "rater": "甲", "answers": ans})
        rep = self.c.get(f"/api/experiments/{eid}").get_json()
        comps = {(c["a"], c["b"]): c for c in rep["human"]["comparisons"]}
        self.assertIn(("draft", "optimized"), comps)
        c = comps[("checked", "optimized")]
        self.assertEqual(c["ratings"], 1)
        self.assertEqual(c["tally"]["overall"]["checked"] + c["tally"]["overall"]["optimized"], 1)
        self.assertEqual(self.c.delete(f"/api/experiments/{eid}/blind/pairs/{new['pair_id']}").status_code, 200)
        self.assertEqual(self.c.get(f"/api/experiments/{eid}").get_json()["blind_pairs"], 1)


class TrustTest(WriteBase):
    def test_trust_report(self):
        from app.server import create_app
        llm.available, llm.chat = (lambda: True), NaturalWriter()
        self.write("brief")
        c = create_app().test_client()
        d = c.get(f"/api/trust/{self.t1}").get_json()
        self.assertGreater(d["reliability"]["docs"], 0)
        self.assertGreaterEqual(d["reliability"]["duplicates"], 1)            # 转载被归并
        self.assertGreater(d["selection"]["list5"], 0)
        self.assertTrue(d["understanding"]["articles"])
        self.assertIn("caveats", d["understanding"]["articles"][0])
        self.assertGreater(d["trace"]["steps"], 0)
        self.assertEqual(c.get("/api/trust/nope").status_code, 400)

    def test_hedges_and_uncertain_section(self):
        """只有单一来源的说法：成稿时自动写明限制、加“还不确定的”一节；旧文章可以一键补上；可信度页判为合格。"""
        from app.server import create_app
        from app import writing
        llm.available, llm.chat = (lambda: True), NaturalWriter()
        task, v = self.write("brief")
        h = v["meta"]["hedges"]
        self.assertEqual(h["missing"], 0)
        if h["needed"]:
            self.assertTrue(h["uncertain_section"] or h["section_added"])
            self.assertIn("还不确定的", v["markdown"])
        # 幂等：再跑一次不重复添加
        secs = [dict(x) for x in v["meta"]["sections"]]
        flags = writing.doc_flags(v["meta"]["research_task"], v["meta"]["docs"])
        r = writing.ensure_hedges(secs, flags)
        self.assertEqual((r["added"], r["section"]), (0, False))
        # 模拟加闸之前写的旧文章：去掉补上的说明和那一节 → 可信度页标出缺口 → 一键补上
        c = create_app().test_client()
        aid = v["article_id"]
        row = db.query_one("SELECT * FROM article_versions WHERE article_id=? ORDER BY version DESC", (aid,))
        meta = row["meta"]
        meta["sections"] = [dict(x, md=writing.HEDGE_NOTE.sub("", x["md"])) for x in meta["sections"] if not x.get("auto")]
        flags = writing.doc_flags(meta["research_task"], meta["docs"])
        # 再把正文里自然写出的限制词也去掉，保证有缺口
        for x in meta["sections"]:
            x["md"] = writing.HEDGE.sub("", x["md"])
        db.execute("UPDATE article_versions SET meta=? WHERE article_id=? AND version=?",
                   (__import__("json").dumps(meta, ensure_ascii=False), aid, row["version"]))
        aud = writing.hedge_audit(meta["sections"], flags)
        if not aud["needed"]:
            self.skipTest("样例素材里没有单一来源的说法")
        d = c.get(f"/api/trust/{self.t1}").get_json()
        art = next(x for x in d["understanding"]["articles"] if x["id"] == aid)
        self.assertGreater(art["hedge_missing"], 0)
        tid = c.post(f"/api/articles/{aid}/revise", json={"op": "hedge", "base_version": row["version"]}).get_json()["id"]
        run_task(tid)
        d = c.get(f"/api/trust/{self.t1}").get_json()
        art = next(x for x in d["understanding"]["articles"] if x["id"] == aid)
        self.assertEqual(art["hedge_missing"], 0)
        self.assertTrue(art["has_uncertain_section"])

    def test_corroborate_single_source_claims(self):
        """能找资料证明就去找：别的网站有能对上的报道 → 补进素材、句子补上出处、不再写“尚待证实”。"""
        from app.collect import sources as S
        from app import writing
        llm.available, llm.chat = (lambda: True), NaturalWriter()
        orig_search, orig_fetch = S.search, S.fetch_page
        calls = []

        def fake_search(source_id, query, limit=15, **kw):
            calls.append((source_id, query))
            if len(calls) <= 200 and "复现" in query or "社区" in query:
                return [{"url": "https://www.example-news.cn/a/1", "title": "社区开发者复现结果与官方宣称存在差距",
                         "summary": "社区开发者的复现结果与官方宣称存在差距，多位开发者表示长上下文表现不及预期。",
                         "text": "社区开发者的复现结果与官方宣称存在差距，多位开发者表示长上下文表现不及预期。",
                         "content_kind": "full", "source_id": source_id, "published_at": "2026-10-12T00:00:00+00:00"}]
            return orig_search(source_id, query, limit=limit, **kw)
        S.search = fake_search
        try:
            task, v = self.write("brief")
        finally:
            S.search, S.fetch_page = orig_search, orig_fetch
        logs = v["meta"]["corroborations"]
        self.assertTrue(logs)
        hit = [x for x in logs if x.get("doc")]
        self.assertTrue(hit, logs)
        n = next(d["n"] for d in task["state"]["docs"] if d["id"] == hit[0]["doc"]["doc_id"])
        body = "\n".join(x["md"] for x in v["meta"]["sections"] if not x.get("auto"))
        self.assertIn(f"[{n}]", body)                         # 句子补上了新出处
        self.assertIn("example-news.cn", v["markdown"])       # 参考资料里有这篇
        flags = writing.doc_flags(v["meta"]["research_task"], task["state"]["docs"])
        aud = writing.hedge_audit([x for x in v["meta"]["sections"] if not x.get("auto")], flags)
        self.assertEqual(aud["missing"], 0)

    def test_naturalize_keeps_caveats(self):
        from app import trust
        self.assertEqual(trust.caveats("目前只有单一来源报道。官方尚未回应。销量增长了。"), 2)


class SharedDraftTest(ExperimentTest):
    def test_variants_share_draft(self):
        fake = JudgeWriter()
        llm.chat = fake
        eid, rep = self.run_exp(research_tasks=[self.t1], skill="brief", detector="manual")
        self.assertEqual(sum("列提纲" in c for c in fake.calls), 1)                 # 只列一次提纲
        st = db.query_one("SELECT state FROM tasks WHERE id=?", (eid,))["state"]
        draft = next(r for r in st["runs"] if r["variant"] == "draft")
        for r in st["runs"]:
            if r["variant"] != "draft":
                ws = db.query_one("SELECT state FROM tasks WHERE id=?", (r["write_task"],))["state"]
                self.assertEqual(ws.get("seeded_from"), draft["write_task"])
