"""把采集能力注册为 Harness 工具：每次调用都会留下输入、输出、耗时、成本和失败原因。"""
from __future__ import annotations

from .. import llm
from ..harness import tool
from . import images as images_mod
from . import sources


@tool("source_search", "在指定信息源中按主题检索，返回统一结构的资料条目")
def source_search(source_id: str, query: str, limit: int = 15, terms: list | None = None,
                  since: float | None = None):
    stats = {}
    items = sources.search(source_id, query, limit=limit, terms=terms, since=since, stats=stats)
    return {"source_id": source_id, "query": query, "count": len(items), "items": items,
            "dropped_old": stats.get("dropped_old", 0)}


@tool("page_fetch", "抓取单个网页并抽取正文、元数据和候选配图；静态页正文不足时用浏览器渲染")
def page_fetch(url: str, mode: str = "auto"):
    return sources.fetch_page(url, mode=mode)


@tool("image_download", "下载图片并做清晰度、尺寸、宽高比检查", cacheable=True)
def image_download(url: str, referer: str | None = None):
    return images_mod.download(url, referer=referer)


@tool("llm", "调用大模型（OpenAI 兼容接口）")
def llm_call(messages: list, json_mode: bool = True, max_tokens: int = 1500):
    return llm.chat(messages, json_mode=json_mode, max_tokens=max_tokens)


@tool("translate_keywords", "中英文关键词互译与同义扩展（大模型优先，词典兜底）")
def translate_keywords(topic: str, zh: list | None = None, en: list | None = None, use_llm: bool = True):
    from .. import keywords
    return keywords.suggest(topic, zh or [], en or [], use_llm=use_llm)
