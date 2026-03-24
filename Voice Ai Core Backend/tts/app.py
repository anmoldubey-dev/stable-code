# ================================================================
# backend/tts/app.py  —  Unified TTS Launcher
# ================================================================
#
# Starts both TTS microservices in parallel from a single command:
#
#   Global TTS  (human_tts)   → http://localhost:8003
#   Indic  TTS  (human_tts_2) → http://localhost:8004
#
# Usage (ALWAYS use the explicit venv Python — never bare "python"):
#   cd backend/tts
#   .venv\Scripts\python.exe app.py
#
# Using bare "python" may resolve to the project root venv or system
# Python, both of which lack parler-tts / transformers and will cause
# "No module named 'transformers'" in the model-load step.
#
# Each service runs in its own process so they load independently
# and a crash in one does not take down the other.
# Ctrl+C stops both cleanly.
#
# ================================================================

import multiprocessing
import os
import signal
import sys

import uvicorn

_VENV_PYTHON = os.path.join(os.path.dirname(__file__), ".venv", "Scripts", "python.exe")


def _assert_correct_venv() -> None:
    """Abort early with a clear message if launched with the wrong Python."""
    exe = sys.executable.replace("\\", "/").lower()
    expected = _VENV_PYTHON.replace("\\", "/").lower()
    if exe != expected:
        print("=" * 60)
        print("  [ERROR] Wrong Python interpreter detected!")
        print(f"  Running : {sys.executable}")
        print(f"  Expected: {_VENV_PYTHON}")
        print()
        print("  Launch with the explicit venv Python:")
        print(f"    {_VENV_PYTHON} app.py")
        print("=" * 60)
        sys.exit(1)


def _run_global_tts() -> None:
    """Run Global TTS service (human_tts) on port 8003."""
    # Set working directory to human_tts so relative paths inside app.py resolve correctly
    os.chdir(os.path.join(os.path.dirname(__file__), "human_tts"))
    sys.path.insert(0, os.getcwd())
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8003,
        reload=False,           # reload not supported in subprocess mode
        log_level="info",
    )


def _run_indic_tts() -> None:
    """Run Indic TTS service (human_tts_2) on port 8004."""
    os.chdir(os.path.join(os.path.dirname(__file__), "human_tts_2"))
    sys.path.insert(0, os.getcwd())
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8004,
        reload=False,
        log_level="info",
    )


def main() -> None:
    _assert_correct_venv()
    print("=" * 60)
    print("  TTS Launcher — starting both services")
    print("  Global TTS  →  http://localhost:8003  (human_tts)")
    print("  Indic  TTS  →  http://localhost:8004  (human_tts_2)")
    print("  Ctrl+C to stop both")
    print("=" * 60)

    global_proc = multiprocessing.Process(
        target=_run_global_tts,
        name="global-tts",
        daemon=True,
    )
    indic_proc = multiprocessing.Process(
        target=_run_indic_tts,
        name="indic-tts",
        daemon=True,
    )

    global_proc.start()
    indic_proc.start()

    print(f"[launcher] Global TTS  PID={global_proc.pid}")
    print(f"[launcher] Indic  TTS  PID={indic_proc.pid}")

    def _shutdown(sig, frame):
        print("\n[launcher] Shutting down both TTS services…")
        global_proc.terminate()
        indic_proc.terminate()
        global_proc.join(timeout=5)
        indic_proc.join(timeout=5)
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Wait for both — if either dies unexpectedly, report and exit
    global_proc.join()
    indic_proc.join()


if __name__ == "__main__":
    multiprocessing.freeze_support()   # needed for Windows PyInstaller builds
    main()
