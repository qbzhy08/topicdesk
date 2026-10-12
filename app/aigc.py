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


MIRRORS = ["", "https://hf-mirror.com"]          # 空 = 官方 huggingface.co


def model_dir() -> "Path":
    from pathlib import Path
    m = settings().get("model") or HC3_MODEL
    if Path(m).expanduser().is_dir():                 # 也可以填本地模型文件夹
        return Path(m).expanduser()
    return config.DATA_DIR / "models" / m.replace("/", "__")


def _model_ready() -> bool:
    d = model_dir()
    if not d.is_dir() or not (d / "config.json").exists():
        return False
    return any(d.glob("*.bin")) or any(d.glob("*.safetensors"))


def status() -> dict:
    """本地检测器是否可用：依赖装了没有、模型下载了没有（不加载模型）。"""
    try:
        import importlib.util
        deps = bool(importlib.util.find_spec("torch") and importlib.util.find_spec("transformers"))
    except (ImportError, ValueError):
        deps = False
    s = settings()
    return {"local_ready": deps, "deps": deps, "model_ready": _model_ready(), "model": s.get("model") or HC3_MODEL,
            "model_dir": str(model_dir()), "hf_endpoint": s.get("hf_endpoint") or "", "loaded": bool(_PIPE),
            "install": "双击 topicdesk 文件夹里的“安装AIGC检测器.command”", "last_error": s.get("last_error")}


def download(log=print) -> str:
    """把模型下载到 data/models/ 下。依次试：设置里填的地址、官方、国内镜像。
    地址通过 endpoint 参数直接传给下载函数（不依赖环境变量，换地址重试也有效）。"""
    from huggingface_hub import snapshot_download
    m = settings().get("model") or HC3_MODEL
    d = model_dir()
    if _model_ready():
        return str(d)
    tried, last = [], None
    for ep in dict.fromkeys([settings().get("hf_endpoint") or ""] + MIRRORS):
        tried.append(ep or "huggingface.co")
        try:
            log(f"下载模型 {m}，地址：{ep or 'huggingface.co（官方）'}")
            snapshot_download(repo_id=m, local_dir=str(d), endpoint=ep or None,
                              allow_patterns=["*.json", "*.txt", "*.bin", "*.safetensors", "*.model"])
            if _model_ready():
                if ep:
                    save_settings(hf_endpoint=ep)
                return str(d)
        except Exception as e:  # noqa: BLE001  网络不通、被墙等：换下一个地址
            last = e
            log(f"  这个地址不行：{str(e)[:160]}")
    raise RuntimeError(f"模型下载失败（试过 {', '.join(tried)}）：{str(last)[:200]}")


def _pipe():
    """加载本地模型（不用 transformers.pipeline，直接用分词器 + 分类模型，兼容 transformers 4.x / 5.x）。"""
    d = str(model_dir())
    if d in _PIPE:
        return _PIPE[d]
    if not _model_ready():
        download()
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer
    tok = AutoTokenizer.from_pretrained(d)
    model = AutoModelForSequenceClassification.from_pretrained(d)
    model.eval()
    labels = {int(k): v for k, v in (model.config.id2label or {}).items()}

    def run(parts):
        out = []
        with torch.no_grad():
            for i in range(0, len(parts), 8):
                enc = tok(parts[i:i + 8], truncation=True, max_length=512, padding=True, return_tensors="pt")
                probs = torch.softmax(model(**enc).logits, dim=-1).tolist()
                out += [[{"label": labels.get(j, f"LABEL_{j}"), "score": p} for j, p in enumerate(row)] for row in probs]
        return out
    _PIPE.clear()
    _PIPE[d] = run
    return run


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
    return {"detector": f"本地 HC3 检测器（{(settings().get('model') or HC3_MODEL).split('/')[-1]}）", "ai_prob": round(acc / tot, 4) if tot else None,
            "chunks": rows}
