"""AICC 독립 실행용 FastAPI 앱.

원본 모놀리스 `server.py`에서 AICC telephony 관련 라우터만 포함하도록 재작성했다.
(auth, slack handlers, CRM 등 모놀리스 본체 의존 라우터는 모두 제외)

포함 라우터:
- /clawops      : CLAW OPS 070 전화 웹훅 (routes_clawops)
- /admin/aicc   : CS팀 관리 대시보드 (admin_aicc.routes)
- /daemon/aicc-status : 간단 상태 점검
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(title="AICC Server")

    # 정적 파일 (admin_aicc.html, playground.html)
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # ── AICC telephony 라우터 등록 ──
    from app.cc_web_interface.voice_chat.routes_clawops import router as clawops_router
    from app.cc_web_interface.admin_aicc.routes import router as admin_aicc_router
    from app.cc_web_interface.playground.routes import router as playground_router

    app.include_router(clawops_router)
    app.include_router(admin_aicc_router)
    app.include_router(playground_router)

    @app.get("/daemon/aicc-status", tags=["daemon"])
    async def daemon_aicc_status():
        """AICC 상태 점검 — DB 경로 및 최근 통화 수."""
        try:
            from app.cc_web_interface.admin_aicc import call_log_db as db
            recent = 0
            try:
                recent = db.count_recent_calls(hours=24)  # 있으면 사용
            except Exception:
                recent = 0
            return {
                "status": "ok",
                "db_path": str(db.get_db_path()),
                "recent_calls_24h": recent,
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    @app.get("/", include_in_schema=False)
    async def root():
        return {
            "service": "AICC Server",
            "admin": "/admin/aicc",
            "playground": "/playground",
            "status": "/daemon/aicc-status",
        }

    return app


# uvicorn app.cc_web_interface.server:app 로도 실행 가능
app = create_app()
