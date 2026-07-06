"""AICC 전용 설정. 모놀리스 settings에서 telephony가 실제로 쓰는 필드만 떼어왔다.
값은 환경변수 / repo 루트 .env 에서 로드."""
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
