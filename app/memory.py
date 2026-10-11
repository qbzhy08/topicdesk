"""长期记忆：写入、召回、更新、遗忘，以及失效的级联传播。

三层记忆：
- 任务内记忆：tasks.state（目标、检索历史、待核验问题、中间结果），由 Harness 管理，不在本模块。
- 主题长期记忆（scope=topic）：
    fact      重要事实及验证状态（meta.verify: supported/unverified/disputed/refuted），
              证据在 evidence 表（claim_id = 记忆 id），记录来源文档及版本
    dispute   未解争议（两条冲突事实）
    delivery  历史交付（交付了哪些资料、引用了哪些事实），derived_from 指向事实和文档
- 用户偏好与编辑反馈（scope=user）：
    preference  阅读时间、知识背景、关注重点、排除内容、写作要求（meta.type 区分）
    feedback    经用户确认可长期复用的编辑反馈

每条记忆都有：来源（source）、适用范围（applies_to）、更新时间、使用次数（used_count / last_used_at）、
派生关系（derived_from）。所有变化写入 memory_events，可追溯。

遗忘与失效：
- 删除（用户）：status=deleted，召回时排除；派生自它的记忆级联标记 invalid。
- 更正（用户或资料更正）：旧条目 superseded，新条目接替；派生条目标记 stale（需更正），并写明原因。
- 时效性事实到期（recheck_at）后标记 stale，召回时提示需要复查。
"""
from __future__ import annotations

import json
import re
import time

from . import db

ACTIVE = ("active", "stale")            # 可被召回的状态（stale 召回时带“需复查”提示）
DEAD = ("deleted", "invalid", "superseded")


# ---------------------------------------------------------------- 基础操作

def event(memory_id: str, ev: str, detail=None, task_id: str | None = None, actor: str = "agent"):
    db.insert("memory_events", dict(memory_id=memory_id, event=ev, detail=detail or {},
                                    task_id=task_id, actor=actor, created_at=db.now()))


def get(mid: str) -> dict | None:
    m = db.query_one("SELECT * FROM memories WHERE id=?", (mid,))
    if m and isinstance(m.get("meta"), str):
        m["meta"] = json.loads(m["meta"] or "{}")
    return m


def _row(m):
    if m and isinstance(m.get("meta"), str):
        m["meta"] = json.loads(m["meta"] or "{}")
    return m


def add(scope: str, kind: str, content: str, *, user_id: str = "default", topic: str | None = None,
        applies_to: str = "all", source: dict | None = None, derived_from: list | None = None,
        mkey: str | None = None, time_sensitive: bool = False, recheck_at: float | None = None,
        confidence: float | None = None, meta: dict | None = None, actor: str = "agent",
        task_id: str | None = None, note: str | None = None) -> str:
    mid = db.new_id("m_")
    db.insert("memories", dict(
        id=mid, scope=scope, user_id=user_id, topic=topic, kind=kind, content=content,
        applies_to=applies_to, source=source or {}, derived_from=derived_from or [],
        status="active", used_count=0, recheck_at=recheck_at, mkey=mkey,
        confidence=confidence, time_sensitive=1 if time_sensitive else 0, note=note,
        meta=db.dumps(meta or {}), created_at=db.now(), updated_at=db.now()))
    event(mid, "create", {"content": content[:200], "source": source}, task_id, actor)
    return mid


def update(mid: str, changes: dict, ev: str = "update", detail=None, *, actor: str = "agent",
           task_id: str | None = None):
    if "meta" in changes and not isinstance(changes["meta"], str):
        changes["meta"] = db.dumps(changes["meta"])
    changes["updated_at"] = db.now()
    db.update("memories", "id", mid, changes)
    event(mid, ev, detail, task_id, actor)


def find(scope: str, kind: str, mkey: str, *, topic: str | None = None, user_id: str = "default",
         include_dead: bool = False) -> dict | None:
    sql = "SELECT * FROM memories WHERE scope=? AND kind=? AND mkey=? AND user_id=?"
    args = [scope, kind, mkey, user_id]
    if topic is not None:
        sql += " AND topic=?"
        args.append(topic)
    if not include_dead:
        sql += " AND status IN ('active','stale')"
    return _row(db.query_one(sql + " ORDER BY updated_at DESC LIMIT 1", args))


def derived_of(mid: str) -> list[dict]:
    """直接派生自 mid 的记忆（derived_from 包含 mid）。"""
    return [_row(r) for r in db.query("SELECT * FROM memories WHERE derived_from LIKE ?", (f'%"{mid}"%',))]


def cascade(mid: str, new_status: str, reason: str, *, actor: str = "system", task_id=None,
            _seen: set | None = None) -> list[str]:
    """把派生自 mid 的记忆级联标记为 new_status（invalid 或 stale），返回受影响的 id。"""
    seen = _seen or set()
    hit = []
    for d in derived_of(mid):
        if d["id"] in seen or d["status"] in DEAD:
            continue
        seen.add(d["id"])
        if d["status"] == "archived":       # 已“不再参考”的交付：保持归档，只记下它也过期了
            update(d["id"], {"meta": {**(d["meta"] or {}), "stale_while_archived": reason}}, "stale",
                   {"upstream": mid, "reason": reason, "archived": True}, actor=actor, task_id=task_id)
            hit.append(d["id"])
            continue
        note = f"{reason}（上游 {mid}）"
        update(d["id"], {"status": new_status, "note": note}, "invalidate" if new_status == "invalid"
               else "stale", {"upstream": mid, "reason": reason}, actor=actor, task_id=task_id)
        hit.append(d["id"])
        hit += cascade(d["id"], new_status, reason, actor=actor, task_id=task_id, _seen=seen)
    return hit


def archive(mid: str, *, actor: str = "user", reason: str = "用户选择不再参考") -> dict:
    """历史交付“不再参考”：下次任务不召回，但记录保留、可以恢复（历史没有对错，不提供更正）。"""
    m = get(mid)
    if m is None:
        raise KeyError(mid)
    if m["kind"] != "delivery":
        raise ValueError("只有历史交付可以设为“不再参考”；偏好和事实请更正或删除")
    if m["status"] in DEAD:
        raise ValueError("这条记忆已失效，不能归档")
    meta = {**(m["meta"] or {}), "archived_from": m["status"]}
    update(mid, {"status": "archived", "note": reason, "meta": meta}, "archive", {"reason": reason}, actor=actor)
    return {"archived": mid}


def restore(mid: str, *, actor: str = "user") -> dict:
    m = get(mid)
    if m is None:
        raise KeyError(mid)
    if m["status"] != "archived":
        raise ValueError("只有“不再参考”的记忆可以恢复")
    meta = dict(m["meta"] or {})
    back = meta.pop("archived_from", "active")
    if meta.pop("stale_while_archived", None):
        back = "stale"          # 归档期间依据被更正：恢复后标为需更正
    update(mid, {"status": back, "note": None, "meta": meta}, "restore", {"to": back}, actor=actor)
    return {"restored": mid, "status": back}


def delivery_impact(mid: str) -> dict:
    """彻底删除前的影响说明。"""
    m = get(mid)
    meta = (m or {}).get("meta") or {}
    return {"docs": len(meta.get("docs") or []), "facts": sum(1 for x in (m or {}).get("derived_from") or []
                                                             if str(x).startswith("m_")),
            "task": meta.get("task"),
            "effects": ["下次检索无法与这次交付对比（“与上次相比”缺少这一次）",
                        "这次交付用到的事实以后被更正时，不会再提醒你这次交付已过期",
                        "资料本身、去重和“新进展”判断不受影响（它们依据资料库，不依据交付记录）"]}


def delete(mid: str, *, actor: str = "user", reason: str = "用户删除", task_id=None) -> dict:
    m = get(mid)
    if m is None:
        raise KeyError(mid)
    update(mid, {"status": "deleted", "note": reason}, "delete", {"reason": reason}, actor=actor,
           task_id=task_id)
    affected = cascade(mid, "invalid", f"依赖的记忆已删除：{reason}", actor="system", task_id=task_id)
    return {"deleted": mid, "invalidated": affected}


def correct(mid: str, new_content: str, *, actor: str = "user", reason: str = "用户更正",
            meta_patch: dict | None = None, task_id=None, derived_from: list | None = None) -> dict:
    """更正：旧条目 superseded，新条目接替（继承来源与范围），派生条目标记需更正。"""
    old = get(mid)
    if old is None:
        raise KeyError(mid)
    meta = {**(old["meta"] or {}), **(meta_patch or {}), "corrected_from": mid,
            "previous_content": old["content"]}
    new_id = add(old["scope"], old["kind"], new_content, user_id=old["user_id"], topic=old["topic"],
                 applies_to=old["applies_to"], source={**(old["source"] or {}), "corrected_by": actor},
                 derived_from=derived_from if derived_from is not None else old["derived_from"],
                 mkey=old["mkey"], time_sensitive=bool(old["time_sensitive"]),
                 recheck_at=old["recheck_at"], confidence=old["confidence"], meta=meta, actor=actor,
                 task_id=task_id, note=reason)
    # 证据复制给新条目（用户更正时证据仍然是原始资料，不是更正本身）；旧条目保留原证据，便于追溯
    db.execute("INSERT INTO evidence (claim_id, doc_id, snippet, stance, created_at) "
               "SELECT ?, doc_id, snippet, stance, ? FROM evidence WHERE claim_id=?", (new_id, db.now(), mid))
    update(mid, {"status": "superseded", "superseded_by": new_id, "note": reason}, "supersede",
           {"by": new_id, "reason": reason, "new_content": new_content[:200]}, actor=actor,
           task_id=task_id)
    affected = cascade(mid, "stale", f"依赖的结论已更正：{reason}", actor="system", task_id=task_id)
    return {"old": mid, "new": new_id, "stale": affected}


def expire_due(now: float | None = None) -> list[str]:
    """时效性记忆到期：标记 stale，召回时提示需要复查。"""
    now = now or db.now()
    rows = db.query("SELECT id FROM memories WHERE status='active' AND recheck_at IS NOT NULL "
                    "AND recheck_at<=?", (now,))
    for r in rows:
        update(r["id"], {"status": "stale", "note": "时效性信息到期，需要复查"}, "stale",
               {"reason": "recheck_due"}, actor="system")
    return [r["id"] for r in rows]


# ---------------------------------------------------------------- 召回

def _tokens(text: str) -> int:
    """粗略 token 估算：中文约 1 字 1 token，英文约 4 字符 1 token。"""
    cjk = len(re.findall(r"[一-鿿]", text))
    return cjk + (len(text) - cjk) // 4 + 4


def _render(m: dict) -> str:
    meta = m.get("meta") or {}
    if m["kind"] == "fact":
        v = meta.get("verify", "unverified")
        tag = {"supported": "已核实", "unverified": "待核实", "disputed": "有争议",
               "refuted": "已被否定"}.get(v, v)
        extra = "；需复查" if m["status"] == "stale" else ""
        return f"[事实·{tag}{extra}] {m['content']}（独立来源 {meta.get('independent', 0)}）"
    if m["kind"] == "dispute":
        return f"[未解争议] {m['content']}"
    if m["kind"] == "delivery":
        return f"[历史交付 {time.strftime('%m-%d %H:%M', time.localtime(m['created_at']))}] {m['content']}"
    if m["kind"] == "preference":
        return f"[长期偏好] {m['content']}"
    return f"[{m['kind']}] {m['content']}"


def recall(user_id: str = "default", topic: str | None = None, purpose: str = "research",
           budget_tokens: int = 1200, task_id: str | None = None, query_terms: list | None = None,
           record_use: bool = True) -> dict:
    """按“用户 → 主题 → 任务用途”召回，在 token 预算内取最有用的条目。
    排除已删除/失效/被取代的条目。返回 {preferences, disputes, facts, deliveries, text, used, dropped}。"""
    prefs = [_row(r) for r in db.query(
        "SELECT * FROM memories WHERE scope='user' AND user_id=? AND status IN ('active','stale') "
        "AND (applies_to IS NULL OR applies_to IN ('all', ?, ?)) ORDER BY updated_at DESC",
        (user_id, f"topic:{topic}", purpose))]
    prefs = [p for p in prefs if pref_valid(p)]      # 早期保存的无意义偏好（如“q”“12”）不召回
    topic_rows = [_row(r) for r in db.query(
        "SELECT * FROM memories WHERE scope='topic' AND topic=? AND status IN ('active','stale') "
        "ORDER BY updated_at DESC", (topic,))] if topic else []
    disputes = [m for m in topic_rows if m["kind"] == "dispute"]
    deliveries = [m for m in topic_rows if m["kind"] == "delivery"][:3]
    facts = [m for m in topic_rows if m["kind"] == "fact" and (m["meta"] or {}).get("verify") != "refuted"]

    terms = [t.lower() for t in (query_terms or [])]

    def fact_score(m):
        meta = m["meta"] or {}
        s = {"disputed": 3, "supported": 2, "unverified": 1}.get(meta.get("verify"), 0)
        s += 1.5 if m["status"] == "stale" else 0          # 需复查的优先提醒
        s += min(m["used_count"], 5) * 0.1
        s += sum(1 for t in terms if t in m["content"].lower()) * 0.5
        s += 0.5 if m["updated_at"] > time.time() - 7 * 86400 else 0
        return -s
    facts.sort(key=fact_score)

    out = {"preferences": [], "disputes": [], "facts": [], "deliveries": [], "dropped": 0}
    used, budget = [], budget_tokens
    for bucket, rows in (("preferences", prefs), ("disputes", disputes), ("deliveries", deliveries),
                         ("facts", facts)):
        for m in rows:
            line = _render(m)
            cost = _tokens(line)
            if cost > budget:
                out["dropped"] += 1
                continue
            budget -= cost
            out[bucket].append({"id": m["id"], "kind": m["kind"], "content": m["content"],
                                "line": line, "meta": m["meta"], "status": m["status"],
                                "applies_to": m["applies_to"]})
            used.append(m["id"])
    out["used"] = used
    out["tokens"] = budget_tokens - budget
    out["text"] = "\n".join(x["line"] for b in ("preferences", "disputes", "deliveries", "facts")
                            for x in out[b])
    if record_use and used:
        now = db.now()
        marks = ",".join("?" * len(used))
        db.execute(f"UPDATE memories SET used_count=used_count+1, last_used_at=? WHERE id IN ({marks})",
                   (now, *used))
        for mid in used:
            event(mid, "recall", {"purpose": purpose}, task_id, "agent")
    return out


# ---------------------------------------------------------------- 偏好

PREF_TYPES = {
    "reading_minutes": "阅读时间（分钟）",
    "background": "知识背景",
    "focus": "关注重点",
    "exclude": "不想看的内容",
    "source_prefer": "来源偏好",
    "style": "写作要求",
}


class PreferenceError(ValueError):
    pass


def validate_preference(ptype: str, value):
    """拦住无意义的输入（如“q”“5”当关注重点），返回规范化后的值。"""
    if ptype not in PREF_TYPES:
        raise PreferenceError("未知的偏好类型")
    if ptype == "reading_minutes":
        try:
            v = int(str(value).strip().rstrip("分钟min "))
        except ValueError as e:
            raise PreferenceError("阅读时间请填分钟数，例如 5") from e
        if not 1 <= v <= 240:
            raise PreferenceError("阅读时间应在 1 到 240 分钟之间")
        return v
    items = value if isinstance(value, (list, tuple)) else re.split(r"[,，、;；]", str(value))
    items = [str(x).strip() for x in items if str(x).strip()]
    if ptype in ("focus", "exclude"):
        bad = [x for x in items if len(x) < 2 or x.isdigit()]
        if bad or not items:
            raise PreferenceError(f"关键词至少 2 个字且不能是纯数字：{bad or '为空'}")
        return items
    if ptype == "source_prefer":
        ids = {r["id"]: r["id"] for r in db.query("SELECT id FROM sources")}
        ids.update({r["name"]: r["id"] for r in db.query("SELECT id, name FROM sources")})
        unknown = [x for x in items if x not in ids]
        if unknown or not items:
            raise PreferenceError(f"未知的来源：{unknown or '为空'}（可填来源名称或 ID，见“信息源”页）")
        return [ids[x] for x in items]
    text = "，".join(items)
    if len(text) < 2 or text.isdigit():
        raise PreferenceError("内容太短或是纯数字，请写清楚，例如：程序员，熟悉机器学习")
    if re.fullmatch(r"\s*\d+\s*(分钟|min|mins|minutes|分)\s*", text, re.I):
        raise PreferenceError(f"“{text}”看起来是阅读时间，请把类型选为“阅读时间（分钟）”")
    return text


def pref_valid(m: dict) -> bool:
    meta = m.get("meta") or {}
    if m.get("kind") != "preference" or not meta.get("type"):
        return True
    try:
        validate_preference(meta["type"], meta.get("value"))
        return True
    except PreferenceError:
        return False


def set_preference(user_id: str, ptype: str, value, *, applies_to: str = "all", actor: str = "user",
                   task_id=None) -> str:
    """长期偏好：只在用户明确确认“长期保存”时调用。同类型同范围的旧偏好被更正取代。"""
    value = validate_preference(ptype, value)
    if isinstance(value, (list, tuple)):
        text = "、".join(map(str, value))
    else:
        text = str(value)
    if ptype == "reading_minutes":
        text = f"{value} 分钟"
    label = PREF_TYPES.get(ptype, ptype)
    mkey = f"{ptype}@{applies_to}"
    old = find("user", "preference", mkey, user_id=user_id)
    meta = {"type": ptype, "label": label, "value": value}
    if old:
        if old["content"] == f"{label}：{text}":
            return old["id"]
        return correct(old["id"], f"{label}：{text}", actor=actor, reason="偏好更新",
                       meta_patch=meta, task_id=task_id)["new"]
    return add("user", "preference", f"{label}：{text}", user_id=user_id, applies_to=applies_to,
               source={"by": actor, "confirmed": True}, mkey=mkey, meta=meta, actor=actor,
               task_id=task_id)


def effective_preferences(recalled: dict, this_time: dict | None = None) -> dict:
    """合并长期偏好与本次要求：本次明确要求优先；本次要求不写入长期记忆。
    返回 {type: {value, from: 'long_term'|'this_time', memory_id}} 和冲突说明。"""
    eff, notes = {}, []
    for p in recalled.get("preferences", []):
        t = (p["meta"] or {}).get("type")
        if t and t not in eff:
            eff[t] = {"value": (p["meta"] or {}).get("value"), "from": "long_term", "memory_id": p["id"]}
    for t, v in (this_time or {}).items():
        if v in (None, "", []):
            continue
        if t in eff and eff[t]["value"] != v:
            notes.append(f"本次要求「{PREF_TYPES.get(t, t)}：{v}」覆盖长期偏好「{eff[t]['value']}」（仅本次生效）")
        eff[t] = {"value": v, "from": "this_time", "memory_id": None}
    return {"prefs": eff, "notes": notes}


# ---------------------------------------------------------------- 查询（给界面用）

def list_memories(scope: str | None = None, topic: str | None = None, kind: str | None = None,
                  status: str | None = None, user_id: str = "default") -> list[dict]:
    sql, args = "SELECT * FROM memories WHERE user_id=?", [user_id]
    for col, val in (("scope", scope), ("topic", topic), ("kind", kind)):
        if val:
            sql += f" AND {col}=?"
            args.append(val)
    if status == "alive":
        sql += " AND (status IN ('active','stale') OR (status='archived' AND kind='delivery'))"
    elif status:
        sql += " AND status=?"
        args.append(status)
    rows = [_row(r) for r in db.query(sql + " ORDER BY updated_at DESC LIMIT 500", args)]
    for r in rows:
        if r["kind"] == "preference" and not pref_valid(r):
            r["invalid_value"] = True
            v = str((r["meta"] or {}).get("value") or "")
            mm = re.fullmatch(r"\s*(\d+)\s*(分钟|min|mins|minutes|分)\s*", v, re.I)
            if mm and (r["meta"] or {}).get("type") != "reading_minutes":
                r["suggest"] = {"type": "reading_minutes", "value": int(mm.group(1)),
                                "label": f"改为“阅读时间：{mm.group(1)} 分钟”"}
    return rows


def detail(mid: str) -> dict:
    m = get(mid)
    if m is None:
        raise KeyError(mid)
    m["events"] = db.query("SELECT * FROM memory_events WHERE memory_id=? ORDER BY id", (mid,))
    m["evidence"] = db.query(
        "SELECT e.*, d.title, d.url, d.source_id, d.version, d.status doc_status, d.duplicate_of "
        "FROM evidence e JOIN documents d ON d.id=e.doc_id WHERE e.claim_id=? ORDER BY e.id", (mid,))
    m["derived"] = [{"id": d["id"], "kind": d["kind"], "status": d["status"], "content": d["content"][:120]}
                    for d in derived_of(mid)]
    ups = []
    for u in m.get("derived_from") or []:
        if str(u).startswith("m_"):
            x = get(u)
            if x:
                ups.append({"id": x["id"], "kind": x["kind"], "status": x["status"], "content": x["content"][:120]})
        elif str(u).startswith("d_"):
            d = db.query_one("SELECT id, title, status FROM documents WHERE id=?", (u,))
            if d:
                ups.append({"id": d["id"], "kind": "document", "status": d["status"], "content": d["title"]})
    m["upstream"] = ups
    if m.get("superseded_by"):
        m["successor"] = get(m["superseded_by"])
    return m
