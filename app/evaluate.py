"""文章评测：事实保真、信息密度、自然度（“AI 腔”）、理解成本，以及相对原始资料的增益。

确定性指标不依赖大模型，可复算；大模型评审（独立事实核验、盲评）的提示词与写作时用的不同，
并且每对文章都交换前后顺序各评一次，抵消位置偏差。

评测对象一律是“完整正文”：文章 Markdown 去掉配图行、图注、阅读说明和参考资料列表后的文字（标题、小标题、
出处编号都保留）。所有变体用同样的处理，AIGC 检测器也只看这份文本。
"""
from __future__ import annotations

import math
import random
import re
import statistics

from . import writing

AI_TROPES = ["首先", "其次", "最后", "综上所述", "总而言之", "值得注意的是", "值得一提的是", "不难看出", "由此可见",
             "总的来说", "此外", "与此同时", "随着", "毋庸置疑", "至关重要", "显著", "进一步", "赋能", "助力", "全方位",
             "深度融合", "具有重要意义", "扮演着", "关键角色", "日益", "不可或缺", "不仅", "更是", "无疑", "引发了广泛关注",
             "备受关注", "在这一背景下", "从某种意义上说", "需要指出的是", "可以说",
             # 新一代大模型常见的说法（老的套话被提示词禁掉后，换成了这些）
             "这意味着", "意味着", "值得关注", "一方面", "另一方面", "从长远来看", "总体来看", "总体而言", "整体来看",
             "换句话说", "简单来说", "归根结底", "本质上", "核心在于", "关键在于", "不再是", "格局", "拐点", "窗口期"]
CONNECTIVE_START = re.compile(r"^(首先|其次|此外|另外|同时|与此同时|然而|因此|总之|综上|最后|而且|并且|但是|不过|"
                              r"换句话说|简单来说|总体来看|整体来看|这意味着|也就是说|事实上|值得注意的是|可以看到|由此|所以|而)")
LATIN_TERM = re.compile(r"\b[A-Za-z][A-Za-z0-9.+-]{1,}\b")
READ_SPEED = 400          # 中文阅读速度：字/分钟（一般读者 300-500）


# ---------------------------------------------------------------- 文本

def plain_text(md: str) -> str:
    """完整正文：去掉配图、图注、阅读说明、参考资料；保留标题、小标题、出处编号。"""
    body = (md or "").split("\n## 参考资料")[0]
    out = []
    lines = body.split("\n")
    skip_caption = False
    for ln in lines:
        s = ln.strip()
        if re.match(r"^!\[[^\]]*\]\([^)]*\)$", s):
            skip_caption = True
            continue
        if skip_caption and s.startswith("*") and s.endswith("*"):
            skip_caption = False
            continue
        skip_caption = False
        if s.startswith("> 阅读说明") or s.startswith("> 未配置大模型"):
            continue
        s = re.sub(r"^#+\s*", "", s)
        s = re.sub(r"^\|?[\s:|-]+\|?$", "", s) if s.startswith("|") and set(s) <= set("|:- ") else s
        s = s.replace("**", "").lstrip("> ")
        out.append(s)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？!?；;])|\n+", text)
    return [p.strip(" -*|") for p in parts if len(p.strip(" -*|")) >= 4]


def _hanzi_len(text: str) -> int:
    return len(re.sub(r"\s|\[\d+\]", "", text))


# ---------------------------------------------------------------- 确定性指标

def fidelity(sections: list[dict], docs: list[dict], verdicts: dict | None = None) -> dict:
    """事实保真（确定性部分）：重新跑逐句核对。数字错误率 = 带出处句子里在出处中找不到的数字 / 带出处句子里的全部数字。"""
    rep = writing.check(sections, docs, verdicts or {})
    s = rep["summary"]
    cited = [i for i in rep["items"] if i.get("cites")]
    nums = sum(len(i.get("numbers") or []) for i in cited)
    bad_nums = sum(len(i.get("missing") or []) for i in cited)
    return {"support_rate": s["support_rate"], "cited": s["cited"], "problems": s["problems"],
            "number_errors": bad_nums, "numbers_checked": nums,
            "number_error_rate": round(bad_nums / nums, 3) if nums else 0.0,
            "uncited_numbers": s["uncited"], "analysis_bad_numbers": s["analysis_num"],
            "weak": s["weak"], "bad_cite": s["bad_cite"], "items": rep["items"]}


def density(text: str, items: list[dict], docs: list[dict]) -> dict:
    """信息密度：每千字可核对事实数、引用的不同来源数、重复率、空话率。"""
    n = max(1, _hanzi_len(text))
    sents = _sentences(text)
    facts = [i for i in items if i.get("cites") and i["status"] in ("supported", "semantic")]
    nums = set()
    for i in facts:
        nums.update(i.get("numbers") or [])
    used = {c for i in items for c in i.get("cites") or []}
    srcs = {d.get("source_id") for d in docs if d["n"] in used}
    dup = 0
    for a in range(len(sents)):
        for b in range(a + 1, min(len(sents), a + 40)):
            if writing.overlap(sents[a], sents[b]) > 0.6 and len(sents[a]) > 12:
                dup += 1
                break
    filler = [s for s in sents if not writing.CITE.search(s) and not re.search(r"\d", s)
              and not s.startswith("【分析】") and len(s) > 12]
    return {"chars": n, "verified_facts": len(facts), "facts_per_1k": round(len(facts) * 1000 / n, 2),
            "distinct_numbers": len(nums), "sources_cited": len(srcs), "redundant_sentences": dup,
            "redundancy": round(dup / max(1, len(sents)), 3), "filler_ratio": round(len(filler) / max(1, len(sents)), 3)}


def naturalness(text: str) -> dict:
    """自然度（“AI 腔”的确定性代理指标）：套话密度、连接词起句比例、句长变化（变异系数越低越像模板）。"""
    n = max(1, _hanzi_len(text))
    hits = {t: text.count(t) for t in AI_TROPES if t in text}
    sents = [s for s in _sentences(text) if not re.fullmatch(r"[^一-鿿]*", s)]
    lens = [_hanzi_len(s) for s in sents]
    cv = statistics.pstdev(lens) / statistics.mean(lens) if len(lens) > 1 and statistics.mean(lens) else 0.0
    conn = sum(1 for s in sents if CONNECTIVE_START.match(s.lstrip("【分析】")))
    dash = text.count("——") + text.count("—")
    paral = len(re.findall(r"[^，。；]{2,8}、[^，。；]{2,8}、[^，。；]{2,8}(?:等|和)", text))   # “A、B、C 等”三连排比
    return {"tropes_per_1k": round(sum(hits.values()) * 1000 / n, 2), "tropes": dict(sorted(hits.items(), key=lambda x: -x[1])[:8]),
            "dash_per_1k": round(dash * 1000 / n, 2), "triplets_per_1k": round(paral * 1000 / n, 2),
            "connective_start": round(conn / max(1, len(sents)), 3), "sentence_len_cv": round(cv, 3),
            "sentences": len(sents)}


def comprehension(text: str) -> dict:
    """理解成本：平均句长、长句比例、没解释的外文术语密度、预计阅读时间、结构提示（小标题/列表/表格）。"""
    sents = _sentences(text)
    lens = [_hanzi_len(s) for s in sents] or [0]
    terms = LATIN_TERM.findall(writing.CITE.sub("", text))
    explained = sum(1 for t in set(terms) if re.search(re.escape(t) + r"\s*[（(]", text))
    uniq = len(set(terms))
    n = max(1, _hanzi_len(text))
    return {"avg_sentence_len": round(statistics.mean(lens), 1), "long_sentence_ratio": round(sum(1 for x in lens if x > 50) / len(lens), 3),
            "latin_terms": uniq, "unexplained_terms": max(0, uniq - explained),
            "terms_per_1k": round(uniq * 1000 / n, 2), "read_minutes": round(n / READ_SPEED, 1)}


def gain(text: str, items: list[dict], docs: list[dict], facts: list[dict] | None = None) -> dict:
    """读改写稿比读原始资料多得到什么（可量化的部分）：
    - 省时间：原始资料总字数 vs 文章字数（按同样阅读速度折算分钟）；
    - 交叉印证：同一段里引用 ≥2 个不同来源的段落数（一篇篇读资料时要自己做的比对）；
    - 去噪：素材里有多少篇最后没用上（被判断为重复或不相关）；
    - 结构与判断：小标题、列表/表格、【分析】段的数量（原始资料里没有的组织和判断）；
    - 争议与可信度：文章点出的有争议/已更正事实。"""
    src_chars = sum(len(d.get("text") or "") for d in docs)
    n = max(1, _hanzi_len(text))
    used = {c for i in items for c in i.get("cites") or []}
    src_of = {d["n"]: d.get("source_id") for d in docs}
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    multi = 0
    for p in paras:
        srcs = {src_of.get(int(c)) for c in writing.CITE.findall(p)} - {None}
        if len(srcs) >= 2:
            multi += 1
    analysis = sum(1 for p in paras if p.strip().startswith("【分析】"))
    disputed = [f for f in facts or [] if f.get("verify") == "disputed"]
    mentioned = [f for f in disputed if writing.overlap(f["content"], text) > 0.3]
    return {"source_docs": len(docs), "source_chars": src_chars, "article_chars": n,
            "compression": round(src_chars / n, 1) if n else None,
            "minutes_sources": round(src_chars / READ_SPEED, 1), "minutes_article": round(n / READ_SPEED, 1),
            "minutes_saved": round((src_chars - n) / READ_SPEED, 1),
            "docs_used": len(used & set(src_of)), "docs_dropped": len(set(src_of) - used),
            "cross_source_paras": multi, "analysis_paras": analysis,
            "disputes_flagged": len(mentioned), "disputes_known": len(disputed)}


def evaluate_version(md: str, meta: dict, docs: list[dict]) -> dict:
    """一篇文章（一个版本）的全部确定性指标。docs 带 text（素材原文）。"""
    text = plain_text(md)
    fid = fidelity(meta.get("sections") or [], docs)
    return {"text": text, "fidelity": {k: v for k, v in fid.items() if k != "items"},
            "density": density(text, fid["items"], docs), "naturalness": naturalness(text),
            "comprehension": comprehension(text), "gain": gain(text, fid["items"], docs, meta.get("facts")),
            "_items": fid["items"]}


# ---------------------------------------------------------------- 大模型评审（独立于写作流程）

CLAIM_JUDGE = """你是事实核查员。逐条判断“文章中的说法”与“出处原文”的关系。不要考虑说法本身是否常识正确，只看出处是否支持。
{items}
每条给出：supported（出处明确支持，包括数字）/ partial（大体支持但有细节、程度或数字不一致）/
unsupported（出处没有提到）/ contradicted（出处说的相反或数字不同）。
只输出 JSON：{{"results": [{{"i": 1, "verdict": "supported", "why": "不超过 20 字"}}]}}"""


def claim_judge_input(items: list[dict], docs: list[dict], k: int = 16, seed: int = 7) -> tuple[list[dict], dict]:
    """抽样带出处的句子（固定随机种子，可复现），附上出处里最相关的原文片段。"""
    texts = {d["n"]: d.get("text") or "" for d in docs}
    pool = [i for i in items if i.get("cites") and not i.get("analysis")]
    rnd = random.Random(seed)
    pick = pool if len(pool) <= k else rnd.sample(pool, k)
    rows = []
    for j, it in enumerate(pick, 1):
        quotes = writing.best_quotes(it["sentence"], texts, [c for c in it["cites"] if c in texts], k=2)
        q = " …… ".join(x[1][:260] for x in quotes)
        rows.append(f"{j}. 文章中的说法：{writing.CITE.sub('', it['sentence'])}\n   出处原文：{q or '（无）'}")
    return pick, {"messages": [{"role": "system", "content": "你只输出 JSON。"},
                               {"role": "user", "content": CLAIM_JUDGE.format(items="\n".join(rows))}], "max_tokens": 1800}


def claim_judge_score(pick: list[dict], data) -> dict:
    res = (data or {}).get("results") if isinstance(data, dict) else None
    counts = {"supported": 0, "partial": 0, "unsupported": 0, "contradicted": 0}
    bad = []
    for r in res or []:
        v = str(r.get("verdict") or "")
        if v in counts:
            counts[v] += 1
            if v in ("unsupported", "contradicted"):
                try:
                    bad.append({"sentence": pick[int(r.get("i")) - 1]["sentence"], "verdict": v, "why": r.get("why")})
                except (TypeError, ValueError, IndexError):
                    pass
    n = sum(counts.values())
    return {"sampled": len(pick), "judged": n, **counts,
            "fidelity": round((counts["supported"] + 0.5 * counts["partial"]) / n, 3) if n else None, "bad": bad[:6]}


PAIR_JUDGE = """你是一位{reader}。下面是同一主题、同一批资料写成的两篇文章，作者和写法都未知。请像真实读者一样读完再比较。
【文章 A】
{a}

【文章 B】
{b}

从读者角度，在每个维度上选出更好的一篇（A / B / 相当），并给 1-5 分：
- accuracy 准确可信：说法有没有交代来源、有没有含糊或夸大
- information 信息量：读完得到的有用信息多不多，有没有空话、重复
- natural 表达自然：读起来像不像一位人类编辑写的，有没有套话和模板腔
- easy 好理解：读懂要花多少力气，术语、长句、结构
- overall 整体更愿意读哪篇
只输出 JSON：{{"accuracy": {{"winner": "A", "A": 4, "B": 3}}, "information": {{...}}, "natural": {{...}}, "easy": {{...}},
"overall": {{...}}, "reason": "不超过 60 字"}}"""

DIMS = ("accuracy", "information", "natural", "easy", "overall")


def pair_judge_inputs(text_x: str, text_y: str, reader: str, limit: int = 3500) -> list[dict]:
    """两次评审：X 在前、Y 在前，各一次（抵消位置偏差）。"""
    x, y = text_x[:limit], text_y[:limit]
    return [{"messages": [{"role": "system", "content": "你只输出 JSON。"},
                          {"role": "user", "content": PAIR_JUDGE.format(reader=reader, a=a, b=b)}], "max_tokens": 900}
            for a, b in ((x, y), (y, x))]


def pair_judge_merge(r_xy, r_yx) -> dict:
    """合并两次评审：换算回 X/Y，分数取平均，胜负按两次一致才算，否则算“相当”。"""
    out = {}
    for d in DIMS:
        sx, sy, wins = [], [], []
        for r, flip in ((r_xy, False), (r_yx, True)):
            v = (r or {}).get(d) if isinstance(r, dict) else None
            if not isinstance(v, dict):
                continue
            a, b = v.get("A"), v.get("B")
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                (sx if not flip else sy).append(a)
                (sy if not flip else sx).append(b)
            w = str(v.get("winner") or "")
            wins.append({"A": "Y" if flip else "X", "B": "X" if flip else "Y"}.get(w, "tie"))
        win = wins[0] if len(wins) == 2 and wins[0] == wins[1] else "tie"
        out[d] = {"x": round(statistics.mean(sx), 2) if sx else None, "y": round(statistics.mean(sy), 2) if sy else None,
                  "winner": win, "consistent": len(wins) == 2 and wins[0] == wins[1]}
    out["reasons"] = [str((r or {}).get("reason") or "")[:80] for r in (r_xy, r_yx) if isinstance(r, dict)]
    return out


# ---------------------------------------------------------------- 汇总

def mean(xs):
    xs = [x for x in xs if isinstance(x, (int, float)) and not (isinstance(x, float) and math.isnan(x))]
    return round(statistics.mean(xs), 3) if xs else None
