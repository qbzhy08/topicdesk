"""对标真人稿：多组不同题材的主题，系统自己检索、写文章，再和同主题真人写的文章逐项对比“像不像人写的”。

流程（每个主题）：
  1. 真人稿：bench/cases.json 里给了网址就直接抓正文；没给就用搜索 API 找一篇主流媒体的报道当对标。
  2. 系统稿：检索（3 天内同主题已有完成的检索就复用，省钱）→ 按指定 Skill 写作。
     真人稿和它的转载（正文一半以上重合）不进素材，比的是“写法”，不是“抄得像不像”。
  3. 对比：确定性指标（数字重复、AI 腔句式、套话、引语、句长变化、限制括注、【分析】段、开头……），
     可选大模型评审（盲评哪篇更像记者写的，挑出系统稿最像 AI 的句子，总结真人稿的写法）。
输出：data/style_bench/<时间>/report.md（给人看）和 data/style_bench/summary.json（给 Claude 读、接着改代码）。

用法：
  python tools/style_bench.py                 # 跑 bench/cases.json 里的全部主题
  python tools/style_bench.py --auto 3        # 另外让系统自己从近几天的热点里选 3 个不同题材的主题
  python tools/style_bench.py --only 1,3      # 只跑第 1、3 个
  python tools/style_bench.py --rejudge       # 不重新写，只对上一轮的文章重新对比（改了指标后用）
  python tools/style_bench.py --no-judge      # 不用大模型评审（只算确定性指标，不花钱）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db, graphs, llm  # noqa: E402,F401  导入即注册检索、写作等编排图
from app import evaluate as ev  # noqa: E402
from app import stylebench as sb  # noqa: E402
from app import writing  # noqa: E402
from app.collect.dedup import title_sim  # noqa: E402
from app.harness import create_task, run_task  # noqa: E402

OUT = ROOT / "data" / "style_bench"

# 自动选题：每类题材一条“找热点”的检索式，对应一种写法（Skill）
GENRES = [
    {"genre": "数据通报", "skill": "data", "query": "统计数据 发布 同比增长 最新"},
    {"genre": "科技产品", "skill": "brief", "query": "发布会 新品 正式发布 科技"},
    {"genre": "科学研究", "skill": "explainer", "query": "研究成果 发表 科学家 团队 最新"},
    {"genre": "社会事件", "skill": "timeline", "query": "最新进展 通报 事件 回应"},
    {"genre": "政策解读", "skill": "explainer", "query": "印发 通知 政策 解读 实施"},
    {"genre": "观点争议", "skill": "compare", "query": "引发热议 争议 专家 观点"},
]
MAINSTREAM = (set(writing.SITE_NAMES) | {"gov.cn"}) - {"zenodo.org", "openalex.org", "github.com", "arxiv.org",
                                                      "huggingface.co", "zhihu.com", "weibo.com"}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def host(u):
    h = (urlparse(u or "").hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


# 自媒体号：挂在门户域名下，但不是记者稿（网易号、百家号、搜狐号、头条号、公众号……），不能当对标的真人稿
SELF_MEDIA = writing.SELF_MEDIA
# 不像新闻的标题：栏目页、站点导航（“统计数据_某某人民政府”）、标签堆砌
JUNK_TITLE = re.compile(r"^[^_|｜]{0,10}[_|｜](?:[^_|｜]*(?:政府|网|频道|首页|栏目))$|^(统计数据|通知公告|政策解读|新闻中心)|"
                        r"(\|[^|]{1,12}){3,}")


def mainstream(u):
    h = host(u)
    return any(h == d or h.endswith("." + d) for d in MAINSTREAM) and not SELF_MEDIA.search(u or "")


def fetch_text(url):
    from app.collect import sources
    art = sources.fetch_page(url)
    text = (art.get("text") or "").strip()
    return {"url": url, "title": art.get("title") or "", "text": text, "site": writing.site_name(url)}


def find_human(topic, exclude_hosts=()):
    """用搜索 API 找一篇主流媒体写的同主题报道（正文 ≥ 500 字）。"""
    from app import websearch
    rows = websearch.search(topic, count=15, since=time.time() - 20 * 86400)
    rows = [r for r in rows if mainstream(r["url"]) and host(r["url"]) not in exclude_hosts]   # 只要主流媒体记者稿
    rows.sort(key=lambda r: -title_sim(topic, r.get("title") or ""))
    for r in rows[:6]:
        try:
            h = fetch_text(r["url"])
        except Exception as e:  # noqa: BLE001
            log("   抓取失败", r["url"][:60], str(e)[:60])
            continue
        if len(h["text"]) >= 500:
            return h
    return None


TOPIC_PROMPT = """今天是 {today}。下面是近几天同一件事的几条新闻标题。把它概括成一个检索主题：6-18 个汉字，写清是什么事（主体 + 事件），
不加评价。标题里没写年份就不要写年份，绝不能写成过去的年份。如果这些标题不是在说同一件具体的新闻事件（比如是栏目页、导航页、
政府网站的统计数据列表），topic 给空字符串。
只输出 JSON：{{"topic": "..."}}
{titles}"""


def auto_cases(n, used_topics):
    """系统自己选题：每类题材检索近 5 天的报道，取被最多家媒体报道的那件事。"""
    from app import websearch
    if not websearch.available():
        log("没有配置搜索 API，不能自动选题")
        return []
    out = []
    for g in GENRES:
        if len(out) >= n:
            break
        try:
            rows = websearch.search(g["query"], count=20, since=time.time() - 5 * 86400)
        except Exception as e:  # noqa: BLE001
            log(f"自动选题「{g['genre']}」检索失败：{str(e)[:60]}")
            continue
        clusters = []
        rows = [r for r in rows if len(r.get("title") or "") >= 10 and not JUNK_TITLE.search(r.get("title") or "")
                and not SELF_MEDIA.search(r["url"])]
        for r in rows:
            t = r.get("title") or ""
            c = next((c for c in clusters if title_sim(t, c[0]["title"]) >= 0.35), None)
            (c.append(r) if c else clusters.append([r]))
        # 至少两家不同网站、其中至少一家主流媒体报道，才算“一件新闻”
        clusters = [c for c in clusters if len({host(x["url"]) for x in c}) >= 2 and any(mainstream(x["url"]) for x in c)]
        clusters.sort(key=lambda c: (-len({host(x["url"]) for x in c}), -sum(mainstream(x["url"]) for x in c)))
        for c in clusters[:3]:
            titles = [x["title"] for x in c][:6]
            topic = re.sub(r"[_|｜-].*$", "", min(titles, key=len)).strip()[:24]
            if llm.available():
                try:
                    topic = str(llm.chat([{"role": "user", "content": TOPIC_PROMPT.format(
                        today=datetime.now().strftime("%Y-%m-%d"), titles="\n".join(titles))}],
                        json_mode=True, max_tokens=200)["data"]["topic"]).strip()[:24]
                    if not topic:
                        log(f"   跳过：{titles[0][:30]}……不像一件具体的新闻")
                        continue
                except Exception:  # noqa: BLE001
                    pass
            if any(title_sim(topic, u) >= 0.5 for u in used_topics):
                continue
            yrs = re.findall(r"(20\d\d)年", topic)
            if yrs and int(yrs[0]) < datetime.now().year - 1:
                topic = re.sub(r"20\d\d年", "", topic).strip()     # 大模型偶尔写错年份：去掉
            human = next((x["url"] for x in c if mainstream(x["url"])), None)    # 自媒体号已排除
            out.append({"topic": topic, "genre": g["genre"], "skill": g["skill"], "auto": True,
                        "human": [human] if human else [], "picked_from": titles})
            used_topics.append(topic)
            log(f"自动选题「{g['genre']}」：{topic}（{len(c)} 条报道）")
            break
    return out


def reuse_research(topic, days):
    rows = db.query("SELECT id, topic, updated_at FROM tasks WHERE graph='research' AND status='done' ORDER BY updated_at DESC LIMIT 200")
    for r in rows:
        if title_sim(r["topic"], topic) >= 0.8 and time.time() - float(r["updated_at"] or 0) < days * 86400:
            return r["id"]
    return None


def article_md(write_task):
    st = db.query_one("SELECT state FROM tasks WHERE id=?", (write_task,))["state"] or {}
    res = st.get("result") or {}
    if not res.get("article_id"):
        return None, None
    v = db.query_one("SELECT markdown, meta FROM article_versions WHERE article_id=? AND version=?",
                     (res["article_id"], res["version"]))
    return res["article_id"], (v or {}).get("markdown")


def run_case(i, case, args, outdir):
    topic = case["topic"]
    log(f"[{i}] {case.get('genre', '')}｜{topic}｜Skill {case['skill']}")
    rec = {**case, "i": i, "humans": [], "errors": []}
    # 1) 真人稿
    for u in case.get("human") or []:
        try:
            h = fetch_text(u)
            if len(h["text"]) < 200:
                rec["errors"].append(f"真人稿正文太短（{len(h['text'])} 字）：{u}")
                continue
            rec["humans"].append(h)
        except Exception as e:  # noqa: BLE001
            rec["errors"].append(f"真人稿抓取失败：{u}：{str(e)[:80]}")
    if not rec["humans"]:
        try:
            h = find_human(topic)
            if h:
                rec["humans"].append(h)
                log(f"   自动找到真人稿：{h['site']}《{h['title'][:30]}》")
        except Exception as e:  # noqa: BLE001
            rec["errors"].append(f"自动找真人稿失败：{str(e)[:80]}")
    if not rec["humans"]:
        log("   没有可用的真人稿，跳过")
        return rec
    for k, h in enumerate(rec["humans"], 1):
        (outdir / f"{i}.human{k}.txt").write_text(f"{h['title']}\n{h['url']}\n\n{h['text']}", encoding="utf-8")
    # 2) 系统稿
    if args.rejudge and case.get("write_task"):
        wid = case["write_task"]
    else:
        rid = None if args.fresh else reuse_research(topic, args.reuse_days)
        if rid:
            log(f"   复用 {args.reuse_days} 天内的检索任务 {rid}")
        else:
            log("   检索中（几分钟）…")
            rid = create_task("research", topic, params={"days": case.get("days", 14)})
            stt = run_task(rid)
            if stt != "done":
                stt += "：" + str((db.query_one("SELECT error FROM tasks WHERE id=?", (rid,)) or {}).get("error") or "")[:200]
                rec["errors"].append(f"检索没有完成：{stt}")
                log(f"   检索没有完成：{stt}")
                return rec
        rec["research_task"] = rid
        log("   写作中（一两分钟）…")
        exclude = {"urls": [h["url"] for h in rec["humans"]], "texts": [h["text"][:3000] for h in rec["humans"]]}
        wid = create_task("write", topic, params={"research_task": rid, "skill": case["skill"], "exclude": exclude},
                          parent_task_id=rid)
        stt = run_task(wid)
        if stt != "done":
            stt += "：" + str((db.query_one("SELECT error FROM tasks WHERE id=?", (wid,)) or {}).get("error") or "")[:200]
            rec["errors"].append(f"写作没有完成：{stt}")
            log(f"   写作没有完成：{stt}")
            return rec
    rec["write_task"] = wid
    aid, md = article_md(wid)
    if not md:
        rec["errors"].append("没有找到写好的文章")
        return rec
    rec["article_id"] = aid
    (outdir / f"{i}.system.md").write_text(md, encoding="utf-8")
    # 3) 对比
    sys_text = ev.plain_text(md)
    rec["system"] = sb.metrics(sys_text, md)
    for h in rec["humans"]:
        h["metrics"] = sb.metrics(h["text"])
    rec["compare"] = sb.compare(rec["system"], [h["metrics"] for h in rec["humans"]])
    if not args.no_judge and llm.available():
        try:
            rec["judge"] = sb.judge(llm.chat, sys_text, rec["humans"][0]["text"], seed=i)
        except Exception as e:  # noqa: BLE001
            rec["errors"].append(f"大模型评审失败：{str(e)[:80]}")
    g = rec["compare"]["gaps"]
    log(f"   对比完成：{len(g)} 项不如真人稿（" + "、".join(x["name"].split("（")[0] for x in g[:5]) + "）"
        + (f"；评审认为{'系统稿' if rec['judge']['system_judged_more_human'] else '真人稿'}更像人写的" if rec.get("judge") else ""))
    return rec


def fmt(v):
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, list):
        return "、".join(map(str, v)) or "无"
    return str(v)


def report(cases, agg, outdir):
    L = [f"# 对标真人稿 {datetime.now():%Y-%m-%d %H:%M}", "",
         f"{agg['compared']}/{agg['cases']} 个主题完成对比。"]
    if agg.get("judge"):
        j = agg["judge"]
        L.append(f"大模型盲评：{j['n']} 组里有 {j['system_more_human']} 组认为系统稿更像人写的。")
    L += ["", "## 最常见的差距", "", "| 差距 | 出现在几篇 | 代码里管它的地方 |", "|---|---|---|"]
    for g in agg["gaps"]:
        L.append(f"| {g['name']} | {g['cases']} | {g['where']} |")
    for c in cases:
        L += ["", f"## {c['i']}. {c['topic']}（{c.get('genre', '')}，Skill {c['skill']}）", ""]
        for e in c.get("errors") or []:
            L.append(f"- ⚠ {e}")
        for h in c.get("humans") or []:
            L.append(f"- 真人稿：[{h['title'][:40]}]({h['url']})（{h['site']}，{h.get('metrics', {}).get('chars', '?')} 字）")
        if c.get("article_id"):
            L.append(f"- 系统稿：文章 {c['article_id']}（{c['system']['chars']} 字），全文见 {c['i']}.system.md")
        if not c.get("compare"):
            continue
        L += ["", "| 指标 | 系统稿 | 真人稿 | |", "|---|---|---|---|"]
        gapkeys = {g["key"] for g in c["compare"]["gaps"]}
        for key, name, *_ in sb.CHECKS:
            if key in c["compare"]["human"]:
                L.append(f"| {name} | {fmt(c['system'].get(key))} | {fmt(c['compare']['human'][key])} | {'✗' if key in gapkeys else '✓'} |")
        if c["system"].get("template_headings"):
            L.append(f"| 模板化小标题 | {fmt(c['system']['template_headings'])} | | ✗ |")
        if c["system"].get("repeated_numbers"):
            L.append(f"\n反复出现的数字：{'、'.join(f'{k}×{v}' for k, v in c['system']['repeated_numbers'].items())}")
        if c["system"].get("trope_examples"):
            L.append("\nAI 腔句子：\n" + "\n".join(f"- {x}" for x in c["system"]["trope_examples"]))
        j = c.get("judge")
        if j:
            L.append(f"\n**大模型盲评**：{'系统稿' if j['system_judged_more_human'] else '真人稿'}更像人写的（把握 {j.get('confidence')}/5）")
            for x in j.get("system_ai_like") or []:
                L.append(f"- 最像 AI 的句子：「{x.get('sentence', '')}」——{x.get('why', '')}")
            for x in j.get("human_moves") or []:
                L.append(f"- 真人稿的写法：{x}")
            for x in j.get("rules") or []:
                L.append(f"- 建议规则：{x}")
    (outdir / "report.md").write_text("\n".join(L) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(ROOT / "bench" / "cases.json"))
    ap.add_argument("--auto", type=int, default=0, help="让系统自己从近几天的热点里另选几个主题")
    ap.add_argument("--only", default="", help="只跑这几个（编号，逗号分隔）")
    ap.add_argument("--reuse-days", type=int, default=3)
    ap.add_argument("--fresh", action="store_true", help="不复用已有检索，全部重新检索")
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--rejudge", action="store_true", help="不重新写，只重新对比上一轮的文章")
    args = ap.parse_args()
    db.init_db()
    OUT.mkdir(parents=True, exist_ok=True)
    if args.rejudge and (OUT / "summary.json").exists():
        cases = [{k: c[k] for k in ("topic", "genre", "skill", "human", "write_task") if k in c}
                 for c in json.loads((OUT / "summary.json").read_text(encoding="utf-8"))["cases"]]
    else:
        cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))["cases"]
        if args.auto:
            cases += auto_cases(args.auto, [c["topic"] for c in cases])
    if args.only:
        keep = {int(x) for x in args.only.split(",") if x.strip().isdigit()}
        cases = [c for k, c in enumerate(cases, 1) if k in keep]
    outdir = OUT / datetime.now().strftime("%Y%m%d-%H%M")
    outdir.mkdir(parents=True, exist_ok=True)
    log(f"共 {len(cases)} 个主题；大模型{'可用' if llm.available() else '未配置（只能用规则写，结果没有参考价值）'}；结果写到 {outdir}")
    done = []
    for i, c in enumerate(cases, 1):
        try:
            done.append(run_case(i, c, args, outdir))
        except Exception as e:  # noqa: BLE001
            log(f"   出错：{e}")
            done.append({**c, "i": i, "errors": [f"出错：{str(e)[:200]}"]})
        agg = sb.aggregate(done)
        report(done, agg, outdir)
        slim = [{k: v for k, v in c.items() if k != "humans"} | {
            "humans": [{k: h.get(k) for k in ("url", "title", "site", "metrics")} for h in c.get("humans") or []]}
            for c in done]
        summary = {"at": datetime.now().isoformat(timespec="seconds"), "dir": str(outdir.relative_to(ROOT)),
                   "aggregate": agg, "cases": slim}
        (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"完成。报告：{outdir / 'report.md'}")


if __name__ == "__main__":
    main()
