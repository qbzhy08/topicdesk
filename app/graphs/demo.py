"""Harness 自检用的演示图（不联网）。

plan → search ⇄ assess → review(HITL) → finalize

- assess 根据已有检索结果决定：覆盖不够就换关键词回到 search，够了才往下走
  ——这正是“已有结果影响下一步行动”的最小示例。
- 工具 demo_search 对关键词 "flaky" 第一次调用会抛异常，用于验证失败重试。
"""
from ..harness import END, Graph, register_graph, tool

_CORPUS = {
    "overview": ["背景介绍 A", "背景介绍 B"],
    "official": ["官方公告 C"],
    "critique": ["独立复现 D", "社区质疑 E"],
}
_flaky_calls = {"n": 0}


@tool("demo_search", "离线演示检索：按关键词返回固定语料")
def demo_search(query: str):
    if query == "flaky":
        _flaky_calls["n"] += 1
        if _flaky_calls["n"] == 1:
            raise TimeoutError("demo source timeout")
    return {"query": query, "hits": _CORPUS.get(query, [])}


def plan(ctx):
    ctx.state["queue"] = list(ctx.params.get("queries", ["overview", "official", "critique"]))
    ctx.state["found"] = []
    ctx.think(f"主题「{ctx.topic}」，先按三个角度检索：{ctx.state['queue']}")
    return "search"


def search(ctx):
    q = ctx.state["queue"].pop(0)
    res = ctx.tool("demo_search", query=q)
    ctx.state["found"].extend(res["hits"])
    ctx.state.setdefault("searched", []).append(q)
    ctx.observe(f"检索 {q} 得到 {len(res['hits'])} 条")
    return "assess"


def assess(ctx):
    need = ctx.params.get("need", 4)
    have = len(ctx.state["found"])
    if have >= need or not ctx.state["queue"]:
        ctx.think(f"已有 {have} 条，目标 {need} 条，" + ("覆盖足够，进入人工审阅" if have >= need
                                                    else "候选关键词用尽，带着缺口进入审阅"))
        return "review"
    ctx.think(f"已有 {have}/{need} 条，缺口仍在，继续检索 {ctx.state['queue'][0]}")
    return "search"


def review(ctx):
    if not ctx.params.get("hitl", True):
        return "finalize"
    ans = ctx.human("review", "choose", "请选择保留哪些资料",
                    {"options": ctx.state["found"]})
    ctx.state["kept"] = ans.get("keep", ctx.state["found"]) if isinstance(ans, dict) else ctx.state["found"]
    ctx.think(f"人工保留 {len(ctx.state['kept'])} 条")
    return "finalize"


def finalize(ctx):
    kept = ctx.state.get("kept", ctx.state.get("found", []))
    ctx.state["result"] = {"items": kept, "budget_note": ctx.state.get("_budget_exhausted")}
    ctx.observe(f"输出 {len(kept)} 条" + (f"（{ctx.state['_budget_exhausted']}，结果可能不完整）"
                                        if ctx.state.get("_budget_exhausted") else ""))
    return END


register_graph(Graph(
    name="demo",
    nodes=dict(plan=plan, search=search, assess=assess, review=review, finalize=finalize),
    start="plan",
    on_budget="finalize",
))
