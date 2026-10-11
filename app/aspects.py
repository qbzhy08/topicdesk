"""子问题拆分：按问题类型套用拆解框架 → 与通用子问题去重合并 → 初步检索后按实际资料修订。

为什么不只让大模型“凭标题猜”：
  1. 盲猜：检索前不知道资料里在讨论什么，容易拆出没人讨论的方向，漏掉真正热的方向；
  2. 重叠：大模型的子问题常和通用子问题（评测与数据、争议……）重复，覆盖率被重复计算；
  3. 一刀切：“X 有没有 Y”这类事实核查、事件、人物团队、技术评测，应该用不同的拆法；
  4. 不稳定：同一主题每次拆法不同，两次运行无法比较覆盖变化。

做法：
  - 先判断问题类型，按该类型的拆解框架给出 4-6 个“具体可回答的问题”，每个带理由、优先级、关键词和检索式；
  - 与通用子问题按名称/关键词重叠度合并，不重复计数；
  - 同一主题优先沿用上次的子问题（记忆），保证可比；
  - 初步检索完成后，让大模型对照已找到资料的标题修订：没有任何讨论的低优先级子问题降为“可选”，
    资料里集中出现但没覆盖到的方向新增为子问题（必须给出依据资料）；
  - 可选：检索前让用户确认 / 改写 / 增加子问题（人工介入）。
"""
from __future__ import annotations

import re

from .collect.dedup import title_sim

QUESTION_TYPES = {
    "tech_eval": ("技术 / 产品评测", ["是什么、为什么重要", "主要对象与方案", "评测方法与基准", "结果与横向对比",
                                    "局限、争议与复现", "最新进展"]),
    "event": ("事件 / 新闻", ["发生了什么（时间线）", "涉及各方及其表态", "原因与背景", "影响与各方反应",
                            "不同说法与争议", "后续进展"]),
    "entity": ("人物 / 团队 / 组织 / 产品", ["基本信息与背景", "代表作品或业务", "本人 / 官方公开表态",
                                      "外界评价与讨论", "争议与传闻", "近况"]),
    "factcheck": ("是非 / 事实核查（“有没有”“是不是”）", ["支持该说法的证据及出处", "否认或反驳的证据",
                                                   "当事人 / 官方说法", "说法从哪里来、怎么传播的", "仍不确定之处"]),
    "compare": ("比较 / 选择", ["候选对象", "对比维度与标准", "各维度对比结果", "适用场景与建议", "争议与局限"]),
    "trend": ("趋势 / 行业", ["现状与关键数据", "主要参与者", "驱动因素", "风险与反对意见", "近期变化与预测"]),
}
# 各类型下不适用的通用子问题
DROP_DEFAULTS = {
    "event": {"eval"}, "entity": {"eval"}, "factcheck": {"eval", "primary"}, "trend": set(),
    "compare": set(), "tech_eval": set(),
}

FRAME_TEXT = "\n".join(f"- {k}（{name}）：{'；'.join(steps)}" for k, (name, steps) in QUESTION_TYPES.items())

PLAN_ASPECTS_RULES = """拆子问题的方法：
1. 先判断问题类型 question_type（只选一个）：
{frames}
2. 按该类型的框架，结合主题拆成 4-6 个子问题。每个子问题写成具体、可以从资料里找到答案的问题（不超过 20 字），
   不要写成泛泛的方向（不要“相关讨论”“其他”）。
3. 系统已有这些通用子问题，不要重复：概况与背景、一手来源、最新进展、争议与局限{eval_note}。
4. 每个子问题给：why（为什么要回答它，一句话）、priority（high=回答主题必需 / normal=补充）、need（需要几篇资料，1-2）、
   keywords（判断资料是否覆盖它的 8-12 个中英文关键词、术语、缩写、代表性名称）、search_zh（2-6 字）、search_en（2-4 词）。
{prev}"""

REFINE_PROMPT = """主题：「{topic}」（问题类型：{qtype}）。初步检索已完成，下面是当前子问题和覆盖情况，以及找到的资料标题。
子问题：
{aspects}
资料（编号. 来源 | 标题 | 开头）：
{docs}
请对照资料修订子问题：
1. 对每个由大模型拆出的子问题（id 以 llm 开头）判断资料里是否有相关讨论：supported 或 unsupported。
2. 资料里集中出现、但现有子问题没有覆盖的重要方向，新增 0-2 个子问题，必须给出支撑它的资料编号（至少 2 篇）。
只输出 JSON：
{{"assessment": [{{"id": "llm0", "status": "supported", "evidence": [1, 4]}}],
  "add": [{{"name": "具体问题", "why": "一句话", "keywords": ["..."], "search_zh": "...", "search_en": "...", "evidence": [2, 5]}}],
  "notes": "一句话"}}"""


def plan_rules(prev_aspects: list[dict] | None, community: bool) -> str:
    prev = ""
    if prev_aspects:
        names = "；".join(a["name"] for a in prev_aspects)
        prev = (f"5. 该主题上次用过这些子问题：{names}。仍然适用的请保持原名称（便于和上次比较），"
                f"不适用的可以替换。")
    return PLAN_ASPECTS_RULES.format(frames=FRAME_TEXT, prev=prev,
                                     eval_note="" if community else "、评测与数据")


def _kw_set(a: dict) -> set:
    return {str(k).lower() for k in a.get("keywords") or [] if len(str(k)) >= 2}


def overlaps(a: dict, b: dict) -> bool:
    """两个子问题是否在问同一件事：名称相似，或判断关键词大量重合。"""
    if title_sim(a["name"], b["name"]) >= 0.5:
        return True
    ka, kb = _kw_set(a), _kw_set(b)
    return bool(ka and kb) and len(ka & kb) >= max(3, min(len(ka), len(kb)) // 2)


def build(defaults: list[dict], llm_aspects: list[dict], qtype: str | None) -> tuple[list[dict], list[dict]]:
    """合并通用子问题与大模型子问题。返回 (子问题列表, 变更记录)。"""
    changes = []
    drop = DROP_DEFAULTS.get(qtype or "", set())
    out = []
    for a in defaults:
        if a["id"] in drop:
            changes.append({"op": "drop_default", "name": a["name"],
                            "why": f"问题类型为“{QUESTION_TYPES[qtype][0]}”，这个通用子问题不适用"})
            continue
        out.append({**a, "origin": a.get("origin", "default")})
    for a in llm_aspects:
        same = next((b for b in out if overlaps(a, b)), None)
        if same:
            same["keywords"] = list(dict.fromkeys(list(same.get("keywords") or []) + list(a.get("keywords") or [])))[:30]
            changes.append({"op": "merge", "name": a["name"], "into": same["name"],
                            "why": "与已有子问题重复，合并关键词，不重复计数"})
            continue
        out.append(a)
    return out, changes


def parse_llm(items: list, start: int = 0, origin: str = "llm") -> list[dict]:
    out = []
    for i, a in enumerate(items or []):
        if not isinstance(a, dict) or not a.get("name"):
            continue
        kws = [str(k) for k in (a.get("keywords") or [])][:16]
        if not kws:
            kws = [w for w in re.split(r"[，,、\s？?]+", a["name"]) if len(w) >= 2][:6]
        try:
            need = max(1, min(3, int(a.get("need") or 1)))
        except (TypeError, ValueError):
            need = 1
        out.append({"id": f"{origin}{start + i}", "name": str(a["name"])[:40], "kind": "keywords", "need": need,
                    "keywords": kws, "search_en": str(a.get("search_en") or "")[:60],
                    "search_zh": str(a.get("search_zh") or "")[:12], "why": str(a.get("why") or "")[:80],
                    "priority": "high" if a.get("priority") == "high" else "normal", "origin": origin})
    return out


def apply_refine(aspects: list[dict], cov: dict, res: dict, n_docs: int, max_total: int = 11) -> list[dict]:
    """按资料修订：unsupported 且没有覆盖的普通优先级子问题降为可选；新增有依据的子问题。返回变更记录。"""
    changes = []
    by_id = {a["id"]: a for a in aspects}
    for x in res.get("assessment") or []:
        a = by_id.get(str(x.get("id")))
        if not a or not a["id"].startswith("llm"):
            continue
        if x.get("status") == "unsupported" and cov.get(a["id"], {}).get("have", 0) == 0:
            if a.get("priority") == "high":
                changes.append({"op": "keep_high", "name": a["name"],
                                "why": "初步检索没找到相关讨论，但它是回答主题必需的，保留并定向补检"})
            else:
                a.update(need=0, optional=True)
                changes.append({"op": "optional", "name": a["name"],
                                "why": "初步检索没有发现这方面的讨论，降为可选，不再追着补"})
    added = 0
    for x in res.get("add") or []:
        ev = [int(i) for i in x.get("evidence") or [] if str(i).isdigit() and int(i) < n_docs]
        if len(ev) < 2 or len(aspects) >= max_total:
            continue
        new = parse_llm([x], start=added, origin="data")[0]
        if any(overlaps(new, b) for b in aspects):
            continue
        new["evidence"] = ev
        aspects.append(new)
        added += 1
        changes.append({"op": "add", "name": new["name"], "why": new["why"] or "资料里集中出现的方向",
                        "evidence": ev})
    return changes


def from_user(names: list[str], start: int = 0) -> list[dict]:
    return parse_llm([{"name": n, "priority": "high"} for n in names if n.strip()], start=start, origin="user")
