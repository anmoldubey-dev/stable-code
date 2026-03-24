# ==========================================================
# APPLICATION FLOW OVERVIEW
# ==========================================================
# 1. CALL_STATUSES  -> Valid call status enum values tuple
# 2. Call           -> ORM model for the calls table
#
# PIPELINE FLOW
# Call (caller_number, agent_id, department, status, started_at, ended_at)
#    ||
# Call.agent      ->  Agent (FK relationship)
# Call.routes     ->  List[CallRoute]  (cascade delete-orphan)
# Call.transcripts -> List[Transcript] (ordered by created_at)
# ==========================================================

"""
ivr_backend/models/call.py
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Enum as SAEnum
from sqlalchemy.orm import relationship
from ..database.connection import Base

CALL_STATUSES = ("dialing", "ringing", "connected", "on_hold", "transferred", "conference", "ended")


# --------------------------------------------------
# Call -> ORM model for the calls table
#    ||
# Fields: caller_number, agent_id, department, status, started_at, ended_at
#    ||
# Relationships: agent, routes (CallRoute), transcripts (Transcript)
# --------------------------------------------------
class Call(Base):
    __tablename__ = "calls"

    id               = Column(Integer, primary_key=True, index=True)
    caller_number    = Column(String(20), nullable=False)
    agent_id         = Column(Integer, ForeignKey("agents.id"), nullable=True)
    department       = Column(String(100), nullable=True)
    status           = Column(SAEnum(*CALL_STATUSES, name="call_status"), default="dialing")
    started_at       = Column(DateTime, default=datetime.utcnow)
    ended_at         = Column(DateTime, nullable=True)
    duration_seconds = Column(Integer, default=0)
    recording_path   = Column(String(255), nullable=True)
    created_at       = Column(DateTime, default=datetime.utcnow)

    agent       = relationship("Agent", back_populates="calls")
    routes      = relationship("CallRoute", back_populates="call", cascade="all, delete-orphan")
    transcripts = relationship("Transcript", back_populates="call", cascade="all, delete-orphan",
                               order_by="Transcript.created_at")
