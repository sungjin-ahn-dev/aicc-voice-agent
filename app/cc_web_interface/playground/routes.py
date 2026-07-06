"""AICC Playground — 로컬에서 파이썬 서버만 켜면 대시보드와 같이 뜨는 "전화 실험실".

발신은 기존 callback_dispatcher(에이전트가 시작 시 등록)를 재사용한다.
키(CLAWOPS/GOOGLE)가 없으면 에이전트가 비활성이라, 그 사실을 페이지에 그대로 안내.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from app.cc_web_interface.admin_aicc import callback_dispatcher, call_log_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/playground", tags=["playground"])

_HTML_PATH = Path(__file__).parent.parent / "static" / "playground.html"


def _gemini_configured() -> bool:
    return bool(os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))


def _build_outbound_prompt(*, name: str, purpose: str, message: str) -> str:
    """발신 테스트용 system_prompt 생성."""
    lines = [
        "당신은 마음케어 고객센터 상담원입니다. "
        "한국어로만, 어르신께 말하듯 짧고 천천히 말하세요.",
        "이 전화는 우리 쪽에서 거는 아웃바운드(발신) 통화입니다.",
        f"- 상대방 이름: {name or '고객'}",
        f"- 전화 목적: {purpose or '안내'}",
    ]
    if message:
        lines.append(f"- 전달할 핵심 메시지: {message}")
    lines.append(
        "통화가 연결되면 먼저 마음케어 고객센터라고 자신을 소개하고, "
        "핵심 메시지를 전달한 뒤 궁금하신 점이 있는지 여쭤보세요."
    )
    lines.append(
        "한 번에 두세 문장 이내로 짧게. 마크다운/영어 약어 사용 금지. "
        "전화번호는 한 자씩 풀어 읽기. 고객센터 번호는 일오팔팔에 공공공공입니다."
    )
    return "\n".join(lines)


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
async def playground_page():
    if not _HTML_PATH.exists():
        raise HTTPException(status_code=404, detail="playground.html not found")
    from fastapi.responses import FileResponse
    return FileResponse(_HTML_PATH, media_type="text/html")


@router.get("/status")
async def playground_status():
    """페이지 상단 상태 배지용."""
    recent = 0
    try:
        recent = call_log_db.get_analytics(days=1).get("total", 0)
    except Exception:
        recent = 0

    agent_ready = callback_dispatcher.is_ready()
    clawops = bool(os.environ.get("CLAWOPS_API_KEY"))
    gemini = _gemini_configured()

    # 사람이 읽을 수 있는 한 줄 진단
    if agent_ready:
        diagnosis = "전화 에이전트 가동 중 — 발신/수신 테스트 가능"
    elif not clawops and not gemini:
        diagnosis = "CLAWOPS_API_KEY 와 GOOGLE_API_KEY 가 .env 에 없습니다 (전화 비활성, 대시보드는 정상)"
    elif not clawops:
        diagnosis = "CLAWOPS_API_KEY 가 없습니다 — 070 전화 송수신 불가"
    elif not gemini:
        diagnosis = "GOOGLE_API_KEY 가 없습니다 — Gemini 음성 응답 불가"
    else:
        diagnosis = "키는 있으나 에이전트가 아직 초기화되지 않았습니다 (서버 재시작 또는 잠시 대기)"

    return {
        "agent_ready": agent_ready,
        "clawops_configured": clawops,
        "gemini_configured": gemini,
        "slack_configured": bool(os.environ.get("SLACK_BOT_TOKEN")),
        "from_number": os.environ.get("AICC_FROM_NUMBER", "07012345678"),
        "recent_calls_today": recent,
        "diagnosis": diagnosis,
    }


@router.get("/recent")
async def playground_recent(limit: int = 10):
    """최근 통화 목록 (페이지가 주기적으로 새로고침)."""
    limit = max(1, min(50, int(limit)))
    rows, total = call_log_db.query_calls(limit=limit, offset=0)
    slim = [
        {
            "call_id": r.get("call_id"),
            "direction": r.get("direction"),
            "from_number": r.get("from_number"),
            "to_number": r.get("to_number"),
            "status": r.get("status"),
            "started_at": r.get("started_at"),
            "duration_sec": r.get("duration_sec"),
            "category": r.get("category"),
            "customer_type": r.get("customer_type"),
        }
        for r in rows
    ]
    return {"total": total, "calls": slim}


@router.post("/call")
async def playground_call(request: Request):
    """입력한 휴대폰 번호로 실제 아웃바운드 발신 테스트.

    Body: { "phone": "010...", "name": "...", "purpose": "...", "message": "..." }
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON body 필요")

    phone = (body.get("phone") or "").replace("-", "").replace(" ", "").strip()
    name = (body.get("name") or "고객").strip()
    purpose = (body.get("purpose") or "마음케어 테스트 통화").strip()
    message = (body.get("message") or "").strip()

    # 번호 검증 (휴대폰만)
    if not phone.startswith("01") or len(phone) < 10:
        raise HTTPException(
            status_code=400,
            detail=f"휴대폰 번호 형식이 올바르지 않습니다: {phone or '(빈 값)'} — 예: 01012345678",
        )

    # 에이전트 비활성이면 이유를 그대로 알려준다
    if not callback_dispatcher.is_ready():
        gemini = _gemini_configured()
        clawops = bool(os.environ.get("CLAWOPS_API_KEY"))
        missing = []
        if not clawops:
            missing.append("CLAWOPS_API_KEY")
        if not gemini:
            missing.append("GOOGLE_API_KEY(또는 GEMINI_API_KEY)")
        return {
            "status": "error",
            "reason": "agent_not_ready",
            "missing_keys": missing,
            "message": (
                "전화 에이전트가 비활성 상태입니다. "
                + (f".env 에 {', '.join(missing)} 를 넣고 서버를 재시작하세요." if missing
                   else "서버 시작 직후라면 잠시 후 다시 시도하세요.")
            ),
        }

    prompt = _build_outbound_prompt(name=name, purpose=purpose, message=message)
    logger.info(f"[PLAYGROUND] 발신 테스트 → {phone} (purpose={purpose})")
    result = await callback_dispatcher.trigger_outbound_call(phone, prompt=prompt)

    # 결과를 페이지가 쓰기 좋게 정리
    if result.get("status") == "initiated":
        return {
            "status": "initiated",
            "call_id": result.get("call_id"),
            "phone": phone,
            "message": f"발신을 시작했습니다. 통화가 끝나면 아래 '최근 통화'와 대시보드에 기록됩니다.",
        }
    return {
        "status": "error",
        "reason": result.get("reason"),
        "phone": phone,
        "message": f"발신 실패: {result.get('reason')}",
    }
