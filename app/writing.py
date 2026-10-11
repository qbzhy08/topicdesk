"""写作的确定性部分：选材、摘段落、事实核对、参考资料。大模型只负责提纲和成文，核对不依赖大模型。

约定（贯穿写作、核对、渲染、导出）：
- 来源结论：句末带出处编号 [n]（可多个 [1][3]），n 对应“参考资料”第 n 条；
- 系统分析：段落以【分析】开头，是系统自己的推断，不需要出处，但不能出现资料里没有的数字；
- 配图位：{{img:槽位名}}（第 2 步使用）。
"""
from __future__ import annotations

import re

from . import db, ranking
from . import memory as mem
from .collect.dedup import parse_date

CITE = re.compile(r"\[(\d{1,2})\]")
ANALYSIS = "【分析】"
# 需要核对的“硬信息”：数字（含单位）、百分比、年份、版本号
NUM = re.compile(r"(?<![A-Za-z0-9_.,])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s?(%|％|[kKMBG]\b|[kK](?=[\u4e00-\u9fff])|万亿|亿元|万元|万|亿|倍|年|月|日|个|款|篇|分|美元|元|台|家|人|项|次|辆|件|名|条|种|米)?")
NOISE_NUMS = {"1", "2", "3", "4", "5"}        # 序号、“两三句”这类小数字不当成需要出处的数字


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").lower()).replace("％", "%")


def bigrams(s: str) -> set:
    s = re.sub(r"[^\w一-鿿]", "", (s or "").lower())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def overlap(claim: str, quote: str) -> float:
    a, b = bigrams(CITE.sub("", claim)), bigrams(quote)
    return len(a & b) / len(a) if a else 0.0


def numbers(text: str) -> list[str]:
    out = []
    for m in NUM.finditer(CITE.sub("", text)):
        n = (m.group(1) + (m.group(2) or "")).replace(" ", "")
        core = re.match(r"[\d,]+(?:\.\d+)?", n).group(0).replace(",", "")
        if core in NOISE_NUMS and n == core:
            continue
        out.append(n)
    return list(dict.fromkeys(out))


def num_in(n: str, text_norm: str) -> bool:
    """数字是否出现在资料里：128K 也匹配 128k、128 K；“2026年”退化为匹配“2026”。"""
    n = _norm(n)
    if n in text_norm:
        return True
    raw = re.match(r"[\d,]+(?:\.\d+)?", n).group(0)
    core, unit = raw.replace(",", ""), n[len(raw):]
    plain = text_norm.replace(",", "")          # 11,000 与 11000 视为同一个数
    if core + unit in plain:
        return True
    if unit in ("年", "月", "日", "个", "款", "篇", "分", "台", "家", "人", "项", "次", "辆", "件", "名", "条", "种", "米"):
        return re.search(rf"(?<![\d.]){re.escape(core)}(?![\d.])", plain) is not None
    return False


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？!?；;])\s*|\n+", text or "")
    return [p.strip() for p in parts if len(p.strip()) >= 6]


# ---------------------------------------------------------------- 选材

def _doc_rows(ids: list[str]) -> dict:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    return {d["id"]: d for d in db.query(f"SELECT id, url, title, author, source_id, published_at, content_kind, "
                                         f"text, summary, novelty, meta, duplicate_of, event_id FROM documents "
                                         f"WHERE id IN ({marks})", ids)}


def select_material(research_task: str, skill: dict, extra_ids: list[str] | None = None,
                    extra_why: dict | None = None) -> dict:
    """按 Skill 选材：先取它指定的阅读清单，再按价值排序补足，并按 prefer 调整顺序。
    返回 {docs: [...], why: {doc_id: 入选理由}, skipped: [...]}。"""
    rk = ranking.latest(research_task)
    if not rk:
        raise ValueError("这个任务还没有价值排序结果，不能写作")
    mat = skill["material"]
    items = {it["id"]: it for it in rk["items"]}
    lst = rk["lists"].get(mat["list"]) or rk["lists"].get("30") or {"selected": []}
    order, why = [], {}
    for s in lst["selected"]:
        order.append(s["id"])
        why[s["id"]] = f"{'五分钟速览' if mat['list'] == '5' else '三十分钟深读'}清单第 {s['rank']} 条"
    for i in rk["orders"]["ours"]:
        if i not in order:
            order.append(i)
            why[i] = f"价值排序第 {rk['orders']['ours'].index(i) + 1} 位（补足素材）"
    pref = mat.get("prefer") or []

    def boost(i):
        it = items.get(i) or {}
        b = 0
        if "primary" in pref and it.get("primary"):
            b += 1
        if "new" in pref and it.get("novelty") == "new":
            b += 1
        if "update" in pref and it.get("novelty") == "update":
            b += 1
        if "disputed" in pref and it.get("disputed"):
            b += 2
        return b
    # 清单内的顺序基本保持，prefer 只在相近位置上调整（每个加分相当于提前 3 位）
    pos = {i: k for k, i in enumerate(order)}
    order.sort(key=lambda i: pos[i] - 3 * boost(i))
    chosen, notes = _apply_reference_rules(order, skill, int(mat.get("max_docs") or 10), why)
    for e in extra_ids or []:
        if e not in chosen:
            chosen.append(e)
            why[e] = (extra_why or {}).get(e) or "写作时发现缺口，补充检索得到"
    rows = _doc_rows(chosen)
    docs = []
    for i in chosen:
        d = rows.get(i)
        if not d or not (d["text"] or d["summary"]):
            continue
        it = items.get(i) or {}
        docs.append({"id": i, "title": d["title"] or d["url"], "url": d["url"], "source_id": d["source_id"],
                     "author": d["author"], "published_at": d["published_at"], "kind": d["content_kind"],
                     "text": d["text"] or d["summary"] or "", "novelty": d["novelty"],
                     "primary": bool((d["meta"] or {}).get("primary")), "value": it.get("value"),
                     "disputed": bool(it.get("disputed")), "unverified": bool(it.get("unverified")),
                     "why": why.get(i, "")})
    for n, d in enumerate(docs, 1):
        d["n"] = n
    return {"docs": docs, "skipped": [i for i in order if i not in chosen][:20], "ranking_version": rk.get("version"),
            "ref_notes": notes}


def _apply_reference_rules(order: list[str], skill: dict, max_docs: int, why: dict) -> tuple[list[str], list[str]]:
    """Skill 的参考资料要求在选材时生效：同一来源最多 max_per_source 篇；一手资料不足 min_primary 时，
    从后面的候选里换进来（替换排在最后的非一手资料）。"""
    ref = skill.get("references") or {}
    cap = int(ref.get("max_per_source") or 99)
    need_primary = int(ref.get("min_primary") or 0)
    rows = _doc_rows(order[:120])
    usable = [i for i in order if i in rows and (rows[i]["text"] or rows[i]["summary"])]
    is_primary = {i: bool((rows[i]["meta"] or {}).get("primary")) for i in usable}
    chosen, per, notes = [], {}, []
    capped = 0
    for i in usable:
        if len(chosen) >= max_docs:
            break
        src = rows[i]["source_id"]
        if per.get(src, 0) >= cap:
            capped += 1
            continue
        per[src] = per.get(src, 0) + 1
        chosen.append(i)
    if capped:
        notes.append(f"同一来源最多 {cap} 篇：跳过 {capped} 篇，让位给其他来源")
    have = sum(1 for i in chosen if is_primary[i])
    if have < need_primary:
        extra = [i for i in usable if is_primary[i] and i not in chosen][:need_primary - have]
        for e in extra:
            victim = next((i for i in reversed(chosen) if not is_primary[i]), None)
            if victim and len(chosen) >= max_docs:
                chosen.remove(victim)
            chosen.append(e)
            why[e] = (why.get(e) or "") + "（Skill 要求至少 {} 篇一手资料，换入）".format(need_primary)
        notes.append(f"一手资料 {have} 篇少于要求的 {need_primary} 篇：换入 {len(extra)} 篇"
                     + ("" if have + len(extra) >= need_primary else "（候选里一手资料不够，写作时如实说明）"))
    return chosen, notes


def reference_check(rep: dict, docs: list[dict], skill: dict) -> dict:
    """成稿是否满足 Skill 的参考资料要求：引用了几个不同来源、几篇一手资料，参考资料是否都有日期。"""
    ref = skill.get("references") or {}
    used = set(rep.get("used") or [])
    cited = [d for d in docs if d["n"] in used]
    srcs = sorted({d.get("source_id") for d in cited if d.get("source_id")})
    prim = [d for d in cited if d.get("primary")]
    undated = [d["n"] for d in cited if not d.get("published_at")] if ref.get("dated") else []
    avail_src = len({d.get("source_id") for d in docs})
    avail_prim = sum(1 for d in docs if d.get("primary"))
    items = [
        {"name": "不同来源", "ok": len(srcs) >= min(ref.get("min_sources", 0), avail_src),
         "detail": f"引用 {len(srcs)} 个（要求 ≥{ref.get('min_sources', 0)}，素材里共 {avail_src} 个）"},
        {"name": "一手资料", "ok": len(prim) >= min(ref.get("min_primary", 0), avail_prim),
         "detail": f"引用 {len(prim)} 篇（要求 ≥{ref.get('min_primary', 0)}，素材里共 {avail_prim} 篇）"},
    ]
    if ref.get("dated"):
        items.append({"name": "参考资料日期", "ok": not undated,
                      "detail": "都有发布日期" if not undated else f"资料 {undated} 缺少发布日期"})
    return {"items": items, "ok": all(i["ok"] for i in items), "sources": srcs, "primary": [d["n"] for d in prim]}


def topic_facts(topic: str, mode: str = "all", limit: int = 12) -> list[dict]:
    rows = mem.list_memories(scope="topic", topic=topic, kind="fact", status="alive")
    order = {"verified_first": {"supported": 0, "unverified": 1, "disputed": 2},
             "disputed_first": {"disputed": 0, "supported": 1, "unverified": 2}}.get(mode, {})
    rows.sort(key=lambda m: order.get((m["meta"] or {}).get("verify"), 1))
    out = []
    for m in rows[:limit]:
        ev = [r["doc_id"] for r in db.query("SELECT DISTINCT doc_id FROM evidence WHERE claim_id=? AND stance!='refute'",
                                            (m["id"],))]
        out.append({"id": m["id"], "content": m["content"], "verify": (m["meta"] or {}).get("verify", "unverified"),
                    "corrected_from": (m["meta"] or {}).get("previous_content"), "docs": ev})
    return out


def excerpt(doc: dict, focus: str, budget: int = 700) -> str:
    """从资料里取和本节目标最相关的几段（保持原文顺序），控制长度。"""
    paras = [p.strip() for p in re.split(r"\n{1,}", doc["text"]) if len(p.strip()) >= 15] or [doc["text"]]
    f = bigrams(focus)
    scored = sorted(range(len(paras)), key=lambda i: -(len(bigrams(paras[i]) & f) + (2 if i == 0 else 0)))
    keep, used = [], 0
    for i in scored:
        p = paras[i][:budget]
        if used + len(p) > budget and keep:
            continue
        keep.append(i)
        used += len(p)
        if used >= budget:
            break
    return "\n".join(paras[i][:budget] for i in sorted(keep))


def source_label(doc: dict, names: dict | None = None) -> str:
    dt = parse_date(doc.get("published_at"))
    name = (names or {}).get(doc.get("source_id")) or doc.get("source_id") or ""
    return f"{name}{'，' + dt.strftime('%Y-%m-%d') if dt else ''}"


def references_md(docs: list[dict], used: set[int], names: dict | None = None) -> str:
    lines = []
    for d in docs:
        if d["n"] in used:
            lines.append(f"{d['n']}. [{d['title']}]({d['url']})（{source_label(d, names)}）")
    return "\n".join(lines)


# ---------------------------------------------------------------- 事实核对

def _units(md: str):
    """把一节正文切成核对单元：(文本, 是否分析)。表格按单元格拆开（第一列是维度名、表头行不核对）。"""
    out = []
    for para in re.split(r"\n\s*\n", md or ""):
        p = para.strip()
        if not p or p.startswith("#") or p.startswith("{{img:"):
            continue
        is_analysis = p.lstrip("> -*").startswith(ANALYSIS)
        table_rows = 0
        for line in p.split("\n"):
            ln = line.strip()
            if ln.startswith("|"):
                if re.match(r"^\|?[\s:|-]+\|?$", ln):
                    continue
                table_rows += 1
                if table_rows == 1:          # 表头
                    continue
                cells = [c.strip() for c in ln.strip("|").split("|")]
                for c in cells[1:]:
                    out.append((c, False))
                continue
            for s in re.split(r"(?<=[。！？!?])", ln):
                out.append((s.strip(" -*>"), is_analysis))
    return [(u, a) for u, a in out if len(CITE.sub("", u).strip()) >= 8]


def passages(text: str) -> list[str]:
    """出处的候选段落：单句 + 相邻两句（改写常常把两句合成一句）。"""
    ss = sentences(text)
    return ss + [ss[i] + ss[i + 1] for i in range(len(ss) - 1)]


def best_quotes(claim: str, texts: dict, cites: list[int], k: int = 3) -> list[tuple[float, str, int]]:
    cands = []
    for c in cites:
        for q in passages(texts.get(c, "")):
            cands.append((overlap(claim, q), q, c))
    cands.sort(key=lambda x: -x[0])
    if not cands or cands[0][0] < 0.12:
        # 字面几乎不重合（常见于中文句子对英文原文）：按字面挑段落没有意义，改给各出处的开头部分
        lead = [(0.0, re.sub(r"\s+", " ", texts.get(c, ""))[:600], c) for c in cites if texts.get(c)]
        return lead[:k]
    return cands[:k]


def check(sections: list[dict], docs: list[dict], verdicts: dict | None = None) -> dict:
    """逐句核对。每句的结果：
       supported   有出处，出处原文里有相近表述（字面），数字都能在出处里找到
       semantic    字面不像，但大模型对照出处原文判断意思一致（改写/概括）
       weak        有出处，但出处里找不到依据（字面不像，大模型也判断不支持，或没配置大模型）
       number      有出处，但句中某个数字在出处里找不到（最危险：数字写错或张冠李戴）
       bad_cite    出处编号不存在
       uncited     不是分析，却有数字而没有出处
       analysis    【分析】段（引用了出处的分析只核对数字）
       analysis_num【分析】段里出现了资料里没有的数字
    verdicts：{句子: {"verdict": supported/partial/unsupported, "reason"}}，来自大模型语义核对。
    返回 {items, summary, used}。"""
    verdicts = verdicts or {}
    by_n = {d["n"]: d for d in docs}
    texts = {d["n"]: d["text"] for d in docs}
    norm = {d["n"]: _norm(d["text"]) for d in docs}
    all_norm = "\n".join(norm.values())
    items, used = [], set()
    for sec in sections:
        for s, is_analysis in _units(sec.get("md") or ""):
            cites = [int(x) for x in CITE.findall(s)]
            nums = numbers(s)
            it = {"section": sec["id"], "sentence": s, "cites": cites, "numbers": nums, "analysis": is_analysis}
            if cites:
                used.update(c for c in cites if c in by_n)
                bad = [c for c in cites if c not in by_n]
                if bad:
                    it.update(status="bad_cite", note=f"出处编号 {bad} 不存在")
                    items.append(it)
                    continue
                top = best_quotes(s, texts, cites)
                best, quote, qdoc = top[0] if top else (0.0, "", None)
                missing = [n for n in nums if not any(num_in(n, norm[c]) for c in cites)]
                it.update(quote=quote[:220], quote_doc=qdoc, overlap=round(best, 2), missing=missing)
                v = verdicts.get(s) or {}
                if missing:
                    other = [d["n"] for d in docs if all(num_in(n, norm[d["n"]]) for n in missing)]
                    it.update(status="number", note=f"数字 {missing} 在出处 {cites} 里找不到"
                              + (f"；资料 {other[:3]} 里有，可能引错了出处" if other else ""), alt=other[:3])
                elif is_analysis:
                    it["status"] = "analysis"
                elif best >= 0.35:
                    it["status"] = "supported"
                elif v.get("verdict") in ("supported", "partial"):
                    it.update(status="semantic", note=("意思一致" if v["verdict"] == "supported" else "基本一致")
                              + (f"：{v.get('reason')}" if v.get("reason") else ""))
                else:
                    it.update(status="weak", note=(f"大模型对照原文判断不支持：{v.get('reason')}" if v.get("verdict")
                                                   else f"出处里没有找到相近的表述（字面相似度 {best:.2f}）"))
            elif is_analysis:
                missing = [n for n in nums if not num_in(n, all_norm)]
                it["status"] = "analysis_num" if missing else "analysis"
                if missing:
                    it["note"] = f"分析里的数字 {missing} 在资料中找不到"
            elif nums:
                src = [d["n"] for d in docs if all(num_in(n, norm[d["n"]]) for n in nums)]
                it.update(status="uncited", note=f"有数字 {nums} 但没有出处" +
                          (f"；资料 {src[:3]} 里有这些数字" if src else "；资料里也找不到"), alt=src[:3])
            else:
                it["status"] = "plain"
            items.append(it)
    keys = ("supported", "semantic", "weak", "number", "bad_cite", "uncited", "analysis", "analysis_num", "plain")
    summary = {k: sum(1 for i in items if i["status"] == k) for k in keys}
    cited = summary["supported"] + summary["semantic"] + summary["weak"] + summary["number"] + summary["bad_cite"]
    summary["cited"] = cited
    summary["support_rate"] = round((summary["supported"] + summary["semantic"]) / cited, 3) if cited else None
    summary["problems"] = summary["weak"] + summary["number"] + summary["bad_cite"] + summary["uncited"] + \
        summary["analysis_num"]
    return {"items": items, "summary": summary, "used": sorted(used)}


SEMANTIC_PROMPT = """判断每条“文章句子”能否由给出的“出处原文”支持。允许改写、概括、合并多句；
但主体、动作、数字、时间、因果方向必须和原文一致，原文没说的推断不算支持。
{items}
只输出 JSON：{{"results": [{{"i": 1, "verdict": "supported", "reason": "不超过 20 字"}}]}}
verdict 只能是 supported（支持）/ partial（大体支持，有轻微引申）/ unsupported（不支持或原文没有）。"""


def semantic_batch(items: list[dict], docs: list[dict]) -> str:
    texts = {d["n"]: d["text"] for d in docs}
    lines = []
    for k, it in enumerate(items, 1):
        qs = best_quotes(it["sentence"], texts, it["cites"], 3)
        quotes = "\n".join(f"   原文{j}：{q[:600] if o < 0.12 else q[:240]}" for j, (o, q, _) in enumerate(qs, 1))
        lines.append(f"{k}. 句子：{CITE.sub('', it['sentence'])[:200]}\n{quotes}")
    return SEMANTIC_PROMPT.format(items="\n".join(lines))


def problems(rep: dict, kinds=("number", "bad_cite", "uncited", "analysis_num", "weak")) -> list[dict]:
    return [i for i in rep["items"] if i["status"] in kinds]


def autofix_citations(sections: list[dict], rep: dict) -> list[dict]:
    """确定性修正：数字只在另一篇资料里出现、或没标出处但资料里有 → 改成正确的出处编号。返回修正记录。"""
    fixes = []
    for it in rep["items"]:
        if it["status"] not in ("number", "uncited") or not it.get("alt"):
            continue
        sec = next(s for s in sections if s["id"] == it["section"])
        new_n = it["alt"][0]
        if it["status"] == "number":
            new_s = CITE.sub("", it["sentence"]).rstrip() + f"[{new_n}]"
        else:
            new_s = it["sentence"].rstrip() + f"[{new_n}]"
        # 句末标点后的编号挪到标点前：……。[3] → ……[3]。
        new_s = re.sub(r"([。！？!?])((?:\[\d+\])+)$", r"\2\1", new_s)
        if it["sentence"] in sec["md"]:
            sec["md"] = sec["md"].replace(it["sentence"], new_s, 1)
            fixes.append({"section": sec["id"], "before": it["sentence"], "after": new_s,
                          "why": "改为数字实际所在的资料" if it["status"] == "number" else "补上出处"})
    return fixes


def renumber(sections: list[dict]) -> tuple[list[dict], dict]:
    """出处编号按正文中第一次出现的顺序重排为 1..k（素材编号可能不连续）。返回 (新的节, {素材编号: 文中编号})。"""
    refmap = {}
    for s in sections:
        for m in CITE.finditer(s.get("md") or ""):
            refmap.setdefault(int(m.group(1)), len(refmap) + 1)
    out = [{**s, "md": CITE.sub(lambda m: f"[{refmap.get(int(m.group(1)), m.group(1))}]", s.get("md") or "")}
           for s in sections]
    return out, refmap


def assemble(title: str, sections: list[dict], docs: list[dict], used: set[int], names: dict | None = None,
             note: str | None = None, cover_md: str | None = None) -> str:
    parts = [f"# {title}", ""]
    if cover_md:
        parts += [cover_md, ""]
    parts += ["> 阅读说明：句末的 [n] 是出处，对应文末参考资料；以“【分析】”开头的段落是本文的分析判断，不是来源原话。"]
    if note:
        parts += ["", f"> {note}"]
    for s in sections:
        parts += ["", f"## {s['heading']}", "", (s.get("md") or "").strip()]
    refs = references_md(sorted(docs, key=lambda d: d["n"]), used, names)
    if refs:
        parts += ["", "## 参考资料", "", refs]
    return "\n".join(parts).strip() + "\n"


# 写作说明混进正文（大模型把给它的指令当成内容写了出来）：这类句子直接删掉
META_TEXT = re.compile(r"记忆中的事实|本节(暂不|不再|将|只|不|所)|本文所依据的(材料|素材)|所依据的材料中|暂不引用|素材(中|里|编号)|资料编号|出处编号|核对(发现|结果)|上一稿|"
                       r"按(核对|要求)改写|根据(提示|要求)|（来源：记忆）|未能确认其具体出处")


def strip_meta(md: str) -> tuple[str, list[str]]:
    removed = []
    out_lines = []
    for line in (md or "").split("\n"):
        if line.lstrip().startswith("|"):
            out_lines.append(line)
            continue
        parts = re.split(r"(?<=[。！？!?])", line)
        keep = []
        for p in parts:
            if p.strip() and META_TEXT.search(p):
                removed.append(p.strip())
            else:
                keep.append(p)
        new = "".join(keep)
        if line.strip() and not new.strip(" -*>【分析】"):
            continue          # 整行都是说明文字
        out_lines.append(new)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out_lines)).strip(), removed

