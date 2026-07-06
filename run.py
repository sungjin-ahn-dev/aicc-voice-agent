"""AICC 서버 독립 실행 진입점.

웹 서버(FastAPI/uvicorn) + CLAW OPS 전화 에이전트를 한 이벤트 루프에서 함께 띄운다.

실행:
    python run.py
    # 또는 포트/호스트 지정
    AICC_HOST=0.0.0.0 AICC_PORT=8000 python run.py
"""
from __future__ import annotations

import asyncio
import logging
import os

import uvicorn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("aicc.run")


async def main() -> None:
    from app.config.settings import get_settings
    from app.cc_web_interface.server import create_app
    from app.aicc_runtime import start_aicc_agent

    settings = get_settings()

    # 필수 키 점검 (없으면 경고만 — 부분 기능은 동작)
    if not settings.CLAWOPS_API_KEY:
        logger.warning("CLAWOPS_API_KEY 미설정 — 전화 에이전트가 시작되지 않습니다 (.env 확인)")
    if not settings.GOOGLE_API_KEY and not os.environ.get("GEMINI_API_KEY"):
        logger.warning("GOOGLE_API_KEY/GEMINI_API_KEY 미설정 — Gemini Live 응답 불가")
    if not settings.SLACK_BOT_TOKEN:
        logger.warning("SLACK_BOT_TOKEN 미설정 — 통화 요약 Slack 전송 비활성")

    app = create_app()

    host = os.environ.get("AICC_HOST", "0.0.0.0")
    port = int(os.environ.get("AICC_PORT", "8000"))

    # SSL (선택) — AICC_SSL_CERT / AICC_SSL_KEY 지정 시 HTTPS
    ssl_kwargs = {}
    cert = os.environ.get("AICC_SSL_CERT")
    key = os.environ.get("AICC_SSL_KEY")
    if cert and key and os.path.exists(cert) and os.path.exists(key):
        ssl_kwargs = {"ssl_certfile": cert, "ssl_keyfile": key}
        logger.info("[WEB] HTTPS 모드 (SSL 인증서 사용)")

    config = uvicorn.Config(app, host=host, port=port, log_level="info", **ssl_kwargs)
    server = uvicorn.Server(config)

    # 전화 에이전트 시작 (실패해도 웹 서버는 계속)
    agent_task = await start_aicc_agent()

    proto = "https" if ssl_kwargs else "http"
    logger.info(f"[WEB] AICC Server: {proto}://{host}:{port}")
    logger.info(f"[WEB] Admin Dashboard: {proto}://localhost:{port}/admin/aicc")
    logger.info(f"[WEB] Phone Playground: {proto}://localhost:{port}/playground")

    try:
        await server.serve()
    finally:
        if agent_task and not agent_task.done():
            agent_task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("종료합니다.")
