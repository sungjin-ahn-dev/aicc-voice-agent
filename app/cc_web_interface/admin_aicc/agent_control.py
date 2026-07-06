"""AICC 에이전트 핫스왑 컨트롤.

main.py가 에이전트를 만들면서 register_apply_handler()로 콜백을 등록해두면,
관리자 화면에서 [지금 적용] 버튼을 눌렀을 때 apply_settings()가 호출되고
콜백이 system_prompt를 새 값으로 갈아끼운다.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

ApplyHandler = Callable[[dict], None]

_handler: Optional[ApplyHandler] = None


def register_apply_handler(fn: ApplyHandler) -> None:
    """main.py에서 에이전트 생성 후 호출. fn(config_dict)을 받아 system_prompt 등 핫스왑."""
    global _handler
    _handler = fn
    logger.info("[AICC_CONTROL] apply handler registered")


def apply_settings(config: dict) -> bool:
    """저장된 설정을 라이브 에이전트에 적용. 핸들러가 없으면 False."""
    if _handler is None:
        logger.warning("[AICC_CONTROL] no apply handler — agent not running yet")
        return False
    try:
        _handler(config)
        logger.info("[AICC_CONTROL] settings applied to live agent")
        return True
    except Exception as e:
        logger.error(f"[AICC_CONTROL] apply failed: {e}")
        return False
