"""子问题拆分：按问题类型的框架、与通用子问题去重合并、按资料修订、沿用上次、人工确认。"""
import json

from app import aspects as A
from app import db, llm
from app.graphs.research import DEFAULT_ASPECTS
from app.harness import create_task, resolve_hitl, run_task
from tests.test_collect import CollectBase

TOPIC = "开源大模型 长上下文 评测"
PARAMS = {"keywords": ["长上下文"], "en_keywords": ["long context LLM"], "max_docs": 30, "days": 365,
          "auto_translate": False, "verify_top": False, "no_judge": True,
          "sources": ["ithome", "36kr", "juejin", "arxiv", "hn", "github"]}


class UnitTest(CollectBase):
    def test_build_merges_overlap_and_drops_by_type(self):
        llm_as = A.parse_llm([
            {"name": "有哪些质疑和局限", "keywords": ["质疑", "局限", "复现", "争议", "批评"]},   # 与通用“争议”重复
            {"name": "支持该说法的证据及出处", "keywords": ["证据", "出处"], "priority": "high"}])
        out, ch = A.build([dict(a) for a in DEFAULT_ASPECTS], llm_as, "factcheck")
        ids = [a["id"] for a in out]
        self.assertNotIn("eval", ids)          # 事实核查类不需要“评测与数据”“论文类一手来源”
        self.assertNotIn("primary", ids)
        self.assertNotIn("llm0", ids)          # 重复的被合并
        self.assertIn("质疑", next(a for a in out if a["id"] == "debate")["keywords"])
        self.assertEqual(next(a for a in out if a["id"] == "llm1")["priority"], "high")
        self.assertEqual({c["op"] for c in ch}, {"drop_default", "merge"})

    def test_refine_needs_evidence(self):
        aspects = A.parse_llm([{"name": "位置外推技术", "keywords": ["外推"]},
                               {"name": "必需的问题", "keywords": ["必需"], "priority": "high"}])
        cov = {"llm0": {"have": 0}, "llm1": {"have": 0}}
        res = {"assessment": [{"id": "llm0", "status": "unsupported"}, {"id": "llm1", "status": "unsupported"}],
               "add": [{"name": "新基准 Transect 与 OneStreamer", "keywords": ["Transect", "OneStreamer"], "evidence": [0, 3]},
                       {"name": "只有一篇依据的方向", "keywords": ["x"], "evidence": [1]}]}
        ch = A.apply_refine(aspects, cov, res, n_docs=10)
        self.assertTrue(aspects[0]["optional"])                  # 普通优先级、没讨论 → 可选
        self.assertFalse(aspects[1].get("optional"))             # 必需的保留
        self.assertEqual([a["origin"] for a in aspects].count("data"), 1)   # 只有 1 篇依据的不新增
        self.assertEqual({c["op"] for c in ch}, {"optional", "keep_high", "add"})


class FlowTest(CollectBase):
    def setUp(self):
        super().setUp()
        self.prompts = []
        self._llm_old = (llm.available, llm.chat)
        llm.available, llm.chat = (lambda: True), self._chat

    def tearDown(self):
        llm.available, llm.chat = self._llm_old
        super().tearDown()

    def _chat(self, messages, **kw):
        t = messages[-1]["content"]
        self.prompts.append(t)
        if "信息检索规划助手" in t:
            d = {"domains": ["tech"], "question_type": "tech_eval", "core_terms_zh": ["长上下文"],
                 "queries_en": ["long context LLM"], "notes": "x",
                 "aspects": [
                     {"name": "开源模型长上下文能力对比", "why": "回答主题必需", "priority": "high",
                      "keywords": ["对比", "开源", "长上下文", "排名"], "search_zh": "长上下文 对比"},
                     {"name": "长上下文基准与评测方法", "keywords": ["基准", "评测", "benchmark", "eval", "测试"]},
                     {"name": "位置外推技术", "keywords": ["位置外推", "YaRN"], "priority": "normal"}]}
        elif "初步检索已完成" in t:
            d = {"assessment": [{"id": "llm2", "status": "unsupported"}],
                 "add": [{"name": "复现结果与官方数据差异", "why": "多篇资料在讨论复现",
                          "keywords": ["复现", "不及预期"], "evidence": [0, 1, 2]}], "notes": "资料集中在复现争议"}
        else:
            raise RuntimeError("not needed")
        return {"content": json.dumps(d), "data": d, "usage": {}, "_cost": 0}

    def test_typed_plan_merge_and_refine(self):
        tid = create_task("research", TOPIC, params=PARAMS, budget={"max_tool_calls": 200, "max_steps": 200})
        self.assertEqual(run_task(tid), "done")
        st = db.query_one("SELECT state FROM tasks WHERE id=?", (tid,))["state"]
        ap = st["aspect_plan"]
        self.assertEqual(ap["type"], "tech_eval")
        self.assertTrue(ap["refined"])
        ops = [(c["op"], c["name"]) for c in ap["changes"]]
        self.assertIn(("merge", "长上下文基准与评测方法"), ops)          # 与通用“评测与数据”重复 → 合并
        self.assertIn(("optional", "位置外推技术"), ops)
        self.assertIn(("add", "复现结果与官方数据差异"), ops)
        cov = st["report"]["coverage"]
        self.assertTrue(cov["llm2"]["optional"])
        self.assertNotIn("llm2", [g for g, v in cov.items() if v["have"] < v["need"]])
        self.assertEqual(next(v for v in cov.values() if v["name"] == "复现结果与官方数据差异")["origin"], "data")
        # 第二次运行：规划提示里带上上次的子问题，便于沿用
        t2 = create_task("research", TOPIC, params=PARAMS, budget={"max_tool_calls": 200, "max_steps": 200})
        self.assertEqual(run_task(t2), "done")
        plan_prompts = [p for p in self.prompts if "信息检索规划助手" in p]
        self.assertIn("开源模型长上下文能力对比", plan_prompts[-1])
        st2 = db.query_one("SELECT state FROM tasks WHERE id=?", (t2,))["state"]
        self.assertIn("开源模型长上下文能力对比", st2["aspect_plan"]["reused"])

    def test_user_confirms_aspects(self):
        tid = create_task("research", TOPIC, params={**PARAMS, "confirm_aspects": True},
                          budget={"max_tool_calls": 200, "max_steps": 200})
        self.assertEqual(run_task(tid), "waiting_human")
        req = db.query_one("SELECT * FROM hitl_requests WHERE task_id=? AND status='open'", (tid,))
        self.assertEqual(req["kind"], "aspects")
        ids = [a["id"] for a in req["payload"]["aspects"]]
        keep = [i for i in ids if i != "llm2"]
        resolve_hitl(req["id"], {"keep": keep, "rename": {"llm0": "哪些开源模型长上下文最强"},
                                 "add": ["长上下文的显存开销"]})
        self.assertEqual(run_task(tid), "done")
        st = db.query_one("SELECT state FROM tasks WHERE id=?", (tid,))["state"]
        names = [a["name"] for a in st["aspects"]]
        self.assertNotIn("位置外推技术", names)
        self.assertIn("哪些开源模型长上下文最强", names)
        self.assertIn("长上下文的显存开销", names)
        self.assertEqual(next(a for a in st["aspects"] if a["name"] == "长上下文的显存开销")["origin"], "user")
