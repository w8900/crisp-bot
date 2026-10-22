import time
from datetime import datetime, timedelta, timezone

import aiosqlite

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    topic_id   INTEGER NOT NULL,
    nickname   TEXT DEFAULT '',
    email      TEXT DEFAULT '',
    ai_enabled INTEGER DEFAULT 1,
    last_human_at REAL DEFAULT 0,
    topic_created_at TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_topic ON sessions(topic_id);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL,           -- user / assistant / human
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS tg_members (
    user_id  INTEGER PRIMARY KEY,
    username TEXT DEFAULT '',
    name     TEXT DEFAULT '',
    is_bot   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ai_answers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_message_id INTEGER NOT NULL,
    session_id    TEXT NOT NULL,
    question      TEXT DEFAULT '',
    answer        TEXT NOT NULL,
    sources       TEXT DEFAULT '',       -- AI 回答所依据的知识库条目(| 分隔)
    rating        TEXT DEFAULT '',       -- '' / good / bad
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_answers_msg ON ai_answers(tg_message_id);

CREATE TABLE IF NOT EXISTS kb_candidates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    question   TEXT DEFAULT '',
    answer     TEXT NOT NULL,
    status     TEXT DEFAULT 'pending',  -- pending / merged / discarded
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    event_type TEXT NOT NULL,           -- low_conf / keyword_handoff
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_report_events_time ON report_events(created_at, event_type);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def init():
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(config.DB_PATH) as conn:
        await conn.executescript(SCHEMA)
        try:
            await conn.execute("ALTER TABLE sessions ADD COLUMN last_human_at REAL DEFAULT 0")
        except aiosqlite.OperationalError:
            pass  # 列已存在
        try:
            await conn.execute("ALTER TABLE sessions ADD COLUMN topic_created_at TEXT DEFAULT ''")
        except aiosqlite.OperationalError:
            pass  # 列已存在
        try:
            await conn.execute("ALTER TABLE ai_answers ADD COLUMN sources TEXT DEFAULT ''")
        except aiosqlite.OperationalError:
            pass  # 列已存在
        await conn.commit()


async def _fetchone(sql: str, args=()):
    async with aiosqlite.connect(config.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(sql, args)
        row = await cur.fetchone()
        return dict(row) if row else None


async def _fetchall(sql: str, args=()):
    async with aiosqlite.connect(config.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(sql, args)
        return [dict(r) for r in await cur.fetchall()]


async def _exec(sql: str, args=()):
    async with aiosqlite.connect(config.DB_PATH) as conn:
        await conn.execute(sql, args)
        await conn.commit()


# ---- sessions ----

async def get_session(session_id: str):
    return await _fetchone("SELECT * FROM sessions WHERE session_id = ?", (session_id,))


async def get_by_topic(topic_id: int):
    return await _fetchone("SELECT * FROM sessions WHERE topic_id = ?", (topic_id,))


async def create_session(session_id: str, topic_id: int, nickname: str, email: str):
    now = _now()
    await _exec(
        "INSERT INTO sessions "
        "(session_id, topic_id, nickname, email, topic_created_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, topic_id, nickname, email, now, now),
    )
    return await get_session(session_id)


async def update_session_topic(
    session_id: str,
    topic_id: int,
    nickname: str,
    email: str,
):
    """回访会话换新 TG 话题，并记录话题创建时间。"""
    await _exec(
        "UPDATE sessions SET topic_id = ?, nickname = ?, email = ?, "
        "topic_created_at = ? WHERE session_id = ?",
        (topic_id, nickname, email, _now(), session_id),
    )
    return await get_session(session_id)


async def set_ai(session_id: str, enabled: bool):
    await _exec("UPDATE sessions SET ai_enabled = ? WHERE session_id = ?", (int(enabled), session_id))


async def pause_ai_if_enabled(session_id: str) -> bool:
    """原子暂停 AI；仅在状态从开启变为暂停时返回 True。"""
    async with aiosqlite.connect(config.DB_PATH) as conn:
        cur = await conn.execute(
            "UPDATE sessions SET ai_enabled = 0 "
            "WHERE session_id = ? AND ai_enabled = 1",
            (session_id,),
        )
        await conn.commit()
        return (cur.rowcount or 0) > 0


async def touch_human(session_id: str):
    """记录最后一次人工回复时间,用于 AI 超时自动恢复。"""
    await _exec("UPDATE sessions SET last_human_at = ? WHERE session_id = ?", (time.time(), session_id))


# ---- messages ----

async def add_message(session_id: str, role: str, content: str):
    await _exec(
        "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        (session_id, role, content, _now()),
    )


async def get_history(session_id: str, limit: int = 20):
    rows = await _fetchall(
        "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
        (session_id, limit),
    )
    return list(reversed(rows))


async def paused_sessions():
    return await _fetchall("SELECT * FROM sessions WHERE ai_enabled = 0")


async def last_message(session_id: str):
    return await _fetchone(
        "SELECT role, content, created_at FROM messages "
        "WHERE session_id = ? ORDER BY id DESC LIMIT 1",
        (session_id,),
    )


async def last_activity_at(session_id: str) -> float:
    """返回会话最后一条消息的 Unix 时间；没有消息则为 0。"""
    row = await _fetchone(
        "SELECT created_at FROM messages WHERE session_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (session_id,),
    )
    if not row:
        return 0
    try:
        return datetime.fromisoformat(row["created_at"]).timestamp()
    except (TypeError, ValueError):
        return 0


async def last_user_message(session_id: str) -> str:
    row = await _fetchone(
        "SELECT content FROM messages WHERE session_id = ? AND role = 'user' ORDER BY id DESC LIMIT 1",
        (session_id,),
    )
    return row["content"] if row else ""


# ---- 每日统计 ----

async def daily_stats(since_iso: str) -> dict:
    row = await _fetchone(
        """SELECT
          (SELECT COUNT(*) FROM sessions WHERE created_at >= ?) AS new_sessions,
          (SELECT COUNT(*) FROM messages WHERE created_at >= ? AND role = 'user') AS visitor_messages,
          (SELECT COUNT(*) FROM messages WHERE created_at >= ? AND role = 'assistant') AS ai_replies,
          (SELECT COUNT(*) FROM messages WHERE created_at >= ? AND role = 'human') AS human_replies,
          (SELECT COUNT(DISTINCT session_id) FROM messages WHERE created_at >= ? AND role = 'human') AS human_sessions,
          (SELECT COUNT(*) FROM report_events WHERE created_at >= ? AND event_type = 'low_conf') AS low_conf,
          (SELECT COUNT(*) FROM report_events WHERE created_at >= ? AND event_type = 'keyword_handoff') AS keyword_handoff,
          (SELECT COUNT(*) FROM ai_answers WHERE created_at >= ? AND rating = 'good') AS ai_good,
          (SELECT COUNT(*) FROM ai_answers WHERE created_at >= ? AND rating = 'bad') AS ai_bad,
          (SELECT COUNT(*) FROM ai_answers WHERE created_at >= ? AND rating = 'improved') AS ai_improved,
          (SELECT COUNT(*) FROM kb_candidates WHERE created_at >= ?) AS kb_candidates,
          (SELECT COUNT(*) FROM kb_candidates WHERE status = 'pending') AS pending_learning""",
        (since_iso,) * 11,
    )
    return row or {}


async def add_report_event(session_id: str, event_type: str):
    await _exec(
        "INSERT INTO report_events (session_id, event_type, created_at) VALUES (?, ?, ?)",
        (session_id, event_type, _now()),
    )


# ---- 键值配置 ----

async def get_setting(key: str) -> str | None:
    row = await _fetchone("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else None


async def set_setting(key: str, value: str):
    await _exec(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# ---- 隐私:定期清理过期对话文字(Crisp 云端有完整记录) ----

async def purge_old(hours: float) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    async with aiosqlite.connect(config.DB_PATH) as conn:
        c1 = await conn.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
        c2 = await conn.execute("DELETE FROM ai_answers WHERE created_at < ?", (cutoff,))
        # 待学习的候选保留到被处理为止,已处理的过期即删
        c3 = await conn.execute(
            "DELETE FROM kb_candidates WHERE created_at < ? AND status != 'pending'", (cutoff,)
        )
        await conn.commit()
        deleted = (c1.rowcount or 0) + (c2.rowcount or 0) + (c3.rowcount or 0)
        if deleted:
            await conn.execute("VACUUM")
        return deleted


# ---- TG 群成员(用于 @ 全员转人工) ----

async def upsert_member(user_id: int, username: str, name: str, is_bot: bool):
    await _exec(
        "INSERT INTO tg_members (user_id, username, name, is_bot) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, name=excluded.name, is_bot=excluded.is_bot",
        (user_id, username, name, int(is_bot)),
    )


async def get_members():
    return await _fetchall("SELECT * FROM tg_members WHERE is_bot = 0")


# ---- 打分:AI 回答与评分 ----

async def add_ai_answer(
    tg_message_ids: list[int],
    session_id: str,
    question: str,
    answer: str,
    sources: list[str] | None = None,
):
    src = "|".join(sources or [])
    for mid in tg_message_ids:
        await _exec(
            "INSERT INTO ai_answers (tg_message_id, session_id, question, answer, sources, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (mid, session_id, question, answer, src, _now()),
        )


async def get_ai_answer_by_msg(tg_message_id: int):
    return await _fetchone("SELECT * FROM ai_answers WHERE tg_message_id = ?", (tg_message_id,))


async def rate_ai_answer(answer_id: int, rating: str):
    await _exec("UPDATE ai_answers SET rating = ? WHERE id = ?", (rating, answer_id))


# ---- 学习闭环:人工答案候选 ----

async def add_kb_candidate(session_id: str, question: str, answer: str):
    await _exec(
        "INSERT INTO kb_candidates (session_id, question, answer, created_at) VALUES (?, ?, ?, ?)",
        (session_id, question, answer, _now()),
    )


async def pending_candidates(limit: int = 50):
    return await _fetchall(
        "SELECT * FROM kb_candidates WHERE status = 'pending' ORDER BY id DESC LIMIT ?", (limit,)
    )


async def set_candidates_status(ids: list[int], status: str):
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    await _exec(f"UPDATE kb_candidates SET status = ? WHERE id IN ({marks})", (status, *ids))
