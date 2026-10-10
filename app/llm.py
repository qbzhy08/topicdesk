"""LLM 客户端：OpenAI 兼容接口（DeepSeek / 通义 / Kimi / 智谱 / OpenAI 等）。

未配置时 available() 返回 False，各流程走规则后备，保证整条链路可运行。
"""
from __future__ import annotations

import json
import os
import re

from . import config


def _cfg():
    # 允许运行中通过“设置”页面修改，故每次读取
    path = config.DATA_DIR / "llm.json"
    d = {"base_url": config.LLM_BASE_URL, "api_key": config.LLM_API_KEY, "model": config.LLM_MODEL,
         "price_in": float(os.environ.get("LLM_PRICE_IN", 0.3)),      # 美元 / 百万 token
         "price_out": float(os.environ.get("LLM_PRICE_OUT", 1.2))}
    if path.exists():
        try:
            d.update({k: v for k, v in json.loads(path.read_text()).items() if v not in (None, "")})
        except (OSError, json.JSONDecodeError):
            pass
    return d


def available() -> bool:
    c = _cfg()
    return bool(c["base_url"] and c["api_key"] and c["model"])


def save_settings(base_url: str, api_key: str, model: str):
    path = config.DATA_DIR / "llm.json"
    old = {}
    if path.exists():
        old = json.loads(path.read_text())
    if api_key == "***":
        api_key = old.get("api_key", "")
    path.write_text(json.dumps({"base_url": base_url.rstrip("/"), "api_key": api_key,
                                "model": model}, ensure_ascii=False))


def public_settings() -> dict:
    c = _cfg()
    return {"base_url": c["base_url"], "model": c["model"],
            "api_key": "***" if c["api_key"] else "", "available": available()}


class LLMError(Exception):
    pass


def chat(messages: list[dict], *, json_mode: bool = False, temperature: float = 0.2,
         max_tokens: int = 1500, timeout: float = 60) -> dict:
    """返回 {content, data(若 json_mode), usage, _cost}。"""
    import requests
    c = _cfg()
    if not available():
        raise LLMError("未配置 LLM")
    body = {"model": c["model"], "messages": messages, "temperature": temperature,
            "max_tokens": max_tokens}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    url = c["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {c['api_key']}", "Content-Type": "application/json"}
    r = requests.post(url, json=body, headers=headers, timeout=timeout)
    if r.status_code == 400 and json_mode:      # 部分服务不支持 response_format
        body.pop("response_format")
        r = requests.post(url, json=body, headers=headers, timeout=timeout)
    if r.status_code >= 400:
        raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
    d = r.json()
    content = d["choices"][0]["message"]["content"] or ""
    usage = d.get("usage") or {}
    cost = (usage.get("prompt_tokens", 0) * c["price_in"] +
            usage.get("completion_tokens", 0) * c["price_out"]) / 1e6
    out = {"content": content, "usage": usage, "_cost": round(cost, 6)}
    if json_mode:
        out["data"] = parse_json(content)
    return out


def parse_json(text: str):
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if m:
        text = m.group(1)
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise LLMError("模型没有返回 JSON")
    try:
        return json.loads(text[start:])
    except json.JSONDecodeError:
        end = max(text.rfind("}"), text.rfind("]"))
        return json.loads(text[start:end + 1])
