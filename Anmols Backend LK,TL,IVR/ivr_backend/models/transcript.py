# ==========================================================
# APPLICATION FLOW OVERVIEW
# ==========================================================
# 1. Transcript  -> ORM model for the transcripts table
#
# PIPELINE FLOW
# Speech turn (agent / caller / system)
#    ||
# Transcript (call_id, speaker, text, created_at)
#    ||
# Transcript.call  ->  Call back_populates ("transcripts", ordered by created_at)
# ==========================================================

"""
ivr_backend/models/transcript.py
"""
from datetime import datetime
from sqlalchemy import Column, Integer, Text, DateTime, ForeignKey, Enum as SAEnum
from sqlalchemy.orm import relationship
from ..database.connection import Base


# --------------------------------------------------
# Transcript -> ORM model for the transcripts table
#    ||
# Fields: call_id, speaker (agent/caller/system), text, created_at
#    ||
# Relationship: call (back_populates Call.transcripts)
# --------------------------------------------------
class Transcript(Base):
    __tablename__ = "transcripts"

    id         = Column(Integer, primary_key=True, index=True)
    call_id    = Column(Integer, ForeignKey("calls.id"), nullable=False)
    speaker    = Column(SAEnum("agent", "caller", "system", name="speaker_role"), nullable=False)
    text       = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    call = relationship("Call", back_populates="transcripts")
