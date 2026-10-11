"""SQLite 持久层。

设计要点：
- 每个线程独立连接，WAL 模式，支持 Web 线程与 Agent 后台线程并发读写。
- JSON 字段统一用 dumps/loads 辅助函数，行对象转 dict 方便 API 直接返回。
- 证据链：documents → claims ← evidence，文章段落通过 citations 指向 claim，
  记忆通过 derived_from 指向上游记忆 / claim，删除时可级联失效。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

from . import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    graph TEXT NOT NULL,              -- 使用哪张编排图
    topic TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT 'default',
    status TEXT NOT NULL,             -- pending/running/waiting_human/interrupted/done/failed/cancelled
    params TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT '{}', -- 任务内记忆（最新快照）
    budget TEXT NOT NULL DEFAULT '{}',
    usage TEXT NOT NULL DEFAULT '{}',
    current_node TEXT,
    error TEXT,
    parent_task_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    node TEXT NOT NULL,
    status TEXT NOT NULL,             -- running/ok/failed/interrupted/skipped
    thought TEXT,                     -- 简要决策依据
    summary TEXT,                     -- 观察结果摘要
    next_node TEXT,
    error TEXT,
    started_at REAL NOT NULL,
    ended_at REAL
);
CREATE INDEX IF NOT EXISTS idx_steps_task ON steps(task_id, seq);

CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    step_id INTEGER,
    tool TEXT NOT NULL,
    input TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    output TEXT,
    status TEXT NOT NULL,             -- ok/error/cached
    error TEXT,
    latency_ms INTEGER,
    cost REAL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_task ON tool_calls(task_id, id);
CREATE INDEX IF NOT EXISTS idx_tool_hash ON tool_calls(task_id, tool, input_hash);

CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    step_seq INTEGER NOT NULL,
    next_node TEXT,
    state TEXT NOT NULL,
    usage TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ckpt_task ON checkpoints(task_id, id);

CREATE TABLE IF NOT EXISTS hitl_requests (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    node TEXT NOT NULL,
    kind TEXT NOT NULL,               -- approve/choose/edit/text
    prompt TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    response TEXT,
    status TEXT NOT NULL,             -- open/resolved/cancelled
    created_at REAL NOT NULL,
    resolved_at REAL
);

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    method TEXT NOT NULL,             -- api/rss/html/browser
    base_url TEXT,
    config TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    health TEXT NOT NULL DEFAULT '{}' -- 最近失败、限流信息
);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    source_id TEXT,
    topic TEXT,
    title TEXT,
    author TEXT,
    published_at TEXT,
    fetched_at REAL NOT NULL,
    content_kind TEXT NOT NULL,       -- full/excerpt/summary
    text TEXT,
    summary TEXT,
    content_hash TEXT,
    simhash TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    prev_version_id TEXT,             -- 同一 URL 的上一版本
    duplicate_of TEXT,                -- 转载/重复归并到的主文档
    event_id TEXT,                    -- 同一事件聚类
    novelty TEXT,                     -- new/update/background/duplicate
    locator TEXT NOT NULL DEFAULT '{}', -- 原文定位（url、页码、锚点）
    meta TEXT NOT NULL DEFAULT '{}',
    recheck_at REAL,
    status TEXT NOT NULL DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS idx_doc_canon ON documents(canonical_url);
CREATE INDEX IF NOT EXISTS idx_doc_topic ON documents(topic);

CREATE TABLE IF NOT EXISTS images (
    id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    url TEXT NOT NULL,
    local_path TEXT,
    width INTEGER, height INTEGER,
    caption TEXT,
    context_text TEXT,
    position INTEGER,
    license TEXT,
    sha256 TEXT,
    phash TEXT,
    status TEXT NOT NULL DEFAULT 'kept', -- kept/rejected
    reject_reason TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unverified', -- unverified/supported/disputed/refuted/stale
    confidence REAL DEFAULT 0,
    time_sensitive INTEGER DEFAULT 0,
    recheck_at REAL,
    superseded_by TEXT,
    note TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    snippet TEXT,
    stance TEXT NOT NULL,             -- support/refute/mention
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,              -- topic/user（任务内记忆在 tasks.state）
    user_id TEXT NOT NULL DEFAULT 'default',
    topic TEXT,
    kind TEXT NOT NULL,               -- fact/delivery/dispute/preference/feedback
    content TEXT NOT NULL,
    applies_to TEXT,                  -- 适用范围，如 "skill:brief" / "all"
    source TEXT NOT NULL DEFAULT '{}',-- 来源：task_id / claim_id / 用户确认
    derived_from TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active', -- active/stale/invalid/deleted
    used_count INTEGER NOT NULL DEFAULT 0,
    last_used_at REAL,
    recheck_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS articles (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    topic TEXT NOT NULL,
    skill TEXT,
    skill_version TEXT,
    title TEXT,
    current_version INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS article_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    markdown TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',  -- citations、配图、生成参数
    note TEXT,
    created_at REAL NOT NULL
);
"""

JSON_FIELDS = {
    "params", "state", "budget", "usage", "input", "output", "payload",
    "response", "config", "health", "locator", "meta", "source", "derived_from", "detail", "data",
}


def now() -> float:
    return time.time()


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def dumps(v) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def conn() -> sqlite3.Connection:
    """线程本地连接。测试中切换 DB_PATH 后调用 reset() 即可。"""
    c = getattr(_local, "conn", None)
    if c is None or getattr(_local, "path", None) != str(config.DB_PATH):
        c = sqlite3.connect(str(config.DB_PATH), timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn, _local.path = c, str(config.DB_PATH)
    return c


def reset():
    c = getattr(_local, "conn", None)
    if c is not None:
        c.close()
    _local.conn = None


MIGRATIONS = [
    # (表, 列, 类型)：已有数据库升级时补列
    ("memories", "mkey", "TEXT"),
    ("memories", "confidence", "REAL"),
    ("memories", "time_sensitive", "INTEGER DEFAULT 0"),
    ("memories", "superseded_by", "TEXT"),
    ("memories", "note", "TEXT"),
    ("memories", "meta", "TEXT NOT NULL DEFAULT '{}'"),
]

EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT NOT NULL,
    event TEXT NOT NULL,      -- create/update/support/conflict/verify/recall/correct/delete/invalidate/supersede
    detail TEXT,
    task_id TEXT,
    actor TEXT NOT NULL DEFAULT 'agent',   -- agent / user / system
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memev ON memory_events(memory_id, id);
CREATE INDEX IF NOT EXISTS idx_mem_topic ON memories(scope, topic, status);
CREATE INDEX IF NOT EXISTS idx_evidence_claim ON evidence(claim_id);
-- Lv2：每次价值评估存一个版本（初评、补充核验后重评、用户标注后重评），用于对比“重新评估”前后的变化
CREATE TABLE IF NOT EXISTS rankings (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    trigger TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rankings_task ON rankings(task_id, version);
-- Lv3 评测：读者盲评（看不到哪篇是初稿/优化稿）和外部 AIGC 检测器分数（手动录入）
CREATE TABLE IF NOT EXISTS eval_ratings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL,
    pair_id TEXT NOT NULL,
    rater TEXT NOT NULL,
    data TEXT NOT NULL,          -- {维度: "甲"/"乙"/"相当", ai_guess, comment}；甲乙对应哪篇只记在实验里
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evr ON eval_ratings(experiment_id, pair_id);
CREATE TABLE IF NOT EXISTS eval_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL,
    run_key TEXT NOT NULL,
    detector TEXT NOT NULL,
    score REAL NOT NULL,         -- AI 生成概率 0-1
    note TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evs ON eval_scores(experiment_id, run_key);
-- 人工相关性标注（按主题），用于计算 P@K；没有人工标注时用自动标注（银标）并注明
CREATE TABLE IF NOT EXISTS labels (
    topic TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    label INTEGER NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (topic, doc_id)
);
"""


def init_db():
    c = conn()
    c.executescript(SCHEMA)
    for table, col, typ in MIGRATIONS:
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    c.executescript(EXTRA_SCHEMA)
    # 修复旧数据：超长标题（如整篇 README 被当成仓库简介）截断
    c.execute("UPDATE documents SET title = substr(replace(title, 'GitHub - ', ''), 1, 160) || '…' "
              "WHERE length(title) > 200")
    c.commit()


@contextmanager
def tx():
    c = conn()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise


def row_to_dict(row) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for k in JSON_FIELDS & d.keys():
        if isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except json.JSONDecodeError:
                pass
    return d


def query(sql: str, params=()) -> list[dict]:
    return [row_to_dict(r) for r in conn().execute(sql, params).fetchall()]


def query_one(sql: str, params=()) -> dict | None:
    return row_to_dict(conn().execute(sql, params).fetchone())


def execute(sql: str, params=()) -> int:
    with tx() as c:
        cur = c.execute(sql, params)
        return cur.lastrowid


def insert(table: str, data: dict) -> int:
    data = {k: (dumps(v) if k in JSON_FIELDS and not isinstance(v, str) else v)
            for k, v in data.items()}
    cols = ", ".join(data)
    marks = ", ".join("?" for _ in data)
    return execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(data.values()))


def update(table: str, key: str, key_val, data: dict):
    data = {k: (dumps(v) if k in JSON_FIELDS and not isinstance(v, str) else v)
            for k, v in data.items()}
    sets = ", ".join(f"{k}=?" for k in data)
    execute(f"UPDATE {table} SET {sets} WHERE {key}=?", (*data.values(), key_val))
