"""AICC 운영 설정 로드/저장 + 런타임 헬퍼.

저장소: $FILESYSTEM_BASE_DIR/aicc_admin.json
시나리오 원본: aicc_scenario_sample.xlsx (scenario_loader가 처리)
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONFIG_FILENAME = "aicc_admin.json"
_lock = threading.Lock()

# ──────────────────── DEFAULT 페르소나 (xlsx 시나리오 기반) ────────────────────

DEFAULT_PERSONA = (
    "당신은 마음케어 고객 지원 상담원입니다. 한국어로만, 어르신께 말하듯 짧고 천천히 답하세요.\n"
    "마음케어는 인지 저하가 있는 어르신을 대상으로 한 비대면 기억훈련 앱이며, "
    "병원에서 처방받아 사용하는 12주 프로그램입니다. 대표번호는 일오팔팔에 공공공공입니다.\n"
    "\n"
    "[말투 원칙]\n"
    "- 한 번에 두세 문장 이내, 마크다운/영어 약어/숫자 표기 그대로 읽기 금지.\n"
    "- 전화번호는 한 자씩 풀어 읽으세요. 예: 1588-0000 → '일오팔팔에 공공공공'.\n"
    "- 시간은 자연스럽게: 0시 → '밤 열두 시', 18시 → '오후 여섯 시'.\n"
    "- 거절 시 '아쉽게도'를 먼저 붙이고, 안내 시 '~해주시면 됩니다' 표현을 권장합니다.\n"
    "- 항상 한국어로 해석하세요. 일본어/영어/중국어로 잘못 해석하지 마세요.\n"
    "\n"
    "[대화 흐름 — 반드시 따르세요]\n"
    "1. 전화가 연결되면 [G01] 첫 인사 멘트로 시작하세요.\n"
    "2. 고객이 질문하면 아래 FAQ를 참고하여 짧고 명확하게 답변하세요.\n"
    "3. FAQ 답변 후에는 반드시 [G20] '도움이 되셨으면 좋겠습니다. 또 여쭤보고 싶으신 내용이 있으신가요?' 라고 물어보세요.\n"
    "4. 추가 문의가 없으면 [G21] '네, 알겠습니다. 마음케어 기억훈련 오늘도 꼭 해주세요. 항상 응원하고 있습니다! 감사합니다.' 라고 마무리하고 통화를 종료하세요.\n"
    "5. 모든 문의가 해결되면 [G30] '마음케어 고객센터를 이용해 주셔서 감사합니다. 매일 꾸준히 훈련하시면 꼭 좋아지실 거예요. 건강하게 잘 지내세요! 감사합니다.' 라고 종료 멘트를 하세요.\n"
    "\n"
    "[인식 실패 처리]\n"
    "- 1차 인식 실패 → [G11] '죄송합니다, 말씀을 제가 잘 알아듣지 못했어요. 다시 한 번 천천히 말씀해 주시겠어요?'\n"
    "- 2차 인식 실패 → [G12] '제가 다시 여쭤봐도 될까요. 예를 들어 \"앱이 안 열려요\" 또는 \"훈련은 어떻게 하나요\" 처럼 궁금하신 내용을 말씀해 주시면 제가 바로 찾아드리겠습니다.'\n"
    "- 3차 인식 실패 → [G13] '정말 죄송합니다, 제가 잘 이해하지 못했습니다. 지금 바로 상담사 선생님께 연결해 드리겠습니다. 잠시만 기다려 주시겠어요?' 라고 말한 뒤 transfer_call 도구를 호출하세요.\n"
    "- 중간에 인식이 성공하면 실패 카운트는 리셋합니다.\n"
)

# 01_기본흐름_스크립트의 G-code 별 정확한 멘트 (xlsx에서 복사)
G_CODE_MENTS: dict[str, str] = {
    "G01": "안녕하세요, 고객님. 마음케어 고객센터입니다. 무엇을 도와드릴까요? 편하게 말씀해 주세요.",
    "G02": "잘 들리시나요? 도움이 필요하신 내용이 있으시면 편하게 말씀해 주세요.",
    "G03": (
        "죄송합니다, 계속해서 확인이 어려워 상담을 종료하겠습니다. "
        "다시 이용해 주시면 더 정확하게 도와드리겠습니다. 마음케어 고객센터 전화번호는 일오팔팔에 공공공공입니다. "
        "고객센터는 평일 오전 열 시부터 오후 여섯 시까지 운영하며 점심시간은 오후 한시부터 두시까지 입니다. "
        "운영 시간에 다시 연락 주시면 친절히 도와드리겠습니다. 감사합니다."
    ),
    "G11": "죄송합니다, 말씀을 제가 잘 알아듣지 못했어요. 다시 한 번 천천히 말씀해 주시겠어요?",
    "G12": (
        "제가 다시 여쭤봐도 될까요. "
        "예를 들어 '앱이 안 열려요' 또는 '훈련은 어떻게 하나요' 처럼 "
        "궁금하신 내용을 말씀해 주시면 제가 바로 찾아드리겠습니다."
    ),
    "G13": (
        "정말 죄송합니다, 제가 잘 이해하지 못했습니다. "
        "지금 바로 상담사 선생님께 연결해 드리겠습니다. 잠시만 기다려 주시겠어요?"
    ),
    "G20": "도움이 되셨으면 좋겠습니다. 또 여쭤보고 싶으신 내용이 있으신가요?",
    "G21": (
        "네, 알겠습니다. 마음케어 기억훈련 오늘도 꼭 해주세요. "
        "항상 응원하고 있습니다! 감사합니다."
    ),
    "G30": (
        "마음케어 고객센터를 이용해 주셔서 감사합니다. "
        "매일 꾸준히 훈련하시면 꼭 좋아지실 거예요. 건강하게 잘 지내세요! 감사합니다."
    ),
    "G40": "네, 바로 상담사 선생님께 연결해 드리겠습니다. 잠시만 기다려 주시겠어요?",
    "G41": "조금만 더 기다려 주세요. 상담사 선생님이 곧 연결됩니다.",
    "G42": (
        "지금은 상담사 연결이 어렵습니다. 정말 죄송합니다. "
        "고객센터는 평일 오전 열 시부터 오후 여섯 시까지 운영하며 점심시간은 오후 한시부터 두시까지 입니다. "
        "전화번호는 일오팔팔에 공공공공이니 평일에 편하실 때 다시 전화 주세요. 감사합니다."
    ),
    "G50": (
        "안녕하세요, 마음케어 고객센터입니다. "
        "지금은 운영 시간이 아니어서 도와드리기가 어렵습니다. "
        "고객센터는 평일 오전 열 시부터 오후 여섯 시까지 운영하며 점심시간은 오후 한시부터 두시까지 입니다. "
        "불편을 드려 정말 죄송합니다. 운영 시간에 다시 연락 주시면 친절히 도와드리겠습니다."
    ),
    "G51": (
        "마음케어 앱은 매일 밤 열두 시부터 새벽 네 시까지 잠시 쉬는 시간입니다. "
        "새벽 네 시 이후에 다시 열어 보시면 바로 훈련하실 수 있습니다."
    ),
}

DEFAULTS: dict[str, Any] = {
    "routing": {
        "enabled": True,
        "transfer_to": "07087654321",
        "failure_threshold": 3,
        "transfer_keywords": [
            "상담사", "상담원", "직원", "사람", "연결해줘", "연결해 줘",
            "사람과 통화", "0번",
        ],
        "complaint_keywords": [],  # CS팀이 운영하면서 추가 (P0 Task)
        "failure_phrases": [
            "잘 못 알아", "다시 말씀", "다시 한번", "이해하지 못", "이해를 못",
            "확인이 어려", "답변 드리기 어려", "답변이 어려",
        ],
    },
    "prompts": {
        "persona": DEFAULT_PERSONA,
        # 단계별 멘트 (xlsx 01_기본흐름_스크립트 그대로)
        "greeting_message": G_CODE_MENTS["G01"],
        "silence_retry_message": G_CODE_MENTS["G02"],          # 1차 침묵
        "silence_close_message": G_CODE_MENTS["G03"],          # 2차 침묵 종료
        "failure_retry_1_message": G_CODE_MENTS["G11"],
        "failure_retry_2_message": G_CODE_MENTS["G12"],
        "failure_transfer_message": G_CODE_MENTS["G13"],
        "additional_inquiry_message": G_CODE_MENTS["G20"],
        "no_inquiry_close_message": G_CODE_MENTS["G21"],
        "normal_close_message": G_CODE_MENTS["G30"],
        "transfer_request_message": G_CODE_MENTS["G40"],
        "transfer_waiting_message": G_CODE_MENTS["G41"],
        "transfer_unavailable_message": G_CODE_MENTS["G42"],
        "out_of_hours_message": G_CODE_MENTS["G50"],
        "app_break_message": G_CODE_MENTS["G51"],
        "lunch_break_message": G_CODE_MENTS["G42"],            # 점심시간도 G42 패턴 사용
        "holiday_message": (
            "안녕하세요, 마음케어 고객센터입니다. 오늘은 휴무일입니다. "
            "고객센터는 평일 오전 열 시부터 오후 여섯 시까지 운영하며 점심시간은 오후 한시부터 두시까지 입니다. "
            "전화번호는 일오팔팔에 공공공공이니 평일에 편하실 때 다시 전화 주세요. 감사합니다."
        ),
    },
    "schedule": {
        "operating_hours": {
            "monday":    {"start": "10:00", "end": "18:00", "active": True},
            "tuesday":   {"start": "10:00", "end": "18:00", "active": True},
            "wednesday": {"start": "10:00", "end": "18:00", "active": True},
            "thursday":  {"start": "10:00", "end": "18:00", "active": True},
            "friday":    {"start": "10:00", "end": "18:00", "active": True},
            "saturday":  {"start": "10:00", "end": "18:00", "active": False},
            "sunday":    {"start": "10:00", "end": "18:00", "active": False},
        },
        "lunch_break": {
            "enabled": True,
            "start": "13:00",
            "end": "14:00",
        },
        "app_break": {
            "enabled": True,
            "start": "00:00",
            "end": "04:00",
        },
        "holidays": [],
    },
    "sms": {
        # 통화 종료 후 발신자에게 자동 SMS 발송
        "enabled": True,
        # 공급자 선택: "auto" (Solapi 키 있으면 Solapi, 없으면 CLAW OPS) / "solapi" / "clawops"
        "provider": "auto",
        # 010 발신자에게만 발송 (070·1588 등 사업자 번호는 자동 skip)
        "mobile_only": True,
        # SMS 본문 합성 — [header] + [요약 또는 no_content] + [footer]
        "header_text": "안녕하세요. 상담 전화 주셔서 감사합니다.",
        "footer_text": (
            "궁금하신 점 생기시면 언제든지 다시 문의 주세요.\n"
            "- 마음케어 고객센터 1588-0000"
        ),
        "no_content_text": "특별히 상담받으신 내용은 없으셨어요.",
        # 통화가 너무 짧으면(턴 수 < min_turns_for_summary) 요약 생략하고 no_content_text 사용
        "min_turns_for_summary": 2,
        # 차단(out_of_hours/holiday/lunch/app_break) 통화엔 SMS 발송 X
        "skip_blocked_calls": True,
        # 자동 전환된 통화에도 발송 (상담사 연결 안내 차원)
        "send_on_transfer": True,
    },
}

_DAY_KEYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _filesystem_base_dir() -> Path:
    """서버 데이터 루트. WSL native 우선."""
    try:
        from app.config.settings import get_settings
        base = get_settings().FILESYSTEM_BASE_DIR or ""
    except Exception:
        base = ""
    if base:
        return Path(base)
    return Path(os.path.expanduser("~/.aicc"))


def get_config_path() -> Path:
    return _filesystem_base_dir() / CONFIG_FILENAME


def _deep_merge_defaults(user: Any, defaults: Any) -> Any:
    if isinstance(defaults, dict) and isinstance(user, dict):
        out = dict(defaults)
        for k, v in user.items():
            if k in defaults:
                out[k] = _deep_merge_defaults(v, defaults[k])
            else:
                out[k] = v
        return out
    return user if user is not None else defaults


def load_config() -> dict[str, Any]:
    path = get_config_path()
    with _lock:
        if not path.exists():
            return json.loads(json.dumps(DEFAULTS))
        try:
            with open(path, encoding="utf-8") as f:
                user = json.load(f)
            return _deep_merge_defaults(user, DEFAULTS)
        except Exception as e:
            logger.error(f"[AICC_CONFIG] load failed ({path}): {e} — using DEFAULTS")
            return json.loads(json.dumps(DEFAULTS))


def save_config(data: dict[str, Any]) -> Path:
    path = get_config_path()
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(path)
    logger.info(f"[AICC_CONFIG] saved → {path}")
    return path


def ensure_seed() -> None:
    if not get_config_path().exists():
        save_config(DEFAULTS)
        logger.info("[AICC_CONFIG] seeded with defaults")


# ──────────────────── 런타임 헬퍼 ────────────────────


def _parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def _in_window(now: dtime, start: dtime, end: dtime) -> bool:
    """now가 [start, end) 안에 있는가. start <= end가 아니면 자정 넘김으로 처리."""
    if start <= end:
        return start <= now < end
    return now >= start or now < end


def check_call_status(config: dict[str, Any], now: datetime | None = None) -> tuple[str, str]:
    """현재 시각이 통화 가능한 상태인지 확인.

    Returns:
        (status, message)
        status: "ok" | "out_of_hours" | "holiday" | "lunch_break" | "app_break"
    """
    if now is None:
        now = datetime.now()

    sched = config.get("schedule", {})
    prompts = config.get("prompts", {})

    # 1. 휴무일
    today_str = now.strftime("%Y-%m-%d")
    if today_str in (sched.get("holidays") or []):
        return ("holiday", prompts.get("holiday_message", ""))

    # 2. 야간 앱 휴식 (G51)
    app_break = sched.get("app_break") or {}
    if app_break.get("enabled"):
        try:
            if _in_window(now.time(), _parse_hhmm(app_break["start"]), _parse_hhmm(app_break["end"])):
                return ("app_break", prompts.get("app_break_message", ""))
        except Exception as e:
            logger.warning(f"[AICC_CONFIG] app_break parse error: {e}")

    # 3. 운영시간 (요일)
    day_key = _DAY_KEYS[now.weekday()]
    day_cfg = (sched.get("operating_hours") or {}).get(day_key) or {}
    if not day_cfg.get("active"):
        return ("out_of_hours", prompts.get("out_of_hours_message", ""))
    try:
        if not _in_window(now.time(), _parse_hhmm(day_cfg.get("start", "10:00")), _parse_hhmm(day_cfg.get("end", "18:00"))):
            return ("out_of_hours", prompts.get("out_of_hours_message", ""))
    except Exception as e:
        logger.warning(f"[AICC_CONFIG] operating_hours parse error: {e}")

    # 4. 점심시간 (운영시간 내부의 자투리 — G42 패턴)
    lunch = sched.get("lunch_break") or {}
    if lunch.get("enabled"):
        try:
            if _in_window(now.time(), _parse_hhmm(lunch["start"]), _parse_hhmm(lunch["end"])):
                return ("lunch_break", prompts.get("lunch_break_message", ""))
        except Exception as e:
            logger.warning(f"[AICC_CONFIG] lunch_break parse error: {e}")

    return ("ok", "")


def _match_any(text: str, keywords: list[str]) -> str | None:
    if not text or not keywords:
        return None
    low = text.replace(" ", "").lower()
    for kw in keywords:
        if not kw:
            continue
        if kw.replace(" ", "").lower() in low:
            return kw
    return None


def match_transfer_keyword(text: str, config: dict[str, Any]) -> str | None:
    routing = config.get("routing", {})
    if not routing.get("enabled"):
        return None
    return _match_any(text, routing.get("transfer_keywords") or [])


def match_complaint_keyword(text: str, config: dict[str, Any]) -> str | None:
    routing = config.get("routing", {})
    if not routing.get("enabled"):
        return None
    return _match_any(text, routing.get("complaint_keywords") or [])


def is_failure_response(text: str, config: dict[str, Any]) -> bool:
    routing = config.get("routing", {})
    return _match_any(text, routing.get("failure_phrases") or []) is not None


def build_system_prompt(config: dict[str, Any], faq_text: str = "") -> str:
    """JSON 설정 + xlsx FAQ를 합쳐 Gemini Live system_prompt 생성."""
    prompts = config.get("prompts", {})
    routing = config.get("routing", {})

    parts: list[str] = []
    parts.append(prompts.get("persona") or DEFAULT_PERSONA)

    # 단계별 멘트 가이드 (AI가 어떤 상황에 어떤 멘트를 해야 하는지)
    parts.append(
        "\n[단계별 멘트 가이드 — 정확히 이 문구로 말하세요]\n"
        f"- 첫 인사: \"{prompts.get('greeting_message', '')}\"\n"
        f"- 1차 인식 실패: \"{prompts.get('failure_retry_1_message', '')}\"\n"
        f"- 2차 인식 실패: \"{prompts.get('failure_retry_2_message', '')}\"\n"
        f"- 3차 인식 실패 (상담사 연결 직전): \"{prompts.get('failure_transfer_message', '')}\"\n"
        f"- FAQ 답변 후 추가 문의 확인: \"{prompts.get('additional_inquiry_message', '')}\"\n"
        f"- 추가 문의 없을 때: \"{prompts.get('no_inquiry_close_message', '')}\"\n"
        f"- 정상 마무리: \"{prompts.get('normal_close_message', '')}\"\n"
        f"- 상담사 직접 요청 받았을 때: \"{prompts.get('transfer_request_message', '')}\"\n"
    )

    if routing.get("enabled"):
        transfer_to = routing.get("transfer_to") or ""
        kw_list = ", ".join(routing.get("transfer_keywords") or [])
        parts.append(
            "[상담사 연결 규칙]\n"
            f"- 고객이 다음 표현을 사용하면 즉시 transfer_call 도구로 {transfer_to} 번호로 전환하세요: {kw_list}.\n"
            "- 강한 불만/욕설 시 즉시 상담사 연결.\n"
            "- 고객 발화를 세 번 연속 알아듣지 못하면 자동으로 상담사 연결되며, 그 직전에 [3차 인식 실패] 멘트를 합니다.\n"
        )
    else:
        parts.append(
            "[상담사 연결 안내]\n"
            "현재 상담사 연결이 비활성화되어 있습니다. 상담사 연결 요청 시 "
            f"\"{prompts.get('transfer_unavailable_message', '')}\" 라고 안내하세요.\n"
        )

    faq = (faq_text or "").strip()
    if faq:
        parts.append(f"\n[자주 묻는 질문 FAQ — 고객 질문이 아래와 비슷하면 해당 답변을 사용하세요]\n{faq}")

    return "\n".join(parts)
