"""写作 Skill：可加载的创作方式。

每个 Skill 是 skills/<id>/SKILL.md：YAML 头部声明读者、篇幅、结构、选材和配图要求，正文是给大模型的写作要求。
新增或修改 Skill 只需放一个文件，不改业务代码；每次写作记录 Skill 的 id、版本号和内容指纹，
同一版本号下内容被改过也能看出来。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml

from . import config

SKILL_DIR = Path(getattr(config, "SKILL_DIR", None) or Path(__file__).resolve().parent.parent / "skills")

REQUIRED = ("id", "name", "version", "reader", "structure")
DEFAULTS = {
    "summary": "", "length": "", "style": [], "body": "",
    "material": {"list": "30", "max_docs": 10, "prefer": [], "facts": "all"},
    "images": {"cover": 1, "body_min": 2, "prefer": [], "placement": ""},
}


class SkillError(ValueError):
    pass


def parse(text: str, origin: str = "") -> dict:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        raise SkillError(f"{origin}：缺少 --- 包围的 YAML 头部")
    try:
        head = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise SkillError(f"{origin}：YAML 格式错误 {e}") from e
    miss = [k for k in REQUIRED if not head.get(k)]
    if miss:
        raise SkillError(f"{origin}：缺少字段 {miss}")
    sk = {**DEFAULTS, **head}
    sk["material"] = {**DEFAULTS["material"], **(head.get("material") or {})}
    sk["images"] = {**DEFAULTS["images"], **(head.get("images") or {})}
    sk["material"]["list"] = str(sk["material"]["list"])
    secs = []
    for i, s in enumerate(sk["structure"]):
        if not isinstance(s, dict) or not s.get("heading"):
            raise SkillError(f"{origin}：structure 第 {i + 1} 项缺少 heading")
        secs.append({"id": str(s.get("id") or f"s{i}"), "heading": str(s["heading"]), "goal": str(s.get("goal") or ""),
                     "words": int(s.get("words") or 250)})
    sk["structure"] = secs
    sk["body"] = m.group(2).strip()
    sk["version"] = str(sk["version"])
    sk["fingerprint"] = hashlib.sha1(text.encode()).hexdigest()[:10]
    return sk


def load_all() -> dict:
    """{id: skill}；格式错误的 Skill 不加载，记录在 errors 里，不影响其他 Skill。"""
    out, errors = {}, []
    if SKILL_DIR.exists():
        for p in sorted(SKILL_DIR.glob("*/SKILL.md")):
            try:
                sk = parse(p.read_text(encoding="utf-8"), str(p.relative_to(SKILL_DIR)))
                sk["path"] = str(p.relative_to(SKILL_DIR.parent))
                out[sk["id"]] = sk
            except (SkillError, OSError) as e:
                errors.append(str(e))
    load_all.errors = errors
    return out


load_all.errors = []


def get(sid: str) -> dict:
    sk = load_all().get(sid)
    if not sk:
        raise SkillError(f"没有找到 Skill：{sid}")
    return sk


def public(sk: dict) -> dict:
    return {k: sk[k] for k in ("id", "name", "version", "fingerprint", "summary", "reader", "length", "structure",
                               "style", "material", "images", "path")}
