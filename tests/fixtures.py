"""离线测试用的假网站：模拟 RSS、API、网页、图片、robots.txt、限流和访问受限。"""
import io
import json
from datetime import datetime, timedelta, timezone

from app.collect import fetch

NOW = datetime.now(timezone.utc)


def rfc822(dt):
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


def png(w, h, color=(30, 120, 200)):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (w, h), color)
    d = ImageDraw.Draw(im)
    for i in range(0, w, 7):                       # 加纹理，避免压缩后过小
        d.line([(i, 0), (w - i, h)], fill=((i * 3) % 255, (i * 7) % 255, 90), width=2)
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


LONG_A = ("某开源大模型发布后宣称支持 128K 上下文，并在多个长上下文评测中领先。" * 8 +
          "官方技术报告给出了大海捞针测试的结果，但没有公布多文档推理任务的成绩。" * 6)
LONG_B = ("社区开发者对该模型的长上下文能力进行了复现评测，发现在 64K 以上时检索准确率明显下降，"
          "与官方宣称存在差距，引发争议。" * 10)


def item(title, link, desc, dt, author=None):
    a = f"<author>{author}</author>" if author else ""
    return (f"<item><title>{title}</title><link>{link}</link><pubDate>{rfc822(dt)}</pubDate>{a}"
            f"<description><![CDATA[{desc}]]></description></item>")


def rss(items):
    return ("<?xml version='1.0' encoding='utf-8'?><rss version='2.0'><channel><title>t</title>"
            + "".join(items) + "</channel></rss>")


def ithome_feed(corrected=False, extra=False):
    body_a = LONG_A if not corrected else LONG_A.replace("128K", "64K（官方更正）") + "更正说明：此前数字有误。" * 5
    desc_a = (f"<p>{body_a}</p>"
              "<p><img class='avatar' src='https://img.ithome.com/avatar/u1.png' width='40' height='40'></p>"
              "<figure><img src='data:image/gif;base64,R0lGOD' data-src='https://img.ithome.com/a/chart.png'>"
              "<figcaption>图 1：各模型长上下文评测得分</figcaption></figure>"
              "<p>上图显示该模型在 128K 长度下仍保持较高分数。参考论文 "
              "<a href='https://arxiv.org/abs/2410.01234v2'>arXiv 2410.01234</a>。</p>"
              "<p><img src='https://img.ithome.com/a/strip-2.png' srcset="
              "'https://img.ithome.com/a/strip-2.png 1x'></p>")
    items = [
        item("开源大模型长上下文评测成绩公布", "https://www.ithome.com/0/800/001.htm?f=rss",
             desc_a, NOW - timedelta(hours=5)),
        item("某手机新品发布", "https://www.ithome.com/0/800/002.htm", "<p>与主题无关的手机新闻" * 30 + "</p>",
             NOW - timedelta(hours=3)),
        item("开源大模型长上下文能力遭质疑：复现评测结果不及预期", "https://www.ithome.com/0/800/003.htm",
             f"<p>{LONG_B}</p>", NOW - timedelta(hours=2)),
    ]
    if extra:
        items.append(item("开源大模型上下文评测新进展：发布 256K 版本", "https://www.ithome.com/0/800/010.htm",
                          "<p>" + "新版本将上下文扩展到 256K，并公开了全部评测脚本。" * 12 + "</p>",
                          NOW - timedelta(minutes=30)))
    return rss(items)


def kr_feed():
    # 36氪转载了 IT之家的第三篇（同文），另有一篇半年前的背景文章
    return rss([
        item("复现评测：开源大模型长上下文能力遭质疑", "https://36kr.com/p/9001?f=rss",
             f"<p>{LONG_B}</p><p>本文转载自 IT之家。</p>", NOW - timedelta(hours=1)),
        item("一文读懂大模型上下文窗口与评测方法", "https://36kr.com/p/8000",
             "<p>" + "上下文窗口决定了模型一次能处理的文本长度，常见评测包括大海捞针、LongBench 等。" * 10 + "</p>",
             NOW - timedelta(days=200)),
    ])


def sspai_feed():
    return rss([item("开源大模型长上下文评测实测体验", "https://sspai.com/post/1001",
                     "<p>我们实测了几款开源大模型的上下文能力…… 查看全文</p>", NOW - timedelta(hours=6),
                     author="少数派作者")])


SSPAI_SPA = "<html><head><title>少数派</title></head><body><div id='app'></div><script src='/app.js'></script></body></html>"
SSPAI_RENDERED = ("<html><head><title>开源大模型长上下文评测实测体验 - 少数派</title>"
                  "<meta name='author' content='少数派作者'></head><body><nav>首页 菜单</nav>"
                  "<article><h1>开源大模型长上下文评测实测体验</h1>"
                  + "".join(f"<p>第{i}段：我们在本地用 32K、64K、128K 三种长度实测了开源大模型的检索和总结表现，"
                            f"长度越长，回答越容易遗漏中间部分的信息。</p>" for i in range(12)) +
                  "<p><img src='https://cdn.sspai.com/p/test-1.png' alt='实测结果对比图'></p>"
                  "<p>如图所示，128K 时准确率下降到六成左右。</p></article>"
                  "<aside class='related'>相关推荐 <img src='https://cdn.sspai.com/ad/promo.png'></aside>"
                  "</body></html>")

ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><entry>
<id>http://arxiv.org/abs/2410.01234v2</id><updated>2026-10-01T00:00:00Z</updated>
<published>2026-09-30T00:00:00Z</published>
<title>LongEval: Benchmarking Long Context in Open LLMs</title>
<summary>We present a long context benchmark for open LLMs and find accuracy drops beyond 64K tokens.</summary>
<author><name>A. Researcher</name></author>
<link href="http://arxiv.org/abs/2410.01234v2" rel="alternate" type="text/html"/>
<link title="pdf" href="http://arxiv.org/pdf/2410.01234v2" rel="related" type="application/pdf"/>
<category term="cs.CL"/></entry></feed>"""

YARN_ATOM = ARXIV_ATOM.replace("2410.01234", "2409.05555").replace(
    "LongEval: Benchmarking Long Context in Open LLMs", "YaRN Revisited: RoPE Position Extrapolation for Long Context").replace(
    "We present a long context benchmark for open LLMs and find accuracy drops beyond 64K tokens.",
    "We study RoPE scaling and position interpolation to extend the context window of LLMs to 256K.")

HN_JSON = json.dumps({"hits": [
    {"objectID": "1", "title": "Long context evals for open LLMs are misleading",
     "url": "https://blog.example.com/long-context", "author": "pg", "points": 320,
     "num_comments": 120, "created_at": (NOW - timedelta(days=1)).isoformat()},
    {"objectID": "2", "title": "Paywalled long context analysis", "url": "https://paywall.example.com/a",
     "author": "x", "points": 40, "num_comments": 3, "created_at": (NOW - timedelta(days=2)).isoformat()},
]})

BLOG = ("<html><head><title>Long context evals for open LLMs are misleading</title>"
        "<meta property='article:published_time' content='" + (NOW - timedelta(days=1)).isoformat() + "'>"
        "</head><body><article>"
        + "".join(f"<p>Paragraph {i}: needle in a haystack tests overstate long context ability of open LLMs; "
                  f"multi-hop benchmarks show sharp drops.</p>" for i in range(10)) +
        "<p>Code: <a href='https://github.com/example/longeval'>github.com/example/longeval</a></p>"
        "<picture><source srcset='https://blog.example.com/fig-800.png 800w, https://blog.example.com/fig-1600.png 1600w'>"
        "<img src='https://blog.example.com/fig-400.png'></picture>"
        "</article></body></html>")

GH_JSON = json.dumps({"items": [
    {"html_url": "https://github.com/example/longeval", "full_name": "example/longeval",
     "description": "Long context benchmark for open LLMs", "stargazers_count": 1200,
     "created_at": "2026-09-01T00:00:00Z", "pushed_at": "2026-10-05T00:00:00Z",
     "owner": {"login": "example"}, "license": {"spdx_id": "MIT"}}]})

GH_PAGE = ("<html><head><title>example/longeval</title></head><body><article class='markdown-body'>"
           + "<p>LongEval is a long context benchmark for open LLMs with 12 tasks.</p>" * 10 +
           "</article></body></html>")


def qbitai_search():
    # 站内搜索结果：标题里没有关键词，但正文命中（真实情况常见）
    return rss([item("字节找到了大模型时强时弱的原因", "https://www.qbitai.com/2026/10/502364.html",
                     "答不答得对，得看 Token 站位", NOW - timedelta(days=1))])


QBIT_PAGE = ("<html><head><title>字节找到了大模型时强时弱的原因 | 量子位</title></head><body>"
             "<div class='article'><h1>字节找到了大模型时强时弱的原因</h1>"
             + "".join(f"<p>第{i}段：研究发现，长上下文里关键信息所处的位置会显著影响大模型的回答准确率，"
                       f"放在中间时最容易被忽略。</p>" for i in range(16)) +
             "<p>论文地址：<a href='https://arxiv.org/abs/2410.01234'>arxiv.org/abs/2410.01234</a></p>"
             "<p>导航：<a href='https://huggingface.co/docs'>HF 文档</a> <a href='https://github.com/features'>GitHub</a></p>"
             "</div></body></html>")

WAF_PAGE = ("<!DOCTYPE html><html><head><meta charset='UTF-8'></head><body><div class='card'>"
            "火山引擎 正在进行安全检测... 为保障您的访问安全，系统正在检测当前网络环境</div>"
            "<script>var waf_verification_challenge=1;</script></body></html>")


JUEJIN_JSON = json.dumps({"err_no": 0, "err_msg": "success", "data": [
    {"result_type": 2, "title_highlight": "<em>长上下文</em>大模型评测踩坑记录",
     "content_highlight": "我们对比了几个开源模型的<em>长上下文</em>表现",
     "result_model": {"article_id": "7001", "article_info": {
         "article_id": "7001", "title": "长上下文大模型评测踩坑记录", "brief_content": "对比几个开源模型",
         "ctime": str(int((NOW - timedelta(days=3)).timestamp())), "view_count": 3200, "digg_count": 88},
         "author_user_info": {"user_name": "掘金作者"}}}], "cursor": "1", "has_more": False}, ensure_ascii=False)

JUEJIN_PAGE = ("<html><head><title>长上下文大模型评测踩坑记录 - 掘金</title></head><body><article class='article'>"
               "<h1>长上下文大模型评测踩坑记录</h1>"
               + "".join(f"<p>第{i}段：我们在 32K 和 128K 两档长度上评测了几个开源大模型，发现长上下文场景下"
                         f"检索类任务和推理类任务的表现差距很大。</p>" for i in range(14)) +
               "</article></body></html>")


JUEJIN_CORROB = json.dumps({"err_no": 0, "err_msg": "success", "data": [
    {"result_type": 2, "result_model": {"article_id": "7002", "article_info": {
        "article_id": "7002", "title": "开源大模型长上下文能力遭质疑，复现评测结果不及预期",
        "brief_content": "多个团队复现开源大模型长上下文评测，结果低于官方数据",
        "ctime": str(int((NOW - timedelta(hours=2)).timestamp())), "view_count": 900, "digg_count": 12},
        "author_user_info": {"user_name": "复现者"}}}], "cursor": "1", "has_more": False}, ensure_ascii=False)

WALLSTCN_JSON = json.dumps({"code": 20000, "message": "OK", "data": {"count": 1, "next_cursor": "", "items": [
    {"id": 1, "title": "<em>股票</em>趋势模型回测：动量策略今年回撤加大", "content": "<p>" +
     "多家机构对动量类股票趋势策略进行回测，发现今年以来最大回撤达到 18%，高于过去五年均值。" * 20 + "</p>",
     "content_short": "", "display_time": int(NOW.timestamp()) - 3600,
     "uri": "https://wallstreetcn.com/articles/3783332", "author": {"display_name": "见闻作者"},
     "image": {"uri": "https://img.wallstcn.com/x.png"}, "is_paid": False, "is_priced": False}]}},
    ensure_ascii=False)

OPENALEX_JSON = json.dumps({"results": [
    {"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1000/xyz", "display_name":
     "Evaluating stock trend prediction models", "publication_date": "2026-08-01",
     "authorships": [{"author": {"display_name": "A. Author"}}],
     "primary_location": {"landing_page_url": "https://doi.org/10.1000/xyz", "source": {"display_name": "J. Finance"}},
     "abstract_inverted_index": {"We": [0], "evaluate": [1], "stock": [2], "trend": [3], "prediction": [4], "models": [5]},
     "cited_by_count": 3}]})


class FakeWeb:
    def __init__(self):
        self.calls = []
        self.state = {"corrected": False, "extra": False, "hn_down": False, "rate_limit_once": set(),
                      "waf_36kr": False}
        self.images = {
            "https://img.ithome.com/a/chart.png": png(900, 500),
            "https://cdn.sspai.com/p/test-1.png": png(1200, 700, (200, 80, 40)),
            "https://blog.example.com/fig-1600.png": png(1600, 900, (40, 160, 90)),
            "https://img.ithome.com/a/strip-2.png": png(1500, 150),
            "https://github.com/example/longeval.png": png(64, 64),
        }

    def __call__(self, url, headers, timeout, method="GET", json_body=None):
        self.calls.append(url)
        H = {"content-type": "text/html; charset=utf-8"}
        X = {"content-type": "application/xml; charset=utf-8"}
        J = {"content-type": "application/json"}
        if url.endswith("/robots.txt"):
            if "paywall.example.com" in url:
                return 200, {"content-type": "text/plain"}, b"User-agent: *\nDisallow: /", url
            return 404, {}, b"", url
        if url in self.state["rate_limit_once"]:
            self.state["rate_limit_once"].discard(url)
            return 429, {"retry-after": "0"}, b"", url
        if url == "https://www.ithome.com/rss/":
            return 200, X, ithome_feed(self.state["corrected"], self.state["extra"]).encode(), url
        if url == "https://36kr.com/feed":
            if self.state["waf_36kr"]:
                return 200, {"content-type": "text/html"}, WAF_PAGE.encode(), url
            return 200, X, kr_feed().encode(), url
        if url.startswith("https://api.juejin.cn/search_api/v1/search") and method == "POST":
            if "遭质疑" in json.dumps(json_body or {}, ensure_ascii=False) and self.state.get("corroborate"):
                return 200, J, JUEJIN_CORROB.encode(), url
            return 200, J, JUEJIN_JSON.encode(), url
        if url == "https://juejin.cn/post/7001":
            return 200, H, JUEJIN_PAGE.encode(), url
        if url.startswith("https://api-one.wallstcn.com/apiv1/search/article"):
            return 200, J, WALLSTCN_JSON.encode(), url
        if url.startswith("https://api.openalex.org/works"):
            return 200, J, OPENALEX_JSON.encode(), url
        if url.startswith("https://www.qbitai.com/?s="):
            return 200, X, qbitai_search().encode(), url
        if url == "https://www.qbitai.com/2026/10/502364.html":
            return 200, H, QBIT_PAGE.encode(), url
        if url == "https://sspai.com/feed":
            return 200, X, sspai_feed().encode(), url
        if url == "https://sspai.com/post/1001":
            return 200, H, SSPAI_SPA.encode(), url
        if url.startswith("https://export.arxiv.org/api/query") and "extrapolation" in url:
            return 200, X, YARN_ATOM.encode(), url
        if url.startswith("https://export.arxiv.org/api/query"):
            return 200, X, ARXIV_ATOM.encode(), url
        if url.startswith("https://hn.algolia.com/"):
            if self.state["hn_down"]:
                raise fetch.FetchError("network", "连接失败：模拟网络不可达", url)
            return 200, J, HN_JSON.encode(), url
        if url.startswith("https://api.github.com/search"):
            return 200, J, GH_JSON.encode(), url
        if url == "https://blog.example.com/long-context":
            return 200, H, BLOG.encode(), url
        if url == "https://github.com/example/longeval":
            return 200, H, GH_PAGE.encode(), url
        if url.startswith("https://arxiv.org/abs/2410.01234"):
            page = ("<html><head><title>LongEval</title></head><body><blockquote class='abstract'>"
                    + "We present a long context benchmark for open LLMs. " * 20 + "</blockquote></body></html>")
            return 200, H, page.encode(), url
        if url.startswith("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch"):
            return 200, J, json.dumps({"esearchresult": {"idlist": ["42211289"]}}).encode(), url
        if url.startswith("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary"):
            return 200, J, json.dumps({"result": {"42211289": {
                "title": "Long context language models in clinical notes", "fulljournalname": "J Med AI",
                "pubdate": "2026 Sep", "authors": [{"name": "Li X"}]}}}).encode(), url
        if url.startswith("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch"):
            return 200, X, (b"<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>42211289</PMID>"
                            b"<Article><Abstract><AbstractText Label='RESULTS'>Accuracy of long context LLMs "
                            b"on clinical notes dropped beyond 32K tokens.</AbstractText></Abstract></Article>"
                            b"</MedlineCitation></PubmedArticle></PubmedArticleSet>"), url
        if url.startswith("https://huggingface.co/spaces/"):
            return 200, H, b"<html><head><title>Leaderboard</title></head><body><gradio-app></gradio-app></body></html>", url
        if url in self.images:
            return 200, {"content-type": "image/png"}, self.images[url], url
        if url.startswith("https://captcha.example.com"):
            return 200, H, b"<html><title>Just a moment...</title>please verify you are human</html>", url
        return 404, H, b"not found", url


def fake_render(url, wait_selector=None, timeout=25):
    if url == "https://sspai.com/post/1001":
        return {"html": SSPAI_RENDERED, "final_url": url, "title": "x", "status": 200,
                "browser": "fake-chrome", "elapsed_ms": 900}
    raise fetch.FetchError("blocked", "模拟：浏览器也被拦截", url)


def fake_fetch_raw(url, timeout=30):
    if url == "https://36kr.com/feed":
        return {"content": kr_feed().encode(), "content_type": "application/xml", "status": 200,
                "browser": "fake-chrome", "elapsed_ms": 3000}
    raise fetch.FetchError("blocked", "模拟：浏览器仍在验证页", url)
