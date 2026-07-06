"""통화 종료 시 Gemini로 트랜스크립트 정제 + 카테고리/고객유형 분류 + FAQ 매칭.

기존에 main.py가 따로 호출하던 트랜스크립트 정제와 통합 → API 호출 횟수 동일.
DB의 from_number 7일 이력으로 신규/재문의 사전 판정 후 Gemini가 보정.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

from app.cc_web_interface.admin_aicc import call_log_db as db
from app.cc_web_interface.admin_aicc import scenario_loader

logger = logging.getLogger(__name__)

CATEGORIES = ["앱 찾기·설치", "훈련 방법", "앱 기술 문제", "훈련 내용", "환불·처방", "기타"]
CUSTOMER_TYPES = ["신규", "재문의", "불만", "일반"]


def _build_prompt(transcript: str, prior_history: bool, faq_summary: str) -> str:
    return f"""아래는 마음케어 고객센터 AI 상담 통화 트랜스크립트입니다. 음성 인식 오류로 한국어가 다른 언어로 잘못 변환된 부분이 있을 수 있습니다.

## 작업 1 — 트랜스크립트 정제
모든 내용을 자연스러운 한국어로 교정하세요. 형식(👤 고객: / 🤖 AI:)은 유지하세요.

## 작업 2 — 분류
다음 항목을 JSON으로 분류하세요:
- "category": 다음 중 하나 — {", ".join(CATEGORIES)}
- "customer_type": 다음 중 하나 — {", ".join(CUSTOMER_TYPES)}
  - 같은 발신자 7일 이내 이력 {'있음' if prior_history else '없음'} (참고)
  - 욕설/강한 불만 표현이 있으면 '불만'
  - 그 외 첫 통화는 '신규', 이력 있으면 '재문의', 불분명하면 '일반'
- "matched_faq_no": 고객 질문이 아래 FAQ 중 하나와 명확히 일치하면 그 번호. 없으면 null.

## 작업 3 — 사후 안내 SMS용 상담 요약
고객에게 통화 후 발송할 SMS 본문의 가운데 부분(상담 요약)을 작성하세요.
- 형식: 자연스러운 평문 (불릿/마크다운 금지, 1~3문장 권장)
- 어조: 정중한 존댓말, 어르신께 보내는 SMS 톤
- 내용: 고객이 실제로 상담받은 내용을 짧게 정리 (질문 / 안내 받은 답변 핵심만)
- 만약 의미 있는 상담 내용이 없거나 매우 짧은 통화면 빈 문자열("")로 출력

## 참고 FAQ 요약
{faq_summary}

## 출력 형식 (반드시 이 형식으로만 출력하세요. 다른 설명 금지)
<<<REFINED>>>
👤 고객: ...
🤖 AI: ...
...
<<<END_REFINED>>>
<<<JSON>>>
{{"category": "...", "customer_type": "...", "matched_faq_no": null}}
<<<END_JSON>>>
<<<SUMMARY>>>
한 두 문장으로 작성한 상담 요약. 의미 있는 상담이 없으면 비워두세요.
<<<END_SUMMARY>>>

## 트랜스크립트
{transcript}
"""


def _faq_summary() -> str:
    items = scenario_loader.get_faq_items()
    if not items:
        return "(FAQ 시나리오 미로드)"
    return "\n".join(f"Q{it['no']} [{it['category']}] {it['q']}" for it in items)


def _parse_response(text: str) -> tuple[str, dict, str]:
    refined = ""
    cls: dict[str, Any] = {}
    summary = ""
    m_ref = re.search(r"<<<REFINED>>>(.*?)<<<END_REFINED>>>", text, re.DOTALL)
    if m_ref:
        refined = m_ref.group(1).strip()
    m_json = re.search(r"<<<JSON>>>(.*?)<<<END_JSON>>>", text, re.DOTALL)
    if m_json:
        raw = m_json.group(1).strip()
        try:
            cls = json.loads(raw)
        except json.JSONDecodeError:
            # 따옴표 정리 등 폴백 시도
            cleaned = raw.replace("'", '"')
            try:
                cls = json.loads(cleaned)
            except Exception:
                cls = {}
    m_sum = re.search(r"<<<SUMMARY>>>(.*?)<<<END_SUMMARY>>>", text, re.DOTALL)
    if m_sum:
        summary = m_sum.group(1).strip()
    return refined, cls, summary


def _normalize_classification(cls: dict, prior_history: bool) -> dict:
    cat = cls.get("category") or ""
    if cat not in CATEGORIES:
        cat = "기타"
    ct = cls.get("customer_type") or ""
    if ct not in CUSTOMER_TYPES:
        ct = "재문의" if prior_history else "신규"
    faq_no = cls.get("matched_faq_no")
    if faq_no in ("", "null", None):
        faq_no = None
    else:
        try:
            faq_no = int(faq_no)
        except (ValueError, TypeError):
            faq_no = None
    return {"category": cat, "customer_type": ct, "matched_faq_no": faq_no}


async def classify_and_save(call_id: str, transcript: str, from_number: str = "") -> dict:
    """Gemini 호출 → 트랜스크립트 정제 + 분류 → DB 저장. 결과 dict 반환."""
    if not transcript or not transcript.strip():
        db.update_classification(
            call_id, category="기타", customer_type="일반", matched_faq_no=None,
            transcript_refined=None, error="empty transcript",
        )
        return {"category": "기타", "customer_type": "일반", "matched_faq_no": None}

    prior_history = db.has_history(from_number, within_days=7, exclude_call_id=call_id)

    try:
        from google import genai
        api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GOOGLE_API_KEY/GEMINI_API_KEY not set")
        client = genai.Client(api_key=api_key)
        prompt = _build_prompt(transcript, prior_history, _faq_summary())
        resp = client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=prompt,
        )
        raw = (resp.text or "").strip()
    except Exception as e:
        logger.error(f"[CALL_CLASSIFIER] Gemini call failed for {call_id}: {e}")
        db.update_classification(
            call_id, category=None, customer_type=None, matched_faq_no=None,
            transcript_refined=None, error=str(e)[:500],
        )
        return {"category": None, "customer_type": None, "matched_faq_no": None}

    refined, cls, summary = _parse_response(raw)
    norm = _normalize_classification(cls, prior_history)

    db.update_classification(
        call_id,
        category=norm["category"],
        customer_type=norm["customer_type"],
        matched_faq_no=norm["matched_faq_no"],
        transcript_refined=refined or None,
        error=None,
    )
    logger.info(
        f"[CALL_CLASSIFIER] {call_id} → {norm['category']} / {norm['customer_type']} "
        f"/ FAQ Q{norm['matched_faq_no']} / summary={'O' if summary else 'X'}"
    )
    return {"refined": refined, "summary": summary, **norm}


async def reclassify(call_id: str) -> dict:
    """기존 통화를 다시 분류 (CS팀이 [재분류 실행] 버튼 누를 때)."""
    row = db.get_call(call_id)
    if not row:
        return {"ok": False, "error": "call not found"}
    return await classify_and_save(
        call_id=call_id,
        transcript=row.get("transcript") or "",
        from_number=row.get("from_number") or "",
    )
