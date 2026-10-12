"""写作 Skill：可加载的创作方式。

每个 Skill 是 skills/<id>/SKILL.md：YAML 头部声明读者、篇幅、结构、选材和配图要求，正文是给大模型的写作要求。
新增或修改 Skill 只需放一个文件，不改业务代码；每次写作记录 Skill 的 id、版本号和内容指纹，
同一版本号下内容被改过也能看出来。
"""
from __future__ import annotations

import hashlib
import re
import shutil
import time
from pathlib import Path

import yaml

from . import config

SKILL_DIR = Path(getattr(config, "SKILL_DIR", None) or Path(__file__).resolve().parent.parent / "skills")

REQUIRED = ("id", "name", "version", "reader", "structure")
DEFAULTS = {
    "summary": "", "length": "", "style": [], "body": "",
    "material": {"list": "30", "max_docs": 10, "prefer": [], "facts": "all"},
    "images": {"cover": 1, "body_min": 2, "prefer": [], "placement": ""},
    # 参考资料要求：至少引用几个不同来源、几篇一手资料，同一来源最多选几篇，是否要求日期
    "references": {"min_sources": 2, "min_primary": 0, "max_per_source": 4, "prefer": [], "dated": True},
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
        names = {"id": "id", "name": "名称", "version": "版本", "reader": "读者", "structure": "结构（至少一节）"}
        raise SkillError(f"还差必填项：{'、'.join(names.get(k, k) for k in miss)}" + (f"（{origin}）" if origin not in ("", "SKILL.md") else ""))
    sk = {**DEFAULTS, **head}
    sk["material"] = {**DEFAULTS["material"], **(head.get("material") or {})}
    sk["images"] = {**DEFAULTS["images"], **(head.get("images") or {})}
    sk["references"] = {**DEFAULTS["references"], **(head.get("references") or {})}
    for k in ("min_sources", "min_primary", "max_per_source"):
        try:
            sk["references"][k] = int(sk["references"][k])
        except (TypeError, ValueError) as e:
            raise SkillError(f"{origin}：references.{k} 应为整数") from e
    sk["material"]["list"] = str(sk["material"]["list"])
    secs = []
    for i, s in enumerate(sk["structure"]):
        if not isinstance(s, dict) or not s.get("heading"):
            raise SkillError(f"结构第 {i + 1} 节还没写小标题")
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
                               "style", "material", "images", "references", "path")} | {"author": sk.get("author"),
                                                                    "custom": bool(sk.get("author"))}


# ---------------------------------------------------------------- Skill 工坊：页面里新建、编辑、复制、删除（DIY）
# 保存就是写 skills/<id>/SKILL.md，和手工放文件完全等价；改动内容但没改版本号时自动升一个小版本，
# 旧内容存到 skills/<id>/history/，文章里记录的“版本号 + 指纹”都能找回当时的 Skill 原文。

ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,29}$")
PREFER_MATERIAL = ["new", "update", "primary", "disputed"]


def raw(sid: str) -> str:
    p = SKILL_DIR / sid / "SKILL.md"
    if not p.exists():
        raise SkillError(f"没有找到 Skill：{sid}")
    return p.read_text(encoding="utf-8")


def _bump(version: str) -> str:
    parts = (str(version).split(".") + ["0", "0"])[:3]
    try:
        parts[2] = str(int(parts[2]) + 1)
    except ValueError:
        parts.append("1")
    return ".".join(parts)


def _set_head(text: str, key: str, value: str) -> str:
    if re.search(rf"^{key}:.*$", text, re.M):
        return re.sub(rf"^{key}:.*$", f"{key}: {value}", text, count=1, flags=re.M)
    return re.sub(r"^---\s*\n", f"---\n{key}: {value}\n", text, count=1)


def lint(sk: dict) -> list[str]:
    """不影响加载、但可能写不好的地方（给编辑器提示）。"""
    warn = []
    if len(sk["structure"]) < 2:
        warn.append("结构只有 1 节：文章容易写成一大段")
    if len(sk["structure"]) > 8:
        warn.append("结构超过 8 节：每节会很短")
    total = sum(x["words"] for x in sk["structure"])
    nums = [int(x) for x in re.findall(r"\d+", str(sk.get("length") or ""))]
    if nums and not (min(nums) * 0.6 <= total <= max(nums) * 1.4):
        warn.append(f"各节字数合计 {total}，和篇幅 {sk['length']} 差得较多")
    if not sk.get("style"):
        warn.append("没有写风格要求")
    if not any("【分析】" in x["goal"] for x in sk["structure"]):
        warn.append("没有哪一节要求写【分析】：文章只会复述资料，没有判断")
    ids = [x["id"] for x in sk["structure"]]
    if len(ids) != len(set(ids)):
        warn.append("结构里有重复的节 id")
    if sk["references"]["min_primary"] > sk["material"].get("max_docs", 10):
        warn.append("一手资料要求比选材上限还多")
    return warn


def validate(text: str) -> dict:
    sk = parse(text, "SKILL.md")
    if not ID_RE.match(sk["id"]):
        raise SkillError("id 只能用小写英文字母、数字、- 和 _，以字母开头，2-30 个字符，例如 my-weekly")
    return {"skill": public({**sk, "path": f"skills/{sk['id']}/SKILL.md"}) | {"body": sk["body"]}, "warnings": lint(sk)}


def save(text: str, editing: str | None = None) -> dict:
    """新建（editing=None）或修改 Skill。返回 {skill, warnings, bumped, previous}。"""
    text = text.replace("\r\n", "\n").strip() + "\n"
    v = validate(text)
    sid = v["skill"]["id"]
    d = SKILL_DIR / sid
    path = d / "SKILL.md"
    if editing is None and path.exists():
        raise SkillError(f"已经有 id 为 {sid} 的 Skill，请换一个 id，或去编辑它")
    if editing is not None and editing != sid:
        raise SkillError("编辑时不能改 id；要换 id 请用“复制为新 Skill”")
    bumped, previous = False, None
    if path.exists():
        old_text = path.read_text(encoding="utf-8")
        if old_text == text:
            return {**v, "bumped": False, "previous": None, "unchanged": True}
        old = parse(old_text, "old")
        if str(old["version"]) == v["skill"]["version"]:          # 内容变了但没改版本号：自动升小版本
            text = _set_head(text, "version", _bump(old["version"]))
            v = validate(text)
            bumped = True
        hist = d / "history"
        hist.mkdir(exist_ok=True)
        previous = f"SKILL.v{old['version']}.{old['fingerprint']}.md"
        (hist / previous).write_text(old_text, encoding="utf-8")
    d.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return {**v, "bumped": bumped, "previous": previous, "unchanged": False}


def history(sid: str) -> list[dict]:
    hist = SKILL_DIR / sid / "history"
    out = []
    if hist.exists():
        for p in sorted(hist.glob("SKILL.v*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
            m = re.match(r"SKILL\.v(.+)\.([0-9a-f]{10})\.md$", p.name)
            if m:
                out.append({"file": p.name, "version": m.group(1), "fingerprint": m.group(2), "saved_at": p.stat().st_mtime})
    return out


def history_text(sid: str, fname: str) -> str:
    if not re.match(r"^SKILL\.v[\w.]+\.[0-9a-f]{10}\.md$", fname):
        raise SkillError("文件名不对")
    p = SKILL_DIR / sid / "history" / fname
    if not p.exists():
        raise SkillError("没有这个历史版本")
    return p.read_text(encoding="utf-8")


def find_by_fingerprint(sid: str, fp: str) -> str | None:
    """文章里记录的 Skill 指纹 → 当时的 Skill 原文（当前版本或历史版本）。"""
    try:
        cur = raw(sid)
        if hashlib.sha1(cur.encode()).hexdigest()[:10] == fp:
            return cur
    except SkillError:
        pass
    for h in history(sid):
        if h["fingerprint"] == fp:
            return history_text(sid, h["file"])
    return None


def remove(sid: str) -> str:
    """删除：整个目录移到 skills/.trash/（可以手工找回），已写好的文章不受影响。"""
    d = SKILL_DIR / sid
    if not (d / "SKILL.md").exists():
        raise SkillError(f"没有找到 Skill：{sid}")
    trash = SKILL_DIR / ".trash"
    trash.mkdir(exist_ok=True)
    dest = trash / f"{sid}-{int(time.time())}"
    shutil.move(str(d), str(dest))
    return str(dest.relative_to(SKILL_DIR.parent))


def from_form(f: dict) -> str:
    """表单 → SKILL.md 文本（页面上的“表单编辑”）。"""
    def lst(x):
        return [str(i).strip() for i in (x if isinstance(x, list) else str(x or "").split("\n")) if str(i).strip()]
    head = {
        "id": str(f.get("id") or "").strip(), "name": str(f.get("name") or "").strip(),
        "version": str(f.get("version") or "1.0.0").strip(), "summary": str(f.get("summary") or "").strip(),
        "reader": str(f.get("reader") or "").strip(), "length": str(f.get("length") or "").strip(),
        "material": {"list": str((f.get("material") or {}).get("list") or "30"),
                     "max_docs": int((f.get("material") or {}).get("max_docs") or 10),
                     "prefer": [x for x in lst((f.get("material") or {}).get("prefer")) if x in PREFER_MATERIAL],
                     "facts": (f.get("material") or {}).get("facts") or "all"},
        "references": {k: int((f.get("references") or {}).get(k) or d) for k, d in
                       (("min_sources", 2), ("min_primary", 0), ("max_per_source", 3))}
                      | {"dated": bool((f.get("references") or {}).get("dated", True))},
        "structure": [{"id": str(x.get("id") or f"s{i + 1}").strip(), "heading": str(x.get("heading") or "").strip(),
                       "goal": str(x.get("goal") or "").strip(), "words": int(x.get("words") or 250)}
                      for i, x in enumerate(f.get("structure") or []) if str(x.get("heading") or "").strip()],
        "style": lst(f.get("style")),
        "images": {"cover": int((f.get("images") or {}).get("cover") or 0),
                   "body_min": int((f.get("images") or {}).get("body_min") or 0),
                   "prefer": lst((f.get("images") or {}).get("prefer")),
                   "placement": str((f.get("images") or {}).get("placement") or "")},
    }
    if f.get("author"):
        head["author"] = str(f["author"])
    base = re.match(r"^---\s*\n(.*?)\n---", f.get("base") or "", re.S)
    if base:                       # 编辑已有 Skill：表单里没有的字段（如 references.prefer）原样保留
        old = yaml.safe_load(base.group(1)) or {}
        merged = dict(old)
        for k, v in head.items():
            merged[k] = {**old[k], **v} if isinstance(v, dict) and isinstance(old.get(k), dict) else v
        head = merged
    y = yaml.safe_dump(head, allow_unicode=True, sort_keys=False, width=200)
    return f"---\n{y}---\n{str(f.get('body') or '').strip()}\n"
