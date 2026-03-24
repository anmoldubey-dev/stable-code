"""
backend/webrtc
──────────────────────────────────────────────────────────────────────────────
DEPRECATED — aiortc-based WebRTC layer.

This module has been superseded by backend/livekit/ which uses a self-hosted
LiveKit server instead of manual aiortc peer connections.

All source files (signaling_server.py, webrtc_gateway.py, webrtc_session.py,
audio_tracks.py, session_manager.py, ice_config.py) are kept as reference but
are no longer loaded by the application.

Active router: backend.livekit.livekit_router  (mounted at /livekit/*)

The utility functions in backend/webrtc/utils.py (wav_bytes_to_pcm,
resample_audio, float32_to_int16) are still imported by backend/livekit/
and remain active.
"""

# webrtc_router is intentionally NOT exported here.
# backend/app.py now imports from backend.livekit instead.
# Keeping this file as a stub so any lingering `from backend.webrtc import ...`
# fails with a clear ImportError rather than a silent wrong import.

