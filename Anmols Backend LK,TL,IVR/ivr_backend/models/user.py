# ==========================================================
# APPLICATION FLOW OVERVIEW
# ==========================================================
# 1. User   -> ORM model for the users table (account credentials + profile)
# 2. Agent  -> ORM model for the agents table (AI agent identity + stats)
#
# PIPELINE FLOW
# User (id, name, email, is_active)
#    ||
# User.agent  ->  Agent (one-to-one, back_populates)
#    ||
# Agent (id, user_id FK, name, persona, voice_model, is_active, last_sentiment)
#    ||
# Agent.calls  ->  List[Call]  (one-to-many relationship)
# ==========================================================

"""
ivr_backend/models/user.py
ORM models mapped to the sr_comsoft_db users + agents tables.
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from ..database.connection import Base


# --------------------------------------------------
# User -> ORM model for the users table (account credentials + profile)
#    ||
# Relationship: agent (one-to-one with Agent)
# --------------------------------------------------
class User(Base):
    __tablename__ = "users"

    id            = Column(Integer, primary_key=True, index=True)
    name          = Column(String(100), nullable=False)
    email         = Column(String(100), unique=True, nullable=True)
    password_hash = Column(String(255), nullable=True)
    phone_number  = Column(String(20), nullable=True)
    country_code  = Column(String(10), nullable=True)
    is_active     = Column(Boolean, default=True)
    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, nullable=True)

    agent = relationship("Agent", back_populates="user", uselist=False)


# --------------------------------------------------
# Agent -> ORM model for the agents table (AI agent identity + stats)
#    ||
# Fields: user_id (FK), name, persona, voice_model, is_active, last_sentiment
#    ||
# Relationships: user (User), calls (Call list)
# --------------------------------------------------
class Agent(Base):
    __tablename__ = "agents"

    id                     = Column(Integer, primary_key=True, index=True)
    user_id                = Column(Integer, ForeignKey("users.id"), nullable=False)
    name                   = Column(String(100), nullable=False)
    persona                = Column(String(255), nullable=True)
    voice_model            = Column(String(50), nullable=True)
    phone_number           = Column(String(20), nullable=True)
    total_calls            = Column(Integer, default=0)
    total_duration_seconds = Column(Integer, default=0)
    last_call_at           = Column(DateTime, nullable=True)
    last_sentiment         = Column(String(50), nullable=True)
    is_active              = Column(Boolean, default=True)
    created_at             = Column(DateTime, default=datetime.utcnow)

    user  = relationship("User", back_populates="agent")
    calls = relationship("Call", back_populates="agent")
