"""AICC (CLAW OPS) 전화 에이전트 런타임.

원본 모놀리스 `app/main.py` 의 AICC Agent 블록(약 515~1082줄)을 독립 함수로 추출했다.
- 하드코딩된 API 키 setdefault 제거 → .env / 환경변수에서 로드
- 하드코딩 녹음 경로 → AICC_RECORDING_PATH 환경변수 또는 데이터 루트 기반
- 발신번호→Slack 매핑(CALLER_SLACK_MAP)은 운영팀이 편집하는 상수로 분리

기능:
- 070 전화 수신 → Gemini Live 응답
- 운영시간/휴무 게이트, 자동 콜백 큐잉
- 무음 감지(G02/G03 자동 발화 + hangup)
- 전환/불만 키워드 + NLU 실패 임계값 기반 자동 상담원 전환
- 통화 종료 시: 녹음 저장 → 분류/정제 → Slack 전송 → 자동 SMS

사용:
    task = await start_aicc_agent()
    # task 는 agent.serve() 코루틴의 asyncio.Task
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from app.config.settings import get_settings

logger = logging.getLogger(__name__)

# ── clawops SDK 상세 로깅 (control/media WS 진단용) ──
logging.getLogger("clawops").setLevel(logging.DEBUG)
logging.getLogger("clawops.agent").setLevel(logging.DEBUG)

# ── 발신번호 → Slack 채널/유저 매핑 (운영팀이 직접 편집) ──
# 매핑되지 않은 번호는 DEFAULT_SLACK_CHANNEL 로 전송.
CALLER_SLACK_MAP: dict[str, str] = {
    # "01012345678": "U0XXXXXXX",   # 예: 담당자 DM
}
DEFAULT_SLACK_CHANNEL = os.environ.get("AICC_DEFAULT_SLACK_CHANNEL", "")

# ── AICC 발신번호 (070) ──
AICC_FROM_NUMBER = os.environ.get("AICC_FROM_NUMBER", "07012345678")


def _recording_dir() -> str:
    """녹음 저장 디렉토리. AICC_RECORDING_PATH > 데이터 루트/aicc_recordings."""
    env_path = os.environ.get("AICC_RECORDING_PATH")
    if env_path:
        return env_path
    try:
        from app.cc_web_interface.admin_aicc import call_log_db as _db
        return str(_db.get_recording_base())
    except Exception:
        return str(Path(os.path.expanduser("~/.aicc")) / "aicc_recordings")


async def start_aicc_agent() -> "asyncio.Task | None":
    """AICC CLAW OPS 에이전트를 시작하고 serve() Task 를 반환한다.

    실패 시 None 을 반환하고 경고만 남긴다 (서버 기동은 계속).
    """
    try:
        from clawops.agent import ClawOpsAgent, GeminiRealtime, BuiltinTool  # noqa: F401
        from app.cc_web_interface.admin_aicc import (
            config as aicc_cfg,
            agent_control as aicc_ctrl,
            scenario_loader as aicc_scenario,
            call_log_db as aicc_db,
            call_classifier as aicc_classifier,
        )

        settings = get_settings()
        rec_dir = _recording_dir()
        os.makedirs(rec_dir, exist_ok=True)

        # CS팀 어드민이 편집하는 설정 파일 (없으면 기본값으로 시드)
        aicc_cfg.ensure_seed()
        # 콜 로그 DB 초기화 (테이블 + 인덱스 생성)
        aicc_db.init_db()
        # aicc_scenario_sample.xlsx에서 G-code 멘트 + 46 FAQ 로드
        faq_text = aicc_scenario.get_faq_text()
        _initial_config = aicc_cfg.load_config()
        system_prompt = aicc_cfg.build_system_prompt(_initial_config, faq_text=faq_text)

        aicc_conversations: dict = {}
        aicc_call_state: dict[str, dict] = {}  # call_id → {failure_count, transferred}

        aicc_agent = ClawOpsAgent(
            from_=AICC_FROM_NUMBER,
            session=GeminiRealtime(
                system_prompt=system_prompt,
                language="ko",
            ),
            recording=True,
            recording_path=rec_dir,
        )

        # 어드민 [저장 + 즉시 적용] 버튼이 호출하는 핫스왑 콜백
        def _apply_to_aicc_agent(new_config: dict) -> None:
            try:
                new_prompt = aicc_cfg.build_system_prompt(new_config, faq_text=faq_text)
                aicc_agent._session._system_prompt = new_prompt
                logging.info("[AICC] 🔄 system_prompt 핫스왑 완료 — 다음 통화부터 적용")
            except Exception as swap_err:
                logging.error(f"[AICC] 핫스왑 실패: {swap_err}")
        aicc_ctrl.register_apply_handler(_apply_to_aicc_agent)

        # 콜백 디스패처에 agent 등록 — /admin/aicc/callbacks/{id}/call-now 라우트에서 사용
        try:
            from app.cc_web_interface.admin_aicc import callback_dispatcher as aicc_callback_dispatcher
            aicc_callback_dispatcher.set_agent(aicc_agent)
        except Exception as cb_err:
            logging.warning(f"[AICC] callback_dispatcher 등록 실패: {cb_err}")

        # ──────── Silence Watchdog (G02·G03 자동 발화) ────────
        SILENCE_RETRY_SEC = 3.0          # phone 발화 끝 후 사용자 침묵 3초 → G02
        SILENCE_CLOSE_SEC = 5.0          # G02 phone 발화 끝 후 추가 5초 침묵 → G03
        AUDIO_END_DWELL_SEC = 0.5        # estimated 끝 + 이만큼 phone latency 버퍼
        AUDIO_NEW_TURN_GAP_SEC = 0.8     # chunk 간 gap이 이보다 크면 새 turn으로 간주
        AUDIO_BYTES_PER_SEC = 48000.0    # PCM16 24kHz raw = 24000 samples × 2 bytes
        SILENCE_HANGUP_GRACE_SEC = 12.0  # G03 발화 시간 확보 후 hangup

        aicc_silence_state: dict[str, dict] = {}

        _orig_handle_audio = aicc_agent._session._handle_audio_data

        async def _hooked_handle_audio_data(audio_data: bytes):
            await _orig_handle_audio(audio_data)
            try:
                call_obj = aicc_agent._session._call
                if call_obj is None:
                    return
                state = aicc_silence_state.get(call_obj.call_id)
                if state is None or state.get("ended"):
                    return
                now = asyncio.get_event_loop().time()
                chunk_sec = len(audio_data) / AUDIO_BYTES_PER_SEC

                prev = state.get("last_assistant_audio_ts") or 0.0
                if prev == 0.0 or (now - prev) > AUDIO_NEW_TURN_GAP_SEC:
                    state["audio_turn_start_ts"] = now
                    state["audio_turn_total_sec"] = chunk_sec
                else:
                    state["audio_turn_total_sec"] = state.get("audio_turn_total_sec", 0.0) + chunk_sec

                state["last_assistant_audio_ts"] = now
                state["audio_estimated_end_ts"] = (
                    state.get("audio_turn_start_ts", now) + state.get("audio_turn_total_sec", 0.0)
                )

                t = state.get("task")
                if not t or t.done():
                    state["task"] = asyncio.create_task(_silence_orchestrator(call_obj.call_id))
            except Exception:
                pass
        aicc_agent._session._handle_audio_data = _hooked_handle_audio_data

        async def _inject_text(text: str) -> bool:
            """Gemini Live session에 텍스트 주입 (model이 응답 발화)."""
            try:
                await aicc_agent._session._session.send_realtime_input(text=text)
                return True
            except Exception as e:
                logging.warning(f"[AICC] 텍스트 주입 실패: {e}")
                return False

        async def _silence_orchestrator(call_id: str):
            state = aicc_silence_state.get(call_id)
            if not state:
                return
            loop = asyncio.get_event_loop()
            try:
                cfg = aicc_cfg.load_config()
                prompts = cfg.get("prompts", {})
                retry_msg = (prompts.get("silence_retry_message") or "").strip()
                close_msg = (prompts.get("silence_close_message") or "").strip()

                # ── Phase 1: phone 발화 끝 + 사용자 침묵 3초 대기 ──
                while True:
                    if state.get("ended") or state.get("transferred"):
                        return
                    now = loop.time()
                    audio_end = state.get("audio_estimated_end_ts") or 0.0
                    last_user = state.get("last_user_ts") or 0.0
                    phone_end_confirmed = audio_end + AUDIO_END_DWELL_SEC

                    if now < phone_end_confirmed:
                        await asyncio.sleep(phone_end_confirmed - now)
                        continue

                    last_activity = max(phone_end_confirmed, last_user)
                    silence_age = now - last_activity if last_activity else 1e9
                    if silence_age >= SILENCE_RETRY_SEC:
                        break
                    await asyncio.sleep(SILENCE_RETRY_SEC - silence_age + 0.05)

                if state.get("ended") or state.get("transferred"):
                    return

                # ── G02 발화 트리거 ──
                if retry_msg:
                    inj = (
                        f"[시스템 알림: 고객이 약 {SILENCE_RETRY_SEC:.0f}초간 말이 없습니다.] "
                        f"다음 멘트를 정확히 그대로 한국어로 말해 주세요:\n\"{retry_msg}\""
                    )
                    if await _inject_text(inj):
                        logging.info(f"[AICC] 🔇 silence G02 트리거 — call_id={call_id}")
                        state["g02_emitted_at"] = loop.time()
                        state["phase"] = "post_g02"
                        state["audio_turn_start_ts"] = 0.0
                        state["audio_turn_total_sec"] = 0.0
                        state["last_assistant_audio_ts"] = 0.0
                        state["audio_estimated_end_ts"] = 0.0

                # ── Phase 2: G02 phone 발화 끝 + 사용자 추가 침묵 5초 ──
                while True:
                    if state.get("ended") or state.get("transferred"):
                        return
                    now = loop.time()
                    audio_end = state.get("audio_estimated_end_ts") or 0.0
                    g02_at = state.get("g02_emitted_at") or 0.0
                    phone_end_confirmed = audio_end + AUDIO_END_DWELL_SEC

                    if now < phone_end_confirmed:
                        await asyncio.sleep(phone_end_confirmed - now)
                        continue

                    ref = max(phone_end_confirmed, g02_at)
                    elapsed = now - ref
                    if elapsed >= SILENCE_CLOSE_SEC:
                        break
                    await asyncio.sleep(SILENCE_CLOSE_SEC - elapsed + 0.05)

                if state.get("ended") or state.get("transferred"):
                    return

                # ── G03 발화 + hangup ──
                if close_msg:
                    inj = (
                        "[시스템 알림: 고객이 계속 말이 없어 통화를 종료합니다.] "
                        f"다음 멘트를 정확히 그대로 한국어로 말해 주세요:\n\"{close_msg}\""
                    )
                    if await _inject_text(inj):
                        logging.info(f"[AICC] 🔇 silence G03 트리거 — call_id={call_id}")
                        state["phase"] = "post_g03"

                await asyncio.sleep(SILENCE_HANGUP_GRACE_SEC)
                if state.get("ended") or state.get("transferred"):
                    return
                call_ref = state.get("call")
                if call_ref:
                    try:
                        await call_ref.hangup()
                        logging.info(f"[AICC] 🔇 silence 자동 hangup — call_id={call_id}")
                    except Exception as e:
                        logging.warning(f"[AICC] silence hangup 실패: {e}")
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logging.error(f"[AICC] silence orchestrator 오류 (call_id={call_id}): {e}")

        def _silence_reset(call_id: str, role: str):
            state = aicc_silence_state.get(call_id)
            if not state or state.get("ended"):
                return
            now = asyncio.get_event_loop().time()
            if role == "user":
                state["last_user_ts"] = now
                t = state.get("task")
                if t and not t.done():
                    t.cancel()
                state["phase"] = "watching"
                state["g02_emitted_at"] = None
                state["task"] = asyncio.create_task(_silence_orchestrator(call_id))
            else:
                state["last_assistant_ts"] = now

        def _silence_stop(call_id: str):
            state = aicc_silence_state.pop(call_id, None)
            if not state:
                return
            state["ended"] = True
            t = state.get("task")
            if t and not t.done():
                t.cancel()

        @aicc_agent.on("call_start")
        async def on_aicc_call_start(call):
            aicc_conversations[call.call_id] = {"from": call.from_number, "to": call.to_number, "log": []}
            aicc_call_state[call.call_id] = {"failure_count": 0, "transferred": False}
            aicc_silence_state[call.call_id] = {
                "task": None,
                "last_user_ts": 0.0,
                "last_assistant_ts": 0.0,
                "last_assistant_audio_ts": 0.0,
                "audio_turn_start_ts": 0.0,
                "audio_turn_total_sec": 0.0,
                "audio_estimated_end_ts": 0.0,
                "g02_emitted_at": None,
                "phase": "idle",
                "ended": False,
                "transferred": False,
                "call": call,
            }
            logging.info(f"[AICC] 📞 전화 수신: from={call.from_number}, to={call.to_number}, id={call.call_id}")

            try:
                aicc_db.insert_call_start(
                    call_id=call.call_id,
                    from_number=call.from_number or "",
                    to_number=call.to_number or "",
                )
            except Exception as db_err:
                logging.error(f"[AICC] DB insert_call_start 실패: {db_err}")

            # 운영시간 / 휴무일 / 야간 휴식 / 점심시간 게이트
            cfg = aicc_cfg.load_config()
            status, msg = aicc_cfg.check_call_status(cfg)
            if status != "ok":
                logging.info(f"[AICC] ⏰ 통화 차단 ({status}) — 안내 멘트 발화 후 종료: {msg!r}")
                try:
                    aicc_db.mark_blocked(call.call_id, status)
                except Exception as db_err:
                    logging.error(f"[AICC] DB mark_blocked 실패: {db_err}")

                # 차단 통화 자동 콜백 큐잉
                try:
                    from app.cc_web_interface.admin_aicc import callback_db as _aicc_cb_db
                    from_n = (call.from_number or "").replace("-", "").replace(" ", "").strip()
                    if from_n and from_n.startswith("01"):
                        _aicc_cb_db.enqueue(
                            from_number=from_n,
                            source=_aicc_cb_db.SOURCE_AUTO_BLOCKED,
                            reason=f"통화 차단({status})",
                            original_call_id=call.call_id,
                            priority=_aicc_cb_db.PRIORITY_NORMAL,
                        )
                except Exception as cb_err:
                    logging.warning(f"[AICC] 자동 콜백 큐잉 실패: {cb_err}")

                spoken = await _inject_text(
                    f"다음 문장을 정확히 그대로 한 번만 말한 후 어떤 추가 말도 하지 마세요. "
                    f"고객의 응답이 와도 더 이상 말하지 마세요.\n\n"
                    f"{msg}"
                )
                if spoken:
                    wait_sec = min(20.0, max(6.0, len(msg) / 7.0))
                    logging.info(f"[AICC] 차단 멘트 발화 중 ({wait_sec:.1f}초 대기)")
                    await asyncio.sleep(wait_sec)
                else:
                    logging.warning("[AICC] 차단 멘트 발화 실패 — 짧게 대기 후 hangup")
                    await asyncio.sleep(0.5)

                _silence_stop(call.call_id)
                try:
                    await call.hangup()
                except Exception as hangup_err:
                    logging.warning(f"[AICC] hangup 실패: {hangup_err}")
                return

        @aicc_agent.on("call_end")
        async def on_aicc_call_end(call):
            logging.info(f"[AICC] 📞 통화 종료: id={call.call_id}")
            aicc_call_state.pop(call.call_id, None)
            _silence_stop(call.call_id)
            conv = aicc_conversations.pop(call.call_id, None)

            transcript_text = ""
            if conv and conv["log"]:
                transcript_text = "\n".join(
                    f"{'👤 고객' if r == 'user' else '🤖 AI'}: {t}" for r, t in conv["log"]
                )
            from_num_raw = conv.get('from', '') if conv else ''
            duration_sec = int(getattr(call, 'duration', 0) or 0)

            import glob as _glob
            mix_files = _glob.glob(f"{rec_dir}/{call.call_id}/mix.wav")
            if not mix_files:
                mix_files = _glob.glob(f"{rec_dir}/{call.call_id}/*.wav")
            recording_relative = None
            if mix_files:
                recording_relative = os.path.relpath(mix_files[0], rec_dir)

            try:
                aicc_db.finalize_call(
                    call_id=call.call_id,
                    transcript=transcript_text,
                    duration_sec=duration_sec,
                    recording_relative_path=recording_relative,
                )
            except Exception as db_err:
                logging.error(f"[AICC] DB finalize_call 실패: {db_err}")

            refined_text = transcript_text
            cls_result: dict = {}
            if transcript_text:
                try:
                    cls_result = await aicc_classifier.classify_and_save(
                        call_id=call.call_id,
                        transcript=transcript_text,
                        from_number=from_num_raw,
                    )
                    if cls_result.get("refined"):
                        refined_text = cls_result["refined"]
                        logging.info("[AICC] ✅ 분류 + 트랜스크립트 정제 완료")
                except Exception as cls_err:
                    logging.warning(f"[AICC] 분류 실패 (원본 트랜스크립트로 진행): {cls_err}")

            # Slack 전송 (정제된 트랜스크립트 사용)
            try:
                from slack_sdk import WebClient
                if settings.SLACK_BOT_TOKEN:
                    slack_client = WebClient(token=settings.SLACK_BOT_TOKEN)
                    if conv and conv["log"]:
                        conv_text = refined_text
                        if len(conv_text) > 3000:
                            conv_text = conv_text[:3000] + "\n..."
                        msg = f"📞 *070 AI 전화 상담 종료*\n\n*발신자:* {conv['from']}\n*대화 ({len(conv['log'])}턴):*\n{conv_text}\n\n---\n_CLAW OPS + Gemini Live_"
                    else:
                        from_disp = from_num_raw or '알 수 없음'
                        msg = f"📞 *070 AI 전화 상담 종료*\n\n*발신자:* {from_disp}\n*대화 내용:* 트랜스크립트 없음\n\n---\n_CLAW OPS + Gemini Live_"
                    clean_num = from_num_raw.replace("-", "").replace("+82", "0").lstrip("82")
                    slack_channel = CALLER_SLACK_MAP.get(clean_num) or DEFAULT_SLACK_CHANNEL
                    if slack_channel:
                        slack_client.chat_postMessage(channel=slack_channel, text=msg)
                        logging.info(f"[AICC] ✅ Slack 전송 완료 → {slack_channel} (from: {clean_num})")

                        for wav_path in mix_files:
                            try:
                                slack_client.files_upload_v2(
                                    channel=slack_channel,
                                    file=wav_path,
                                    title=f"통화 녹음 ({call.call_id})",
                                    initial_comment="🎙️ 통화 녹음 파일",
                                )
                                logging.info(f"[AICC] ✅ 녹음 파일 전송 완료: {wav_path}")
                            except Exception as wav_err:
                                logging.warning(f"[AICC] 녹음 파일 전송 실패: {wav_err}")
                    else:
                        logging.warning("[AICC] Slack 채널 미설정 — 전송 건너뜀 (CALLER_SLACK_MAP / AICC_DEFAULT_SLACK_CHANNEL)")
            except Exception as e:
                logging.error(f"[AICC] Slack 전송 실패: {e}")

            # 사후 안내 SMS 발송
            try:
                from app.cc_web_interface.admin_aicc import sms_sender
                db_row = aicc_db.get_call(call.call_id) or {}
                final_status = db_row.get("status") or aicc_db.STATUS_COMPLETED
                block_reason = db_row.get("block_reason")
                turns = len(conv["log"]) if (conv and conv.get("log")) else 0
                sms_summary = (cls_result or {}).get("summary", "") or ""
                asyncio.create_task(sms_sender.send_call_summary_sms(
                    call_id=call.call_id,
                    to_number=from_num_raw,
                    summary=sms_summary,
                    turns=turns,
                    call_status=final_status,
                    block_reason=block_reason,
                ))
                logging.info(f"[AICC] 📨 SMS 발송 작업 큐잉 완료 (status={final_status}, summary={'O' if sms_summary else 'X'})")
            except Exception as sms_err:
                logging.error(f"[AICC] SMS 발송 트리거 실패: {sms_err}")

        @aicc_agent.on("call_failed")
        async def on_aicc_call_failed(call, reason):
            try:
                reason_repr = repr(reason)
            except Exception:
                reason_repr = str(reason)
            call_attrs = {
                k: getattr(call, k, None)
                for k in ("call_id", "from_number", "to_number", "duration", "status")
            }
            logging.error(
                f"[AICC] ❌ 통화 실패 — reason={reason_repr} "
                f"type={type(reason).__name__} call={call_attrs}"
            )
            _silence_stop(call.call_id)
            if isinstance(reason, BaseException):
                logging.exception("[AICC] call_failed traceback", exc_info=reason)
            try:
                if not aicc_db.get_call(call.call_id):
                    aicc_db.insert_call_start(
                        call_id=call.call_id,
                        from_number=getattr(call, 'from_number', '') or '',
                        to_number=getattr(call, 'to_number', '') or '',
                    )
                aicc_db.mark_failed(call.call_id, str(reason)[:500])
            except Exception as db_err:
                logging.error(f"[AICC] DB mark_failed 실패: {db_err}")

        @aicc_agent.on("dtmf")
        async def on_aicc_dtmf(call, digit):
            logging.info(f"[AICC] 🔢 DTMF: call_id={call.call_id} digit={digit}")

        @aicc_agent.on("transcript")
        async def on_aicc_transcript(call, role, text):
            logging.info(f"[AICC] 💬 [{role}] {text}")
            if call.call_id in aicc_conversations:
                log = aicc_conversations[call.call_id]["log"]
                if log and log[-1][0] == role:
                    log[-1] = (role, log[-1][1] + text)
                else:
                    log.append((role, text))
            _silence_reset(call.call_id, role)

            # 자동 상담사 전환 로직 (어드민 설정에 따라)
            cs = aicc_call_state.get(call.call_id)
            if not cs or cs.get("transferred"):
                return
            cfg = aicc_cfg.load_config()
            routing = cfg.get("routing", {})
            if not routing.get("enabled"):
                return
            transfer_to = (routing.get("transfer_to") or "").strip()
            if not transfer_to:
                return
            threshold = int(routing.get("failure_threshold") or 3)

            try:
                if role == "user":
                    kw = aicc_cfg.match_transfer_keyword(text, cfg)
                    if kw:
                        cs["transferred"] = True
                        logging.info(f"[AICC] 📲 자동 전환 (전환 키워드='{kw}') → {transfer_to}")
                        try:
                            aicc_db.mark_transferred(call.call_id, transfer_to, aicc_db.TRANSFER_KEYWORD)
                        except Exception:
                            pass
                        _silence_stop(call.call_id)
                        await call.transfer(to=transfer_to, mode="blind")
                        return
                    kw = aicc_cfg.match_complaint_keyword(text, cfg)
                    if kw:
                        cs["transferred"] = True
                        logging.info(f"[AICC] 📲 자동 전환 (불만 키워드='{kw}') → {transfer_to}")
                        try:
                            aicc_db.mark_transferred(call.call_id, transfer_to, aicc_db.TRANSFER_COMPLAINT)
                        except Exception:
                            pass
                        _silence_stop(call.call_id)
                        await call.transfer(to=transfer_to, mode="blind")
                        return
                elif role == "model":
                    if aicc_cfg.is_failure_response(text, cfg):
                        cs["failure_count"] = int(cs.get("failure_count", 0)) + 1
                        logging.info(f"[AICC] ⚠️ NLU 실패 카운트 {cs['failure_count']}/{threshold}")
                        try:
                            aicc_db.update_failure_count(call.call_id, cs["failure_count"])
                        except Exception:
                            pass
                        if cs["failure_count"] >= threshold:
                            cs["transferred"] = True
                            logging.info(f"[AICC] 📲 자동 전환 (실패 임계값 도달) → {transfer_to}")
                            try:
                                aicc_db.mark_transferred(call.call_id, transfer_to, aicc_db.TRANSFER_FAILURE_THRESHOLD)
                            except Exception:
                                pass
                            _silence_stop(call.call_id)
                        await call.transfer(to=transfer_to, mode="blind")
                    else:
                        if cs.get("failure_count", 0) > 0:
                            logging.info("[AICC] ✓ NLU 성공 — 실패 카운터 리셋")
                            cs["failure_count"] = 0
                            try:
                                aicc_db.update_failure_count(call.call_id, 0)
                            except Exception:
                                pass
            except Exception as transfer_err:
                logging.error(f"[AICC] 자동 전환 실패: {transfer_err}")

        clawops_task = asyncio.create_task(aicc_agent.serve())
        logging.info(f"[AICC] 🚀 CLAW OPS Agent started — {AICC_FROM_NUMBER} 대기 중")
        return clawops_task
    except Exception as e:
        logging.warning(f"[AICC] CLAW OPS Agent 시작 실패 (무시): {e}")
        return None
