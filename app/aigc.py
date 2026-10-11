"""AIGC 检测：所有文本用同一个检测器、同样的完整正文测，结果才可比较。

两种检测器（同一次实验只用一种）：
- hc3（本地模型）：Hello-SimpleAI/chatgpt-detector-roberta-chinese，HC3 论文（Guo et al., 2023,
  "How Close is ChatGPT to Human Experts?"）公开的中文检测模型。在本机运行，文本不外传。
  需要先安装：pip install torch transformers（模型约 400MB，首次运行自动下载；国内网络可在设置里填 HF 镜像）。
  长文按 ≤500 字切块，按字数加权平均“AI 生成”概率。
- manual（外部检测器）：用户把同一篇完整正文粘到同一个外部检测器（如某个在线 AIGC 检测服务），把结果录入。

注意：检测器只能给出“像不像 AI 写的”的统计判断，不能证明作者身份；降低这个分数不能以牺牲事实为代价，
所以实验报告里它总是和事实保真、表达自然、理解成本放在一起看。
"""
from __future__ import annotations

import json
import os
import re

from . import config

HC3_MODEL = "Hello-SimpleAI/chatgpt-detector-roberta-chinese"
_PIPE = {}


def settings() -> dict:
    p = config.DATA_DIR / "aigc.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(model: str | None = None, hf_endpoint: str | None = None) -> dict:
    s = settings()
    if model is not None:
        s["model"] = model.strip() or HC3_MODEL
    if hf_endpoint is not None:
        s["hf_endpoint"] = hf_endpoint.strip()
    (config.DATA_DIR / "aigc.json").write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    return s


def status() -> dict:
    """本地检测器是否可用（不加载模型，只检查依赖）。"""
    try:
        import importlib.util
        ok = bool(importlib.util.find_spec("torch") and importlib.util.find_spec("transformers"))
    except (ImportError, ValueError):
        ok = False
    s = settings()
    return {"local_ready": ok, "model": s.get("model") or HC3_MODEL, "hf_endpoint": s.get("hf_endpoint") or "",
            "loaded": bool(_PIPE), "install": "pip install torch transformers"}


def _pipe():
    s = settings()
    model = s.get("model") or HC3_MODEL
    if model in _PIPE:
        return _PIPE[model]
    if s.get("hf_endpoint"):
        os.environ["HF_ENDPOINT"] = s["hf_endpoint"]
    from transformers import pipeline      # 延迟导入：没装也不影响其他功能
    _PIPE.clear()
    _PIPE[model] = pipeline("text-classification", model=model, tokenizer=model, truncation=True, max_length=512, top_k=None)
    return _PIPE[model]


def chunks(text: str, size: int = 500) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
    out, cur = [], ""
    for p in paras:
        if len(cur) + len(p) > size and cur:
            out.append(cur)
            cur = ""
        while len(p) > size:
            out.append(p[:size])
            p = p[size:]
        cur = (cur + "\n" + p).strip()
    if cur:
        out.append(cur)
    return out


def _ai_label(label: str) -> bool:
    l = label.lower()
    return any(k in l for k in ("chatgpt", "ai", "fake", "machine", "generated", "label_1"))


def detect(text: str) -> dict:
    """返回 {detector, ai_prob, chunks:[{chars, ai_prob}]}。ai_prob 是按字数加权的“AI 生成”概率。"""
    pipe = _pipe()
    parts = chunks(text)
    res = pipe(parts)
    rows, tot, acc = [], 0, 0.0
    for part, r in zip(parts, res):
        scores = r if isinstance(r, list) else [r]
        p_ai = sum(x["score"] for x in scores if _ai_label(x["label"]))
        if not any(_ai_label(x["label"]) for x in scores):           # 只返回了一个标签
            top = scores[0]
            p_ai = top["score"] if _ai_label(top["label"]) else 1 - top["score"]
        rows.append({"chars": len(part), "ai_prob": round(p_ai, 4)})
        tot += len(part)
        acc += p_ai * len(part)
    return {"detector": f"hc3:{settings().get('model') or HC3_MODEL}", "ai_prob": round(acc / tot, 4) if tot else None,
            "chunks": rows}
