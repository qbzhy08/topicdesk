"""文章导出：交付用的图文文件。

- Markdown：zip 包（article.md + images/），图片改成相对路径，任何 Markdown 编辑器都能直接打开；
- 手机 HTML：单个 .html 文件，图片内嵌（base64），适合手机阅读和转发；出处上标点一下跳到文末参考资料。
两种格式都保留出处编号、【分析】标记、参考资料和图注（含图源）。
"""
from __future__ import annotations

import base64
import html
import io
import mimetypes
import re
import zipfile
from pathlib import Path

from . import config, mdrender

IMG = re.compile(r"!\[([^\]]*)\]\((/media/[^)\s]+)\)")


def _local(src: str) -> Path | None:
    p = (config.DATA_DIR / src.replace("/media/", "", 1)).resolve()
    try:
        p.relative_to(config.DATA_DIR.resolve())
    except ValueError:
        return None
    return p if p.exists() else None


def safe_name(title: str) -> str:
    return re.sub(r"[\\/:*?\"<>|\s]+", "_", title or "article").strip("_")[:60] or "article"


def markdown_zip(md: str, title: str) -> bytes:
    buf = io.BytesIO()
    used = {}

    def repl(m):
        p = _local(m.group(2))
        if not p:
            return m.group(0)
        name = used.setdefault(m.group(2), f"images/{len(used) + 1:02d}{p.suffix or '.png'}")
        return f"![{m.group(1)}]({name})"
    out = IMG.sub(repl, md)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("article.md", out)
        for src, name in used.items():
            z.write(_local(src), name)
    return buf.getvalue()


MOBILE_CSS = """
:root { --ink:#1f1c18; --muted:#6d665c; --line:#e2dbcd; --accent:#b8432f; --paper:#fbf9f4; --card:#fff; }
@media (prefers-color-scheme: dark) { :root { --ink:#ece6da; --muted:#a59d90; --line:#3a352e; --accent:#e07a62; --paper:#171512; --card:#1f1c18; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--paper); color: var(--ink); font: 17px/1.85 -apple-system, "PingFang SC", "Noto Sans SC", "Microsoft YaHei", sans-serif;
  -webkit-text-size-adjust: 100%; }
main { max-width: 680px; margin: 0 auto; padding: 20px 18px 60px; }
h1 { font: 700 26px/1.35 "Noto Serif SC", "Songti SC", serif; margin: 8px 0 14px; }
h2 { font: 700 20px/1.4 "Noto Serif SC", "Songti SC", serif; margin: 32px 0 10px; padding-top: 14px; border-top: 1px solid var(--line); }
p { margin: 12px 0; }
blockquote { margin: 12px 0; padding: 4px 14px; border-left: 3px solid var(--line); color: var(--muted); font-size: 14px; }
figure { margin: 18px -18px; }
figure img { display: block; width: 100%; height: auto; background: #fff; }
figcaption { font-size: 13px; color: var(--muted); padding: 6px 18px 0; line-height: 1.6; }
sup.cite a { color: var(--accent); text-decoration: none; font-size: 12px; padding: 0 1px; }
.analysis { background: color-mix(in srgb, var(--accent) 7%, transparent); padding: 10px 12px; border-radius: 6px; }
.tag-analysis { display: inline-block; font-size: 12px; color: var(--accent); border: 1px solid var(--accent); border-radius: 3px; padding: 0 5px; margin-right: 6px; line-height: 1.6; }
.table-wrap { overflow-x: auto; margin: 14px 0; }
table { border-collapse: collapse; font-size: 14px; min-width: 100%; }
th, td { border: 1px solid var(--line); padding: 6px 8px; text-align: left; vertical-align: top; }
ol.refs { font-size: 13px; color: var(--muted); padding-left: 22px; word-break: break-all; }
ol.refs a { color: var(--muted); }
ul, ol { padding-left: 22px; }
footer { margin-top: 36px; font-size: 12px; color: var(--muted); border-top: 1px solid var(--line); padding-top: 10px; }
"""


def mobile_html(md: str, title: str, footer: str = "") -> str:
    def inline(m):
        p = _local(m.group(2))
        if not p:
            return m.group(0)
        mime = mimetypes.guess_type(p.name)[0] or "image/png"
        data = base64.b64encode(p.read_bytes()).decode()
        return f"![{m.group(1)}](data:{mime};base64,{data})"
    body = mdrender.render(IMG.sub(inline, md))
    body = body.replace('<img src="data:', '<img loading="lazy" src="data:')
    return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{html.escape(title)}</title><style>{MOBILE_CSS}</style></head><body><main>{body}"
            f"<footer>{html.escape(footer)}</footer></main></body></html>")
