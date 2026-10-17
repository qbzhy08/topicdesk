"""对标真人稿：同一主题，系统写的文章和真人记者/编辑写的文章逐项比“像不像人写的”。

确定性指标（不依赖大模型，可复算）都是“写法”层面的，和事实对错无关：
  重复：同一组数字在几句话里反复出现；
  腔调：AI 腔句式（不是…而是、这意味着、把…放在一起看…）、套话、连接词起句、破折号；
  节奏：句长变化（真人稿长短句交错，变异系数高）；
  人声：直接引语（谁说的“……”）；
  交代：每千字的限制括注（系统自动补的“仅见于…”）、【分析】段、给读者/作者的提醒；
  结构：小标题数量、模板化小标题；开头第一句有没有具体事实（数字、机构、人）。
每项都给出“真人稿的水平”和“系统稿的水平”，差距大的列为待改进项，并对应到代码里管这件事的位置。

可选的大模型评审：两篇随机标成 A/B，问哪篇更像记者写的、系统稿里最像 AI 的句子、真人稿用了哪些系统稿没用的手法。
"""
from __future__ import annotations

import random
import re
import statistics

from . import evaluate as ev
from . import writing

TEMPLATE_HEADINGS = re.compile(r"^(先说结论|核心结论|三个概念|来龙去脉|各方怎么看|我们的判断|发生了什么|为什么值得关注|"
                               r"为什么重要|背景|结论|总结|小结|一句话总结|关键数字)")
# 系统自动加的“尚待确认”一节是有意保留的可信度设计（列出只有单一来源的说法），不算模板化小标题
AUTO_HEADINGS = re.compile(r"^(尚待确认|还待核实的|还不确定的)$")
QUOTE = re.compile(r"[“「]([^”」]{6,120})[”」]")
SPEAK = re.compile(r"(说|表示|称|指出|介绍|认为|坦言|透露|强调|告诉|回应|提到)[：:，,]?\s*$")
ATTRIB_AFTER = re.compile(r"^[”」]?\s*[，,]?\s*[一-鿿A-Za-z·]{1,16}(说|表示|称|指出|介绍|认为|坦言|透露|强调|补充)")
REMINDER = re.compile(r"引用时|写作时|读的时候|阅读时|需要注意的是|值得注意的是|请当作|读者应|宜以")
FACTY = re.compile(r"\d|[一-鿿]{2,}(部|局|院|委|会|署|厅|公司|集团|大学|研究所|中心|组织)|[“「]")


def _plain_from_md(md: str) -> str:
    return ev.plain_text(md)


def _paras(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n|\n", text or "") if p.strip()]


def _sents(text: str) -> list[str]:
    return [s for s in ev._sentences(text) if re.search(r"[一-鿿]", s)]


def quotes(text: str) -> list[str]:
    """直接引语：引号里 6 字以上，并且前后交代了是谁说的。"""
    out = []
    for m in QUOTE.finditer(text or ""):
        before = text[max(0, m.start() - 24):m.start()]
        after = text[m.end():m.end() + 20]
        if SPEAK.search(before) or ATTRIB_AFTER.match(after):
            out.append(m.group(1))
    return out


def number_repeats(sents: list[str]) -> dict:
    """同一个“硬数字”出现在几句话里。真人稿通常每个数字只完整说一次。"""
    seen = {}
    for s in sents:
        for n in set(writing.numbers(s)) - writing.NOISE_NUMS:
            if len(re.sub(r"\D", "", n)) < 2 and not re.search(r"[.%％亿万]", n):
                continue
            if re.search(r"[年月日]$", n):          # 日期、年份反复出现是正常的（“比2025年”“10月8日”）
                continue
            seen[n] = seen.get(n, 0) + 1
    rep = {n: c for n, c in seen.items() if c >= 2}
    total = len(seen)
    return {"distinct": total, "repeated": len(rep),
            "repeat_rate": round(len(rep) / total, 3) if total else 0.0,
            "extra_mentions": sum(c - 1 for c in rep.values()),
            "top": dict(sorted(rep.items(), key=lambda x: -x[1])[:6])}


def metrics(text: str, md: str | None = None) -> dict:
    """text：纯文本正文（真人稿是抓取的正文；系统稿用 evaluate.plain_text 处理过的 Markdown）。
    md：系统稿原始 Markdown（用来数小标题），真人稿不传。"""
    text = text or ""
    n = max(1, ev._hanzi_len(text))
    k = 1000 / n
    sents = _sents(text)
    lens = [ev._hanzi_len(s) for s in sents]
    mean_len = statistics.mean(lens) if lens else 0
    cv = statistics.pstdev(lens) / mean_len if len(lens) > 1 and mean_len else 0.0
    nat = ev.naturalness(text)
    tropes = writing.trope_sentences(text)
    heads = [h.strip() for h in re.findall(r"^##\s+(.+)$", md or "", re.M) if h.strip() != "参考资料"]
    paras = _paras(text)
    heads_set = {writing.clean_heading(h) for h in heads}
    title = re.search(r"^#\s+(.+)$", md or "", re.M)
    body_paras = [p for p in paras if p not in heads_set and not (title and p == title.group(1).strip())]
    first = next((p for p in body_paras if len(p) >= 12), "")
    first_sent = (_sents(first) or [""])[0]
    q = quotes(text)
    nr = number_repeats(sents)
    return {
        "chars": n,
        "sentences": len(sents),
        "sent_len_mean": round(mean_len, 1),
        "sent_len_cv": round(cv, 3),
        "repeat_rate": nr["repeat_rate"],
        "extra_number_mentions_per_1k": round(nr["extra_mentions"] * k, 2),
        "repeated_numbers": nr["top"],
        "trope_sentences_per_1k": round(len(tropes) * k, 2),
        "trope_examples": [x["sentence"][:80] + f"（{x['why']}）" for x in tropes[:6]],
        "tropes_per_1k": nat["tropes_per_1k"],
        "connective_start": nat["connective_start"],
        "dash_per_1k": nat["dash_per_1k"],
        "quotes": len(q),
        "quotes_per_1k": round(len(q) * k, 2),
        "hedge_notes_per_1k": round(len(writing.HEDGE_NOTE.findall(text)) * k, 2),
        "analysis_paras": sum(1 for p in paras if p.startswith(writing.ANALYSIS)),
        "reminders": sum(1 for s in sents if REMINDER.search(s)),
        "headings": len(heads),
        "template_headings": [h for h in heads if TEMPLATE_HEADINGS.match(h) and not AUTO_HEADINGS.match(h)],
        "lead_concrete": bool(FACTY.search(first_sent)) and not re.match(r"^(所谓|什么是|先说|我们|本文|这篇)", first_sent),
        "lead": first_sent[:60],
    }


# 每一项：(键, 名称, 方向, 容差, 代码里管它的位置)
# 方向 "low"：越低越像人（系统超过真人稿 + 容差就算差距）；"high"：越高越像人；"bool"：真人稿是 True 系统也应是 True。
CHECKS = [
    ("extra_number_mentions_per_1k", "数字反复出现（每千字多余的复述次数）", "low", 1.0,
     "write.OUTLINE_PROMPT 各节分工 / SECTION_PROMPT 规则 7 / _tidy 同组数字去重"),
    ("trope_sentences_per_1k", "AI 腔句式（每千字句数）", "low", 0.5,
     "writing.TROPE_PATTERNS + naturalize 点名改写 / SECTION_PROMPT 规则 10"),
    ("tropes_per_1k", "套话词（每千字）", "low", 0.8, "write.AI_TROPES / NATURAL_PROMPT"),
    ("connective_start", "连接词起句比例", "low", 0.05, "NATURAL_PROMPT 第 2 条"),
    ("dash_per_1k", "破折号（每千字）", "low", 0.5, "NATURAL_PROMPT 第 2 条"),
    ("hedge_notes_per_1k", "自动限制括注（每千字）", "low", 1.0, "writing.ensure_hedges（交代出处方即可、同一说法只说一次）"),
    ("reminders", "给读者/作者的提醒句", "low", 0, "writing.META_CLAUSE / SECTION_PROMPT 规则 10"),
    ("analysis_paras", "【分析】段数", "low", 2, "SECTION_PROMPT 规则 11（每节最多一段）"),
    ("quotes_per_1k", "直接引语（每千字）", "high", 0.3, "SECTION_PROMPT 规则 9（素材里有原话就引一句）"),
    ("sent_len_cv", "句长变化（变异系数）", "high", 0.08, "NATURAL_PROMPT 第 3 条（长短句交替）"),
    ("lead_concrete", "开头第一句是具体事实", "bool", 0, "SECTION_PROMPT 规则 8"),
]


def compare(sys_m: dict, humans: list[dict]) -> dict:
    """系统稿 vs 真人稿（多篇取平均）。返回 {human: 平均水平, gaps: [差距项], ok: [达标项]}。"""
    if not humans:
        return {"human": {}, "gaps": [], "ok": []}
    hm = {}
    for key, *_ in CHECKS:
        vals = [h[key] for h in humans if key in h]
        if not vals:
            continue
        hm[key] = (sum(1 for v in vals if v) / len(vals) >= 0.5) if isinstance(vals[0], bool) else round(statistics.mean(vals), 3)
    gaps, ok = [], []
    for key, name, way, tol, where in CHECKS:
        if key not in hm or key not in sys_m:
            continue
        s, h = sys_m[key], hm[key]
        if way == "low":
            bad = s > h + tol
        elif way == "high":
            bad = s < h - tol
        else:
            bad = bool(h) and not s
        row = {"key": key, "name": name, "system": s, "human": h, "where": where}
        (gaps if bad else ok).append(row)
    if sys_m.get("template_headings"):
        gaps.append({"key": "template_headings", "name": "模板化小标题", "system": sys_m["template_headings"],
                     "human": "（真人稿小标题写具体内容）", "where": "write.OUTLINE_PROMPT 提纲要求 / Skill 结构的默认小标题"})
    return {"human": hm, "gaps": gaps, "ok": ok}


JUDGE_PROMPT = """下面是同一主题的两篇中文文章 A 和 B。其中一篇是记者/编辑写的，另一篇是 AI 写作系统写的（你不知道是哪篇）。
请从读者角度比较“读起来像不像有经验的记者写的”，不比较谁信息多。
只输出 JSON：
{{"more_human": "A 或 B", "confidence": 1-5,
  "ai_like": [{{"article": "A 或 B", "sentence": "原文中最像 AI 写的句子（原样摘录，不超过 60 字）", "why": "为什么像 AI（10-30 字）"}}],
  "human_moves": ["较像人写的那篇用了、另一篇没用的具体写法（每条 15-40 字，最多 4 条）"],
  "rules": ["给 AI 写作系统的可执行改进规则（每条 15-40 字，最多 3 条）"]}}
ai_like 最多 5 条，两篇都可以挑。

A：
{a}

B：
{b}"""


def judge(chat, sys_text: str, human_text: str, seed: int = 0, limit: int = 3000) -> dict:
    """大模型评审（随机决定系统稿是 A 还是 B，抵消位置偏差）。chat 是 llm.chat。"""
    rnd = random.Random(seed)
    sys_is_a = rnd.random() < 0.5
    a, b = (sys_text, human_text) if sys_is_a else (human_text, sys_text)
    r = chat([{"role": "system", "content": "你只输出 JSON。"},
              {"role": "user", "content": JUDGE_PROMPT.format(a=a[:limit], b=b[:limit])}],
             json_mode=True, max_tokens=1200, timeout=120)
    d = r.get("data") or {}
    sys_label = "A" if sys_is_a else "B"
    more = str(d.get("more_human") or "").strip().upper()[:1]
    return {"system_judged_more_human": more == sys_label, "confidence": d.get("confidence"),
            "system_ai_like": [x for x in d.get("ai_like") or [] if str(x.get("article", "")).upper()[:1] == sys_label][:5],
            "human_ai_like": [x for x in d.get("ai_like") or [] if str(x.get("article", "")).upper()[:1] != sys_label][:3],
            "human_moves": (d.get("human_moves") or [])[:4], "rules": (d.get("rules") or [])[:3]}


def aggregate(cases: list[dict]) -> dict:
    """多篇汇总：每项差距出现在几篇里，系统与真人的平均水平；大模型评审的胜率和高频规则。"""
    done = [c for c in cases if c.get("compare")]
    agg = {}
    for c in done:
        for g in c["compare"]["gaps"]:
            a = agg.setdefault(g["key"], {"name": g["name"], "where": g["where"], "cases": 0, "examples": []})
            a["cases"] += 1
            a["examples"].append(f"{c['topic'][:16]}：系统 {g['system']} / 真人 {g['human']}")
    ranked = sorted(agg.items(), key=lambda x: -x[1]["cases"])
    js = [c["judge"] for c in done if c.get("judge")]
    return {"cases": len(cases), "compared": len(done),
            "gaps": [{"key": k, **v} for k, v in ranked],
            "judge": {"n": len(js), "system_more_human": sum(1 for j in js if j.get("system_judged_more_human")),
                      "rules": [r for j in js for r in j.get("rules") or []][:12],
                      "human_moves": [r for j in js for r in j.get("human_moves") or []][:12],
                      "ai_sentences": [x for j in js for x in j.get("system_ai_like") or []][:12]} if js else None}
