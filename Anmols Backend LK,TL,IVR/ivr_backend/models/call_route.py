# ==========================================================
# APPLICATION FLOW OVERVIEW
# ==========================================================
# 1. CallRoute  -> ORM model for the call_routes table
#
# PIPELINE FLOW
# Call transfer / conference / ivr_redirect event
#    ||
# CallRoute (call_id, from/to agent_id, from/to department, action_type)
#    ||
# CallRoute.call  ->  Call back_populates ("routes")
# ==========================================================

"""
ivr_backend/models/call_route.py
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Enum as SAEnum
from sqlalchemy.orm import relationship
from ..database.connection import Base


# --------------------------------------------------
# CallRoute -> ORM model for the call_routes table
#    ||
# Fields: call_id, from/to agent_id, from/to department, action_type, routed_at
#    ||
# Relationship: call (back_populates Call.routes)
# --------------------------------------------------
class CallRoute(Base):
    __tablename__ = "call_routes"

    id                = Column(Integer, primary_key=True, index=True)
    call_id           = Column(Integer, ForeignKey("calls.id"), nullable=False)
    from_agent_id     = Column(Integer, ForeignKey("agents.id"), nullable=True)
    to_agent_id       = Column(Integer, ForeignKey("agents.id"), nullable=True)
    from_department   = Column(String(100), nullable=True)
    to_department     = Column(String(100), nullable=True)
    action_type       = Column(SAEnum("transfer", "ivr_redirect", "conference", name="route_action"), nullable=False)
    routed_at         = Column(DateTime, default=datetime.utcnow)

    call = relationship("Call", back_populates="routes")
