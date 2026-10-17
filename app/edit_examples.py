"""把编辑亲手改过的句子变成写作示例（改前 → 改后）。

来源：审阅页里“直接修改”某一段（op=text）。大模型按要求改写的不算——那不是人写的。
1) 对齐：改前、改后的段落按句切开，每个改后的句子找最像的改前句子（字二元组重合 ≥ 0.3）；
   完全相同的、只改了出处编号或标点的不要；改了数字的是事实更正，不是写法，不要。
2) 存进 edit_examples 表：改前、改后、改了什么（AI 腔句式去掉了 / 变短了 / 换了说法）、来自哪个 Skill、哪篇文章。
3) 写作时“去 AI 味”那一步：按和这一节文字的相似度挑 2-3 条，放进提示词当示范（照着改的方向，不照抄内容），
   同 Skill 的优先、近期的优先；用了哪几条记在写作记录里，可以追溯。
4) 示例可以在“判断校准”同页的列表里删掉（改错了、或只是个人一时的口味）。
"""
from __future__ import annotations

import re

from . import db, writing

MIN_LEN, MAX_LEN = 8, 160


def _sents(text: str) -> list[str]:
    out = []
    for p in re.split(r"(?<=[。！？!?；;])|\n+", text or ""):
        p = p.strip().lstrip("-*> ").strip()
        if p and not p.startswith("|") and not p.startswith("{{img"):
            out.append(p)
    return out


def _core(s: str) -> str:
    return re.sub(r"[\s\W_]+", "", writing.CITE.sub("", s or ""))


def classify(before: str, after: str) -> str | None:
    """改了什么。返回 None 表示不是写法上的修改（没实质变化、或者改的是事实）。"""
    b, a = _core(before), _core(after)
    if not b or not a or b == a:
        return None
    nb = set(writing.numbers(writing.CITE.sub("", before))) - writing.NOISE_NUMS
    na = set(writing.numbers(writing.CITE.sub("", after))) - writing.NOISE_NUMS
    if nb != na:
        return None                        # 改数字是事实更正，交给核对，不当写法示例
    if writing.trope_sentences(after) and not writing.trope_sentences(before):
        return None                        # 越改越像 AI 腔（加了“这意味着”之类）的不当示范
    kinds = []
    if writing.trope_sentences(before) and not writing.trope_sentences(after):
        kinds.append("去掉 AI 腔句式")
    if len(a) <= len(b) * 0.75:
        kinds.append("改短")
    if "——" in before and "——" not in after:
        kinds.append("去掉破折号")
    if writing.HEDGE.search(after) and not writing.HEDGE.search(before):
        kinds.append("补上限制条件")
    return "、".join(kinds) or "换了说法"


def pairs_from_edit(before: str, after: str) -> list[dict]:
    """改前、改后两段文字 → 句子级的修改对。"""
    bs = [s for s in _sents(before) if MIN_LEN <= len(_core(s)) <= MAX_LEN]
    out, used = [], set()
    for s in _sents(after):
        if not (MIN_LEN <= len(_core(s)) <= MAX_LEN):
            continue
        best, score = None, 0.0
        for k, b in enumerate(bs):
            if k in used:
                continue
            o = max(writing.overlap(s, b), writing.overlap(b, s))
            if o > score:
                best, score = k, o
        if best is None or score < 0.3:
            continue
        kind = classify(bs[best], s)
        if not kind:
            used.add(best)
            continue
        used.add(best)
        out.append({"before": writing.CITE.sub("", bs[best]).strip(), "after": writing.CITE.sub("", s).strip(),
                    "kind": kind})
    return out


def record(before: str, after: str, *, skill: str | None, article_id: str | None, topic: str | None,
           user_id: str = "default") -> list[dict]:
    got = pairs_from_edit(before, after)
    for x in got:
        if db.query_one("SELECT 1 FROM edit_examples WHERE before_text=? AND after_text=?", (x["before"], x["after"])):
            continue
        db.insert("edit_examples", dict(id=db.new_id("ex_"), user_id=user_id, skill=skill, topic=topic,
                                        article_id=article_id, before_text=x["before"], after_text=x["after"],
                                        kind=x["kind"], used=0, created_at=db.now()))
    return got


def list_examples(user_id: str = "default", limit: int = 100) -> list[dict]:
    return db.query("SELECT * FROM edit_examples WHERE user_id=? ORDER BY created_at DESC LIMIT ?", (user_id, limit))


def delete(ex_id: str) -> bool:
    if not db.query_one("SELECT 1 FROM edit_examples WHERE id=?", (ex_id,)):
        return False
    db.execute("DELETE FROM edit_examples WHERE id=?", (ex_id,))
    return True


def pick(text: str, skill: str | None, k: int = 3, user_id: str = "default") -> list[dict]:
    """给这一节挑示例：和这一节文字最像的优先（字二元组重合），同 Skill 加分，越新越优先。"""
    rows = db.query("SELECT * FROM edit_examples WHERE user_id=? ORDER BY created_at DESC LIMIT 300", (user_id,))
    if not rows:
        return []
    grams = writing.bigrams(writing.CITE.sub("", text or ""))
    scored = []
    for i, r in enumerate(rows):
        g = writing.bigrams(r["before_text"])
        sim = len(g & grams) / len(g) if g else 0.0
        score = sim + (0.15 if skill and r["skill"] == skill else 0) + (0.1 if "AI 腔" in (r["kind"] or "") else 0) \
            - 0.001 * i
        scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    out, seen = [], set()
    for _, r in scored:
        if r["kind"] in seen and len(out) >= 2:
            continue                       # 示例类型尽量不重复
        out.append(r)
        seen.add(r["kind"])
        if len(out) >= k:
            break
    return out


def prompt_block(examples: list[dict]) -> str:
    if not examples:
        return ""
    return ("\n编辑过去亲手改过的句子（改前 → 改后）。照着改的方向改，不要照抄内容：\n" +
            "\n".join(f"   - 改前：{e['before_text']}\n     改后：{e['after_text']}（{e['kind']}）" for e in examples))


def mark_used(ids: list[str]):
    for i in ids:
        db.execute("UPDATE edit_examples SET used=used+1 WHERE id=?", (i,))
