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
from .collect.sources import self_archive as _self_archive

CITE = re.compile(r"\[(\d{1,2})\]")
ANALYSIS = "【分析】"
# 需要核对的“硬信息”：数字（含单位）、百分比、年份、版本号
NUM = re.compile(r"(?<![A-Za-z0-9_.,])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s?(%|％|[kKMBG]\b|[kK](?=[\u4e00-\u9fff])|万亿|亿元|万元|万|亿|倍|年|月|日|个|款|篇|分|美元|元|台|家|人|项|次|辆|件|名|条|种|米)?")
NOISE_NUMS = {"1", "2", "3", "4", "5"}        # 序号、“两三句”这类小数字不当成需要出处的数字

# 限制条件与争议：改写时最容易被“顺”掉的那类话
HEDGE = re.compile(r"可能|或许|预计|尚未|尚不|未经|仅|只有|单一来源|据称|宣称|声称|争议|质疑|不确定|待核实|待确认|有待|局限|"
                   r"样本|前提|条件下|并非|不代表|还不能|目前公开资料|未能确认|存疑|分歧|计划|有望|目标是|将于|不一|存疑|仅见于")
# 系统自动补上的限制说明（核对时先去掉再比对出处，不影响出处核对）
HEDGE_NOTE = re.compile(r"（(?:目前只有[^）]{0,20}报道[^）]{0,12}|这一说法存在争议[^）]{0,16}|仅见于[^）]{1,20}|各方数字不一|尚无一手资料印证)）")
UNCERTAIN_HEAD = re.compile(r"不确定|争议|问号|没有答案|小心|局限|证据|待证实|待核实|待确认|存疑")
# 交代了“谁说的”：单一来源的说法写明出处方，也算交代了限制（新闻写作的常规做法）
ATTRIB = re.compile(r"据|显示|援引|测算|统计|盘点|称|表示|指出|发布|披露|报告|通报|介绍|透露")


def doc_flags(research_task: str | None, docs: list[dict]) -> dict:
    """每篇素材当前的可信状态（按检索任务最新一版排序结果，核验后会更新）：
    {n: {"risky": 只有单一来源且不是一手资料（或补充核验没找到印证）, "disputed": 有争议, "label": 来源名}}"""
    rk = ranking.latest(research_task) if research_task else None
    items = {it["id"]: it for it in (rk or {}).get("items") or []}
    out = {}
    for d in docs:
        it = items.get(d.get("id")) or {}
        primary = bool(it.get("primary", d.get("primary")))
        disputed = bool(it.get("disputed", d.get("disputed")))
        single = (it.get("independent") or 1) <= 1 if it else bool(d.get("unverified"))
        unreviewed = bool(d.get("url")) and _self_archive(d["url"])
        out[d["n"]] = {"risky": unreviewed or bool(it.get("unverified", d.get("unverified"))) or (single and not primary),
                       "disputed": disputed, "label": publisher_label(d)}
    # 争议按“句子”判断，不按“文章”：一篇资料里有一条说法有争议，不代表引用它的每句话都有争议
    out["_disputed"] = disputed_facts(research_task)
    return out


def disputed_facts(research_task: str | None) -> list[str]:
    """检索任务主题下，记忆里当前仍有争议的事实（内容）。"""
    if not research_task:
        return []
    t = db.query_one("SELECT topic FROM tasks WHERE id=?", (research_task,))
    if not t or not t.get("topic"):
        return []
    rows = db.query("SELECT content, meta FROM memories WHERE topic=? AND kind='fact' AND "
                    "status IN ('active','stale')", (t["topic"],))
    out = []
    for r in rows:
        m = r["meta"]
        if isinstance(m, str):
            import json
            try:
                m = json.loads(m or "{}")
            except ValueError:
                m = {}
        if (m or {}).get("verify") == "disputed":
            out.append(r["content"])
    return out


SITE_NAMES = {
    "gov.cn": "政府网站", "mct.gov.cn": "文化和旅游部", "people.com.cn": "人民网", "news.cn": "新华网",
    "xinhuanet.com": "新华网", "cctv.com": "央视网", "cctv.cn": "央视网", "chinanews.com.cn": "中国新闻网",
    "sina.com.cn": "新浪", "ifeng.com": "凤凰网", "qq.com": "腾讯网", "163.com": "网易", "sohu.com": "搜狐",
    "thepaper.cn": "澎湃新闻", "zhihu.com": "知乎", "weibo.com": "微博", "xhby.net": "新华报业网",
    "10jqka.com.cn": "同花顺", "caixin.com": "财新", "yicai.com": "第一财经", "jiemian.com": "界面新闻",
    "36kr.com": "36氪", "github.com": "GitHub", "arxiv.org": "arXiv", "huggingface.co": "Hugging Face",
    "reuters.com": "路透社", "bloomberg.com": "彭博社", "nytimes.com": "纽约时报",
    "gmw.cn": "光明网", "stcn.com": "证券时报网", "rfi.fr": "法广", "dw.com": "德国之声", "bbc.com": "BBC",
    "bbc.co.uk": "BBC", "apnews.com": "美联社", "afp.com": "法新社", "cls.cn": "财联社", "nbd.com.cn": "每日经济新闻",
    "bjnews.com.cn": "新京报", "chinadaily.com.cn": "中国日报网", "ce.cn": "中国经济网", "cnr.cn": "央广网",
    "youth.cn": "中国青年网", "huanqiu.com": "环球网", "eastmoney.com": "东方财富网", "stockstar.com": "证券之星",
    "ithome.com": "IT之家", "sspai.com": "少数派", "guancha.cn": "观察者网", "cyol.com": "中青在线",
    "chinanews.com": "中新网", "zaobao.com": "联合早报", "nobelprize.org": "诺贝尔奖官网",
    "zenodo.org": "Zenodo 自存档", "openalex.org": "OpenAlex", "hk01.com": "香港01",
}
# 这些是“怎么找到的”（搜索渠道），不是“谁发布的”；写进正文和参考资料时换成网站名
CHANNEL_SOURCES = {"web_search", "websearch", "official", "web", "search", "gap", "bocha", "tavily"}


def site_name(url: str) -> str:
    from urllib.parse import urlparse
    host = (urlparse(url or "").hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    for i in range(len(parts) - 1):
        k = ".".join(parts[i:])
        if k in SITE_NAMES and k != "gov.cn":
            return SITE_NAMES[k]
    if host.endswith(".gov.cn"):
        return "政府网站"
    return host


# 自媒体号：挂在门户域名下，但不是记者稿（网易号、百家号、搜狐号、头条号、公众号、知乎……）
SELF_MEDIA = re.compile(r"dy\.163\.com|163\.com/dy/|baijiahao\.baidu|sohu\.com/a/|toutiao\.com|mp\.weixin|zhihu|weibo|"
                        r"bilibili|douyin|xiaohongshu|tieba\.baidu|ishare\.ifeng|k\.sina\.com\.cn|"
                        r"cj\.sina\.com\.cn/articles|new\.qq\.com/rain|page\.om\.qq")
SOCIAL_SOURCES = {"zhihu", "weibo", "wechat", "tieba", "xiaohongshu", "douyin", "bilibili", "hn", "reddit", "v2ex"}


def source_tier(d: dict) -> str:
    """来源层级（写进给大模型的素材标签里）：大模型自己分不清“谁说话算数”，要由程序明确告诉它。"""
    from .collect.sources import is_primary
    url = d.get("url") or ""
    if _self_archive(url):
        return "自存档，未经同行评审"
    if d.get("primary") or is_primary(url):
        return "一手/官方"
    if d.get("source_id") in SOCIAL_SOURCES or SELF_MEDIA.search(url):
        return "自媒体/社区，个人说法"
    host = site_name(url)
    if host != (re.sub(r"^www\.", "", (url.split("/")[2] if "//" in url else "")).lower()):
        return "媒体报道"
    return "其他网站"


def publisher_label(d: dict) -> str:
    """正文和“仅见于……”里用的来源名：优先网站/机构名，而不是“全网搜索”这类检索渠道名。"""
    if d.get("url") and _self_archive(d["url"]):
        return "一篇未经同行评审的自存档论文"
    label = (d.get("label") or d.get("source_id") or "").split("，")[0]
    if d.get("url") and (d.get("source_id") in CHANNEL_SOURCES or "搜索" in label or "一手披露" in label
                         or not label):
        return site_name(d["url"]) or label
    return label


def _num_key(sentence: str) -> frozenset:
    """一句话里的“硬数字”，用来识别同一个说法在文中第二次出现。"""
    return frozenset(n for n in numbers(HEDGE_NOTE.sub("", CITE.sub("", sentence))) if n not in NOISE_NUMS)


def _matches_disputed(sentence: str, facts: list[str]) -> bool:
    """这句话是不是在复述某条有争议的事实：文字相近，并且用到了那条事实里的数字。"""
    plain = HEDGE_NOTE.sub("", CITE.sub("", sentence))
    mine = set(numbers(plain)) - NOISE_NUMS
    for f in facts or []:
        fn = set(numbers(f)) - NOISE_NUMS
        if fn and not (mine & fn):
            continue
        if overlap(f, plain) >= 0.35 or overlap(plain, f) >= 0.35:
            return True
    return False


def _risky_kind(cites: list[int], flags: dict, sentence: str = "") -> str | None:
    fl = [flags.get(c) for c in cites if c in flags]
    if not fl:
        return None
    if "_disputed" in flags:
        # 句子级：只有真的在复述有争议的那条说法，才算有争议
        if any(f["disputed"] for f in fl) and _matches_disputed(sentence, flags["_disputed"]):
            return "disputed"
    elif any(f["disputed"] for f in fl):
        return "disputed"
    if all(f["risky"] for f in fl):
        return "single"
    return None


def _hedged(sentence: str, kind: str) -> bool:
    plain = CITE.sub("", sentence)
    if HEDGE.search(plain):
        return True
    # 单一来源的说法，句子里已经交代了“谁说的”（据……、……显示、……称），读者知道这是一家之言
    return kind == "single" and bool(ATTRIB.search(plain))


def hedge_audit(sections: list[dict], flags: dict) -> dict:
    """需要写明限制的说法（引用的出处都只有单一来源、不是一手资料，或有争议）有没有写出限制。
    同一个说法（同一组数字）在文中只需要交代一次：第一次出现写明了，后面复述不再要求。"""
    need, missing, seen = [], [], {}
    for sec in sections:
        if sec.get("auto"):
            continue
        for u, is_analysis in _units(sec.get("md") or ""):
            cites = [int(x) for x in CITE.findall(u)]
            kind = _risky_kind(cites, flags, u) if cites and not is_analysis else None
            if not kind:
                continue
            key = _num_key(u)
            x = {"section": sec["id"], "sentence": u, "kind": kind, "cites": cites}
            if key and key in seen:
                x["repeat"] = True
                if not seen[key]:
                    seen[key] = _hedged(u, kind)
                need.append(x)
                continue
            ok = _hedged(u, kind)
            if key:
                seen[key] = ok
            need.append(x)
            if not ok:
                missing.append(x)
    has_sec = any(UNCERTAIN_HEAD.search(s.get("heading") or "") for s in sections if not s.get("auto"))
    return {"needed": len([x for x in need if not x.get("repeat")]), "missing": len(missing), "items": need,
            "missing_items": missing, "uncertain_section": has_sec}


def _short_claim(sentence: str, limit: int = 46) -> str:
    t = HEDGE_NOTE.sub("", CITE.sub("", sentence)).strip().rstrip("。；;!！")
    if len(t) > limit:
        cut = max(t.rfind(c, 0, limit) for c in "，,；、")
        t = t[:cut] if cut >= 12 else t[:limit] + "…"
    return t


def _note(kind: str, labels: list[str]) -> str:
    if kind == "disputed":
        return "（各方数字不一）"
    return f"（仅见于{labels[0]}）" if len(labels) == 1 else "（尚无一手资料印证）"


def ensure_hedges(sections: list[dict], flags: dict) -> dict:
    """保证“只有单一来源 / 有争议”的说法写明了限制，并且文章里有一处交代还不确定的内容。
    1) 第一次出现、又没交代来源或限制的句子，在出处编号前补一个很短的括注（仅见于××网 / 各方数字不一）；
       同一组数字后面再出现不重复补；
    2) 有需要写明限制的说法、结构里却没有写不确定性的一节：在文末加一小段“尚待确认”，列出（带出处）。
    原地修改 sections；返回 {added: 补了几句, section: 是否加了一节}。幂等：再跑一次不会重复添加。"""
    had_auto = any(x.get("auto") for x in sections)
    sections[:] = [x for x in sections if not x.get("auto")]     # 自动生成的那一节每次按最新核实状态重写
    # 旧版本留下的冗长括注先清掉，按现在的规则重新判断
    for sec in sections:
        sec["md"] = HEDGE_NOTE.sub("", sec.get("md") or "")
    aud = hedge_audit(sections, flags)
    added = 0
    by_sec = {s["id"]: s for s in sections}
    for x in aud["missing_items"]:
        sec = by_sec[x["section"]]
        s0 = x["sentence"]
        if s0 not in (sec.get("md") or ""):
            continue
        labels = list(dict.fromkeys(flags[c]["label"] for c in x["cites"] if c in flags and flags[c]["label"]))
        note = _note(x["kind"], labels)
        m = re.search(r"((?:\[\d{1,2}\])+)\s*([。！？!?；;]?)\s*$", s0)
        new = s0[:m.start()] + note + m.group(1) + m.group(2) if m else s0.rstrip("。") + note + "。"
        sec["md"] = sec["md"].replace(s0, new, 1)
        added += 1
    made = False
    if aud["needed"] and not aud["uncertain_section"] and not by_sec.get("uncertain"):
        seen, lines = set(), []
        for x in aud["items"]:
            if x.get("repeat"):
                continue
            claim = _short_claim(x["sentence"])
            if claim in seen or len(lines) >= 4:
                continue
            seen.add(claim)
            refs = "".join(f"[{c}]" for c in x["cites"])
            labels = list(dict.fromkeys(flags[c]["label"] for c in x["cites"] if c in flags and flags[c]["label"]))
            why = "各方给出的数字不一致" if x["kind"] == "disputed" else \
                (f"目前只见于{labels[0]}" if len(labels) == 1 else "目前只有单一来源")
            lines.append(f"- {claim}，{why}{refs}。")
        if lines:
            # 有意保留的可信度设计：真人新闻稿通常不单列，但这里要让读者一眼看到哪些说法还没有第二个来源
            sections.append({"id": "uncertain", "heading": "尚待确认", "auto": True, "md": "\n".join(lines)})
            made = not had_auto
    return {"added": added, "section": made, "needed": aud["needed"]}


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
    m = re.match(r"[-+−]?([\d,]+(?:\.\d+)?)", n)       # 图表数值可能是负数（如 -12%），或者不是数字
    if not m:
        return False
    raw = m.group(1)
    n = n[m.start(1):]
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


def _url_key(u: str) -> str:
    u = re.sub(r"^https?://(www\.|m\.)?", "", (u or "").strip().lower())
    return u.split("?")[0].split("#")[0].rstrip("/")


def is_excluded(row: dict, exclude: dict | None) -> bool:
    """对标评测用：真人稿（及其转载）不能进素材，否则比的就是“抄得像不像”。
    exclude = {"urls": [...], "texts": [真人稿正文]}；同一 URL，或正文一半以上和真人稿重合（转载/通稿），都排除。"""
    if not exclude or not row:
        return False
    if _url_key(row.get("url")) in {_url_key(u) for u in exclude.get("urls") or []}:
        return True
    head = (row.get("text") or row.get("summary") or "")[:2000]
    if len(head) < 60:
        return False
    for t in exclude.get("texts") or []:
        if t and (overlap(head, t) >= 0.5 or overlap(t[:2000], head) >= 0.5):
            return True
    return False


def select_material(research_task: str, skill: dict, extra_ids: list[str] | None = None,
                    extra_why: dict | None = None, exclude: dict | None = None) -> dict:
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
    if exclude:
        rows0 = _doc_rows(order[:150])
        order = [i for i in order if not is_excluded(rows0.get(i), exclude)]
    chosen, notes = _apply_reference_rules(order, skill, int(mat.get("max_docs") or 10), why)
    for e in extra_ids or []:
        if exclude and is_excluded(_doc_rows([e]).get(e), exclude):
            continue
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
                     "primary": bool((d["meta"] or {}).get("primary")) and not _self_archive(d["url"]), "value": it.get("value"),
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
    if doc.get("source_id") in CHANNEL_SOURCES and doc.get("url"):
        name = site_name(doc["url"]) or name          # “全网搜索”是检索渠道，读者要看的是哪家网站发的
    if doc.get("url") and _self_archive(doc["url"]):
        name = "自存档论文，未经同行评审"
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
        for s_full, is_analysis in _units(sec.get("md") or ""):
            s = HEDGE_NOTE.sub("", s_full)           # 系统补上的限制说明不参与出处比对
            cites = [int(x) for x in CITE.findall(s)]
            if sec.get("auto"):                     # 系统生成的“还不确定的”一节：条目摘自正文已核对过的句子
                used.update(c for c in cites if c in by_n)
                continue
            nums = numbers(s)
            it = {"section": sec["id"], "sentence": s_full, "cites": cites, "numbers": nums, "analysis": is_analysis}
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
                v = verdicts.get(s_full) or verdicts.get(s) or {}
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


SEMANTIC_PROMPT = """你是事实核对员。逐条判断“文章句子”能否由给出的“出处原文”支持。
标准：允许改写、概括、合并多句；但主体（谁）、动作（做了什么）、数字、时间、范围（全国/某地、全部/部分）、
因果方向和确定程度（“计划”不能写成“已经”，“可能”不能写成“将会”）必须和原文一致。原文没说的推断不算支持。
只看给出的原文，不要用你自己知道的知识补充；原文不够判断就选 unsupported。

例子（不是本次要判断的内容）：
- 原文“预计明年推出”→句子“明年将推出”：partial（把预计写成了确定）
- 原文“比2024年增长19%”→句子“同比增长19%”：unsupported（比较基准变了）
- 原文“北京地区销量增长30%”→句子“销量增长30%”：unsupported（范围变了）

{items}
只输出 JSON：{{"results": [{{"i": 1, "verdict": "supported", "evidence": "原文里支持它的那一小段，原样复制", "reason": "不超过 20 字"}}]}}
verdict 只能是 supported（支持）/ partial（大体支持，有轻微引申）/ unsupported（不支持或原文没有）。
evidence：supported 和 partial 必须给，从出处原文里原样复制 10-60 字，不能改字、不能翻译（英文原文就抄英文），
中间要省略的用“……”隔开；unsupported 留空。"""


def _ev_norm(s: str) -> str:
    """比对用的规范化：全角半角统一（NFKC）、小写、去掉空白和标点。"""
    import unicodedata
    s = unicodedata.normalize("NFKC", s or "").lower()
    return re.sub(r"[\s\W_]+", "", s)


def _segment_match(seg: str, text: str) -> float:
    """一段依据在原文里的最好匹配程度（0-1）：先找完全相同的子串；找不到就在原文里定位和它最像的一个
    连续窗口，算两者的相似度。必须是“连续的一段话”像，不能是把原文各处的字拼起来。"""
    import difflib
    if not seg:
        return 0.0
    if seg in text:
        return 1.0
    sm = difflib.SequenceMatcher(None, text, seg, autojunk=False)
    best = 0.0
    # 以依据里的几段 4 字片段为锚点，在原文中找到出现位置，再比较锚点附近等长的窗口
    anchors = {seg[k:k + 4] for k in range(0, max(1, len(seg) - 3), max(1, len(seg) // 6))}
    starts = set()
    for a in anchors:
        pos = text.find(a)
        while pos >= 0 and len(starts) < 40:
            starts.add(max(0, pos - seg.find(a)))
            pos = text.find(a, pos + 1)
    for st0 in starts:
        win = text[max(0, st0 - 4): st0 + len(seg) + 4]
        sm.set_seqs(win, seg)
        best = max(best, sm.ratio())
        if best >= 0.95:
            break
    return round(best, 3)


def evidence_match(evidence: str, texts: list[str], sentence: str = "") -> dict:
    """核对大模型给的“依据”：
    1) 依据必须是原文里连续的一段（容忍全半角、标点、空格、个别字差异：最佳窗口相似度 ≥ 0.85）；
       依据里用“……”省略中间的，拆成几段分别核对，每段都要找得到；
    2) 依据要和句子相关：句子里有关键数字的，依据里至少要出现其中一个（防止抄一段真实但无关的原文）。
    返回 {ok, why, score}。"""
    raw = (evidence or "").strip()
    segs = [_ev_norm(x) for x in re.split(r"……|\.\.\.|…", raw)]
    segs = [x for x in segs if len(x) >= 4]
    if not segs or max(len(x) for x in segs) < 6:
        return {"ok": False, "why": "没有给出依据", "score": 0.0}
    text = _ev_norm("\n".join(t or "" for t in texts))
    scores = [_segment_match(x, text) for x in segs]
    if min(scores) < 0.85:
        return {"ok": False, "why": "依据在原文里找不到", "score": min(scores)}
    nums = [n for n in numbers(CITE.sub("", sentence or "")) if n not in NOISE_NUMS]
    if nums and not any(num_in(n, _norm(raw)) for n in nums):
        return {"ok": False, "why": "依据和句子的数字对不上", "score": min(scores)}
    return {"ok": True, "why": "", "score": min(scores)}


def evidence_ok(evidence: str, texts: list[str], sentence: str = "") -> bool:
    return evidence_match(evidence, texts, sentence)["ok"]


def checked_verdict(r: dict, texts: list[str], sentence: str = "") -> dict:
    """语义核对结果过一道程序检查：判为支持、却给不出原文里真实存在且和句子相关的依据的，降为不支持。
    降级的都记下来（claimed、evidence、why），可以在判断校准里请人确认这道检查有没有误杀。"""
    v = r.get("verdict")
    reason = str(r.get("reason") or "")[:40]
    if v in ("supported", "partial"):
        m = evidence_match(str(r.get("evidence") or ""), texts, sentence)
        if not m["ok"]:
            return {"verdict": "unsupported", "reason": f"大模型说支持，但{m['why']}", "claimed": v,
                    "evidence": str(r.get("evidence") or "")[:120], "evidence_score": m["score"]}
        return {"verdict": v, "reason": reason, "evidence": str(r.get("evidence") or "")[:120]}
    return {"verdict": v, "reason": reason}


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


def undash(text: str) -> str:
    """破折号“——”是 AI 稿最显眼的节奏标记之一：换成逗号（引号里的原话不动）。"""
    out, depth = [], 0
    parts = re.split(r"([“”「」])", text or "")
    for p in parts:
        if p in "“「":
            depth += 1
        elif p in "”」":
            depth = max(0, depth - 1)
        elif depth == 0 and "——" in p:
            p = re.sub(r"\s*——+\s*", "，", p)
            p = re.sub(r"，([，。；：！？])", r"\1", p)
            p = re.sub(r"([。；：！？\n])，", r"\1", p)
        out.append(p)
    return "".join(out)


def clean_title(t: str | None) -> str:
    """资料标题去掉话题标签符号、站名尾巴等噪声（“共庆华诞#”→“共庆华诞”）。"""
    t = re.sub(r"#+", "", t or "").strip()
    t = re.sub(r"\s*[-_|｜]\s*(新浪|凤凰|网易|搜狐|腾讯)[^-_|｜]{0,8}$", "", t)
    return t.strip(" ·-_|") or (t or "")


def clean_heading(h: str | None) -> str:
    """小标题里不放“【分析】”这类标记，也不要“先说结论：”这类模板前缀。"""
    h = (h or "").replace(ANALYSIS, "").strip()
    h = re.sub(r"^(先说结论|结论先行|一句话总结|我们的判断|核心结论|来龙去脉|各方怎么看|三个概念|背景)\s*[:：]\s*", "", h)
    return h.strip()


# 规则能识别的“AI 腔”句式：自然化改写时点名这些句子
TROPE_PATTERNS = [
    (re.compile(r"不(?:只|仅)?是[^。！？]{1,30}?而是"), "“不是……而是……”对举"),
    (re.compile(r"不再是[^。！？]{1,30}?(?:而是|，)"), "“不再是……”"),
    (re.compile(r"(?:把|将)[^。！？]{1,30}?放在一起看"), "“把……放在一起看”"),
    (re.compile(r"这意味着|意味着"), "“这意味着”"),
    (re.compile(r"换挡|结构性变化|新常态|拐点"), "抽象大词"),
    (re.compile(r"大白话|换句话说|简单来说|说到底"), "口头禅式转述"),
    (re.compile(r"引用时(?:宜|应|必须|要)|写作时(?:应|宜)|读的时候(?:要|请)|需要注意的是|值得注意的是"), "对作者/读者的说教"),
    (re.compile(r"真正(?:值得|重要|的增量)|更重要的是|关键在于|核心在于"), "强调套话"),
    (re.compile(r"[——]{2}|——"), "破折号制造节奏"),
]


def trope_sentences(md: str) -> list[dict]:
    out = []
    for p in re.split(r"(?<=[。！？!?])|\n+", md or ""):
        p = p.strip()
        if len(p) < 6 or p.startswith("|"):
            continue
        for rx, why in TROPE_PATTERNS:
            if rx.search(p):
                out.append({"sentence": p, "why": why})
                break
    return out


def assemble(title: str, sections: list[dict], docs: list[dict], used: set[int], names: dict | None = None,
             note: str | None = None, cover_md: str | None = None) -> str:
    parts = [f"# {title}", ""]
    if cover_md:
        parts += [cover_md, ""]
    parts += ["> 阅读说明：句末的 [n] 是出处，对应文末参考资料；以“【分析】”开头的段落是本文的分析判断，不是来源原话。"]
    if note:
        parts += ["", f"> {note}"]
    for s in sections:
        parts += ["", f"## {clean_heading(s['heading']) or s['heading']}", "", (s.get("md") or "").strip()]
    refs = references_md(sorted(docs, key=lambda d: d["n"]), used, names)
    if refs:
        parts += ["", "## 参考资料", "", refs]
    return "\n".join(parts).strip() + "\n"


# 写作说明混进正文（大模型把给它的指令当成内容写了出来）：这类句子直接删掉
META_TEXT = re.compile(r"记忆中的事实|本节(暂不|不再|将|只|不|所)|本文所依据的(材料|素材)|所依据的材料中|暂不引用|素材(中|里|编号)|资料编号|出处编号|核对(发现|结果)|上一稿|"
                       r"按(核对|要求)改写|根据(提示|要求)|（来源：记忆）|未能确认其具体出处|争议标记|"
                       r"^(?:【分析】)?\s*(?:引用时|写作时|读的时候)(?:宜|应|要|必须|请)")


# 句子后半截是给作者/读者的提醒（“……，引用时宜以官方口径为准”）：只删这半截，前半截的事实保留
META_CLAUSE = re.compile(r"[，,；;]\s*(?:引用时|写作时|转述时|读的时候|使用时)[^。！？]*(?=[。！？])")


def strip_meta(md: str) -> tuple[str, list[str]]:
    removed = [m.group(0).lstrip("，,；; ") for m in META_CLAUSE.finditer(md or "")]
    md = META_CLAUSE.sub("", md or "")
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

