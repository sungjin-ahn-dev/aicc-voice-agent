"""AICC 통화 로그 DB (SQLite).

나중에 PostgreSQL/S3로 옮길 걸 감안해서 SQLite 전용 문법은 최대한 피했다.
timestamp는 전부 ISO 8601 UTC TEXT(→ TIMESTAMPTZ 호환), 녹음은 상대경로만 저장(→ base만 교체).

DB: $FILESYSTEM_BASE_DIR/db/aicc_calls.db, 녹음: $FILESYSTEM_BASE_DIR/aicc_recordings/
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DB_FILENAME = "aicc_calls.db"
RECORDING_DIR_NAME = "aicc_recordings"

_lock = threading.Lock()
_initialized = False


def _filesystem_base_dir() -> Path:
    try:
        from app.config.settings import get_settings
        base = get_settings().FILESYSTEM_BASE_DIR or ""
    except Exception:
        base = ""
    if base:
        return Path(base)
    return Path(os.path.expanduser("~/.aicc"))


def get_db_path() -> Path:
    return _filesystem_base_dir() / "db" / DB_FILENAME


def get_recording_base() -> Path:
    return _filesystem_base_dir() / RECORDING_DIR_NAME


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ──────────────────── Schema ────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS aicc_calls (
    call_id TEXT PRIMARY KEY,
    from_number TEXT,
    to_number TEXT,
    direction TEXT NOT NULL DEFAULT 'inbound',
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_sec INTEGER,
    status TEXT NOT NULL,
    block_reason TEXT,
    transferred_to TEXT,
    transfer_reason TEXT,
    failure_count INTEGER NOT NULL DEFAULT 0,
    transcript TEXT,
    transcript_refined TEXT,
    recording_relative_path TEXT,
    category TEXT,
    customer_type TEXT,
    matched_faq_no INTEGER,
    classification_status TEXT NOT NULL DEFAULT 'pending',
    classification_error TEXT,
    notes TEXT,
    sms_status TEXT,
    sms_sent_at TEXT,
    sms_message_id TEXT,
    sms_body TEXT,
    sms_summary TEXT,
    sms_error TEXT,
    sms_provider TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_aicc_calls_started_at ON aicc_calls(started_at)",
    "CREATE INDEX IF NOT EXISTS idx_aicc_calls_from_number ON aicc_calls(from_number)",
    "CREATE INDEX IF NOT EXISTS idx_aicc_calls_status ON aicc_calls(status)",
    "CREATE INDEX IF NOT EXISTS idx_aicc_calls_category ON aicc_calls(category)",
    "CREATE INDEX IF NOT EXISTS idx_aicc_calls_customer_type ON aicc_calls(customer_type)",
    "CREATE INDEX IF NOT EXISTS idx_aicc_calls_classification_status ON aicc_calls(classification_status)",
    "CREATE INDEX IF NOT EXISTS idx_aicc_calls_sms_status ON aicc_calls(sms_status)",
]

# 기존 DB에 누락된 컬럼이 있으면 자동 추가 (PostgreSQL 이전 시 alembic으로 대체)
_SMS_COLUMNS = [
    ("sms_status", "TEXT"),
    ("sms_sent_at", "TEXT"),
    ("sms_message_id", "TEXT"),
    ("sms_body", "TEXT"),
    ("sms_summary", "TEXT"),
    ("sms_error", "TEXT"),
    ("sms_provider", "TEXT"),
]


def _migrate_sms_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(aicc_calls)").fetchall()}
    for col, ddl in _SMS_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE aicc_calls ADD COLUMN {col} {ddl}")
            logger.info(f"[AICC_CALL_DB] migrated: ADD COLUMN {col}")


def init_db() -> None:
    global _initialized
    path = get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        with sqlite3.connect(path) as conn:
            conn.execute(SCHEMA_SQL)
            _migrate_sms_columns(conn)
            for sql in INDEX_SQL:
                conn.execute(sql)
            conn.commit()
    _initialized = True
    logger.info(f"[AICC_CALL_DB] initialized at {path}")


def _conn() -> sqlite3.Connection:
    if not _initialized:
        init_db()
    c = sqlite3.connect(get_db_path())
    c.row_factory = sqlite3.Row
    return c


# ──────────────────── Status / reason 상수 ────────────────────

STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"
STATUS_TRANSFERRED = "transferred"
STATUS_FAILED = "failed"
STATUS_BLOCKED = "blocked"

BLOCK_OUT_OF_HOURS = "out_of_hours"
BLOCK_HOLIDAY = "holiday"
BLOCK_APP_BREAK = "app_break"
BLOCK_LUNCH = "lunch_break"

TRANSFER_KEYWORD = "keyword"
TRANSFER_COMPLAINT = "complaint"
TRANSFER_FAILURE_THRESHOLD = "failure_threshold"
TRANSFER_MANUAL = "manual"

CLASSIFICATION_PENDING = "pending"
CLASSIFICATION_DONE = "done"
CLASSIFICATION_FAILED = "failed"

SMS_SENT = "sent"
SMS_FAILED = "failed"
SMS_SKIPPED = "skipped"     # 010 아닌 번호 / 차단 통화 등
SMS_DISABLED = "disabled"   # 어드민에서 SMS off


# ──────────────────── CRUD ────────────────────


def insert_call_start(
    call_id: str,
    from_number: str,
    to_number: str,
    direction: str = "inbound",
) -> None:
    ts = now_iso()
    with _lock:
        with _conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO aicc_calls
                (call_id, from_number, to_number, direction, started_at, status,
                 classification_status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    call_id, from_number or "", to_number or "", direction,
                    ts, STATUS_IN_PROGRESS, CLASSIFICATION_PENDING, ts, ts,
                ),
            )
            conn.commit()


def mark_blocked(call_id: str, reason: str) -> None:
    ts = now_iso()
    with _lock:
        with _conn() as conn:
            conn.execute(
                """
                UPDATE aicc_calls
                SET status = ?, block_reason = ?, ended_at = ?, updated_at = ?,
                    classification_status = 'done'
                WHERE call_id = ?
                """,
                (STATUS_BLOCKED, reason, ts, ts, call_id),
            )
            conn.commit()


def mark_transferred(call_id: str, to: str, reason: str) -> None:
    ts = now_iso()
    with _lock:
        with _conn() as conn:
            conn.execute(
                """
                UPDATE aicc_calls
                SET status = ?, transferred_to = ?, transfer_reason = ?, updated_at = ?
                WHERE call_id = ?
                """,
                (STATUS_TRANSFERRED, to, reason, ts, call_id),
            )
            conn.commit()


def update_failure_count(call_id: str, count: int) -> None:
    ts = now_iso()
    with _lock:
        with _conn() as conn:
            conn.execute(
                "UPDATE aicc_calls SET failure_count = ?, updated_at = ? WHERE call_id = ?",
                (count, ts, call_id),
            )
            conn.commit()


def finalize_call(
    call_id: str,
    transcript: str,
    duration_sec: Optional[int],
    recording_relative_path: Optional[str],
    final_status: Optional[str] = None,
) -> None:
    """통화 종료 시 호출. 이미 transferred/blocked로 마킹된 경우 status는 보존.
    final_status가 None이면 in_progress → completed로 자연 전환.
    """
    ts = now_iso()
    with _lock:
        with _conn() as conn:
            row = conn.execute(
                "SELECT status FROM aicc_calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            current_status = row["status"] if row else STATUS_IN_PROGRESS
            # 종료 status: in_progress이면 completed, 그 외(transferred/blocked/failed)는 유지
            if final_status:
                new_status = final_status
            elif current_status == STATUS_IN_PROGRESS:
                new_status = STATUS_COMPLETED
            else:
                new_status = current_status

            conn.execute(
                """
                UPDATE aicc_calls
                SET ended_at = ?, duration_sec = ?, transcript = ?,
                    recording_relative_path = ?, status = ?, updated_at = ?
                WHERE call_id = ?
                """,
                (
                    ts, duration_sec, transcript or "",
                    recording_relative_path or None, new_status, ts, call_id,
                ),
            )
            conn.commit()


def mark_failed(call_id: str, reason: str = "") -> None:
    ts = now_iso()
    with _lock:
        with _conn() as conn:
            conn.execute(
                """
                UPDATE aicc_calls
                SET status = ?, ended_at = ?, notes = COALESCE(notes, '') || ?, updated_at = ?
                WHERE call_id = ?
                """,
                (STATUS_FAILED, ts, f"\n[failed] {reason}" if reason else "", ts, call_id),
            )
            conn.commit()


def update_sms(
    call_id: str,
    status: str,
    body: Optional[str] = None,
    summary: Optional[str] = None,
    message_id: Optional[str] = None,
    error: Optional[str] = None,
    provider: Optional[str] = None,
) -> None:
    """SMS 발송 결과 기록. status는 SMS_SENT/FAILED/SKIPPED/DISABLED 중 하나."""
    ts = now_iso()
    sent_at = ts if status == SMS_SENT else None
    with _lock:
        with _conn() as conn:
            conn.execute(
                """
                UPDATE aicc_calls
                SET sms_status = ?, sms_sent_at = ?, sms_message_id = ?,
                    sms_body = ?, sms_summary = ?, sms_error = ?, sms_provider = ?,
                    updated_at = ?
                WHERE call_id = ?
                """,
                (status, sent_at, message_id, body, summary, error, provider, ts, call_id),
            )
            conn.commit()


def update_classification(
    call_id: str,
    category: Optional[str],
    customer_type: Optional[str],
    matched_faq_no: Optional[int],
    transcript_refined: Optional[str],
    error: Optional[str] = None,
) -> None:
    ts = now_iso()
    status = CLASSIFICATION_FAILED if error else CLASSIFICATION_DONE
    with _lock:
        with _conn() as conn:
            conn.execute(
                """
                UPDATE aicc_calls
                SET category = ?, customer_type = ?, matched_faq_no = ?,
                    transcript_refined = ?, classification_status = ?,
                    classification_error = ?, updated_at = ?
                WHERE call_id = ?
                """,
                (
                    category, customer_type, matched_faq_no,
                    transcript_refined, status, error, ts, call_id,
                ),
            )
            conn.commit()


# ──────────────────── 조회 ────────────────────


def get_call(call_id: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM aicc_calls WHERE call_id = ?", (call_id,)
        ).fetchone()
        return dict(row) if row else None


def has_history(from_number: str, within_days: int = 7, exclude_call_id: str = "") -> bool:
    """같은 번호가 within_days 이내에 다시 전화한 적 있는지 (재문의 판정용)."""
    if not from_number:
        return False
    cutoff = (datetime.now(timezone.utc) - timedelta(days=within_days)).isoformat()
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM aicc_calls
            WHERE from_number = ? AND started_at >= ? AND call_id != ?
            """,
            (from_number, cutoff, exclude_call_id),
        ).fetchone()
        return (row["c"] or 0) > 0


def query_calls(
    from_date: Optional[str] = None,   # ISO date 'YYYY-MM-DD'
    to_date: Optional[str] = None,
    category: Optional[str] = None,
    customer_type: Optional[str] = None,
    status: Optional[str] = None,
    from_number: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    where = []
    params: list[Any] = []
    if from_date:
        where.append("started_at >= ?")
        params.append(f"{from_date}T00:00:00+00:00")
    if to_date:
        where.append("started_at <= ?")
        params.append(f"{to_date}T23:59:59+00:00")
    if category:
        where.append("category = ?")
        params.append(category)
    if customer_type:
        where.append("customer_type = ?")
        params.append(customer_type)
    if status:
        where.append("status = ?")
        params.append(status)
    if from_number:
        where.append("from_number = ?")
        params.append(from_number)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    with _conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM aicc_calls {where_sql}", params
        ).fetchone()["c"]
        rows = conn.execute(
            f"""
            SELECT * FROM aicc_calls
            {where_sql}
            ORDER BY started_at DESC
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        ).fetchall()
        return [dict(r) for r in rows], total


def get_analytics(days: int = 7) -> dict:
    """일/카테고리/고객유형별 집계."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _conn() as conn:
        # 전체 카운트 + status 분포
        rows_status = conn.execute(
            """
            SELECT status, COUNT(*) AS c FROM aicc_calls
            WHERE started_at >= ?
            GROUP BY status
            """,
            (cutoff,),
        ).fetchall()
        status_dist = {r["status"]: r["c"] for r in rows_status}
        total = sum(status_dist.values())

        rows_cat = conn.execute(
            """
            SELECT COALESCE(category, '미분류') AS k, COUNT(*) AS c FROM aicc_calls
            WHERE started_at >= ? AND status NOT IN ('blocked')
            GROUP BY k
            ORDER BY c DESC
            """,
            (cutoff,),
        ).fetchall()
        category_dist = [{"key": r["k"], "count": r["c"]} for r in rows_cat]

        rows_ct = conn.execute(
            """
            SELECT COALESCE(customer_type, '미분류') AS k, COUNT(*) AS c FROM aicc_calls
            WHERE started_at >= ? AND status NOT IN ('blocked')
            GROUP BY k
            ORDER BY c DESC
            """,
            (cutoff,),
        ).fetchall()
        customer_type_dist = [{"key": r["k"], "count": r["c"]} for r in rows_ct]

        # 일별 추이
        rows_daily = conn.execute(
            """
            SELECT substr(started_at, 1, 10) AS day, COUNT(*) AS c FROM aicc_calls
            WHERE started_at >= ?
            GROUP BY day
            ORDER BY day ASC
            """,
            (cutoff,),
        ).fetchall()
        daily = [{"day": r["day"], "count": r["c"]} for r in rows_daily]

        # 핵심 KPI
        transferred = status_dist.get(STATUS_TRANSFERRED, 0)
        failed = status_dist.get(STATUS_FAILED, 0)
        completed = status_dist.get(STATUS_COMPLETED, 0)
        blocked = status_dist.get(STATUS_BLOCKED, 0)
        engaged = transferred + completed + failed  # 실제 응답한 통화
        transfer_rate = (transferred / engaged * 100) if engaged else 0
        fail_rate = (failed / engaged * 100) if engaged else 0

        return {
            "days": days,
            "total": total,
            "engaged": engaged,
            "by_status": status_dist,
            "transfer_rate_pct": round(transfer_rate, 1),
            "fail_rate_pct": round(fail_rate, 1),
            "block_count": blocked,
            "categories": category_dist,
            "customer_types": customer_type_dist,
            "daily": daily,
        }


def get_data_integrity() -> dict:
    """데이터 누락/오류 감지."""
    with _conn() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM aicc_calls").fetchone()["c"]
        unclassified = conn.execute(
            f"""SELECT COUNT(*) AS c FROM aicc_calls
                WHERE classification_status != '{CLASSIFICATION_DONE}'
                AND status NOT IN ('blocked', 'failed', 'in_progress')"""
        ).fetchone()["c"]
        no_recording = conn.execute(
            """SELECT COUNT(*) AS c FROM aicc_calls
               WHERE (recording_relative_path IS NULL OR recording_relative_path = '')
               AND status IN ('completed', 'transferred')"""
        ).fetchone()["c"]
        no_transcript = conn.execute(
            """SELECT COUNT(*) AS c FROM aicc_calls
               WHERE (transcript IS NULL OR transcript = '')
               AND status IN ('completed', 'transferred')"""
        ).fetchone()["c"]
        classification_failed = conn.execute(
            f"""SELECT COUNT(*) AS c FROM aicc_calls
                WHERE classification_status = '{CLASSIFICATION_FAILED}'"""
        ).fetchone()["c"]
        in_progress_stale = conn.execute(
            """SELECT COUNT(*) AS c FROM aicc_calls
               WHERE status = 'in_progress' AND started_at < ?""",
            ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),),
        ).fetchone()["c"]

        return {
            "total_calls": total,
            "unclassified": unclassified,
            "no_recording": no_recording,
            "no_transcript": no_transcript,
            "classification_failed": classification_failed,
            "in_progress_stale": in_progress_stale,
        }


def list_unclassified(limit: int = 50) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            f"""SELECT * FROM aicc_calls
                WHERE classification_status != '{CLASSIFICATION_DONE}'
                AND transcript IS NOT NULL AND transcript != ''
                AND status NOT IN ('in_progress', 'blocked')
                ORDER BY started_at DESC
                LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
