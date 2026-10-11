"""把文章 Markdown 渲染成 HTML（文章页预览和手机 HTML 导出共用）。

只支持写作约定里会出现的语法：标题、段落、列表、引用、表格、粗体、链接、图片、行内代码，
以及两个约定：出处 [n] → 上标链接到参考资料；“【分析】”开头的段落 → 分析样式（和来源结论区分）。
"""
from __future__ import annotations

import html
import re

CITE = re.compile(r"\[(\d{1,2})\](?!\()")


def _inline(s: str, cite_href: str = "#ref-") -> str:
    s = html.escape(s, quote=False)
    s = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", lambda m: f'<img src="{m.group(2)}" alt="{m.group(1)}">', s)
    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", lambda m: f'<a href="{m.group(2)}" target="_blank" rel="noopener">{m.group(1)}</a>', s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = CITE.sub(lambda m: f'<sup class="cite"><a href="{cite_href}{m.group(1)}">{m.group(1)}</a></sup>', s)
    return s


def _para(text: str, cite_href: str) -> str:
    t = text.strip()
    if t.startswith("【分析】"):
        return f'<p class="analysis"><span class="tag-analysis">分析</span>{_inline(t[4:], cite_href)}</p>'
    return f"<p>{_inline(t, cite_href)}</p>"


def render(md: str, cite_href: str = "#ref-") -> str:
    lines = (md or "").replace("\r\n", "\n").split("\n")
    out, i, in_refs = [], 0, False
    while i < len(lines):
        ln = lines[i]
        if not ln.strip():
            i += 1
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", ln)
        if m:
            lvl = len(m.group(1))
            in_refs = m.group(2).strip() == "参考资料"
            out.append(f"<h{lvl}>{_inline(m.group(2), cite_href)}</h{lvl}>")
            i += 1
            continue
        if ln.lstrip().startswith(">"):
            buf = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                buf.append(lines[i].lstrip()[1:].strip())
                i += 1
            out.append("<blockquote>" + "".join(_para(b, cite_href) for b in buf if b) + "</blockquote>")
            continue
        if ln.lstrip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", lines[i + 1]):
            head = [c.strip() for c in ln.strip().strip("|").split("|")]
            i += 2
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            out.append('<div class="table-wrap"><table><thead><tr>' + "".join(f"<th>{_inline(c, cite_href)}</th>" for c in head)
                       + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{_inline(c, cite_href)}</td>" for c in r)
                                                        + "</tr>" for r in rows) + "</tbody></table></div>")
            continue
        lm = re.match(r"^\s*([-*]|\d+\.)\s+", ln)
        if lm:
            ordered = lm.group(1)[0].isdigit()
            items = []
            while i < len(lines) and re.match(r"^\s*([-*]|\d+\.)\s+", lines[i]):
                mm = re.match(r"^\s*([-*]|\d+\.)\s+(.*)$", lines[i])
                num = mm.group(1)[:-1] if ordered else None
                rid = f' id="ref-{num}"' if in_refs and num else ""
                body = mm.group(2)
                items.append(f"<li{rid}>{_inline(body, cite_href)}</li>" if not body.startswith("【分析】")
                             else f'<li class="analysis"><span class="tag-analysis">分析</span>{_inline(body[4:], cite_href)}</li>')
                i += 1
            tag = "ol" if ordered else "ul"
            cls = ' class="refs"' if in_refs else ""
            out.append(f"<{tag}{cls}>" + "".join(items) + f"</{tag}>")
            continue
        img = re.match(r"^!\[([^\]]*)\]\(([^)\s]+)\)\s*$", ln.strip())
        if img:
            cap = ""
            if i + 1 < len(lines) and lines[i + 1].strip().startswith("*") and lines[i + 1].strip().endswith("*"):
                cap = lines[i + 1].strip().strip("*")
                i += 1
            out.append(f'<figure><img src="{img.group(2)}" alt="{html.escape(img.group(1))}" loading="lazy">'
                       + (f"<figcaption>{_inline(cap, cite_href)}</figcaption>" if cap else "") + "</figure>")
            i += 1
            continue
        buf = []
        while i < len(lines) and lines[i].strip() and not re.match(r"^(#{1,4}\s|>|\s*([-*]|\d+\.)\s|\|)", lines[i]):
            buf.append(lines[i].strip())
            i += 1
        if buf:
            out.append(_para(" ".join(buf) if all(b.isascii() for b in buf) else "".join(buf), cite_href))
        else:
            i += 1
    return "\n".join(out)
