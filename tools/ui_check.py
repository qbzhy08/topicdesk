"""页面自动巡检：把主题编辑台的每一类页面、用库里现有的每组数据都打开一遍，电脑和手机两种宽度，自动找显示问题。

只读：只打开页面、不点任何会改数据的按钮（不提交、不重跑、不删除）。

检查项：
  - 脚本报错（页面 JS 异常、控制台 error）
  - 接口失败（HTTP ≥ 400）
  - 页面上出现 undefined / NaN / null / [object Object] / Infinity 这类“程序没处理好”的文字
  - 横向溢出（页面比屏幕宽，手机上要左右拖）、超出屏幕的元素
  - 图片打不开
  - 页面主体是空的（内容没加载出来）
输出：<输出目录>/report.html（问题列表 + 每页截图）和 summary.json。

用法：python tools/ui_check.py --base http://127.0.0.1:8000 --out data/ui_check [--max 6]
"""
from __future__ import annotations

import argparse
import html
import json
import re
import time
from pathlib import Path
from urllib.request import urlopen

BAD_TEXT = re.compile(r"\bundefined\b|\bNaN\b|\[object Object\]|\bnull\b(?! *[a-zA-Z_])|Infinity|\$\{")
VIEWPORTS = [("电脑", 1440, 900), ("手机", 390, 844)]


def get(base, path):
    with urlopen(base + path, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def pages(base, n):
    """要打开的页面：固定页面 + 库里每类数据各取前 n 个。"""
    out = [("新主题", "#/new"), ("资料库", "#/library"), ("记忆", "#/memory"), ("写法 Skill", "#/skills"),
           ("新建 Skill", "#/skills/new"), ("评测", "#/experiments"), ("信息源", "#/sources"), ("设置", "#/settings")]
    try:
        tasks = get(base, "/api/tasks")
        by_graph = {}
        for t in tasks:
            by_graph.setdefault(t["graph"], []).append(t)
        for g, ts in by_graph.items():
            for t in ts[:n]:
                out.append((f"任务·{g}·{t['topic'][:12]}·{t['status']}", f"#/task/{t['id']}"))
                if g == "research" and t["status"] == "done":
                    out.append((f"可信度·{t['topic'][:12]}", f"#/trust/{t['id']}"))
    except Exception as e:  # noqa: BLE001
        out.append((f"（读取任务列表失败：{e}）", None))
    try:
        for a in get(base, "/api/articles")[:n]:
            out.append((f"文章·{(a.get('title') or a['id'])[:14]}", f"#/article/{a['id']}"))
            if (a.get("current_version") or 0) > 1:
                out.append((f"文章旧版本·{(a.get('title') or a['id'])[:10]}", f"#/article/{a['id']}?v=1"))
    except Exception as e:  # noqa: BLE001
        out.append((f"（读取文章失败：{e}）", None))
    try:
        for x in get(base, "/api/experiments")[:n]:
            out.append((f"实验报告·{x['topic'][:14]}·{x['status']}", f"#/experiments/{x['id']}"))
            out.append((f"盲评页·{x['topic'][:10]}", f"#/blind/{x['id']}"))
    except Exception as e:  # noqa: BLE001
        out.append((f"（读取实验失败：{e}）", None))
    try:
        for s in get(base, "/api/skills")["skills"][:n]:
            out.append((f"编辑 Skill·{s['name']}", f"#/skills/edit/{s['id']}"))
    except Exception as e:  # noqa: BLE001
        out.append((f"（读取 Skill 失败：{e}）", None))
    try:
        docs = get(base, "/api/docs?limit=5")
        docs = docs.get("items", docs) if isinstance(docs, dict) else docs
        for d in docs[:min(n, 3)]:
            out.append((f"资料·{(d.get('title') or d['id'])[:14]}", f"#/doc/{d['id']}"))
    except Exception:  # noqa: BLE001
        pass
    try:
        mems = get(base, "/api/memories")
        mems = mems.get("items", mems) if isinstance(mems, dict) else mems
        for m in mems[:min(n, 3)]:
            out.append((f"记忆·{m['content'][:12]}", f"#/memory/{m['id']}"))
    except Exception:  # noqa: BLE001
        pass
    return out


CHECK_JS = r"""
() => {
  const vw = window.innerWidth;
  const over = document.documentElement.scrollWidth - vw;
  const wide = [];
  for (const el of document.querySelectorAll('#detail *')) {
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.right > vw + 2 && !el.closest('.table-wrap, pre, .sx, .bl-text, code')) {
      wide.push((el.className && typeof el.className === 'string' ? '.' + el.className.split(' ')[0] : el.tagName.toLowerCase()) + ' 右边超出 ' + Math.round(r.right - vw) + 'px');
      if (wide.length > 5) break;
    }
  }
  const imgs = [...document.querySelectorAll('#detail img')].filter((i) => i.complete && i.naturalWidth === 0).map((i) => i.getAttribute('src'));
  const detail = document.querySelector('#detail');
  const text = detail ? detail.innerText : '';
  return {over, wide, imgs: imgs.slice(0, 5), textLen: text.length, text: text.slice(0, 200000)};
}
"""


def run(base, out_dir, n):
    from playwright.sync_api import sync_playwright
    out = Path(out_dir)
    (out / "shots").mkdir(parents=True, exist_ok=True)
    plist = pages(base, n)
    print(f"共 {len(plist)} 个页面 × {len(VIEWPORTS)} 种宽度", flush=True)
    results = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        for vname, w, h in VIEWPORTS:
            ctx = b.new_context(viewport={"width": w, "height": h})
            pg = ctx.new_page()
            errs, bad_req = [], []
            pg.on("pageerror", lambda e: errs.append(f"脚本异常：{e}"))
            pg.on("console", lambda m: errs.append(f"控制台：{m.text[:200]}") if m.type == "error" else None)
            pg.on("response", lambda r: bad_req.append(f"{r.status} {r.url.replace(base, '')[:120]}")
                  if r.status >= 400 and "/api/" in r.url else None)
            pg.goto(base + "/#/new")
            pg.wait_for_timeout(800)
            for i, (name, hsh) in enumerate(plist):
                if not hsh:
                    results.append({"name": name, "view": vname, "issues": [name], "shot": None})
                    continue
                print(f"  [{vname} {i + 1}/{len(plist)}] {name}", flush=True)
                errs.clear()
                bad_req.clear()
                t0 = time.time()
                pg.evaluate(f"location.hash = {json.dumps(hsh)}")
                pg.wait_for_timeout(1800)
                try:
                    pg.wait_for_load_state("networkidle", timeout=6000)
                except Exception:  # noqa: BLE001  一直在轮询的页面（运行中的任务）不会 idle
                    pass
                info = pg.evaluate(CHECK_JS)
                issues = []
                for m in sorted(set(BAD_TEXT.findall(info["text"]))):
                    ctx_m = re.search(r".{0,30}" + re.escape(m) + r".{0,30}", info["text"])
                    issues.append(f"页面文字里有“{m}”：…{ctx_m.group(0).strip() if ctx_m else ''}…")
                if info["over"] > 2:
                    issues.append(f"页面比屏幕宽 {info['over']}px（要左右拖动）")
                issues += [f"元素超出屏幕：{x}" for x in info["wide"][:3]]
                issues += [f"图片打不开：{x}" for x in info["imgs"]]
                if info["textLen"] < 30:
                    issues.append("页面主体几乎是空的（内容可能没加载出来）")
                issues += errs[:5] + [f"接口失败：{x}" for x in bad_req[:5]]
                shot = f"shots/{vname}_{i:03d}.png"
                pg.screenshot(path=str(out / shot), full_page=True, clip=None)
                results.append({"name": name, "hash": hsh, "view": vname, "issues": issues, "shot": shot,
                                "seconds": round(time.time() - t0, 1)})
            ctx.close()
        b.close()
    bad = [r for r in results if r["issues"]]
    summary = {"base": base, "pages": len(plist), "checks": len(results), "with_issues": len(bad),
               "issues": [{"page": r["name"], "view": r["view"], "issues": r["issues"]} for r in bad]}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    def row(r):
        shot = f"<a href='{html.escape(r['shot'])}' target=_blank><img src='{html.escape(r['shot'])}' loading=lazy></a>" if r["shot"] else ""
        probs = "<br>".join(html.escape(x) for x in r["issues"]) or "✓"
        return (f"<tr class='{'bad' if r['issues'] else 'ok'}'><td>{html.escape(r['name'])}<br><small>{html.escape(r.get('hash') or '')}</small></td>"
                f"<td>{r['view']}</td><td>{probs}</td><td>{shot}</td></tr>")
    rows = "".join(row(r) for r in sorted(results, key=lambda r: (not r["issues"], r["name"])))
    (out / "report.html").write_text(
        "<!doctype html><meta charset=utf-8><title>页面巡检</title><style>body{font:14px -apple-system,sans-serif;margin:20px}"
        "table{border-collapse:collapse;width:100%}td{border-bottom:1px solid #ddd;padding:6px;vertical-align:top}"
        "tr.bad td:nth-child(3){color:#b8432f}img{width:220px;max-height:160px;object-fit:cover;object-position:top;border:1px solid #ccc}</style>"
        f"<h1>页面巡检</h1><p>{len(plist)} 个页面 × {len(VIEWPORTS)} 种宽度 = {len(results)} 次检查，"
        f"<b>{len(bad)}</b> 次发现问题。地址：{html.escape(base)}，时间：{time.strftime('%Y-%m-%d %H:%M')}</p>"
        f"<table><tr><th>页面</th><th>宽度</th><th>问题</th><th>截图</th></tr>{rows}</table>", encoding="utf-8")
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--out", default="data/ui_check")
    ap.add_argument("--max", type=int, default=6, help="每类数据最多打开几个")
    a = ap.parse_args()
    s = run(a.base.rstrip("/"), a.out, a.max)
    print(f"检查 {s['checks']} 次，{s['with_issues']} 次有问题。报告：{Path(a.out) / 'report.html'}")
    for x in s["issues"][:40]:
        print(f"- [{x['view']}] {x['page']}：{'；'.join(x['issues'])[:300]}")
