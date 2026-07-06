"""AICC 콜백 요청 큐 DB.

상담 실패/차단/짧은 통화 등으로 다시 연락해야 하는 고객 대기열. call_log_db와 같은 base dir.
우선순위 1(낮음)~5(긴급, 기본 3), 상태는 pending/in_progress/done/failed/cancelled.
취소는 물리 삭제 안 하고 status='cancelled'로만 두고, 재시도는 retry_count/max_retries로 관리.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.cc_web_interface.admin_aicc.call_log_db import _filesystem_base_dir, now_iso

logger = logging.getLogger(__name__)

DB_FILENAME = "callback_queue.db"

_lock = threading.Lock()
_initialized = False


def get_db_path() -> Path:
    return _filesystem_base_dir() / "db" / DB_FILENAME


# ──────────────────── 상수 ────────────────────

STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

VALID_STATUSES = {STATUS_PENDING, STATUS_IN_PROGRESS, STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED}

# 우선순위: 1(낮음) ~ 5(매우 긴급)
PRIORITY_LOW = 1
PRIORITY_NORMAL = 3
PRIORITY_URGENT = 5

# 소스 — 왜 콜백이 필요해졌는지
SOURCE_AUTO_BLOCKED = "auto_blocked"          # 차단 통화 자동 큐잉
SOURCE_AUTO_FAILED = "auto_failed"            # 실패 통화 자동 큐잉
SOURCE_AUTO_SHORT_CALL = "auto_short_call"    # 짧은 통화 자동 큐잉
SOURCE_MANUAL = "manual"                       # 관리자 수동 추가


# ──────────────────── Schema ────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS callback_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_number TEXT NOT NULL,
    customer_name TEXT,
    source TEXT NOT NULL,
    reason TEXT,
    original_call_id TEXT,
    priority INTEGER NOT NULL DEFAULT 3,
    status TEXT NOT NULL DEFAULT 'pending',
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    scheduled_at TEXT,
    last_attempt_at TEXT,
    last_attempt_call_id TEXT,
    last_attempt_result TEXT,
    completed_at TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cbq_status ON callback_queue(status);
CREATE INDEX IF NOT EXISTS idx_cbq_priority_created ON callback_queue(priority DESC, created_at);
CREATE INDEX IF NOT EXISTS idx_cbq_from_number ON callback_queue(from_number);
CREATE INDEX IF NOT EXISTS idx_cbq_scheduled_at ON callback_queue(scheduled_at);
"""


# ──────────────────── 초기화 ────────────────────


def _init():
    global _initialized
    if _initialized:
        return
    with _lock:
        if _initialized:
            return
        db_path = get_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(db_path)) as conn:
            conn.executescript(SCHEMA_SQL)
        _initialized = True
        logger.info(f"[CALLBACK_DB] 초기화 완료: {db_path}")


def _conn() -> sqlite3.Connection:
    _init()
    c = sqlite3.connect(str(get_db_path()))
    c.row_factory = sqlite3.Row
    return c


# ──────────────────── CRUD ────────────────────


def enqueue(
    *,
    from_number: str,
    source: str,
    customer_name: Optional[str] = None,
    reason: Optional[str] = None,
    original_call_id: Optional[str] = None,
    priority: int = PRIORITY_NORMAL,
    max_retries: int = 3,
    scheduled_at: Optional[str] = None,
    notes: Optional[str] = None,
) -> int:
    """콜백 큐에 추가. row id 반환.

    같은 번호로 pending/in_progress 항목이 이미 있으면 중복 추가 안 함
    (대신 priority 상향 + retry_count 누적).
    """
    now = now_iso()
    priority = max(1, min(5, int(priority)))

    with _conn() as c:
        # 같은 번호의 pending/in_progress 있는지 확인
        existing = c.execute(
            """
            SELECT id, priority, retry_count
            FROM callback_queue
            WHERE from_number = ?
              AND status IN ('pending', 'in_progress')
            ORDER BY priority DESC, created_at ASC
            LIMIT 1
            """,
            (from_number,),
        ).fetchone()

        if existing:
            # 기존 항목 priority 상향 (둘 중 높은 값) + retry_count 누적은 안 함
            new_priority = max(existing["priority"], priority)
            c.execute(
                """
                UPDATE callback_queue
                SET priority = ?,
                    updated_at = ?,
                    notes = COALESCE(notes || char(10), '') || ?
                WHERE id = ?
                """,
                (new_priority, now, f"[{now}] 재요청: {reason or source}", existing["id"]),
            )
            c.commit()
            logger.info(f"[CALLBACK_DB] 중복 enqueue → 기존 #{existing['id']} priority {new_priority}")
            return int(existing["id"])

        cur = c.execute(
            """
            INSERT INTO callback_queue
              (from_number, customer_name, source, reason, original_call_id,
               priority, status, retry_count, max_retries,
               scheduled_at, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?)
            """,
            (
                from_number, customer_name, source, reason, original_call_id,
                priority, max_retries,
                scheduled_at, notes, now, now,
            ),
        )
        c.commit()
        cb_id = int(cur.lastrowid)
        logger.info(
            f"[CALLBACK_DB] enqueued #{cb_id} from={from_number} "
            f"source={source} priority={priority}"
        )
        return cb_id


def list_callbacks(
    *,
    status: Optional[str] = None,           # None = 전체
    statuses: Optional[list[str]] = None,   # 다중 상태 필터
    from_number: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """콜백 목록. 우선순위 ↓, 생성순 ↑."""
    where = []
    params: list[Any] = []

    if statuses:
        where.append(f"status IN ({','.join(['?'] * len(statuses))})")
        params.extend(statuses)
    elif status:
        where.append("status = ?")
        params.append(status)

    if from_number:
        where.append("from_number = ?")
        params.append(from_number)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    with _conn() as c:
        total = c.execute(
            f"SELECT COUNT(*) AS n FROM callback_queue {where_sql}", tuple(params)
        ).fetchone()["n"]

        rows = c.execute(
            f"""
            SELECT * FROM callback_queue
            {where_sql}
            ORDER BY
                CASE status
                    WHEN 'in_progress' THEN 0
                    WHEN 'pending' THEN 1
                    WHEN 'failed' THEN 2
                    WHEN 'done' THEN 3
                    WHEN 'cancelled' THEN 4
                END,
                priority DESC,
                created_at ASC
            LIMIT ? OFFSET ?
            """,
            tuple(params + [limit, offset]),
        ).fetchall()

    return {
        "total": int(total),
        "items": [dict(r) for r in rows],
    }


def get_callback(cb_id: int) -> Optional[dict[str, Any]]:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM callback_queue WHERE id = ?", (cb_id,)
        ).fetchone()
    return dict(row) if row else None


def update_priority(cb_id: int, priority: int) -> bool:
    priority = max(1, min(5, int(priority)))
    with _conn() as c:
        cur = c.execute(
            "UPDATE callback_queue SET priority = ?, updated_at = ? WHERE id = ?",
            (priority, now_iso(), cb_id),
        )
        c.commit()
    return cur.rowcount > 0


def update_status(
    cb_id: int,
    status: str,
    *,
    result: Optional[str] = None,
    call_id: Optional[str] = None,
) -> bool:
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status}")
    now = now_iso()
    completed_at = now if status in (STATUS_DONE, STATUS_CANCELLED, STATUS_FAILED) else None

    with _conn() as c:
        cur = c.execute(
            """
            UPDATE callback_queue
            SET status = ?,
                last_attempt_result = COALESCE(?, last_attempt_result),
                last_attempt_call_id = COALESCE(?, last_attempt_call_id),
                completed_at = COALESCE(?, completed_at),
                updated_at = ?
            WHERE id = ?
            """,
            (status, result, call_id, completed_at, now, cb_id),
        )
        c.commit()
    return cur.rowcount > 0


def mark_attempt(cb_id: int, call_id: Optional[str] = None) -> None:
    """발신 시도 시각 + retry_count 증가."""
    now = now_iso()
    with _conn() as c:
        c.execute(
            """
            UPDATE callback_queue
            SET retry_count = retry_count + 1,
                last_attempt_at = ?,
                last_attempt_call_id = COALESCE(?, last_attempt_call_id),
                status = 'in_progress',
                updated_at = ?
            WHERE id = ?
            """,
            (now, call_id, now, cb_id),
        )
        c.commit()


def append_note(cb_id: int, note: str) -> bool:
    """notes 컬럼에 타임스탬프 + 내용 append."""
    line = f"[{now_iso()}] {note}"
    with _conn() as c:
        cur = c.execute(
            """
            UPDATE callback_queue
            SET notes = COALESCE(notes || char(10), '') || ?,
                updated_at = ?
            WHERE id = ?
            """,
            (line, now_iso(), cb_id),
        )
        c.commit()
    return cur.rowcount > 0


def cancel(cb_id: int, reason: Optional[str] = None) -> bool:
    """soft cancel."""
    if reason:
        append_note(cb_id, f"취소: {reason}")
    return update_status(cb_id, STATUS_CANCELLED, result=reason or "cancelled")


def get_stats() -> dict[str, int]:
    """대시보드 KPI용."""
    with _conn() as c:
        rows = c.execute(
            "SELECT status, COUNT(*) AS n FROM callback_queue GROUP BY status"
        ).fetchall()
    out = {s: 0 for s in VALID_STATUSES}
    for r in rows:
        out[r["status"]] = int(r["n"])
    out["total"] = sum(out.values())
    return out


def dequeue_next() -> Optional[dict[str, Any]]:
    """워커가 다음에 처리할 콜백 1건 (pending 중 우선순위 ↓, 생성순 ↑).

    예약(scheduled_at)이 미래면 skip.
    """
    now = now_iso()
    with _conn() as c:
        row = c.execute(
            """
            SELECT * FROM callback_queue
            WHERE status = 'pending'
              AND (scheduled_at IS NULL OR scheduled_at <= ?)
              AND retry_count < max_retries
            ORDER BY priority DESC, created_at ASC
            LIMIT 1
            """,
            (now,),
        ).fetchone()
    return dict(row) if row else None
