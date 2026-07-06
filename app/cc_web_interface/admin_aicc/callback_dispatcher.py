"""콜백 큐 → 실제 ClawOps outbound 발신 디스패처.

main.py가 aicc_agent 초기화 후 set_agent(aicc_agent)로 등록.
routes.py가 trigger_callback(cb_id)로 즉시 발신.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from app.cc_web_interface.admin_aicc import callback_db

logger = logging.getLogger(__name__)


_agent: Any = None


def set_agent(agent: Any) -> None:
    """main.py에서 aicc_agent 초기화 후 호출."""
    global _agent
    _agent = agent
    logger.info("[CALLBACK_DISPATCHER] agent 등록 완료")


def is_ready() -> bool:
    return _agent is not None


def _normalize_phone(num: str) -> str:
    if not num:
        return ""
    return (num or "").replace("-", "").replace(" ", "").strip()


async def trigger_outbound_call(phone: str, prompt: Optional[str] = None) -> dict:
    """ClawOps outbound 발신.

    Returns:
        {"status": "initiated"|"error", "call_id": str, "reason": str|None}
    """
    if _agent is None:
        return {"status": "error", "reason": "agent_not_ready", "call_id": ""}

    phone_n = _normalize_phone(phone)
    if not phone_n:
        return {"status": "error", "reason": "invalid_phone", "call_id": ""}

    try:
        # 개인화 프롬프트가 있으면 세션 오버라이드 시도
        if prompt:
            try:
                from clawops.agent import GeminiRealtime
                session = await _agent.call(
                    phone_n,
                    session=GeminiRealtime(system_prompt=prompt, language="ko"),
                    timeout=600,
                )
                logger.info(f"[CALLBACK_DISPATCHER] 개인화 프롬프트 발신 → {phone_n}")
            except (TypeError, AttributeError) as e:
                logger.warning(f"[CALLBACK_DISPATCHER] session override 미지원 ({e}) — 기본 프롬프트 사용")
                session = await _agent.call(phone_n, timeout=600)
        else:
            session = await _agent.call(phone_n, timeout=600)

        call_id = getattr(session, "call_id", "") or ""
        asyncio.create_task(_wait_session(session, call_id, phone_n))
        logger.info(f"[CALLBACK_DISPATCHER] outbound 시작 → {phone_n} call_id={call_id}")
        return {"status": "initiated", "call_id": call_id, "reason": None}

    except Exception as e:
        logger.error(f"[CALLBACK_DISPATCHER] outbound 실패 → {phone_n}: {e}", exc_info=True)
        return {"status": "error", "reason": f"{type(e).__name__}: {e}"[:300], "call_id": ""}


async def _wait_session(session: Any, call_id: str, phone: str) -> None:
    """outbound session 백그라운드 대기. 종료 후 정리."""
    try:
        if hasattr(session, "wait"):
            await session.wait()
        logger.info(f"[CALLBACK_DISPATCHER] outbound session 종료: {call_id} ({phone})")
    except Exception as e:
        logger.error(f"[CALLBACK_DISPATCHER] outbound session 예외 {call_id}: {e}")


async def execute_callback(cb_id: int, prompt: Optional[str] = None) -> dict:
    """콜백 큐 항목을 실제로 발신.

    1. DB에서 콜백 정보 로드
    2. 상태 → in_progress + retry_count++ + last_attempt_at
    3. outbound 발신
    4. 결과에 따라 상태 업데이트
    """
    cb = callback_db.get_callback(cb_id)
    if not cb:
        return {"status": "error", "reason": "callback_not_found"}
    if cb["status"] in (callback_db.STATUS_DONE, callback_db.STATUS_CANCELLED):
        return {"status": "error", "reason": f"already_{cb['status']}"}
    if cb["retry_count"] >= cb["max_retries"]:
        callback_db.update_status(cb_id, callback_db.STATUS_FAILED, result="max_retries_reached")
        return {"status": "error", "reason": "max_retries_reached"}

    # 발신 시도 마킹
    callback_db.mark_attempt(cb_id)

    # 실제 발신
    result = await trigger_outbound_call(cb["from_number"], prompt=prompt)

    if result["status"] == "initiated":
        callback_db.update_status(
            cb_id,
            callback_db.STATUS_IN_PROGRESS,
            result=f"initiated: {result['call_id']}",
            call_id=result["call_id"],
        )
        callback_db.append_note(cb_id, f"발신 시작: call_id={result['call_id']}")
    else:
        # 실패 — 재시도 가능하면 pending 으로 되돌림
        if cb["retry_count"] + 1 >= cb["max_retries"]:
            callback_db.update_status(cb_id, callback_db.STATUS_FAILED, result=result.get("reason"))
        else:
            callback_db.update_status(cb_id, callback_db.STATUS_PENDING, result=result.get("reason"))
        callback_db.append_note(cb_id, f"발신 실패: {result.get('reason')}")

    return result
