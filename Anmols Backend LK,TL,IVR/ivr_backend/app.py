# ==========================================================
# APPLICATION FLOW OVERVIEW
# ==========================================================
# 1. lifespan()  -> Migrate DB schema, create tables, seed demo data
# 2. health()    -> GET /health liveness probe
#
# PIPELINE FLOW
# lifespan (startup)
#    ||
# ALTER calls.status ENUM  ->  Base.metadata.create_all
#    ||
# RECORDINGS_DIR.mkdir  ->  seed_demo_data(db)
#    ||
# app with /calls + /tts routers ready on port 8001
# ==========================================================

"""
ivr_backend/app.py
──────────────────────────────────────────────────────────────────────────────
SR Comsoft IVR Backend — dedicated FastAPI service for call management.

Run from project root:
    python -m uvicorn ivr_backend.app:app --reload --port 8001

Endpoints:
    POST   /calls/start
    POST   /calls/{id}/end
    GET    /calls/active
    GET    /calls/history
    POST   /calls/{id}/transfer
    POST   /calls/{id}/transcript
    GET    /calls/{id}/transcripts
    GET    /calls/{id}/recording
    POST   /tts/generate
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text, inspect as sa_inspect

from .database.connection import engine
from .database.connection import Base, SessionLocal
from .routes import calls, tts
from .services.call_service import seed_demo_data

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)
logger = logging.getLogger("ivr_backend")

RECORDINGS_DIR = Path(__file__).parent / "recordings"


# --------------------------------------------------
# lifespan -> Boot IVR backend: migrate DB, create tables, seed demo data
#    ||
# ALTER calls.status ENUM -> Base.metadata.create_all
#    ||
# RECORDINGS_DIR.mkdir -> seed_demo_data -> yield
# --------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("IVR Backend starting up…")

    # If the calls table already exists (user-created), ensure its status ENUM
    # includes all values our backend needs (on_hold, conference).
    existing_tables = sa_inspect(engine).get_table_names()
    if "calls" in existing_tables:
        with engine.connect() as conn:
            try:
                conn.execute(text(
                    "ALTER TABLE calls MODIFY COLUMN status "
                    "ENUM('dialing','ringing','connected','on_hold','conference',"
                    "'transferred','ended') DEFAULT 'dialing'"
                ))
                conn.commit()
                logger.info("calls.status enum updated.")
            except Exception as exc:
                logger.warning("calls enum alter skipped: %s", exc)

    # Create any missing tables (no-op for tables that already exist)
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables ready.")
    # Ensure recordings directory exists
    RECORDINGS_DIR.mkdir(exist_ok=True)
    # Seed demo data (idempotent)
    db = SessionLocal()
    try:
        seed_demo_data(db)
        logger.info("Demo data seeded.")
    finally:
        db.close()
    logger.info("IVR Backend ready on port 8001.")
    yield
    logger.info("IVR Backend shutting down.")


app = FastAPI(
    title="SR Comsoft IVR Backend",
    description="Call management, routing, transcripts, and TTS proxy.",
    version="1.0.0",
    lifespan=lifespan,
)

# ── CORS — allow IVR-frontend dev server and main backend ─────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(calls.router, prefix="/calls")
app.include_router(tts.router,   prefix="/tts")


# --------------------------------------------------
# health -> GET /health — liveness probe, returns {status: ok}
# --------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok", "service": "ivr_backend"}
