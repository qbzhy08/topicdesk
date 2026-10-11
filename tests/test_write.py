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


class WriteTest(CollectBase):
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
        self.assertLessEqual(len(b["docs"]), 6)
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
