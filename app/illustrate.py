"""配图：选图、质检、放置、示意图、成稿检查。图片出现在读者需要它的地方。

选图范围：只用素材资料自带的配图（检索阶段已下载到本地，带图注、图片前后的正文、尺寸、感知哈希）。
质检（不依赖大模型）：
  清晰度   拉普拉斯边缘方差，过低 = 模糊
  尺寸     太小放大后会糊；封面要求横图且足够宽
  压缩     每像素字节数过低 = 压缩严重、有色块
  裁切     URL 带缩略图/裁切参数，或尺寸像缩略图
  重复     与已选图片感知哈希接近（同一张图的不同尺寸、转载）
放置：图注 + 图片前后正文 与 各段落的相似度；段落引用了这张图所在的资料额外加分；
      每节最多一张，优先放在第一次提到图中内容的段落之后。配置了大模型时再确认图文是否相符。
原图不够：用正文里已核对过的数据画示意图（时间线 / 柱状图，SVG），图注写明“示意图”和数据出处；
      柱状图的每个数字都必须能在出处原文里找到，否则不画。
成稿检查：占位符残留、缺图、重复图、图注缺失。
"""
from __future__ import annotations

import hashlib
import html
import io
import re
from pathlib import Path

from . import config, db, writing
from .collect.images import hamming

THUMB_URL = re.compile(r"thumb|_\d{2,3}x\d{2,3}\b|/\d{2,3}x\d{2,3}/|resize[=/,]\d{2,3}\b|crop|w_\d{2,3}\b|imageView2?/\d/w/\d{2,3}\b", re.I)
PLACEHOLDER = re.compile(r"\{\{img:[^}]*\}\}|【?(此处|这里)(插入|放|配)(一张)?(图|图片)】?|\[图片?\d*\]|（图略）|TODO", re.I)


# ---------------------------------------------------------------- 质检

def quality(local_path: str, width: int | None, height: int | None, url: str = "") -> dict:
    """返回 {ok, sharpness, issues: [...], warn: [...]}。"""
    from PIL import Image, ImageFilter, ImageStat
    issues, warn = [], []
    p = config.DATA_DIR / local_path
    try:
        data = p.read_bytes()
        im = Image.open(io.BytesIO(data))
        im.load()
    except (OSError, ValueError) as e:
        return {"ok": False, "sharpness": None, "issues": [f"本地文件不可用：{e}"], "warn": []}
    w, h = im.size
    g = im.convert("L")
    if max(w, h) > 640:
        g = g.resize((640, max(1, int(640 * h / w))) if w >= h else (max(1, int(640 * w / h)), 640))
    lap = g.filter(ImageFilter.Kernel((3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0], 1, 128))
    sharp = round(ImageStat.Stat(lap).var[0], 1)
    # 文字图表/信息图：大面积接近白色的底 + 很多细小笔画。这类图缩小后字看不清，不适合当封面
    hist = g.histogram()
    white = sum(hist[236:]) / max(1, sum(hist))
    chartlike = white > 0.42 and sharp > 300
    small = im.convert("RGB").resize((96, max(1, int(96 * h / w))))
    colors = len(small.getcolors(maxcolors=96 * 96 + 1) or [])
    if sharp < 25:
        issues.append(f"模糊（清晰度 {sharp}，低于 25）")
    elif sharp < 60:
        warn.append(f"略模糊（清晰度 {sharp}）")
    if w < 480 or h < 270:
        issues.append(f"尺寸偏小 {w}×{h}，放大后会糊")
    elif w < 900:
        warn.append(f"宽 {w} 像素，在宽屏上会按原尺寸显示（不拉伸）")
    if chartlike and w < 1200:
        warn.append("文字较多的图表，分辨率不高时小字可能看不清（点图片可看原图）")
    bpp = len(data) / max(1, w * h)
    if (im.format or "").upper() in ("JPEG", "WEBP") and bpp < 0.02:
        warn.append(f"压缩较重（{bpp:.3f} 字节/像素）")
    if THUMB_URL.search(url or ""):
        issues.append("链接带缩略图/裁切参数，可能不是完整原图")
    if w / h > 3.2 or h / w > 2.2:
        issues.append(f"宽高比 {w}:{h} 过于极端，可能是长条或被裁切")
    return {"ok": not issues, "sharpness": sharp, "issues": issues, "warn": warn, "w": w, "h": h,
            "chartlike": chartlike, "white": round(white, 2), "colors": colors}


# ---------------------------------------------------------------- 候选与放置

def candidates(doc_ids: list[str]) -> list[dict]:
    if not doc_ids:
        return []
    marks = ",".join("?" * len(doc_ids))
    rows = db.query(f"SELECT i.*, d.title AS doc_title, d.source_id, d.url AS doc_url FROM images i "
                    f"JOIN documents d ON d.id=i.doc_id WHERE i.doc_id IN ({marks}) AND i.status='kept' "
                    f"AND i.local_path IS NOT NULL ORDER BY i.position", doc_ids)
    return rows


def _img_text(im: dict) -> str:
    return " ".join(x for x in (im.get("caption"), im.get("context_text")) if x)


def paragraphs(md: str) -> list[str]:
    return [p for p in re.split(r"\n\s*\n", md or "") if p.strip()]


def score(im: dict, para: str, cites_doc: bool) -> float:
    t = _img_text(im)
    s = writing.overlap(t, para) if t else 0.0          # 图注/上下文 在段落里出现的比例
    s2 = writing.overlap(para, t) if t else 0.0
    nums = set(writing.numbers(t)) & set(writing.numbers(para))
    return round(0.6 * max(s, s2) + 0.1 * len(nums) + (0.25 if cites_doc else 0.0), 3)


def place(cands: list[dict], sections: list[dict], docs: list[dict], need: int,
          min_score: float = 0.2) -> tuple[list[dict], list[dict]]:
    """给正文选图并定位。返回 (figures, rejected)。每节最多一张；同一张图（感知哈希相近）只用一次。"""
    n_of = {d["id"]: d["n"] for d in docs}
    rejected, pool = [], []
    for im in cands:
        q = im.get("quality") or quality(im["local_path"], im.get("width"), im.get("height"), im.get("url"))
        im["quality"] = q
        if not q["ok"]:
            rejected.append({"image": im["id"], "url": im["url"], "doc": im["doc_title"], "why": "；".join(q["issues"])})
            continue
        pool.append(im)
    options = []
    for im in pool:
        n = n_of.get(im["doc_id"])
        for si, sec in enumerate(sections):
            for pi, para in enumerate(paragraphs(sec.get("md"))):
                if para.lstrip().startswith("|"):
                    continue
                cites = n is not None and f"[{n}]" in para
                # 分辨率加分：同样相关时优先清晰的大图（最多 +0.08）
                sc = round(score(im, para, cites) + min(im["quality"]["w"], 1600) / 20000, 3)
                options.append((sc, im, si, pi, cites))
    options.sort(key=lambda x: -x[0])
    figs, used_img, used_sec, hashes = [], set(), set(), []
    used_para = {}
    # 第一轮：每节最多一张；第二轮（还不够时）：同一节可再放一张，但要隔开至少 2 段，门槛略降
    for thr, relaxed in [(min_score, False), (min_score * 0.75, True)]:
        for sc, im, si, pi, cites in options:
            if len(figs) >= need:
                break
            if im["id"] in used_img or sc < thr:
                continue
            if si in used_sec and (not relaxed or any(abs(pi - q) < 2 for q in used_para.get(si, []))):
                continue
            if im.get("phash") and any(hamming(im["phash"], h) <= 6 for h in hashes):
                rejected.append({"image": im["id"], "url": im["url"], "doc": im["doc_title"], "why": "与已选图片近似重复"})
                used_img.add(im["id"])
                continue
            used_img.add(im["id"])
            used_sec.add(si)
            used_para.setdefault(si, []).append(pi)
            if im.get("phash"):
                hashes.append(im["phash"])
            figs.append({"kind": "photo", "image_id": im["id"], "src": "/media/" + im["local_path"], "url": im["url"],
                         "section": sections[si]["id"], "para": pi, "score": sc, "doc_id": im["doc_id"],
                         "doc_title": im["doc_title"], "source_id": im["source_id"], "doc_url": im["doc_url"],
                         "orig_caption": im.get("caption"), "license": im.get("license"),
                         "w": im["quality"]["w"], "h": im["quality"]["h"], "sharpness": im["quality"]["sharpness"],
                         "warn": im["quality"]["warn"],
                         "why": f"图注/上下文与这段的相似度 {sc:.2f}" + ("，且这段引用了图片所在的资料" if cites else "")})
    for im in pool:
        if im["id"] not in used_img:
            best = max((o[0] for o in options if o[1] is im), default=0)
            rejected.append({"image": im["id"], "url": im["url"], "doc": im["doc_title"],
                             "why": f"和正文各段都对不上（最高相似度 {best:.2f}）" if best < min_score * 0.75
                             else f"正文图已够数（{need} 张），或相关段落附近已有图"})
    return figs, rejected


def pick_cover(cands: list[dict], used: set, topic_terms: list[str]) -> dict | None:
    """封面：横图（宽高比 1.3-2.4）、宽度 ≥ 960、清晰、不是文字图表，和主题词相关度最高；正文已用的图（及其近似图）不当封面。"""
    best, best_s = None, -1
    terms = " ".join(topic_terms)
    used_hash = [c["phash"] for c in cands if c["id"] in used and c.get("phash")]
    for im in cands:
        q = im.get("quality") or quality(im["local_path"], im.get("width"), im.get("height"), im.get("url"))
        if not q["ok"] or im["id"] in used or q["w"] < 960 or not 1.3 <= q["w"] / q["h"] <= 2.4 or q.get("chartlike"):
            continue      # 封面：横版照片类、宽 ≥ 960；满是小字的图表不当封面
        if im.get("phash") and any(hamming(im["phash"], h) <= 6 for h in used_hash):
            continue
        s = writing.overlap(terms, _img_text(im) + (im.get("doc_title") or "")) + min(q["w"], 1600) / 4000
        if s > best_s:
            best, best_s = im, s
    return best


# ---------------------------------------------------------------- 示意图（SVG）

FONT = "-apple-system, 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'Noto Sans CJK SC', sans-serif"


def _wrap(text: str, width_chars: int) -> list[str]:
    out, cur, n = [], "", 0
    for ch in text:
        w = 1 if ord(ch) > 0x2E80 else 0.55
        if n + w > width_chars:
            out.append(cur)
            cur, n = "", 0
        cur += ch
        n += w
    if cur:
        out.append(cur)
    return out


def _save_svg(svg: str) -> str:
    d = config.IMAGE_DIR / "gen"
    d.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha1(svg.encode()).hexdigest()[:16] + ".svg"
    (d / name).write_text(svg, encoding="utf-8")
    return str((d / name).relative_to(config.DATA_DIR))


def _badge(x, y, text="示意图"):
    return (f'<rect x="{x}" y="{y}" width="{14 * len(text) + 16}" height="22" rx="4" fill="#fff4e5" stroke="#d98a2b"/>'
            f'<text x="{x + 8}" y="{y + 15}" font-size="13" fill="#a35d00">{html.escape(text)}</text>')


def timeline_svg(title: str, events: list[dict], source_note: str) -> str:
    w, row = 720, 64
    h = 96 + row * len(events) + 40
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" font-family="{FONT}">',
             f'<rect width="{w}" height="{h}" fill="#fbfaf7"/>',
             f'<text x="24" y="40" font-size="20" font-weight="700" fill="#1f1c18">{html.escape(title)}</text>', _badge(w - 100, 22),
             f'<line x1="132" y1="72" x2="132" y2="{72 + row * len(events) - 20}" stroke="#c9c1b4" stroke-width="2"/>']
    for i, e in enumerate(events):
        y = 84 + i * row
        parts.append(f'<circle cx="132" cy="{y}" r="6" fill="#c0462d"/>')
        parts.append(f'<text x="116" y="{y + 5}" font-size="14" text-anchor="end" fill="#6b6357">{html.escape(e["date"])}</text>')
        for j, ln in enumerate(_wrap(e["text"], 30)[:2]):
            parts.append(f'<text x="150" y="{y + 5 + j * 20}" font-size="15" fill="#1f1c18">{html.escape(ln)}</text>')
    parts.append(f'<text x="24" y="{h - 16}" font-size="12" fill="#8a8273">{html.escape(source_note)}</text></svg>')
    return "".join(parts)


def bar_svg(title: str, unit: str, items: list[dict], source_note: str) -> str:
    w, row = 720, 46
    h = 100 + row * len(items) + 36
    vmax = max(i["value"] for i in items) or 1
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" font-family="{FONT}">',
             f'<rect width="{w}" height="{h}" fill="#fbfaf7"/>',
             f'<text x="24" y="40" font-size="20" font-weight="700" fill="#1f1c18">{html.escape(title)}</text>', _badge(w - 100, 22)]
    for i, it in enumerate(items):
        y = 76 + i * row
        bw = int(420 * it["value"] / vmax)
        parts.append(f'<text x="200" y="{y + 20}" font-size="14" text-anchor="end" fill="#3b362f">{html.escape(it["label"][:14])}</text>')
        parts.append(f'<rect x="212" y="{y + 4}" width="{max(bw, 2)}" height="24" rx="3" fill="#2c5d8a"/>')
        parts.append(f'<text x="{220 + bw}" y="{y + 21}" font-size="14" fill="#1f1c18">{html.escape(_fmt(it["value"]) + unit)}</text>')
    parts.append(f'<text x="24" y="{h - 14}" font-size="12" fill="#8a8273">{html.escape(source_note)}</text></svg>')
    return "".join(parts)


def cover_svg(title: str, subtitle: str) -> str:
    w, h = 1200, 630
    lines = _wrap(title, 16)[:3]
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" font-family="{FONT}">',
             '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#1f2a38"/>'
             '<stop offset="1" stop-color="#7a2e1f"/></linearGradient></defs>',
             f'<rect width="{w}" height="{h}" fill="url(#g)"/>',
             '<circle cx="1040" cy="120" r="220" fill="#ffffff" opacity="0.05"/><circle cx="1120" cy="560" r="160" fill="#ffffff" opacity="0.04"/>']
    y0 = 250 - (len(lines) - 1) * 38
    for i, ln in enumerate(lines):
        parts.append(f'<text x="80" y="{y0 + i * 76}" font-size="60" font-weight="700" fill="#ffffff">{html.escape(ln)}</text>')
    parts.append(f'<text x="80" y="{y0 + len(lines) * 76 + 20}" font-size="26" fill="#f1d9c9">{html.escape(subtitle)}</text>')
    parts.append(_badge(80, 560, "示意封面：没有合适的原始图片，按标题生成"))
    parts.append("</svg>")
    return "".join(parts)


def numbers_svg(title: str, items: list[dict], source_note: str) -> str:
    """关键数字卡片：每格一个数字 + 一句说明 + 出处编号。数字全部来自已核对的句子。"""
    cols = 2
    rows = (len(items) + 1) // cols
    w, cw, ch = 720, 336, 118
    h = 80 + rows * (ch + 12) + 30
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" font-family="{FONT}">',
             f'<rect width="{w}" height="{h}" fill="#fbfaf7"/>',
             f'<text x="24" y="40" font-size="20" font-weight="700" fill="#1f1c18">{html.escape(title)}</text>', _badge(w - 100, 22)]
    for k, it in enumerate(items):
        x, y = 24 + (k % cols) * (cw + 24), 64 + (k // cols) * (ch + 12)
        parts.append(f'<rect x="{x}" y="{y}" width="{cw}" height="{ch}" rx="8" fill="#ffffff" stroke="#e2dcd2"/>')
        parts.append(f'<text x="{x + 18}" y="{y + 46}" font-size="32" font-weight="700" fill="#c0462d">{html.escape(it["num"])}</text>')
        for j, ln in enumerate(_wrap(it["label"], 19)[:2]):
            parts.append(f'<text x="{x + 18}" y="{y + 76 + j * 20}" font-size="14" fill="#3b362f">{html.escape(ln)}</text>')
    parts.append(f'<text x="24" y="{h - 12}" font-size="12" fill="#8a8273">{html.escape(source_note)}</text></svg>')
    return "".join(parts)


def number_items(check_items: list[dict], limit: int = 4) -> list[dict]:
    """关键数字：取“出处支持”的句子里的数字（字面核对通过，数字一定在出处里），每句一个，带句子的一小段做说明。"""
    out, seen = [], set()
    for it in check_items:
        if it["status"] != "supported" or it.get("analysis") or not it["numbers"]:
            continue
        nums = [n for n in it["numbers"] if not re.fullmatch(r"(19|20)\d{2}年?|\d{1,2}月|\d{1,2}日", n)]
        if not nums:
            continue
        num = max(nums, key=len)
        if num in seen:
            continue
        sent = writing.CITE.sub("", it["sentence"]).strip("。")
        # 说明文字：数字所在的那个分句（去掉数字本身），读者看得出这个数字是什么
        clause = next((c for c in re.split(r"[，,；;：:]", sent) if num.replace(" ", "") in c.replace(" ", "")), sent)
        label = clause.replace(num, "").strip(" ，,、") or clause
        seen.add(num)
        out.append({"num": num, "label": label[:26], "cites": it["cites"], "section": it["section"]})
        if len(out) >= limit:
            break
    return out


def _fmt(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


DATE = re.compile(r"((?:19|20)\d{2})\s*年\s*(\d{1,2})\s*月(?:\s*(\d{1,2})\s*日)?")


def timeline_events(check_items: list[dict], sections: list[dict]) -> list[dict]:
    """从“出处支持 / 改写一致”的句子里取带日期的事件，按时间排序（同一天只取一条）。"""
    seen, out = set(), []
    for it in check_items:
        if it["status"] not in ("supported", "semantic") or it.get("analysis"):
            continue
        m = DATE.search(it["sentence"])
        if not m:
            continue
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
        key = (y, mo, d)
        if key in seen:
            continue
        seen.add(key)
        text = writing.CITE.sub("", it["sentence"][m.end():]).strip("，,：: 。")
        text = re.sub(r"^(起|以来|前后|，)", "", text).strip("，,")
        if len(text) < 6:
            text = writing.CITE.sub("", it["sentence"]).strip("。")
        out.append({"date": f"{y}.{mo:02d}" + (f".{d:02d}" if d else ""), "sort": key, "text": text[:58],
                    "cites": it["cites"], "section": it["section"]})
    out.sort(key=lambda e: e["sort"])
    return out[:7]


INFO_WORDS = re.compile(r"图表|数据|统计|走势|趋势|对比|占比|份额|销量|出货|增速|同比|环比|排名|榜单|示意|流程|架构|结构|时间线|路线图|"
                        r"参数|配置|价格|售价|规格|财报|营收|利润|测评|评测|跑分|成绩|得分|分布|曲线|柱状|饼图|表\s*\d|图\s*\d|"
                        r"来源[:：]|资料来源|数据来源|单位[:：]|source|chart|data", re.I)
PHOTO_WORDS = re.compile(r"图\s*/\s*IC|视觉中国|东方IC|新华社|中新社|摄影|摄$|记者\s*\S{2,4}\s*摄|供图|图虫|Getty|Shutterstock|"
                         r"现场|发布会|亮相|合影|展台|外观|实拍|海报|封面", re.I)


def classify(im: dict, q: dict) -> dict:
    """这张图对读者有没有信息量：图表/信息图/流程图/参数表/数据截图 = info；新闻现场照、图库照片 = photo。
    看两类信号：图注和图片前后正文里的词，以及图片本身（图表通常白底多、颜色少、笔画细密；照片颜色多）。"""
    text = f"{im.get('caption') or ''} {im.get('context_text') or ''}"
    score, why = 0, []
    if INFO_WORDS.search(text):
        score += 2
        why.append(f"图注/上下文提到「{INFO_WORDS.search(text).group(0)}」")
    if q.get("chartlike"):
        score += 2
        why.append("白底、笔画细密，像图表")
    elif (q.get("colors") or 9999) < 900 and (q.get("white") or 0) > 0.25:
        score += 1
        why.append("颜色少、有大面积底色，像图形")
    if PHOTO_WORDS.search(text):
        score -= 3
        why.append(f"图注像新闻照片（「{PHOTO_WORDS.search(text).group(0)}」）")
    if (q.get("colors") or 0) > 3000 and not q.get("chartlike"):
        score -= 1
        why.append("颜色丰富，像照片")
    return {"info": score >= 2, "score": score, "why": "；".join(why) or "没有明显的图表特征"}


CHARTS_PROMPT = """下面是一篇文章引用的资料中带数字的句子（[n] 是出处编号）。找出最多 2 组适合画图、能帮读者看清数据的同类数字：
- bar：3-8 个可比较的数值，单位相同（例如不同车型的价格、不同公司的销量、不同机构的份额）
- line：同一指标在 3-12 个时间点的数值（例如 2022-2026 年各年销量、各月价格）
数值必须原样来自句子（可以去掉千分位逗号），不要换算、估计或补全；凑不出就返回空数组。
{lines}
只输出 JSON：{{"charts": [{{"kind": "bar", "title": "图题（不超过 18 字，说明比的是什么）", "unit": "万元",
  "items": [{{"label": "不超过 10 字", "value": 32.99}}], "refs": [3], "section": "最相关的节 id（可选：{sections}）",
  "takeaway": "这张图说明了什么（一句话，不超过 30 字，只能是图中数字直接体现的结论）"}}]}}"""


def numeric_lines(docs: list[dict], check_items: list[dict], limit: int = 40) -> str:
    """给大模型找图表数据用：正文里核对通过的句子 + 出处原文中含 2 个以上数字的句子。"""
    out, seen = [], set()
    for it in check_items:
        if it["status"] in ("supported", "semantic") and len(it["numbers"]) >= 1:
            t = f"[{','.join(map(str, it['cites']))}] {writing.CITE.sub('', it['sentence'])[:180]}（节 {it['section']}）"
            if t not in seen:
                seen.add(t)
                out.append(t)
    for d in docs:
        for snt in writing.sentences(d["text"])[:200]:
            if len(writing.numbers(snt)) >= 2 and len(snt) <= 220:
                t = f"[{d['n']}] {snt}"
                if t not in seen:
                    seen.add(t)
                    out.append(t)
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    return "\n".join(out[:limit])


def line_svg(title: str, unit: str, items: list[dict], source_note: str) -> str:
    w, h = 720, 380
    x0, x1, y0, y1 = 70, 690, 300, 80
    vals = [i["value"] for i in items]
    vmin, vmax = min(vals), max(vals)
    lo = 0 if vmin >= 0 and vmin < vmax * 0.6 else vmin - (vmax - vmin) * 0.15
    hi = vmax + (vmax - lo) * 0.12 or 1
    X = lambda k: x0 + (x1 - x0) * (k / max(1, len(items) - 1))  # noqa: E731
    Y = lambda v: y0 - (y0 - y1) * ((v - lo) / ((hi - lo) or 1))  # noqa: E731
    pts = " ".join(f"{X(k):.1f},{Y(i['value']):.1f}" for k, i in enumerate(items))
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" font-family="{FONT}">',
             f'<rect width="{w}" height="{h}" fill="#fbfaf7"/>',
             f'<text x="24" y="40" font-size="20" font-weight="700" fill="#1f1c18">{html.escape(title)}</text>', _badge(w - 100, 22),
             f'<line x1="{x0}" y1="{y0}" x2="{x1}" y2="{y0}" stroke="#c9c1b4"/>',
             f'<polyline points="{pts}" fill="none" stroke="#2c5d8a" stroke-width="3"/>']
    for k, i in enumerate(items):
        parts.append(f'<circle cx="{X(k):.1f}" cy="{Y(i["value"]):.1f}" r="4.5" fill="#2c5d8a"/>')
        parts.append(f'<text x="{X(k):.1f}" y="{Y(i["value"]) - 10:.1f}" font-size="13" text-anchor="middle" fill="#1f1c18">'
                     f'{html.escape(_fmt(i["value"]) + unit)}</text>')
        parts.append(f'<text x="{X(k):.1f}" y="{y0 + 22}" font-size="13" text-anchor="middle" fill="#6b6357">{html.escape(i["label"][:8])}</text>')
    parts.append(f'<text x="24" y="{h - 16}" font-size="12" fill="#8a8273">{html.escape(source_note)}</text></svg>')
    return "".join(parts)


CHART_PROMPT = """下面是一篇文章里已经核对过出处的句子（[n] 是出处编号）。如果其中有适合画成一张简单柱状图的同类数字
（3-6 个可比较的数值，单位相同，例如不同时期的占比、不同对象的数值），给出图表数据；否则 kind 填 none。
数值必须原样来自句子，不要换算、不要估计。
{lines}
只输出 JSON：{{"kind": "bar", "title": "图题（不超过 18 字）", "unit": "%", "items": [{{"label": "不超过 10 字", "value": 30}}],
  "refs": [3], "section": "句子所在的节 id"}}"""


def verify_chart(spec: dict, docs: list[dict]) -> str | None:
    """柱状图的每个数值都必须能在引用的出处原文里找到。返回问题描述，没问题返回 None。"""
    by_n = {d["n"]: writing._norm(d["text"]) for d in docs}
    refs = [int(r) for r in spec.get("refs") or [] if str(r).isdigit() and int(r) in by_n]
    if not refs:
        return "没有给出有效的出处编号"
    items = spec.get("items") or []
    lo, hi = (3, 12) if spec.get("kind") == "line" else (3, 8)
    if not lo <= len(items) <= hi:
        return f"数据点 {len(items)} 个，不适合画{'折线图' if spec.get('kind') == 'line' else '柱状图'}"
    for it in items:
        try:
            v = float(it["value"])
        except (TypeError, ValueError, KeyError):
            return "数值不是数字"
        if not any(writing.num_in(_fmt(v) + (spec.get("unit") or ""), by_n[r]) or writing.num_in(_fmt(v), by_n[r])
                   for r in refs):
            return f"数值 {_fmt(v)}{spec.get('unit') or ''} 在出处 {refs} 里找不到"
    return None


# ---------------------------------------------------------------- 写入正文 / 成稿检查

def figure_md(f: dict, refmap: dict | None = None) -> str:
    alt = re.sub(r"[\[\]()]", "", f.get("alt") or f.get("caption") or "配图")[:40]
    return f"![{alt}]({f['src']})\n*{f['caption']}*"


def insert_figures(sections: list[dict], figs: list[dict]) -> list[dict]:
    """把正文配图插到对应段落之后（返回新的 sections，不改原对象）。"""
    out = []
    for sec in sections:
        paras = paragraphs(sec.get("md"))
        mine = sorted([f for f in figs if f.get("section") == sec["id"]], key=lambda f: f.get("para", 0))
        for f in reversed(mine):
            k = min(f.get("para", 0) + 1, len(paras))
            paras.insert(k, figure_md(f))
        out.append({**sec, "md": "\n\n".join(paras)})
    return out


def final_check(md: str, figs: list[dict], cover: dict | None, need: int) -> list[dict]:
    """成稿检查：占位符残留、缺图、重复、图注缺失、图片文件不存在。"""
    out = []
    ph = PLACEHOLDER.findall(md)
    out.append({"name": "占位符残留", "ok": not ph, "detail": f"发现 {ph[:3]}" if ph else "没有"})
    body = [f for f in figs]
    out.append({"name": "正文配图数量", "ok": len(body) >= need,
                "detail": f"{len(body)} 张（要求至少 {need} 张）"})
    out.append({"name": "封面", "ok": bool(cover), "detail": ("原图" if cover and cover["kind"] == "photo" else "示意封面") if cover else "缺少"})
    srcs = [f["src"] for f in body] + ([cover["src"]] if cover else [])
    out.append({"name": "重复图片", "ok": len(srcs) == len(set(srcs)), "detail": "没有" if len(srcs) == len(set(srcs)) else "有重复"})
    deco = [f for f in body if f.get("decorative")]
    useful = len(body) - len(deco)
    out.append({"name": "有信息量的图", "ok": not deco and useful > 0,
                "detail": f"{useful} 张信息图/数据图" + (f"，{len(deco)} 张照片补位" if deco else "")})
    miss = [f for f in body if not (f.get("caption") or "").strip()]
    out.append({"name": "图注", "ok": not miss, "detail": "每张都有" if not miss else f"{len(miss)} 张缺图注"})
    gone = [s for s in srcs if not (config.DATA_DIR / s.replace("/media/", "", 1)).exists()]
    out.append({"name": "图片文件", "ok": not gone, "detail": "都在本地" if not gone else f"{len(gone)} 张找不到文件"})
    return out
