"""正文与配图抽取（不依赖第三方 readability 库，便于排查和调参）。

extract_article(html, url) -> {
    title, author, published_at, text, paragraphs, content_kind,
    images: [{url, alt, caption, context_before, context_after, position, width, height,
              license, reject_reason}],
    cover, license, site_name, links
}
"""
from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag

NOISE_TAGS = ["script", "style", "noscript", "iframe", "form", "button", "svg", "nav",
              "footer", "header", "aside", "select", "input", "template"]
NOISE_HINT = re.compile(
    r"comment|share|social|related|recommend|sidebar|footer|header|nav|menu|breadcrumb|"
    r"advert|\bad\b|ads|sponsor|promo|banner|subscribe|newsletter|popup|modal|toolbar|"
    r"copyright|author-info|tags?-list|hot-?list|rank", re.I)
CONTENT_HINT = re.compile(r"article|content|post|entry|main|body|text|detail|story|news", re.I)

IMG_REJECT_URL = re.compile(
    r"avatar|logo|icon|sprite|emoji|badge|qr-?code|qrcode|erweima|tracking|pixel|spacer|"
    r"blank\.(gif|png)|loading\.(gif|svg)|placeholder|/ads?/|adserver|doubleclick|"
    r"banner|gravatar|headimg|profile_image|thumb_?\d{2}x\d{2}", re.I)
IMG_REJECT_CLASS = re.compile(r"avatar|logo|icon|emoji|ad-|advert|qrcode|author|profile|"
                              r"share|thumb-small|rank", re.I)
LAZY_ATTRS = ("data-src", "data-original", "data-lazy-src", "data-actualsrc", "data-url",
              "data-original-src", "data-echo", "data-lazy", "file")
CC_RE = re.compile(r"creativecommons\.org/(licenses|publicdomain)/([\w\-/.]+)", re.I)


def _clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


# ---------------------------------------------------------------- 元数据

def _meta(soup, *names):
    for n in names:
        tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n}) \
            or soup.find("meta", attrs={"itemprop": n})
        if tag and tag.get("content"):
            return _clean(tag["content"])
    return None


def _jsonld(soup) -> dict:
    out = {}
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) \
            if isinstance(data, dict) else []
        for it in items:
            if not isinstance(it, dict):
                continue
            t = it.get("@type")
            t = " ".join(t) if isinstance(t, list) else str(t)
            if any(k in t for k in ("Article", "Posting", "Report", "Blog")):
                out = it
                break
    return out


def _author_from(ld, soup):
    a = ld.get("author")
    if isinstance(a, list) and a:
        a = a[0]
    if isinstance(a, dict):
        a = a.get("name")
    if isinstance(a, str) and a.strip():
        return _clean(a)
    m = _meta(soup, "author", "article:author", "byl", "dc.creator", "twitter:creator")
    if m and not m.startswith("http"):
        return m
    for sel in ("[rel=author]", ".author", ".byline", "[class*=author-name]", "[itemprop=author]"):
        el = soup.select_one(sel)
        if el and 0 < len(el.get_text(strip=True)) < 40:
            return _clean(el.get_text())
    return None


def _published_from(ld, soup):
    for v in (ld.get("datePublished"),
              _meta(soup, "article:published_time", "og:article:published_time", "pubdate",
                    "publishdate", "date", "dc.date", "datePublished")):
        if v:
            return v
    t = soup.find("time", attrs={"datetime": True})
    if t:
        return t["datetime"]
    m = re.search(r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})日?(?:\s+(\d{1,2}):(\d{2}))?",
                  soup.get_text(" ")[:4000])
    if m:
        y, mo, d, hh, mm = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}" + (f"T{int(hh):02d}:{mm}:00" if hh else "")
    return None


# ---------------------------------------------------------------- 正文定位

def _text_len(el) -> int:
    return len(el.get_text(" ", strip=True))


def _link_density(el) -> float:
    total = _text_len(el) or 1
    links = sum(len(a.get_text(" ", strip=True)) for a in el.find_all("a"))
    return links / total


def _score(el: Tag) -> float:
    ps = el.find_all(["p", "h2", "h3", "li", "blockquote", "pre"], recursive=True)
    text = sum(len(p.get_text(" ", strip=True)) for p in ps if len(p.get_text(strip=True)) > 15)
    if text == 0:
        text = _text_len(el) * 0.3
    hint = " ".join([el.get("id", "")] + el.get("class", []))
    s = text * (1 - _link_density(el))
    if CONTENT_HINT.search(hint):
        s *= 1.3
    if NOISE_HINT.search(hint):
        s *= 0.3
    if el.name in ("article", "main"):
        s *= 1.4
    return s


def _main_container(soup) -> Tag:
    body = soup.body or soup
    cands = body.find_all(["article", "main", "div", "section", "td"])
    best, best_s = body, _score(body) * 0.5
    for c in cands:
        if _text_len(c) < 120:
            continue
        s = _score(c)
        if s > best_s:
            best, best_s = c, s
    # 如果最佳节点几乎就是它某个子节点，下钻一层，减少夹带的边栏
    return best


def _strip_noise(root: Tag):
    for t in root.find_all(NOISE_TAGS):
        t.decompose()
    for t in root.find_all(True):
        if t.attrs is None:
            continue
        hint = " ".join([t.get("id", "") or ""] + (t.get("class") or []))
        if hint and NOISE_HINT.search(hint) and t.name not in ("article", "main", "body") \
                and _text_len(t) < 600 and not t.find("img", recursive=True):
            t.decompose()


# ---------------------------------------------------------------- 图片

def _best_from_srcset(srcset: str, base: str):
    best, best_w = None, -1
    for part in srcset.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        u = bits[0]
        w = 0
        if len(bits) > 1:
            d = bits[1]
            try:
                w = float(d[:-1]) * (1000 if d.endswith("x") else 1)
            except ValueError:
                w = 0
        if w > best_w:
            best, best_w = u, w
    return urljoin(base, best) if best else None, (int(best_w) if best_w > 100 else None)


def _img_url(img: Tag, base: str):
    """处理懒加载与多分辨率：srcset/picture 取最大，data-* 优先于占位 src。"""
    width_hint = None
    # <picture><source srcset>：lxml 有时会把 <img> 嵌进 <source>，两种结构都处理
    pic = img.find_parent("picture")
    srcs = (pic.find_all("source") if pic is not None else []) + \
        [p for p in img.parents if getattr(p, "name", None) == "source"][:1]
    best_u, best_w = None, -1
    for src in srcs:
        ss = src.get("srcset") or src.get("data-srcset")
        if ss:
            u, w = _best_from_srcset(ss, base)
            if u and (w or 0) > best_w:
                best_u, best_w = u, w or 0
    if best_u:
        return best_u, (best_w or None)
    ss = img.get("data-srcset") or img.get("srcset")
    if ss:
        u, width_hint = _best_from_srcset(ss, base)
        if u and not u.startswith("data:"):
            return u, width_hint
    for a in LAZY_ATTRS:
        v = img.get(a)
        if v and not v.startswith("data:") and not v.endswith((".svg",)):
            return urljoin(base, v.strip()), None
    v = img.get("src")
    if v and not v.startswith("data:"):
        return urljoin(base, v.strip()), None
    return None, None


def _int(v):
    try:
        return int(str(v).replace("px", "").strip())
    except (TypeError, ValueError):
        return None


def _caption_for(img: Tag) -> str | None:
    fig = img.find_parent("figure")
    if fig:
        fc = fig.find("figcaption")
        if fc and fc.get_text(strip=True):
            return _clean(fc.get_text())
    # 常见做法：图片后紧跟一个居中的短段落作为图注
    holder = img.find_parent(["p", "div"]) or img
    nxt = holder.find_next_sibling()
    if nxt is not None and isinstance(nxt, Tag) and not nxt.find("img") and nxt.name != "figure":
        cls = " ".join(nxt.get("class", []))
        t = nxt.get_text(strip=True)
        if t and (re.search(r"caption|desc|note|img-?title|pic-?text", cls, re.I)
                  or (len(t) < 60 and (nxt.get("style", "").find("center") >= 0
                                       or re.match(r"^(图|圖|Figure|Fig\.?)\s*\d*", t)))):
            return _clean(t)
    for attr in ("title", "data-caption"):
        if img.get(attr):
            return _clean(img[attr])
    return None


def _block_text(el) -> str:
    return _clean(el.get_text(" ")) if el is not None else ""


def _neighbors(img: Tag, blocks: list[Tag]):
    """图片前后最近的正文段落，作为配图依据。"""
    holder = img
    while holder.parent is not None and holder.parent.name not in ("body", "article", "main") \
            and holder not in blocks:
        if holder.parent.name in ("p", "figure", "div") and len(holder.parent.find_all("img")) == 1:
            holder = holder.parent
        else:
            break
    before = after = ""
    prev = holder.find_previous(["p", "h2", "h3", "li", "blockquote"])
    while prev is not None and not before:
        t = _block_text(prev)
        if len(t) >= 8:
            before = t[-220:]
        prev = prev.find_previous(["p", "h2", "h3", "li", "blockquote"])
    nxt = holder.find_next(["p", "h2", "h3", "li", "blockquote"])
    while nxt is not None and not after:
        t = _block_text(nxt)
        if len(t) >= 8 and not nxt.find("img"):
            after = t[:220]
        nxt = nxt.find_next(["p", "h2", "h3", "li", "blockquote"])
    return before, after


def _reject_reason(img: Tag, url: str, w, h) -> str | None:
    hint = " ".join([img.get("id", "") or ""] + (img.get("class") or []) + [img.get("alt", "")])
    parent_hint = ""
    for p in list(img.parents)[:3]:
        parent_hint += " " + " ".join([p.get("id", "") or ""] + (p.get("class") or []))
    if IMG_REJECT_URL.search(url):
        return "url 命中广告/头像/图标特征"
    if IMG_REJECT_CLASS.search(hint) or IMG_REJECT_CLASS.search(parent_hint):
        return "class/alt 命中广告/头像/图标特征"
    if (w and w < 160) or (h and h < 100):
        return f"声明尺寸过小 {w}x{h}"
    if w and h and (w / max(h, 1) > 4.5 or h / max(w, 1) > 4.5):
        return f"宽高比异常 {w}x{h}（横幅/长条）"
    if url.lower().split("?")[0].endswith((".svg", ".ico")):
        return "矢量图标"
    return None


def _license(soup) -> str | None:
    lk = soup.find("a", rel="license") or soup.find("link", rel="license")
    if lk and lk.get("href"):
        m = CC_RE.search(lk["href"])
        return f"CC {m.group(2).strip('/')}" if m else lk["href"]
    m = CC_RE.search(str(soup)[:300000])
    if m:
        return f"CC {m.group(2).strip('/')}"
    cr = _meta(soup, "copyright", "dc.rights")
    return cr


# ---------------------------------------------------------------- 主函数

def extract_article(html: str, url: str, *, is_fragment: bool = False) -> dict:
    soup = BeautifulSoup(html, "lxml")
    ld = {} if is_fragment else _jsonld(soup)
    title = None if is_fragment else (_meta(soup, "og:title", "twitter:title") or
                                      (soup.title.get_text(strip=True) if soup.title else None))
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True) and (not title or h1.get_text(strip=True) in title):
        title = _clean(h1.get_text())
    lic = None if is_fragment else _license(soup)
    site = None if is_fragment else _meta(soup, "og:site_name", "application-name")
    cover = None if is_fragment else _meta(soup, "og:image", "twitter:image")
    author = None if is_fragment else _author_from(ld, soup)
    published = None if is_fragment else _published_from(ld, soup)
    description = None if is_fragment else _meta(soup, "description", "og:description")

    # noscript 里常有懒加载图片的真实地址
    for ns in soup.find_all("noscript"):
        inner = BeautifulSoup(ns.decode_contents(), "lxml")
        imgs = inner.find_all("img")
        if imgs and ns.parent is not None:
            prev_img = ns.find_previous_sibling("img")
            if prev_img is not None and not _img_url(prev_img, url)[0]:
                prev_img["src"] = imgs[0].get("src", "")
            elif prev_img is None:
                ns.replace_with(imgs[0])

    root = soup if is_fragment else _main_container(soup)
    _strip_noise(root)

    blocks = root.find_all(["p", "h2", "h3", "h4", "li", "blockquote", "pre"])
    paragraphs, seen = [], set()
    for b in blocks:
        if b.find_parent(["li", "blockquote"]) and b.name == "p":
            pass
        t = _clean(b.get_text(" "))
        if len(t) < 2 or t in seen:
            continue
        seen.add(t)
        prefix = "## " if b.name in ("h2", "h3", "h4") else ""
        paragraphs.append(prefix + t)
    if not paragraphs:
        t = _clean(root.get_text("\n"))
        paragraphs = [x for x in (s.strip() for s in t.split("\n")) if x]
    if not paragraphs and ld.get("articleBody"):
        paragraphs = [p for p in re.split(r"\n+", ld["articleBody"]) if p.strip()]
    text = "\n\n".join(paragraphs)

    images, seen_img = [], set()
    pos_index = {id(b): i for i, b in enumerate(blocks)}
    for img in root.find_all("img"):
        u, wh = _img_url(img, url)
        if not u or u in seen_img:
            continue
        seen_img.add(u)
        w = _int(img.get("width")) or _int(img.get("data-w")) or wh
        h = _int(img.get("height")) or _int(img.get("data-h"))
        before, after = _neighbors(img, blocks)
        prev_block = img.find_previous(["p", "h2", "h3", "h4", "li", "blockquote", "pre"])
        position = pos_index.get(id(prev_block), -1) + 1 if prev_block is not None else 0
        images.append({
            "url": u, "alt": _clean(img.get("alt")) or None, "caption": _caption_for(img),
            "context_before": before or None, "context_after": after or None,
            "position": position, "width": w, "height": h, "license": lic,
            "reject_reason": _reject_reason(img, u, w, h),
        })

    links = []
    for a in root.find_all("a", href=True):
        href = urljoin(url, a["href"])
        if href.startswith("http") and urlsplit(href).netloc != urlsplit(url).netloc:
            links.append({"url": href, "text": _clean(a.get_text())[:80]})

    n = len(text)
    kind = "full" if n >= 600 else ("excerpt" if n >= 120 else "summary")
    return {
        "title": title, "author": author, "published_at": published, "site_name": site,
        "description": description, "text": text, "paragraphs": len(paragraphs),
        "content_kind": kind, "images": images, "cover": urljoin(url, cover) if cover else None,
        "license": lic, "links": links[:80],
    }


def html_to_text(fragment: str) -> str:
    return _clean(BeautifulSoup(fragment or "", "lxml").get_text(" "))
