# ================================================================
# FILE EXECUTION FLOW
# ================================================================
#
# [ START ]
#    |
#    v
# +-------------------------------+
# | get_livekit_token()           |
# | * issue JWT spawn worker      |
# +-------------------------------+
#    |
#    |----> <TokenService> -> generate_token()  * sign browser JWT
#    |
#    |----> ai_worker_task()                    * spawn background worker
#    |
#    v
# +-------------------------------+
# | ai_worker_task()              |
# | * full lifecycle one call     |
# +-------------------------------+
#    |
#    |----> <rtc.Room> -> connect()             * join LiveKit room
#    |
#    |----> <TtsAudioSource> -> start()         * begin frame pump
#    |
#    |----> <LiveKitSessionManager> -> add()    * register session
#    |
#    |----> _send_greeting()                    * play TTS greeting
#    |
#    v
# +-------------------------------+
# | _send_greeting()              |
# | * synthesize opening greeting |
# +-------------------------------+
#    |
#    |----> load_greetings()                           * fetch greeting text
#    |
#    |----> <TtsAudioSource> -> push_tts_wav()         * stream audio
#    |
#    |----> _publish_data()                            * notify browser
#    |
#    v
# +-------------------------------+
# | _inbound_audio_loop()         |
# | * receive PCM frames from mic |
# +-------------------------------+
#    |
#    |----> <AudioBuf> -> push()                * accumulate audio
#    |
#    |----> <AudioBuf> -> ready()               * detect utterance end
#    |
#    |----> <TtsAudioSource> -> clear()         * barge-in drain
#    |
#    |----> _publish_data()                     * notify barge-in
#    |
#    |----> _process_turn()                     * full AI pipeline
#    |
#    v
# +-------------------------------+
# | _process_turn()               |
# | * STT LLM TTS pipeline        |
# +-------------------------------+
#    |
#    |----> stt_sync()                          * Whisper transcribe
#    |
#    |----> _collapse_repetitions()             * clean STT output
#    |
#    |----> _is_hallucination()                 * drop bad output
#    |
#    |----> _publish_data()                     * send transcript
#    |
#    |----> _gemini_sync()                      * Gemini LLM reply
#    |     OR
#    |----> _qwen_sync()                        * Qwen LLM reply
#    |
#    |----> tts()                               * HTTP TTS WAV bytes
#    |
#    |----> <TtsAudioSource> -> push_tts_wav()  * stream to browser
#    |
#    |----> _publish_data()                     * send response text
#    |
#    |----> _save_transcript()                  * persist to IVR
#    |
#    v
# +-------------------------------+
# | livekit_health()              |
# | * return session count        |
# +-------------------------------+
#
# Teardown:
#    |----> <TtsAudioSource> -> stop()                       * stop pump
#    |----> _publish_data()                                  * hangup signal
#    |----> _finalize_ivr_call()                             * save recording
#    |----> <LiveKitSessionManager> -> cleanup_session()     * remove session
#    |----> <rtc.Room> -> disconnect()                       * leave room
#
# ================================================================

import asyncio
import io
import json
import logging
import random
import uuid
import wave
from pathlib import Path
from typing import List, Optional

import numpy as np
from fastapi import APIRouter

from .audio_source import TtsAudioSource
from .livekit_session import LiveKitSession
from .session_manager import livekit_session_manager
from .token_service import LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, generate_token

from backend.core.config import LANGUAGE_CONFIG
from backend.core.state import _m
from backend.core.persona import extract_agent_name, generate_greeting
from backend.core.stt import stt_sync, _collapse_repetitions, _is_hallucination
from backend.core.tts import tts, _humanize_text
from backend.core.llm import _gemini_sync, _qwen_sync
from backend.services.greeting_loader import load_greetings

logger = logging.getLogger("callcenter.livekit.worker")

_IVR_BASE        = "http://localhost:8001"
_IVR_RECORDINGS  = Path(__file__).parent.parent.parent / "ivr_backend" / "recordings"

_WORKER_IDENTITY_PREFIX = "ai-worker-"

livekit_router = APIRouter(prefix="/livekit", tags=["livekit"])


@livekit_router.get("/token")
async def get_livekit_token(
    lang:  str = "en",
    llm:   str = "gemini",
    voice: str = "",
):
    room_id    = str(uuid.uuid4())
    session_id = str(uuid.uuid4())

    registry    = _m.get("voice_registry", {})
    lang_voices = registry.get(lang) or registry.get("en") or []
    selected    = (
        next((v for v in lang_voices if v["name"] == voice), None)
        or (lang_voices[0] if lang_voices else None)
    )
    voice_stem = selected["name"] if selected else voice
    agent_name = extract_agent_name(voice_stem)

    user_token = generate_token(
        room_name    = room_id,
        identity     = f"user-{session_id[:8]}",
        name         = "Caller",
        can_publish  = True,
        can_subscribe= True,
    )

    asyncio.ensure_future(
        ai_worker_task(
            room_id    = room_id,
            session_id = session_id,
            lang       = lang,
            llm_key    = llm,
            voice_stem = voice_stem,
            agent_name = agent_name,
        )
    )

    logger.info(
        "[Token] issued  session=%s  room=%s  lang=%s llm=%s voice=%s agent=%s",
        session_id[:8], room_id[:8], lang, llm, voice_stem, agent_name,
    )

    return {
        "token":      user_token,
        "url":        LIVEKIT_URL,
        "room":       room_id,
        "agent_name": agent_name,
        "session_id": session_id,
    }


@livekit_router.get("/health")
async def livekit_health():
    return {
        "status":        "ok",
        "active_sessions": livekit_session_manager.count,
        "livekit_url":   LIVEKIT_URL,
        "api_key":       LIVEKIT_API_KEY,
    }


def _ivr_post(path: str, body: dict) -> Optional[dict]:
    try:
        import requests as _req
        r = _req.post(f"{_IVR_BASE}{path}", json=body, timeout=3.0)
        if r.ok:
            return r.json()
    except Exception:
        pass
    return None


def _ivr_patch(path: str, body: dict) -> None:
    try:
        import requests as _req
        _req.patch(f"{_IVR_BASE}{path}", json=body, timeout=3.0)
    except Exception:
        pass


async def _register_ivr_call(session: LiveKitSession) -> None:
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _ivr_post, "/calls/start", {
        "caller_number": f"LiveKit-{session.session_id[:8]}",
        "department":    "AI Call",
    })
    if data and data.get("id"):
        session.ivr_call_id = data["id"]
        logger.info("[IVR] call registered  call_id=%s", session.ivr_call_id)


async def _save_transcript(call_id: int, speaker: str, text: str) -> None:
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _ivr_post,
        f"/calls/{call_id}/transcript",
        {"speaker": speaker, "text": text},
    )


def _build_recording(turns: List[dict]) -> Optional[bytes]:
    from backend.webrtc.utils import wav_bytes_to_pcm, resample_audio

    TARGET_SR   = 16_000
    GAP_SAMPLES = int(TARGET_SR * 0.12)
    all_pcm: List[np.ndarray] = []

    for turn in turns:
        try:
            if turn["type"] == "ai":
                pcm_f32, sr = wav_bytes_to_pcm(turn["wav"])
                if sr != TARGET_SR:
                    pcm_f32 = resample_audio(pcm_f32, sr, TARGET_SR)
                trim_frames = turn.get("trim_frames", 0)
                if trim_frames > 0:
                    trim_samples = trim_frames * 320
                    if trim_samples >= len(pcm_f32):
                        continue
                    pcm_f32 = pcm_f32[:-trim_samples]
                all_pcm.append(pcm_f32)
            elif turn["type"] == "user":
                all_pcm.append(turn["pcm"].astype(np.float32))
            all_pcm.append(np.zeros(GAP_SAMPLES, dtype=np.float32))
        except Exception:
            continue

    if not all_pcm:
        return None

    combined = np.concatenate(all_pcm)
    pcm_i16  = (np.clip(combined, -1.0, 1.0) * 32767).astype(np.int16)
    out = io.BytesIO()
    with wave.open(out, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(TARGET_SR)
        wf.writeframes(pcm_i16.tobytes())
    return out.getvalue()


async def _finalize_ivr_call(session: LiveKitSession) -> None:
    if not session.ivr_call_id:
        return
    loop = asyncio.get_event_loop()

    if session.recording_turns:
        wav_data = _build_recording(session.recording_turns)
        if wav_data:
            try:
                _IVR_RECORDINGS.mkdir(exist_ok=True)
                filename = f"{session.ivr_call_id}.wav"
                (_IVR_RECORDINGS / filename).write_bytes(wav_data)
                await loop.run_in_executor(None, _ivr_patch,
                    f"/calls/{session.ivr_call_id}/recording",
                    {"recording_path": filename},
                )
                logger.info(
                    "[IVR] recording saved  call_id=%s  file=%s",
                    session.ivr_call_id, filename,
                )
            except Exception:
                logger.debug("[IVR] recording save failed  call_id=%s", session.ivr_call_id)

    await loop.run_in_executor(None, _ivr_post,
        f"/calls/{session.ivr_call_id}/end", {},
    )
    logger.info("[IVR] call ended  call_id=%s", session.ivr_call_id)


async def _publish_data(session: LiveKitSession, msg: dict) -> None:
    if session.room is None or session.closed:
        return
    try:
        await session.room.local_participant.publish_data(
            payload = json.dumps(msg).encode("utf-8"),
            reliable = True,
        )
    except Exception:
        pass


async def _send_greeting(session: LiveKitSession) -> None:
    try:
        greetings    = load_greetings()
        raw_greeting = (
            greetings.get(session.lang)
            or generate_greeting(session.lang, session.agent_name)
        )
        greeting_text = raw_greeting.format(name=session.agent_name)
        session.history.append({"role": "assistant", "text": greeting_text})

        wav_bytes = await tts(greeting_text, session.lang, session.voice_name)
        if not wav_bytes:
            logger.warning(
                "[Greeting] TTS returned empty bytes — no greeting audio  session=%s",
                session.session_id[:8],
            )
        else:
            logger.info(
                "[Greeting] TTS done  %d bytes  session=%s",
                len(wav_bytes), session.session_id[:8],
            )
            await session.audio_source.push_tts_wav(wav_bytes)
            session.recording_turns.append({"type": "ai", "wav": wav_bytes})

        await _publish_data(session, {
            "type":       "greeting",
            "text":       greeting_text,
            "agent_name": session.agent_name,
        })

        if session.ivr_call_id:
            asyncio.ensure_future(
                _save_transcript(session.ivr_call_id, "agent", greeting_text)
            )

        logger.info(
            "[Greeting] sent  session=%s  text=%r",
            session.session_id[:8], greeting_text[:60],
        )
    except Exception:
        logger.exception("[Greeting] error  session=%s", session.session_id[:8])


async def _inbound_audio_loop(session: LiveKitSession, track) -> None:
    from livekit import rtc

    logger.info(
        "[Inbound] audio loop started  session=%s",
        session.session_id[:8],
    )

    try:
        stream = rtc.AudioStream(track, sample_rate=16_000, num_channels=1)
    except TypeError:
        stream = rtc.AudioStream(track)

    async for event in stream:
        if session.closed:
            break

        frame = getattr(event, "frame", event)
        raw   = getattr(frame, "data", None)
        if raw is None:
            continue
        pcm_int16 = np.frombuffer(bytes(raw), dtype=np.int16)
        pcm_f32   = pcm_int16.astype(np.float32) / 32768.0

        sr = getattr(frame, "sample_rate", 16_000)
        if sr != 16_000:
            from backend.webrtc.utils import resample_audio
            pcm_f32 = resample_audio(pcm_f32, sr, 16_000)

        session.buf.push(pcm_f32)

        if session.buf.ready() and not session.lock.locked():
            pcm_utt = session.buf.flush()
            if pcm_utt is not None:
                session.recording_turns.append({"type": "user", "pcm": pcm_utt})

                drained = session.audio_source.clear()
                session._trim_last_ai_turn(drained)
                await _publish_data(session, {"type": "barge_in"})

                asyncio.ensure_future(_process_turn(pcm_utt, session))

    logger.info(
        "[Inbound] audio loop ended  session=%s",
        session.session_id[:8],
    )


async def _process_turn(pcm: np.ndarray, session: LiveKitSession) -> None:
    loop = asyncio.get_event_loop()

    async with session.lock:

        session.interrupted = False

        try:
            user_text: str = await loop.run_in_executor(
                None, stt_sync, pcm, session.lang
            )
        except Exception:
            logger.exception("[Turn] STT error  session=%s", session.session_id[:8])
            session.buf.flush()
            return

        if not user_text:
            return

        user_text = _collapse_repetitions(user_text)
        if _is_hallucination(user_text):
            logger.warning("[Turn] hallucination dropped  session=%s", session.session_id[:8])
            return

        await _publish_data(session, {"type": "transcript", "text": user_text})
        session.history.append({"role": "user", "text": user_text})
        hist_snap = list(session.history)

        if session.ivr_call_id:
            asyncio.ensure_future(
                _save_transcript(session.ivr_call_id, "caller", user_text)
            )

        llm_fn = _gemini_sync if session.llm_key == "gemini" else _qwen_sync
        logger.info("[Turn] LLM start  llm=%s  session=%s", session.llm_key, session.session_id[:8])
        try:
            ai_text: str = await loop.run_in_executor(
                None, llm_fn, hist_snap, session.lang, session.voice_name
            )
        except Exception:
            logger.exception("[Turn] LLM error  session=%s", session.session_id[:8])
            session.buf.flush()
            canned = LANGUAGE_CONFIG.get(session.lang, LANGUAGE_CONFIG["en"]).get(
                "canned_error", "Sorry, I had a connection issue. Could you repeat that?"
            )
            await _publish_data(session, {"type": "response", "text": canned})
            return

        logger.info("[Turn] LLM done  text=%r  session=%s", ai_text[:60] if ai_text else "", session.session_id[:8])

        if not ai_text:
            return

        await asyncio.sleep(random.uniform(0.2, 0.5))

        if session.interrupted:
            session.interrupted = False
            barge_text = random.choice(
                LANGUAGE_CONFIG.get(session.lang, LANGUAGE_CONFIG["en"])["barge_phrases"]
            )
            logger.info("[Turn] barge-in pivot=%r  session=%s", barge_text, session.session_id[:8])
            session.history.append({"role": "assistant", "text": barge_text})
            try:
                barge_wav = await tts(barge_text, session.lang, session.voice_name)
                await session.audio_source.push_tts_wav(barge_wav)
                session.recording_turns.append({"type": "ai", "wav": barge_wav})
            except Exception:
                logger.exception("[Turn] barge-in TTS error  session=%s", session.session_id[:8])
            await _publish_data(session, {
                "type": "response", "text": barge_text, "barge_in": True
            })
            return

        session.history.append({"role": "assistant", "text": ai_text})
        tts_text = _humanize_text(ai_text, session.lang)

        logger.info("[Turn] TTS start  session=%s", session.session_id[:8])
        try:
            wav_bytes = await tts(tts_text, session.lang, session.voice_name)
            if not wav_bytes:
                logger.warning(
                    "[Turn] TTS returned empty bytes — no audio will play  session=%s",
                    session.session_id[:8],
                )
            else:
                logger.info(
                    "[Turn] TTS done  %d bytes  session=%s",
                    len(wav_bytes), session.session_id[:8],
                )
                await session.audio_source.push_tts_wav(wav_bytes)
                session.recording_turns.append({"type": "ai", "wav": wav_bytes})
        except Exception:
            logger.exception("[Turn] TTS error  session=%s", session.session_id[:8])

        await _publish_data(session, {"type": "response", "text": ai_text})

        if session.ivr_call_id:
            asyncio.ensure_future(
                _save_transcript(session.ivr_call_id, "agent", ai_text)
            )

        if _m.get("memory"):
            _u, _a, _l = user_text, ai_text, session.lang

            async def _persist() -> None:
                try:
                    await loop.run_in_executor(
                        None, _m["memory"].save_interaction, _u, _a, _l
                    )
                except Exception as exc:
                    logger.debug("[Turn] FAISS error: %s  session=%s", exc, session.session_id[:8])

            asyncio.create_task(_persist())


async def ai_worker_task(
    room_id:    str,
    session_id: str,
    lang:       str,
    llm_key:    str,
    voice_stem: str,
    agent_name: str,
) -> None:
    from livekit import rtc

    session = LiveKitSession(
        session_id = session_id,
        agent_name = agent_name,
        lang       = lang,
        llm_key    = llm_key,
        voice_name = voice_stem,
    )

    room = rtc.Room()
    session.room = room

    worker_token = generate_token(
        room_name     = room_id,
        identity      = f"{_WORKER_IDENTITY_PREFIX}{session_id[:8]}",
        name          = agent_name,
        can_publish   = True,
        can_subscribe = True,
    )

    @room.on("participant_connected")
    def _on_participant_connected(participant) -> None:
        ident: str = getattr(participant, "identity", "") or ""
        if _WORKER_IDENTITY_PREFIX in ident:
            return
        if not session.connected:
            session.connected = True
            logger.info(
                "[Worker] user joined  participant=%s  session=%s",
                ident[:16], session.session_id[:8],
            )
            asyncio.ensure_future(_send_greeting(session))

    @room.on("participant_disconnected")
    def _on_participant_disconnected(participant) -> None:
        ident: str = getattr(participant, "identity", "") or ""
        if _WORKER_IDENTITY_PREFIX in ident:
            return
        remaining = [
            p for p in room.remote_participants.values()
            if _WORKER_IDENTITY_PREFIX not in (getattr(p, "identity", "") or "")
        ]
        if not remaining:
            logger.info(
                "[Worker] user disconnected — ending session  session=%s",
                session.session_id[:8],
            )
            session.closed = True

    @room.on("track_subscribed")
    def _on_track_subscribed(track, publication, participant) -> None:
        ident: str = getattr(participant, "identity", "") or ""
        if _WORKER_IDENTITY_PREFIX in ident:
            return

        is_audio = isinstance(track, rtc.RemoteAudioTrack)
        if not is_audio:
            kind_val = getattr(track, "kind", None)
            is_audio = (kind_val == 1 or kind_val == rtc.TrackKind.KIND_AUDIO
                        if hasattr(rtc, "TrackKind") else kind_val == 1)

        if is_audio:
            logger.info(
                "[Worker] subscribing to mic track  session=%s  participant=%s",
                session.session_id[:8], ident[:16],
            )
            loop = asyncio.get_event_loop()
            asyncio.ensure_future(_inbound_audio_loop(session, track), loop=loop)
        else:
            logger.debug(
                "[Worker] ignoring non-audio track kind=%s  session=%s",
                getattr(track, "kind", "?"), session.session_id[:8],
            )

    @room.on("data_received")
    def _on_data_received(data_packet) -> None:
        try:
            raw  = getattr(data_packet, "data", data_packet)
            msg  = json.loads(bytes(raw).decode("utf-8"))
            mtype = msg.get("type", "")

            if mtype == "interrupt":
                session.mark_interrupted()
                logger.info("[Worker] barge-in  session=%s", session.session_id[:8])

            elif mtype == "hangup":
                logger.info("[Worker] hangup received  session=%s", session.session_id[:8])
                session.closed = True

        except Exception:
            pass

    @room.on("disconnected")
    def _on_disconnected(*args) -> None:
        session.closed = True

    try:
        await room.connect(LIVEKIT_URL, worker_token)
        logger.info(
            "[Worker] connected to room  session=%s  room=%s",
            session.session_id[:8], room_id[:8],
        )
    except Exception:
        logger.exception(
            "[Worker] failed to connect to LiveKit  session=%s", session.session_id[:8]
        )
        return

    try:
        audio_source       = TtsAudioSource()
        session.audio_source = audio_source
        audio_source.start()

        ai_track = rtc.LocalAudioTrack.create_audio_track(
            "ai-voice", audio_source.source
        )
        publish_options = rtc.TrackPublishOptions(
            source = rtc.TrackSource.SOURCE_MICROPHONE,
        )
        await room.local_participant.publish_track(ai_track, publish_options)
        logger.info("[Worker] audio track published  session=%s", session.session_id[:8])
    except Exception:
        logger.exception("[Worker] failed to publish audio track  session=%s", session.session_id[:8])
        await room.disconnect()
        return

    await livekit_session_manager.add(session)
    asyncio.ensure_future(_register_ivr_call(session))

    for participant in room.remote_participants.values():
        ident = getattr(participant, "identity", "") or ""
        if _WORKER_IDENTITY_PREFIX not in ident and not session.connected:
            session.connected = True
            logger.info(
                "[Worker] user already in room  session=%s",
                session.session_id[:8],
            )
            asyncio.ensure_future(_send_greeting(session))
            break

    logger.info("[Worker] waiting for call events  session=%s", session.session_id[:8])
    while not session.closed:
        await asyncio.sleep(0.5)

    logger.info("[Worker] session ending  session=%s", session.session_id[:8])

    if session.audio_source:
        session.audio_source.stop()

    try:
        await _publish_data(session, {"type": "hangup"})
    except Exception:
        pass

    asyncio.ensure_future(_finalize_ivr_call(session))

    await livekit_session_manager.cleanup_session(session.session_id)

    try:
        await room.disconnect()
    except Exception:
        pass

    logger.info("[Worker] task complete  session=%s", session.session_id[:8])
