"""통화 종료 후 자동 SMS 발송.

공급자는 auto 모드에서 Solapi 키 있으면 Solapi, 없으면 CLAW OPS를 쓴다(NCP도 지원).
config.sms.provider 로 강제 지정 가능. 본문은 헤더 + 요약(or no_content) + 풋터로 합성하고,
010 번호에만 보낸다(070·1588 등 사업자번호는 skip). 결과는 aicc_calls.sms_* 에 기록.

주의: CLAW OPS 백엔드는 070 발신번호를 사전등록해둬야 실제로 나간다.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from app.cc_web_interface.admin_aicc import call_log_db as db
from app.cc_web_interface.admin_aicc import config as cfg

logger = logging.getLogger(__name__)

# AICC 발신번호 (CLAW OPS 백엔드 사용 시. Solapi는 SOLAPI_SENDER 환경변수 사용)
SMS_FROM_NUMBER = "07012345678"

# 단문 SMS 최대 (UTF-8 기준 안전 버퍼)
SMS_MAX_BYTES = 90  # 한글 약 45자 — 가운데 요약은 이 안에서 끝나야 단문 단가 유지
# 초과 시 LMS(type="mms") 자동 전환

# Provider 식별자
PROVIDER_SOLAPI = "solapi"
PROVIDER_CLAWOPS = "clawops"
PROVIDER_NCP = "ncp"

# NCP SENS 엔드포인트
NCP_BASE_URL = "https://sens.apigw.ntruss.com"


# ──────────────────── 본문 합성 ────────────────────


def _normalize_phone(num: str) -> str:
    if not num:
        return ""
    return re.sub(r"[^\d]", "", num).replace("+82", "0").lstrip("82") if num.startswith("+") else re.sub(r"[^\d]", "", num)


def _is_mobile(num: str) -> bool:
    """010/011/016/017/018/019 시작 = 휴대폰."""
    n = _normalize_phone(num)
    return bool(re.match(r"^01[016789]\d{7,8}$", n))


def _byte_len(s: str) -> int:
    return len(s.encode("utf-8"))


def build_sms_body(
    sms_cfg: dict[str, Any],
    summary: str,
    has_meaningful_content: bool,
) -> tuple[str, str]:
    """SMS 본문 합성. (body, middle_used) 반환.

    middle_used: 실제로 가운데 자리에 들어간 텍스트 (요약 또는 no_content_text)
    """
    header = (sms_cfg.get("header_text") or "").strip()
    footer = (sms_cfg.get("footer_text") or "").strip()
    no_content = (sms_cfg.get("no_content_text") or "").strip()

    middle = summary.strip() if (has_meaningful_content and summary.strip()) else no_content

    parts = [p for p in [header, middle, footer] if p]
    return "\n\n".join(parts), middle


# ──────────────────── ClawOps 클라이언트 ────────────────────


_clawops_client: Any = None


def _get_clawops_client():
    """ClawOps SDK lazy 초기화. 한번 만들고 재사용."""
    global _clawops_client
    if _clawops_client is not None:
        return _clawops_client
    try:
        from clawops import ClawOps
    except ImportError as e:
        logger.error(f"[SMS] clawops 패키지 import 실패: {e}")
        return None

    api_key = os.environ.get("CLAWOPS_API_KEY")
    account_id = os.environ.get("CLAWOPS_ACCOUNT_ID")
    if not api_key:
        logger.error("[SMS] CLAWOPS_API_KEY 미설정")
        return None
    try:
        if account_id:
            _clawops_client = ClawOps(api_key=api_key, account_id=account_id)
        else:
            _clawops_client = ClawOps(api_key=api_key)
        logger.info("[SMS] ClawOps client 초기화 완료")
    except Exception as e:
        logger.error(f"[SMS] ClawOps client 생성 실패: {e}")
        return None
    return _clawops_client


# ──────────────────── Solapi 백엔드 ────────────────────


SOLAPI_URL = "https://api.solapi.com/messages/v4/send"


def _solapi_configured() -> bool:
    return bool(os.environ.get("SOLAPI_API_KEY") and os.environ.get("SOLAPI_API_SECRET"))


def _clawops_configured() -> bool:
    return bool(os.environ.get("CLAWOPS_API_KEY"))


def _ncp_configured() -> bool:
    return bool(
        os.environ.get("NCP_ACCESS_KEY")
        and os.environ.get("NCP_SECRET_KEY")
        and os.environ.get("NCP_SMS_SERVICE_ID")
    )


def get_active_provider(config: Optional[dict] = None) -> Optional[str]:
    """어드민 설정에 따라 활성 공급자 결정. 키 없으면 None.

    우선순위(auto): Solapi → CLAW OPS → NCP. 어드민 설정으로 강제 지정 가능.
    """
    if config is None:
        try:
            config = cfg.load_config()
        except Exception:
            config = {}
    pref = (config.get("sms", {}).get("provider") or "auto").strip().lower()
    if pref == "solapi":
        return PROVIDER_SOLAPI if _solapi_configured() else None
    if pref == "clawops":
        return PROVIDER_CLAWOPS if _clawops_configured() else None
    if pref == "ncp":
        return PROVIDER_NCP if _ncp_configured() else None
    # auto
    if _solapi_configured():
        return PROVIDER_SOLAPI
    if _clawops_configured():
        return PROVIDER_CLAWOPS
    if _ncp_configured():
        return PROVIDER_NCP
    return None


def get_provider_status() -> dict:
    return {
        "solapi_configured": _solapi_configured(),
        "clawops_configured": _clawops_configured(),
        "ncp_configured": _ncp_configured(),
        "active_provider": get_active_provider(),
    }


def _solapi_auth_header() -> str:
    api_key = os.environ.get("SOLAPI_API_KEY", "")
    secret = os.environ.get("SOLAPI_API_SECRET", "")
    date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    salt = uuid.uuid4().hex
    sig = hmac.new(secret.encode(), f"{date}{salt}".encode(), hashlib.sha256).hexdigest()
    return f"HMAC-SHA256 apiKey={api_key}, date={date}, salt={salt}, signature={sig}"


async def _send_via_solapi(to: str, text: str) -> dict:
    sender = os.environ.get("SOLAPI_SENDER", "").replace("-", "") or SMS_FROM_NUMBER.replace("-", "")
    payload = {
        "message": {
            "to": _normalize_phone(to),
            "from": sender,
            "text": text,
        }
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                SOLAPI_URL,
                json=payload,
                headers={
                    "Authorization": _solapi_auth_header(),
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json() if resp.text else {}
            msg_id = data.get("messageId") or data.get("groupId") or ""
            logger.info(f"[SMS/Solapi] sent → {to} ({msg_id})")
            return {"ok": True, "provider": PROVIDER_SOLAPI, "message_id": str(msg_id)}
    except httpx.HTTPStatusError as e:
        body = ""
        try:
            body = e.response.text[:300]
        except Exception:
            pass
        return {"ok": False, "provider": PROVIDER_SOLAPI, "error": f"{e.response.status_code} {body}"}
    except Exception as e:
        return {"ok": False, "provider": PROVIDER_SOLAPI, "error": str(e)[:300]}


async def _send_via_clawops(to: str, body: str, is_long: bool) -> dict:
    """기존 ClawOps SDK 사용 — async wrapper."""
    client = _get_clawops_client()
    if client is None:
        return {"ok": False, "provider": PROVIDER_CLAWOPS, "error": "clawops_client_unavailable"}
    to_normalized = _normalize_phone(to)

    def _send_sync():
        kwargs = dict(to=to_normalized, from_=SMS_FROM_NUMBER, body=body)
        if is_long:
            kwargs["type"] = "mms"
            kwargs["subject"] = "마음케어 상담 안내"
        return client.messages.create(**kwargs)

    try:
        msg = await asyncio.to_thread(_send_sync)
        message_id = getattr(msg, "message_id", None) or getattr(msg, "id", None) or ""
        logger.info(f"[SMS/ClawOps] sent → {to_normalized} ({message_id})")
        return {"ok": True, "provider": PROVIDER_CLAWOPS, "message_id": str(message_id)}
    except Exception as e:
        return {"ok": False, "provider": PROVIDER_CLAWOPS, "error": f"{type(e).__name__}: {e}"[:500]}


# ──────────────────── NCP SENS 백엔드 ────────────────────


def _ncp_signature(method: str, uri: str, timestamp: str) -> str:
    """NCP API Gateway v2 서명 (HMAC-SHA256, base64).

    문서: https://api.ncloud-docs.com/docs/common-ncpapi
    """
    access_key = os.environ.get("NCP_ACCESS_KEY", "")
    secret = os.environ.get("NCP_SECRET_KEY", "").encode("utf-8")
    msg = f"{method} {uri}\n{timestamp}\n{access_key}".encode("utf-8")
    return base64.b64encode(hmac.new(secret, msg, hashlib.sha256).digest()).decode("utf-8")


async def _send_via_ncp(to: str, body: str, is_long: bool, subject: Optional[str] = None) -> dict:
    """NCP SENS SMS/LMS 발송."""
    service_id = os.environ.get("NCP_SMS_SERVICE_ID", "")
    from_number = (os.environ.get("NCP_FROM_NUMBER") or SMS_FROM_NUMBER).replace("-", "")
    if not service_id:
        return {"ok": False, "provider": PROVIDER_NCP, "error": "NCP_SMS_SERVICE_ID 미설정"}

    uri = f"/sms/v2/services/{service_id}/messages"
    url = f"{NCP_BASE_URL}{uri}"
    ts = str(int(time.time() * 1000))
    to_normalized = _normalize_phone(to)

    payload: dict[str, Any] = {
        "type": "LMS" if is_long else "SMS",
        "contentType": "COMM",
        "countryCode": "82",
        "from": from_number,
        "content": body,
        "messages": [{"to": to_normalized}],
    }
    if is_long:
        payload["subject"] = (subject or "마음케어 안내")[:40]

    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "x-ncp-apigw-timestamp": ts,
        "x-ncp-iam-access-key": os.environ.get("NCP_ACCESS_KEY", ""),
        "x-ncp-apigw-signature-v2": _ncp_signature("POST", uri, ts),
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json() if resp.text else {}

        # NCP는 202 + statusName: success 가 성공
        if data.get("statusName") != "success":
            return {
                "ok": False,
                "provider": PROVIDER_NCP,
                "error": f"{data.get('statusCode')}: {data}"[:300],
            }
        request_id = data.get("requestId", "")
        logger.info(f"[SMS/NCP] sent → {to_normalized} ({request_id}, {'LMS' if is_long else 'SMS'})")
        return {"ok": True, "provider": PROVIDER_NCP, "message_id": str(request_id)}

    except httpx.HTTPStatusError as e:
        body_text = ""
        try:
            body_text = e.response.text[:300]
        except Exception:
            pass
        return {"ok": False, "provider": PROVIDER_NCP,
                "error": f"{e.response.status_code} {body_text}"}
    except Exception as e:
        return {"ok": False, "provider": PROVIDER_NCP, "error": f"{type(e).__name__}: {e}"[:300]}


# ──────────────────── 메인 진입 ────────────────────


async def send_call_summary_sms(
    *,
    call_id: str,
    to_number: str,
    summary: str,
    turns: int,
    call_status: str,                # completed / transferred / blocked / failed
    block_reason: Optional[str] = None,
) -> dict[str, Any]:
    """통화 종료 후 호출. 어드민 설정 따라 발송하거나 skip.

    Returns:
        {"status": "sent"|"failed"|"skipped"|"disabled", ...}
    """
    config = cfg.load_config()
    sms_cfg = config.get("sms") or {}

    # 1. 전체 OFF
    if not sms_cfg.get("enabled", True):
        db.update_sms(call_id, status=db.SMS_DISABLED)
        logger.info(f"[SMS] {call_id}: 어드민에서 SMS 비활성")
        return {"status": db.SMS_DISABLED}

    # 2. 차단 통화는 skip (운영시간 외 등)
    if call_status == db.STATUS_BLOCKED and sms_cfg.get("skip_blocked_calls", True):
        db.update_sms(call_id, status=db.SMS_SKIPPED, error=f"call_blocked:{block_reason or ''}")
        logger.info(f"[SMS] {call_id}: 차단 통화 ({block_reason}) — skip")
        return {"status": db.SMS_SKIPPED, "reason": "blocked"}

    # 3. 자동 전환된 통화 (옵션)
    if call_status == db.STATUS_TRANSFERRED and not sms_cfg.get("send_on_transfer", True):
        db.update_sms(call_id, status=db.SMS_SKIPPED, error="transferred_no_send")
        logger.info(f"[SMS] {call_id}: 전환 통화 — skip (어드민 설정)")
        return {"status": db.SMS_SKIPPED, "reason": "transferred"}

    # 4. 휴대폰 아니면 skip
    if sms_cfg.get("mobile_only", True) and not _is_mobile(to_number):
        db.update_sms(call_id, status=db.SMS_SKIPPED, error=f"non_mobile:{to_number}")
        logger.info(f"[SMS] {call_id}: 휴대폰 아님 ({to_number}) — skip")
        return {"status": db.SMS_SKIPPED, "reason": "non_mobile"}

    # 5. 본문 합성
    min_turns = int(sms_cfg.get("min_turns_for_summary", 2))
    has_meaningful = (turns >= min_turns) and bool((summary or "").strip())
    body, middle = build_sms_body(sms_cfg, summary or "", has_meaningful)

    if not body.strip():
        db.update_sms(call_id, status=db.SMS_SKIPPED, error="empty_body")
        logger.warning(f"[SMS] {call_id}: 본문 비어있음 — skip")
        return {"status": db.SMS_SKIPPED, "reason": "empty_body"}

    # 6. 공급자 자동 선택 (Solapi 우선, 없으면 CLAW OPS)
    provider = get_active_provider(config)
    if provider is None:
        db.update_sms(call_id, status=db.SMS_FAILED, body=body, summary=middle,
                      error="no_sms_provider_configured")
        return {"status": db.SMS_FAILED, "reason": "no_provider"}

    to_normalized = _normalize_phone(to_number)
    is_long = _byte_len(body) > SMS_MAX_BYTES

    # 7. 발송
    if provider == PROVIDER_SOLAPI:
        result = await _send_via_solapi(to_normalized, body)
    elif provider == PROVIDER_NCP:
        result = await _send_via_ncp(to_normalized, body, is_long)
    else:
        result = await _send_via_clawops(to_normalized, body, is_long)

    patch_kwargs = {"body": body, "summary": middle, "provider": result.get("provider")}
    if result.get("ok"):
        db.update_sms(call_id, status=db.SMS_SENT, message_id=result.get("message_id", ""),
                      error=None, **patch_kwargs)
        logger.info(f"[SMS] ✅ 발송 완료 {call_id} → {to_normalized} "
                    f"({'LMS' if is_long else 'SMS'}, {_byte_len(body)}B, "
                    f"provider={result.get('provider')}, mid={result.get('message_id')})")
        return {"status": db.SMS_SENT, "provider": result.get("provider"),
                "message_id": result.get("message_id"), "is_long": is_long}

    err = result.get("error", "unknown")
    db.update_sms(call_id, status=db.SMS_FAILED, error=err[:500], **patch_kwargs)
    logger.error(f"[SMS] ❌ 발송 실패 {call_id} → {to_normalized} ({result.get('provider')}): {err}")
    return {"status": db.SMS_FAILED, "provider": result.get("provider"), "error": err}


# ──────────────────── 재발송 (대시보드용) ────────────────────


async def resend_for_call(call_id: str) -> dict[str, Any]:
    """대시보드 [재발송] 버튼이 호출. 기존 row의 sms_summary가 있으면 그대로,
    없으면 transcript_refined에서 짧게 추출해서 재시도."""
    row = db.get_call(call_id)
    if not row:
        return {"status": db.SMS_FAILED, "error": "call_not_found"}

    summary = row.get("sms_summary") or row.get("transcript_refined") or ""
    if len(summary) > 300:
        summary = summary[:300] + "..."

    return await send_call_summary_sms(
        call_id=call_id,
        to_number=row.get("from_number") or "",
        summary=summary,
        turns=999 if summary else 0,    # 재발송 시 요약 있으면 의미 있는 통화로 간주
        call_status=row.get("status") or "completed",
        block_reason=row.get("block_reason"),
    )


# ──────────────────── 수동 발송 (대시보드용) ────────────────────


async def send_manual_sms(
    *,
    to: str,
    content: str,
    subject: Optional[str] = None,
    provider: Optional[str] = None,   # "solapi" | "clawops" | "ncp" | None (auto)
    dry_run: bool = True,             # ⭐ 기본 미발송. 명시적 False 줘야 실제 발송.
    mobile_only: bool = True,
) -> dict[str, Any]:
    """관리자가 임의 번호에 임의 본문으로 단건 SMS 발송.

    dry_run=True (기본): 미리보기만 반환, 실제 발송 X (실수 방지).
    dry_run=False: 실제 발송 + 결과 dict 반환.

    90byte 초과 시 자동 LMS 전환.
    """
    to_normalized = _normalize_phone(to)

    # 1. 휴대폰 검증
    if mobile_only and not _is_mobile(to_normalized):
        return {
            "status": "error",
            "reason": "non_mobile",
            "to": to_normalized,
            "message": f"휴대폰 번호가 아닙니다: {to_normalized}",
        }

    # 2. 본문 검증
    body = (content or "").strip()
    if not body:
        return {"status": "error", "reason": "empty_content", "message": "본문이 비어있습니다."}

    body_bytes = _byte_len(body)
    is_long = body_bytes > SMS_MAX_BYTES

    # 3. provider 결정
    if provider:
        active = provider.lower().strip()
        if active == PROVIDER_SOLAPI and not _solapi_configured():
            return {"status": "error", "reason": "provider_not_configured", "provider": active}
        if active == PROVIDER_CLAWOPS and not _clawops_configured():
            return {"status": "error", "reason": "provider_not_configured", "provider": active}
        if active == PROVIDER_NCP and not _ncp_configured():
            return {"status": "error", "reason": "provider_not_configured", "provider": active}
    else:
        active = get_active_provider()
        if active is None:
            return {"status": "error", "reason": "no_provider_configured"}

    # 4. dry_run이면 여기서 종료 (실수 방지 기본값)
    if dry_run:
        return {
            "status": "preview",
            "to": to_normalized,
            "body": body,
            "byte_len": body_bytes,
            "is_long": is_long,
            "message_type": "LMS" if is_long else "SMS",
            "provider": active,
            "subject": subject if is_long else None,
            "note": "실제 발송하려면 dry_run=false 로 다시 호출하세요.",
        }

    # 5. 실제 발송
    if active == PROVIDER_SOLAPI:
        result = await _send_via_solapi(to_normalized, body)
    elif active == PROVIDER_NCP:
        result = await _send_via_ncp(to_normalized, body, is_long, subject=subject)
    else:
        result = await _send_via_clawops(to_normalized, body, is_long)

    logger.info(
        f"[SMS/Manual] to={to_normalized} provider={active} "
        f"type={'LMS' if is_long else 'SMS'} bytes={body_bytes} ok={result.get('ok')}"
    )

    return {
        "status": "sent" if result.get("ok") else "failed",
        "to": to_normalized,
        "body": body,
        "byte_len": body_bytes,
        "is_long": is_long,
        "message_type": "LMS" if is_long else "SMS",
        "provider": result.get("provider"),
        "message_id": result.get("message_id"),
        "error": result.get("error"),
    }


# ──────────────────── NCP 알림톡 (Biz Message) ────────────────────


PROVIDER_NCP_ALIMTALK = "ncp_alimtalk"


async def _send_alimtalk_via_ncp(
    *,
    plus_friend_id: str,
    template_code: str,
    to: str,
    content: str,
    title: Optional[str] = None,
    use_sms_failover: bool = False,
    failover_subject: Optional[str] = None,
    failover_content: Optional[str] = None,
    failover_from: Optional[str] = None,
) -> dict[str, Any]:
    """NCP SENS 알림톡 발송 (Biz Message).

    POST /alimtalk/v2/services/{biz_service_id}/messages
    사전 승인된 templateCode + plusFriendId 필수.
    """
    biz_service_id = os.environ.get("NCP_BIZ_SERVICE_ID", "")
    if not biz_service_id:
        return {"ok": False, "provider": PROVIDER_NCP_ALIMTALK,
                "error": "NCP_BIZ_SERVICE_ID 미설정"}

    to_normalized = _normalize_phone(to)
    uri = f"/alimtalk/v2/services/{biz_service_id}/messages"
    url = f"{NCP_BASE_URL}{uri}"
    ts = str(int(time.time() * 1000))

    message: dict[str, Any] = {
        "countryCode": "82",
        "to": to_normalized,
        "content": content,
    }
    if title:
        message["title"] = title

    if use_sms_failover:
        message["useSmsFailover"] = True
        failover_config: dict[str, Any] = {}
        if failover_subject:
            failover_config["subject"] = failover_subject
        # 미입력 시 알림톡 본문 그대로
        failover_config["content"] = failover_content or content
        if failover_from:
            failover_config["from"] = failover_from.replace("-", "")
        # 길이로 SMS/LMS 자동 결정
        failover_config["type"] = "LMS" if _byte_len(failover_config["content"]) > SMS_MAX_BYTES else "SMS"
        message["failoverConfig"] = failover_config

    payload = {
        "plusFriendId": plus_friend_id,
        "templateCode": template_code,
        "messages": [message],
    }

    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "x-ncp-apigw-timestamp": ts,
        "x-ncp-iam-access-key": os.environ.get("NCP_ACCESS_KEY", ""),
        "x-ncp-apigw-signature-v2": _ncp_signature("POST", uri, ts),
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json() if resp.text else {}

        # 알림톡은 202 + statusName: success | processing 이 성공
        if data.get("statusName") not in ("success", "processing"):
            return {"ok": False, "provider": PROVIDER_NCP_ALIMTALK,
                    "error": f"{data.get('statusCode')}: {data}"[:400]}

        request_id = data.get("requestId", "")
        messages = data.get("messages", [])
        msg = messages[0] if messages else {}
        msg_status = msg.get("requestStatusName", "")

        if msg_status == "fail":
            return {
                "ok": False,
                "provider": PROVIDER_NCP_ALIMTALK,
                "error": msg.get("requestStatusDesc", "unknown_fail"),
                "status_code": msg.get("requestStatusCode"),
                "request_id": request_id,
            }

        logger.info(
            f"[ALIMTALK/NCP] sent → {to_normalized} "
            f"template={template_code} channel={plus_friend_id} "
            f"({request_id}, msg_id={msg.get('messageId', '')})"
        )
        return {
            "ok": True,
            "provider": PROVIDER_NCP_ALIMTALK,
            "message_id": msg.get("messageId", ""),
            "request_id": request_id,
            "status_name": data.get("statusName"),
        }
    except httpx.HTTPStatusError as e:
        body_text = ""
        try:
            body_text = e.response.text[:300]
        except Exception:
            pass
        return {"ok": False, "provider": PROVIDER_NCP_ALIMTALK,
                "error": f"{e.response.status_code} {body_text}"}
    except Exception as e:
        return {"ok": False, "provider": PROVIDER_NCP_ALIMTALK,
                "error": f"{type(e).__name__}: {e}"[:300]}


async def send_manual_alimtalk(
    *,
    to: str,
    content: str,
    template_code: str,
    plus_friend_id: Optional[str] = None,   # 미입력 시 NCP_KAKAO_CHANNEL
    title: Optional[str] = None,
    use_sms_failover: bool = False,
    dry_run: bool = True,
    mobile_only: bool = True,
) -> dict[str, Any]:
    """관리자가 임의 번호에 알림톡 단건 발송."""
    to_normalized = _normalize_phone(to)

    if mobile_only and not _is_mobile(to_normalized):
        return {"status": "error", "reason": "non_mobile", "to": to_normalized,
                "message": f"휴대폰 번호가 아닙니다: {to_normalized}"}

    body = (content or "").strip()
    if not body:
        return {"status": "error", "reason": "empty_content", "message": "본문이 비어있습니다."}

    if not template_code:
        return {"status": "error", "reason": "no_template_code", "message": "템플릿 코드가 필요합니다."}

    channel = plus_friend_id or os.environ.get("NCP_KAKAO_CHANNEL", "")
    if not channel:
        return {"status": "error", "reason": "no_channel",
                "message": "카카오 채널 ID가 필요합니다 (plus_friend_id 또는 NCP_KAKAO_CHANNEL)."}

    if not _ncp_configured():
        return {"status": "error", "reason": "ncp_not_configured",
                "message": "NCP_ACCESS_KEY / NCP_SECRET_KEY / NCP_BIZ_SERVICE_ID 미설정"}

    if dry_run:
        return {
            "status": "preview",
            "to": to_normalized,
            "plus_friend_id": channel,
            "template_code": template_code,
            "content": body,
            "title": title,
            "use_sms_failover": use_sms_failover,
            "byte_len": _byte_len(body),
            "note": "실제 발송하려면 dry_run=false 로 다시 호출하세요.",
        }

    result = await _send_alimtalk_via_ncp(
        plus_friend_id=channel,
        template_code=template_code,
        to=to_normalized,
        content=body,
        title=title,
        use_sms_failover=use_sms_failover,
        failover_from=os.environ.get("NCP_FROM_NUMBER") or SMS_FROM_NUMBER,
    )

    logger.info(
        f"[ALIMTALK/Manual] to={to_normalized} template={template_code} "
        f"failover={use_sms_failover} ok={result.get('ok')}"
    )

    return {
        "status": "sent" if result.get("ok") else "failed",
        "to": to_normalized,
        "plus_friend_id": channel,
        "template_code": template_code,
        "content": body,
        "title": title,
        "use_sms_failover": use_sms_failover,
        "provider": result.get("provider"),
        "message_id": result.get("message_id"),
        "request_id": result.get("request_id"),
        "error": result.get("error"),
    }


async def send_manual_alimtalk_batch(
    *,
    numbers: list[str],
    content: str,
    template_code: str,
    plus_friend_id: Optional[str] = None,
    title: Optional[str] = None,
    use_sms_failover: bool = False,
    dry_run: bool = True,
    mobile_only: bool = True,
    max_concurrent: int = 5,
) -> dict[str, Any]:
    """알림톡 대량 발송."""
    if not numbers:
        return {"status": "error", "reason": "empty_numbers", "results": []}

    seen: set[str] = set()
    unique: list[str] = []
    for n in numbers:
        norm = _normalize_phone(n or "")
        if norm and norm not in seen:
            seen.add(norm)
            unique.append(norm)

    if not unique:
        return {"status": "error", "reason": "all_invalid", "results": []}

    sem = asyncio.Semaphore(max_concurrent)

    async def _one(num: str) -> dict[str, Any]:
        async with sem:
            return await send_manual_alimtalk(
                to=num,
                content=content,
                template_code=template_code,
                plus_friend_id=plus_friend_id,
                title=title,
                use_sms_failover=use_sms_failover,
                dry_run=dry_run,
                mobile_only=mobile_only,
            )

    results = await asyncio.gather(*[_one(n) for n in unique], return_exceptions=False)

    sent = sum(1 for r in results if r.get("status") == "sent")
    failed = sum(1 for r in results if r.get("status") == "failed")
    preview = sum(1 for r in results if r.get("status") == "preview")
    errors = sum(1 for r in results if r.get("status") == "error")

    return {
        "status": "preview" if dry_run else ("sent" if (failed == 0 and errors == 0) else "partial"),
        "total": len(unique),
        "counts": {"sent": sent, "failed": failed, "preview": preview, "error": errors},
        "results": results,
    }


async def send_manual_sms_batch(
    *,
    numbers: list[str],
    content: str,
    subject: Optional[str] = None,
    provider: Optional[str] = None,
    dry_run: bool = True,
    mobile_only: bool = True,
    max_concurrent: int = 5,
) -> dict[str, Any]:
    """대량 발송. numbers 리스트의 각 번호에 같은 본문 발송.

    중복 자동 제거, 동시 발송 제한.
    """
    if not numbers:
        return {"status": "error", "reason": "empty_numbers", "results": []}

    # 중복 제거 + 정규화
    seen: set[str] = set()
    unique: list[str] = []
    for n in numbers:
        norm = _normalize_phone(n or "")
        if norm and norm not in seen:
            seen.add(norm)
            unique.append(norm)

    if not unique:
        return {"status": "error", "reason": "all_invalid", "results": []}

    sem = asyncio.Semaphore(max_concurrent)

    async def _one(num: str) -> dict[str, Any]:
        async with sem:
            return await send_manual_sms(
                to=num,
                content=content,
                subject=subject,
                provider=provider,
                dry_run=dry_run,
                mobile_only=mobile_only,
            )

    results = await asyncio.gather(*[_one(n) for n in unique], return_exceptions=False)

    sent = sum(1 for r in results if r.get("status") == "sent")
    failed = sum(1 for r in results if r.get("status") == "failed")
    preview = sum(1 for r in results if r.get("status") == "preview")
    errors = sum(1 for r in results if r.get("status") == "error")

    return {
        "status": "preview" if dry_run else ("sent" if (failed == 0 and errors == 0) else "partial"),
        "total": len(unique),
        "counts": {"sent": sent, "failed": failed, "preview": preview, "error": errors},
        "results": results,
    }
