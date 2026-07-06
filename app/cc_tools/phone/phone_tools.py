"""
Phone Tools for Claude Code SDK — CLAW OPS 전화 발신
모놀리스가 Slack에서 명령받아 070번호로 전화를 걸 수 있는 도구
"""

import asyncio
import logging
import os
from typing import Any, Dict

from claude_agent_sdk import create_sdk_mcp_server, tool

logger = logging.getLogger(__name__)

# ~/.eco/config.env에서 환경변수 로드 (Electron 없이 실행 시)
_eco_config = os.path.join(os.path.expanduser("~"), ".eco", "config.env")
if os.path.exists(_eco_config):
    from dotenv import load_dotenv
    load_dotenv(_eco_config, override=False)

if not os.environ.get("CLAWOPS_API_KEY"):
    logger.warning("[PHONE] CLAWOPS_API_KEY 환경변수가 설정되지 않았습니다")


def _load_faq() -> str:
    """FAQ 로드 (캐싱)"""
    global _faq_cache
    if "_faq_cache" in globals() and _faq_cache:
        return _faq_cache
    from pathlib import Path
    faq_items = []
    faq_path = Path(__file__).parent.parent.parent / "aicc_inbound_faq.docx"
    if faq_path.exists():
        try:
            from docx import Document as DocxDocument
            doc = DocxDocument(str(faq_path))
            for table in doc.tables:
                for row in table.rows:
                    cells = [cell.text.strip() for cell in row.cells]
                    if len(cells) >= 3 and cells[0].startswith("Q"):
                        faq_items.append(f"Q: {cells[1]}\nA: {cells[2]}")
        except Exception:
            pass
    _faq_cache = "\n\n".join(faq_items) if faq_items else ""
    return _faq_cache


@tool(
    "make_phone_call",
    "070번호로 전화를 겁니다. AI가 지정된 메시지를 전달하고 대화합니다. 전화가 종료되면 대화 내용을 반환합니다.",
    {
        "type": "object",
        "properties": {
            "phone_number": {
                "type": "string",
                "description": "전화번호 (예: 01012345678, 010-1234-5678)"
            },
            "purpose": {
                "type": "string",
                "description": "전화 목적 (예: '마음케어 재처방 안내', '고객 만족도 조사')"
            },
            "message": {
                "type": "string",
                "description": "AI가 전달할 핵심 메시지 (예: '처방 기간이 7일 후 만료됩니다. 재처방을 위해 병원 방문을 부탁드립니다.')"
            },
            "patient_name": {
                "type": "string",
                "description": "환자/고객 이름 (선택)"
            },
            "additional_context": {
                "type": "string",
                "description": "추가 컨텍스트 (병원명, 담당의, 제품명 등)"
            }
        },
        "required": ["phone_number", "purpose", "message"]
    }
)
async def make_phone_call(args: Dict[str, Any]) -> Dict[str, Any]:
    """CLAW OPS + Gemini Live로 아웃바운드 전화 발신"""
    phone = args["phone_number"].replace("-", "").replace(" ", "")
    purpose = args["purpose"]
    message = args["message"]
    patient_name = args.get("patient_name", "고객")
    additional_context = args.get("additional_context", "")

    # 번호 검증
    if not phone.startswith("01") or len(phone) < 10:
        return {"content": [{"type": "text", "text": f"유효하지 않은 전화번호입니다: {phone}"}]}

    logger.info(f"[PHONE_TOOL] 발신 시작: {phone}, 목적: {purpose}")

    try:
        from clawops.agent import ClawOpsAgent, GeminiRealtime

        faq_text = _load_faq()

        system_prompt = (
            f"당신은 마음케어 서비스 담당자입니다. 한국어로 짧고 명확하게 말하세요.\n"
            f"전화 목적: {purpose}\n"
            f"전달할 핵심 메시지: {message}\n"
            f"상대방 이름: {patient_name}\n"
        )
        if additional_context:
            system_prompt += f"추가 정보: {additional_context}\n"
        system_prompt += (
            "\n통화 시작 시 자신을 소개하고, 핵심 메시지를 전달한 후 질문이 있는지 물어보세요.\n"
            "마크다운 사용 금지. 2-3문장으로 짧게. 고객센터 1588-0000.\n"
        )
        if faq_text:
            system_prompt += f"\nFAQ:\n{faq_text}"

        conversation_log = []

        agent = ClawOpsAgent(
            from_="07012345678",
            session=GeminiRealtime(
                system_prompt=system_prompt,
                language="ko",
            ),
            recording=False,
        )

        @agent.on("transcript")
        async def on_transcript(call, role, text):
            if conversation_log and conversation_log[-1][0] == role:
                conversation_log[-1] = (role, conversation_log[-1][1] + text)
            else:
                conversation_log.append((role, text))

        await agent.connect()
        session = await agent.call(phone, timeout=60)
        logger.info(f"[PHONE_TOOL] 통화 연결: {session.call_id}")
        await session.wait()
        await agent.disconnect()

        # 결과 포맷팅
        if conversation_log:
            lines = [f"{'고객' if r == 'user' else 'AI'}: {t}" for r, t in conversation_log]
            conv_text = "\n".join(lines)
        else:
            conv_text = "(대화 내용 없음 — 부재중이거나 짧은 통화)"

        result = f"📞 전화 발신 완료\n대상: {patient_name} ({phone})\n목적: {purpose}\n\n대화 내용:\n{conv_text}"
        logger.info(f"[PHONE_TOOL] 발신 완료: {phone}, {len(conversation_log)}턴")

        return {"content": [{"type": "text", "text": result}]}

    except Exception as e:
        logger.error(f"[PHONE_TOOL] 발신 실패: {e}", exc_info=True)
        return {"content": [{"type": "text", "text": f"전화 발신 실패: {str(e)}"}]}


phone_tools = [make_phone_call]


def create_phone_mcp_server():
    """Claude Code SDK용 Phone MCP 서버"""
    return create_sdk_mcp_server(
        name="phone",
        version="1.0.0",
        tools=phone_tools
    )
