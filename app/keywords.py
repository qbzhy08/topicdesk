"""中英文关键词互译与扩展。

suggest(topic, zh, en) → {terms_zh, queries_en, alt_zh, alt_en, domains, by, notes}
- 有大模型：让模型给出常用说法、对应英文检索式、同义/替代说法（用于检索无结果时自动调整）。
- 没有大模型：用内置术语词典做最长匹配翻译，并给出同义词。
- 结果缓存（同一主题再次检索时保持一致，也省调用）。
用户在界面上看到的是可编辑的建议：可以直接改，提交时以用户填写的为准。
"""
from __future__ import annotations

import json
import re

from . import config, llm
from .collect.sources import guess_domains, topic_terms

# 术语词典：中文 → [英文主译, 其他译法...]。按领域整理，覆盖常见主题。
GLOSSARY = {
    # AI / 科技
    "大模型": ["LLM", "large language model", "foundation model"],
    "大语言模型": ["large language model", "LLM"],
    "开源大模型": ["open-source LLM", "open LLM", "open-weight model"],
    "开源模型": ["open-source model", "open-weight model"],
    "人工智能": ["artificial intelligence", "AI"],
    "机器学习": ["machine learning"], "深度学习": ["deep learning"], "强化学习": ["reinforcement learning", "RL"],
    "长上下文": ["long context", "long-context"], "上下文窗口": ["context window", "context length"],
    "上下文": ["context"], "大海捞针": ["needle in a haystack", "NIAH"],
    "评测": ["evaluation", "benchmark"], "测评": ["evaluation", "benchmark", "assessment"],
    "基准": ["benchmark"], "基准测试": ["benchmark"], "榜单": ["leaderboard"], "排行榜": ["leaderboard"],
    "复现": ["reproduction", "replication"], "推理": ["reasoning", "inference"], "推理加速": ["inference acceleration"],
    "多模态": ["multimodal"], "智能体": ["agent", "AI agent"], "代理": ["agent"],
    "检索增强": ["retrieval-augmented generation", "RAG"], "向量数据库": ["vector database"],
    "微调": ["fine-tuning"], "预训练": ["pretraining"], "蒸馏": ["distillation"], "量化": ["quantization"],
    "幻觉": ["hallucination"], "对齐": ["alignment"], "安全": ["safety", "security"],
    "芯片": ["chip", "semiconductor"], "半导体": ["semiconductor"], "算力": ["compute", "computing power"],
    "显卡": ["GPU"], "自动驾驶": ["autonomous driving", "self-driving"], "机器人": ["robot", "robotics"],
    "具身智能": ["embodied AI"], "开源": ["open source"], "编程": ["programming", "coding"],
    "代码生成": ["code generation"], "软件": ["software"], "云计算": ["cloud computing"],
    "数据库": ["database"], "操作系统": ["operating system"], "手机": ["smartphone"],
    "注意力机制": ["attention mechanism"], "位置编码": ["positional encoding"], "显存": ["GPU memory", "VRAM"],
    "稀疏注意力": ["sparse attention"], "混合专家": ["mixture of experts", "MoE"],
    # 金融 / 经济
    "股票": ["stock", "equity"], "股市": ["stock market"], "股价": ["stock price"],
    "趋势": ["trend"], "股票趋势": ["stock trend"], "预测": ["prediction", "forecasting"],
    "股票预测": ["stock prediction", "stock forecasting"], "量化交易": ["quantitative trading", "algorithmic trading"],
    "量化选股": ["quantitative stock selection", "factor investing"], "回测": ["backtesting", "backtest"],
    "因子": ["factor"], "基金": ["fund", "mutual fund"], "债券": ["bond"], "利率": ["interest rate"],
    "汇率": ["exchange rate"], "通胀": ["inflation"], "通货膨胀": ["inflation"], "央行": ["central bank"],
    "降息": ["rate cut"], "加息": ["rate hike"], "财报": ["earnings report", "financial results"],
    "营收": ["revenue"], "利润": ["profit", "earnings"], "估值": ["valuation"], "上市": ["IPO", "listing"],
    "风险": ["risk"], "风控": ["risk management"], "波动率": ["volatility"], "投资": ["investment"],
    "宏观经济": ["macroeconomics"], "经济": ["economy"], "期货": ["futures"], "加密货币": ["cryptocurrency", "crypto"],
    "比特币": ["bitcoin"], "房地产": ["real estate", "housing market"], "消费": ["consumption", "consumer spending"],
    # 学术 / 医学 / 科学
    "疫苗": ["vaccine"], "癌症": ["cancer"], "肿瘤": ["tumor", "oncology"], "糖尿病": ["diabetes"],
    "临床试验": ["clinical trial"], "新药": ["new drug", "drug development"], "药物": ["drug", "medication"],
    "基因": ["gene", "genomics"], "基因编辑": ["gene editing", "CRISPR"], "蛋白质": ["protein"],
    "阿尔茨海默": ["Alzheimer's disease"], "抑郁症": ["depression"], "肥胖": ["obesity"], "减重": ["weight loss"],
    "气候变化": ["climate change"], "碳中和": ["carbon neutrality"], "新能源": ["renewable energy"],
    "电池": ["battery"], "储能": ["energy storage"], "核聚变": ["nuclear fusion"], "量子计算": ["quantum computing"],
    "材料": ["materials"], "超导": ["superconductivity"],
    # 综合 / 政策
    "政策": ["policy"], "监管": ["regulation"], "法规": ["regulation", "law"], "教育": ["education"],
    "就业": ["employment", "jobs"], "城市更新": ["urban renewal"], "人口": ["population", "demographics"],
    "老龄化": ["population aging"], "隐私": ["privacy"], "版权": ["copyright"],
}
# 反向词典：英文 → 中文
REVERSE: dict[str, str] = {}
for zh, ens in GLOSSARY.items():
    for en in ens:
        REVERSE.setdefault(en.lower(), zh)

STOP_ZH = {"的", "与", "和", "及", "对", "在", "中", "关于", "以及", "如何", "什么", "最新", "进展", "研究", "分析"}


def _cache_path():
    return config.DATA_DIR / "keyword_cache.json"


def _cache_get(key: str):
    p = _cache_path()
    if p.exists():
        try:
            return json.loads(p.read_text()).get(key)
        except (OSError, json.JSONDecodeError):
            return None
    return None


def _cache_put(key: str, val: dict):
    p = _cache_path()
    try:
        d = json.loads(p.read_text()) if p.exists() else {}
    except (OSError, json.JSONDecodeError):
        d = {}
    d[key] = val
    p.write_text(json.dumps(d, ensure_ascii=False))


def is_english(text: str) -> bool:
    letters = re.findall(r"[A-Za-z]", text or "")
    cjk = re.findall(r"[一-鿿]", text or "")
    return len(letters) > 2 * len(cjk)


def _segment_zh(text: str) -> list[str]:
    """按词典做最长匹配切分，切不出的部分保留原样（≥2 字）。"""
    out, i, buf = [], 0, ""
    keys = sorted(GLOSSARY, key=len, reverse=True)
    while i < len(text):
        hit = next((k for k in keys if text.startswith(k, i)), None)
        if hit:
            if len(buf.strip()) >= 2 and buf.strip() not in STOP_ZH:
                out.append(buf.strip())
            buf = ""
            out.append(hit)
            i += len(hit)
        else:
            ch = text[i]
            if ch in "的与和及、，, /" or ch.isspace():
                if len(buf.strip()) >= 2 and buf.strip() not in STOP_ZH:
                    out.append(buf.strip())
                buf = ""
            else:
                buf += ch
            i += 1
    if len(buf.strip()) >= 2 and buf.strip() not in STOP_ZH:
        out.append(buf.strip())
    return list(dict.fromkeys(out))


def _dict_translate(topic: str, zh: list[str], en: list[str]) -> dict:
    notes = []
    if is_english(topic) and not zh:
        # 英文主题 → 中文关键词
        low = topic.lower()
        zh_terms = [REVERSE[k] for k in sorted(REVERSE, key=len, reverse=True) if k in low]
        zh_terms = list(dict.fromkeys(zh_terms))
        zh_terms = [t for t in zh_terms if not any(t != o and t in o for o in zh_terms)][:4]   # 去掉被长词包含的短词
        notes.append(f"词典：{topic} → {zh_terms or '未找到对应中文'}")
        return {"terms_zh": zh_terms, "queries_en": en or [topic], "alt_zh": [], "alt_en": [], "notes": notes}
    segs = []
    for t in (zh or []) + [topic]:
        segs += _segment_zh(t)
    segs = list(dict.fromkeys(s for s in segs if s not in STOP_ZH))
    known = [s for s in segs if s in GLOSSARY]
    unknown = [s for s in segs if s not in GLOSSARY and not re.search(r"[A-Za-z]", s)]
    terms_zh = list(dict.fromkeys((zh or []) + [s for s in segs if len(s) <= 6]))[:5]
    # 英文检索式：核心词主译拼接；再用各词的第二译法组成一条替代检索式
    def _dedup(words):
        out = []
        for w in words:
            if not any(w.lower() in x.lower() for x in out):
                out = [x for x in out if x.lower() not in w.lower()] + [w]
        return out
    base = _dedup([GLOSSARY[s][0] for s in known])
    variant = _dedup([GLOSSARY[s][1] if len(GLOSSARY[s]) > 1 else GLOSSARY[s][0] for s in known])
    queries = list(en or [])
    if base:
        queries.append(" ".join(base[:4]))
    if variant and variant != base:
        queries.append(" ".join(variant[:4]))
    queries = list(dict.fromkeys(q for q in queries if q))[:4]
    alt_en = list(dict.fromkeys(e for s in known for e in GLOSSARY[s][1:]))[:6]
    notes.append("词典：" + "；".join(f"{s} → {GLOSSARY[s][0]}" for s in known) if known else "词典未覆盖")
    if unknown:
        notes.append(f"未收录的词（建议配置大模型或手动补英文）：{unknown}")
    return {"terms_zh": terms_zh, "queries_en": queries, "alt_zh": [], "alt_en": alt_en, "notes": notes}


PROMPT = """把下面的检索主题整理成中英文检索关键词。
主题：{topic}
用户已填写的中文关键词：{zh}
用户已填写的英文检索式：{en}
要求：
- terms_zh：2-4 个中文核心词，用中文媒体常见说法，短（2-5 字）；保留用户已填写的。
- queries_en：2-4 条英文检索式，每条 2-4 个词，用英文论文/社区常用说法；保留用户已填写的。
- alt_zh / alt_en：同义词或替代说法（各 2-4 个），检索无结果时用来自动调整。
- domains：从这些 id 中选（可多选）：{domain_list}。
只输出 JSON：{{"terms_zh":[],"queries_en":[],"alt_zh":[],"alt_en":[],"domains":[],"note":"一句话"}}"""


def suggest(topic: str, zh: list[str] | None = None, en: list[str] | None = None,
            use_llm: bool = True, call=None) -> dict:
    """call：可选的 LLM 调用函数（Harness 里传 ctx.tool 的包装，便于追踪）。"""
    zh, en = [x for x in (zh or []) if x], [x for x in (en or []) if x]
    key = json.dumps([topic, zh, en, bool(use_llm and llm.available())], ensure_ascii=False)
    hit = _cache_get(key)
    if hit:
        return {**hit, "cached": True}
    res = None
    if use_llm and llm.available():
        try:
            msgs = [{"role": "system", "content": "你只输出 JSON。"},
                    {"role": "user", "content": PROMPT.format(topic=topic, zh=zh, en=en, domain_list=__import__("app.domains", fromlist=["x"]).prompt_list())}]
            out = call(msgs) if call else llm.chat(msgs, json_mode=True, max_tokens=600)
            d = out["data"]
            res = {"terms_zh": list(dict.fromkeys(zh + (d.get("terms_zh") or [])))[:5],
                   "queries_en": list(dict.fromkeys(en + (d.get("queries_en") or [])))[:4],
                   "alt_zh": d.get("alt_zh") or [], "alt_en": d.get("alt_en") or [],
                   "domains": [x for x in d.get("domains") or [] if x in __import__("app.domains", fromlist=["x"]).ids()],
                   "by": "llm",
                   "notes": [f"大模型：{d.get('note', '')}"]}
        except Exception as e:
            res = None
            fallback_note = f"大模型翻译失败（{str(e)[:60]}），改用词典"
    if res is None:
        res = _dict_translate(topic, zh, en)
        res["by"] = "dict"
        if use_llm and llm.available():
            res["notes"].insert(0, fallback_note)
    res.setdefault("domains", guess_domains(" ".join([topic] + res["terms_zh"] + res["queries_en"])))
    if not res["terms_zh"] and not is_english(topic):
        res["terms_zh"] = topic_terms(topic)
    _cache_put(key, res)
    return res
