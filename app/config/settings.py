"""AICC 독립 실행용 최소 설정.

원본 모놀리스 `app/config/settings.py`에서 AICC telephony 코드가 실제로 사용하는
필드만 추출했다. 값은 환경변수 또는 repo 루트의 `.env` 파일에서 읽는다.

원본에서 사용 확인된 필드:
- FILESYSTEM_BASE_DIR  : DB·녹음 저장 루트 (call_log_db, config)
- SLACK_BOT_TOKEN      : 통화 요약/녹음 Slack 전송
- CLAWOPS_API_KEY      : ClawOps 인증 (routes_clawops, sms_sender, 런타임)
- CLAWOPS_ACCOUNT_ID   : ClawOps 계정
- CLAWOPS_WEBHOOK_SECRET : ClawOps 웹훅 서명 검증
- GOOGLE_API_KEY       : Gemini Live (런타임/분류기에서 os.environ로도 읽음)
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# repo 루트의 .env 를 os.environ 으로도 로드한다.
# (sms_sender, 런타임 등 일부 모듈이 os.environ 에서 직접 키를 읽기 때문)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_PATH = _REPO_ROOT / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH, override=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_PATH),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── 데이터 저장 루트 (비우면 ~/.aicc 사용) ──
    FILESYSTEM_BASE_DIR: str = ""

    # ── Slack (통화 요약/녹음 전송) ──
    SLACK_BOT_TOKEN: str = ""
    AICC_DEFAULT_SLACK_CHANNEL: str = ""

    # ── CLAW OPS (telephony) ──
    CLAWOPS_API_KEY: str = ""
    CLAWOPS_ACCOUNT_ID: str = ""
    CLAWOPS_WEBHOOK_SECRET: str = ""

    # ── Gemini Live ──
    GOOGLE_API_KEY: str = ""


@lru_cache
def get_settings() -> "Settings":
    return Settings()
