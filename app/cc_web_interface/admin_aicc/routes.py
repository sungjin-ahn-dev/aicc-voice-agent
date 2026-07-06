"""AICC 관리자 대시보드 라우트 — 설정 관리, 콜 로그/분석, SMS·알림톡 발송, 콜백 큐.
엔드포인트는 아래 라우트 데코레이터 참고."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from app.cc_web_interface.admin_aicc import (
    agent_control,
    call_classifier,
    call_log_db as call_db,
    callback_db,
    callback_dispatcher,
    config as cfg,
    sms_sender,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/aicc", tags=["admin-aicc"])

_HTML_PATH = Path(__file__).parent.parent / "static" / "admin_aicc.html"


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
async def admin_page():
    if not _HTML_PATH.exists():
        raise HTTPException(status_code=404, detail="admin_aicc.html not found")
    return FileResponse(_HTML_PATH, media_type="text/html")


@router.get("/config")
async def get_config():
    return cfg.load_config()


@router.put("/config")
async def put_config(request: Request):
    try:
        data = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {e}")

    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="config must be an object")

    # 최소한의 형식 검증 (없는 필드는 DEFAULTS와 머지됨)
    merged = cfg._deep_merge_defaults(data, cfg.DEFAULTS)

    cfg.save_config(merged)
    applied = agent_control.apply_settings(merged)

    return JSONResponse({
        "ok": True,
        "saved": True,
        "applied_to_live_agent": applied,
        "message": (
            "저장 완료. 다음 통화부터 새 설정이 적용됩니다."
            if applied else
            "저장 완료. 단 라이브 에이전트가 아직 시작되지 않아 핫스왑은 건너뛰었습니다."
        ),
    })


@router.get("/status")
async def status():
    """디버그용: 현재 라이브 적용 가능한 상태인지."""
    from app.cc_web_interface.admin_aicc import agent_control as ac
    return {
        "config_path": str(cfg.get_config_path()),
        "config_exists": cfg.get_config_path().exists(),
        "live_handler_registered": ac._handler is not None,
        "db_path": str(call_db.get_db_path()),
    }


# ──────────────────── 콜 로그 / 분석 ────────────────────


@router.get("/calls")
async def list_calls(
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
    category: str | None = None,
    customer_type: str | None = None,
    status: str | None = None,
    from_number: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    rows, total = call_db.query_calls(
        from_date=from_date,
        to_date=to_date,
        category=category,
        customer_type=customer_type,
        status=status,
        from_number=from_number,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "calls": rows,
    }


@router.get("/calls/{call_id}")
async def get_call_detail(call_id: str):
    row = call_db.get_call(call_id)
    if not row:
        raise HTTPException(status_code=404, detail="call not found")
    # 녹음 파일 절대 경로도 함께 (UI 다운로드 링크 안내용)
    rec_rel = row.get("recording_relative_path")
    if rec_rel:
        row["recording_url"] = f"/admin/aicc/calls/{call_id}/recording"
    return row


@router.get("/calls/{call_id}/recording")
async def download_recording(call_id: str):
    row = call_db.get_call(call_id)
    if not row:
        raise HTTPException(status_code=404, detail="call not found")
    rel = row.get("recording_relative_path")
    if not rel:
        raise HTTPException(status_code=404, detail="no recording for this call")
    abs_path = call_db.get_recording_base() / rel
    if not abs_path.exists():
        raise HTTPException(status_code=404, detail=f"recording file missing: {rel}")
    return FileResponse(
        path=str(abs_path),
        media_type="audio/wav",
        filename=f"{call_id}.wav",
    )


@router.post("/calls/{call_id}/reclassify")
async def reclassify_call(call_id: str):
    result = await call_classifier.reclassify(call_id)
    return result


@router.post("/calls/{call_id}/resend-sms")
async def resend_sms(call_id: str):
    """대시보드 [SMS 재발송] 버튼."""
    result = await sms_sender.resend_for_call(call_id)
    return result


@router.get("/sms/preview")
async def sms_preview(
    summary: str | None = Query(None, description="가운데에 들어갈 요약(미지정 시 no_content_text)"),
):
    """현재 어드민 설정으로 SMS 본문이 어떻게 합성되는지 미리보기.

    어드민 화면에서 헤더/풋터 편집 시 실시간 프리뷰 용도.
    """
    config = cfg.load_config()
    sms_cfg = config.get("sms") or {}
    has_meaningful = bool((summary or "").strip())
    body, middle = sms_sender.build_sms_body(sms_cfg, summary or "", has_meaningful)
    return {
        "body": body,
        "middle_used": middle,
        "byte_len": sms_sender._byte_len(body),
        "is_long": sms_sender._byte_len(body) > sms_sender.SMS_MAX_BYTES,
        "active_provider": sms_sender.get_active_provider(config),
    }


@router.get("/sms/status")
async def sms_provider_status():
    """현재 환경에서 사용 가능한 SMS 공급자 + 활성 공급자."""
    return sms_sender.get_provider_status()


@router.post("/sms/manual-send")
async def manual_send_sms(request: Request):
    """수동 SMS 발송 (단건 또는 대량).

    Body:
        {
            "to": "01012345678"  OR  ["010...", "010..."],
            "content": "본문",
            "subject": "LMS 제목" (선택, 90byte 초과 시 LMS 자동 전환),
            "provider": "solapi" | "clawops" | "ncp" | null (선택, null=auto),
            "dry_run": true | false  (기본 true — 실수 방지)
        }

    Returns:
        단건이면 send_manual_sms 결과, 리스트면 send_manual_sms_batch 결과.
        dry_run=true (기본): 미리보기만, 실제 발송 안 함.
        dry_run=false: 실제 발송.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON body 필요")

    to = body.get("to")
    content = (body.get("content") or "").strip()
    subject = body.get("subject")
    provider = body.get("provider")
    dry_run = bool(body.get("dry_run", True))   # ⭐ 기본 dry_run

    if not to:
        raise HTTPException(status_code=400, detail="to(수신번호) 필수")
    if not content:
        raise HTTPException(status_code=400, detail="content(본문) 필수")

    # 리스트 또는 문자열 모두 수용
    if isinstance(to, list):
        result = await sms_sender.send_manual_sms_batch(
            numbers=to,
            content=content,
            subject=subject,
            provider=provider,
            dry_run=dry_run,
        )
    else:
        result = await sms_sender.send_manual_sms(
            to=str(to),
            content=content,
            subject=subject,
            provider=provider,
            dry_run=dry_run,
        )

    logger.info(
        f"[manual-send] to={to if isinstance(to, str) else f'{len(to)}건'} "
        f"dry_run={dry_run} status={result.get('status')}"
    )
    return result


@router.post("/alimtalk/manual-send")
async def manual_send_alimtalk(request: Request):
    """수동 알림톡 발송 (NCP SENS Biz Message).

    Body:
        {
            "to": "01012345678" | ["010...", "010..."],
            "content": "본문 (템플릿 규격에 맞게 변수 치환된 최종)",
            "template_code": "TEMP001" (사전 승인된 코드, 필수),
            "plus_friend_id": "@maumcare" (선택, env: NCP_KAKAO_CHANNEL),
            "title": "강조 제목" (선택, 강조 표기형 템플릿만),
            "use_sms_failover": true|false (선택, 카톡 못 받으면 SMS),
            "dry_run": true|false (기본 true)
        }
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON body 필요")

    to = body.get("to")
    content = (body.get("content") or "").strip()
    template_code = (body.get("template_code") or "").strip()
    plus_friend_id = body.get("plus_friend_id") or None
    title = body.get("title")
    use_sms_failover = bool(body.get("use_sms_failover", False))
    dry_run = bool(body.get("dry_run", True))

    if not to:
        raise HTTPException(status_code=400, detail="to(수신번호) 필수")
    if not content:
        raise HTTPException(status_code=400, detail="content(본문) 필수")
    if not template_code:
        raise HTTPException(status_code=400, detail="template_code(템플릿 코드) 필수")

    if isinstance(to, list):
        result = await sms_sender.send_manual_alimtalk_batch(
            numbers=to,
            content=content,
            template_code=template_code,
            plus_friend_id=plus_friend_id,
            title=title,
            use_sms_failover=use_sms_failover,
            dry_run=dry_run,
        )
    else:
        result = await sms_sender.send_manual_alimtalk(
            to=str(to),
            content=content,
            template_code=template_code,
            plus_friend_id=plus_friend_id,
            title=title,
            use_sms_failover=use_sms_failover,
            dry_run=dry_run,
        )

    logger.info(
        f"[alimtalk-manual] to={to if isinstance(to, str) else f'{len(to)}건'} "
        f"template={template_code} failover={use_sms_failover} "
        f"dry_run={dry_run} status={result.get('status')}"
    )
    return result


@router.get("/clawops/recent-calls")
async def clawops_recent_calls(
    status: str | None = Query(None, description="queued|ringing|in-progress|completed|failed"),
    page_size: int = Query(20, ge=1, le=100),
):
    """ClawOps 측에 기록된 최근 통화 — 우리 서버 이벤트 핸들러에 도달도 못 한 통화 디버깅용.

    예: 전화를 걸었는데 우리 서버 로그에 call_start가 안 떴을 때
    → 이 엔드포인트로 ClawOps 측 status 확인
    → 'failed'로 보이면 라우팅/계정 문제, 아예 안 보이면 통신사 단계 문제.
    """
    import os
    try:
        from clawops import ClawOps
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"clawops SDK 미설치: {e}")
    api_key = os.environ.get("CLAWOPS_API_KEY")
    account_id = os.environ.get("CLAWOPS_ACCOUNT_ID")
    if not api_key:
        raise HTTPException(status_code=400, detail="CLAWOPS_API_KEY 미설정")
    try:
        client = ClawOps(api_key=api_key, account_id=account_id) if account_id else ClawOps(api_key=api_key)
        kwargs: dict = {"page_size": page_size}
        if status:
            kwargs["status"] = status
        page = client.calls.list(**kwargs)
        items = []
        for c in page:
            items.append({
                k: getattr(c, k, None)
                for k in ("call_id", "id", "from_", "to", "status", "direction",
                          "duration", "created_at", "started_at", "ended_at",
                          "end_reason", "error", "error_code")
            })
        return {"count": len(items), "calls": items}
    except Exception as e:
        logger.error(f"[CLAWOPS] recent-calls 조회 실패: {e}")
        raise HTTPException(status_code=502, detail=f"ClawOps API 호출 실패: {e}")


@router.get("/analytics")
async def analytics(days: int = Query(7, ge=1, le=365)):
    return call_db.get_analytics(days=days)


@router.get("/data-integrity")
async def data_integrity():
    return call_db.get_data_integrity()


@router.post("/reclassify-all")
async def reclassify_all_unclassified(limit: int = Query(20, ge=1, le=100)):
    """미분류/실패 통화를 일괄 재분류 (백오프 없이 순차)."""
    rows = call_db.list_unclassified(limit=limit)
    results = []
    for r in rows:
        out = await call_classifier.classify_and_save(
            call_id=r["call_id"],
            transcript=r.get("transcript") or "",
            from_number=r.get("from_number") or "",
        )
        results.append({"call_id": r["call_id"], **out})
    return {"processed": len(results), "results": results}


# ──────────────────── 콜백 큐 ────────────────────


@router.get("/callbacks")
async def list_callbacks(
    status: str | None = Query(None, description="pending|in_progress|done|failed|cancelled"),
    active_only: bool = Query(False, description="true면 pending+in_progress만"),
    from_number: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """콜백 큐 목록."""
    statuses = None
    if active_only:
        statuses = [callback_db.STATUS_PENDING, callback_db.STATUS_IN_PROGRESS]
    return callback_db.list_callbacks(
        status=status, statuses=statuses,
        from_number=from_number, limit=limit, offset=offset,
    )


@router.get("/callbacks/stats")
async def callbacks_stats():
    """상태별 카운트 (대시보드 KPI)."""
    return {
        "stats": callback_db.get_stats(),
        "dispatcher_ready": callback_dispatcher.is_ready(),
    }


@router.post("/callbacks")
async def create_callback(request: Request):
    """수동으로 콜백 큐에 추가.

    Body:
        {
            "from_number": "01012345678",
            "customer_name": "홍길동" (선택),
            "reason": "왜 콜백이 필요한지" (선택),
            "priority": 1~5 (기본 3),
            "max_retries": 정수 (기본 3),
            "scheduled_at": "ISO 8601" (선택, 예약),
            "notes": "메모" (선택)
        }
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON body 필요")

    from_number = (body.get("from_number") or "").replace("-", "").replace(" ", "").strip()
    if not from_number:
        raise HTTPException(status_code=400, detail="from_number 필수")

    cb_id = callback_db.enqueue(
        from_number=from_number,
        source=callback_db.SOURCE_MANUAL,
        customer_name=body.get("customer_name"),
        reason=body.get("reason") or "관리자 수동 추가",
        original_call_id=body.get("original_call_id"),
        priority=int(body.get("priority", callback_db.PRIORITY_NORMAL)),
        max_retries=int(body.get("max_retries", 3)),
        scheduled_at=body.get("scheduled_at"),
        notes=body.get("notes"),
    )
    return callback_db.get_callback(cb_id)


@router.get("/callbacks/{cb_id}")
async def get_callback_detail(cb_id: int):
    cb = callback_db.get_callback(cb_id)
    if not cb:
        raise HTTPException(status_code=404, detail="callback not found")
    return cb


@router.patch("/callbacks/{cb_id}")
async def update_callback(cb_id: int, request: Request):
    """priority / status / note 부분 업데이트.

    Body: { "priority": 1~5 } | { "status": "done|cancelled" } | { "note": "..." }
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON body 필요")

    cb = callback_db.get_callback(cb_id)
    if not cb:
        raise HTTPException(status_code=404, detail="callback not found")

    changed = False
    if "priority" in body:
        callback_db.update_priority(cb_id, int(body["priority"]))
        changed = True
    if "status" in body:
        try:
            callback_db.update_status(cb_id, body["status"], result=body.get("result"))
            changed = True
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    if body.get("note"):
        callback_db.append_note(cb_id, body["note"])
        changed = True

    if not changed:
        raise HTTPException(status_code=400, detail="priority/status/note 중 하나 필요")

    return callback_db.get_callback(cb_id)


@router.delete("/callbacks/{cb_id}")
async def cancel_callback(cb_id: int, reason: str | None = Query(None)):
    """soft cancel — 물리 삭제 X."""
    cb = callback_db.get_callback(cb_id)
    if not cb:
        raise HTTPException(status_code=404, detail="callback not found")
    callback_db.cancel(cb_id, reason=reason)
    return callback_db.get_callback(cb_id)


@router.post("/callbacks/{cb_id}/call-now")
async def call_now(cb_id: int, request: Request):
    """콜백을 즉시 발신.

    Body (선택):
        {
            "prompt": "이번 통화 인사 멘트·맥락" (없으면 기본 시스템 프롬프트)
        }
    """
    if not callback_dispatcher.is_ready():
        raise HTTPException(
            status_code=503,
            detail="ClawOps agent가 아직 초기화 안 됐어요. 서버 시작 후 잠시 후 재시도.",
        )

    cb = callback_db.get_callback(cb_id)
    if not cb:
        raise HTTPException(status_code=404, detail="callback not found")

    prompt = None
    try:
        body = await request.json()
        prompt = (body or {}).get("prompt")
    except Exception:
        pass

    result = await callback_dispatcher.execute_callback(cb_id, prompt=prompt)
    return {"callback": callback_db.get_callback(cb_id), "dispatch": result}
