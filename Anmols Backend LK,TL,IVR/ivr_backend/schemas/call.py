# ==========================================================
# APPLICATION FLOW OVERVIEW
# ==========================================================
# 1. CallStartRequest      -> POST /calls/start request body schema
# 2. TransferRequest       -> POST /calls/{id}/transfer request body schema
# 3. CallRouteResponse     -> Serialized route in call responses
# 4. CallResponse          -> Full call data response model with agent_name
# 5. from_orm_with_agent() -> Build CallResponse dict from ORM Call object
#
# PIPELINE FLOW
# HTTP request body
#    ||
# CallStartRequest / TransferRequest  ->  Pydantic validation
#    ||
# Route handler  ->  call_service CRUD
#    ||
# CallResponse.from_orm_with_agent(call)  ->  JSON response
# ==========================================================

from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime


class CallStartRequest(BaseModel):
    caller_number: str
    department: Optional[str] = "General"
    agent_id: Optional[int] = None


class TransferRequest(BaseModel):
    to_department: Optional[str] = None
    to_agent_id: Optional[int] = None
    action_type: str = "transfer"


class CallRouteResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    from_department: Optional[str]
    to_department: Optional[str]
    action_type: str
    routed_at: datetime


class CallResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    caller_number: str
    agent_id: Optional[int]
    agent_name: Optional[str] = None
    department: Optional[str]
    status: str
    started_at: datetime
    ended_at: Optional[datetime]
    duration_seconds: int
    recording_path: Optional[str]
    created_at: datetime
    routes: List[CallRouteResponse] = []

    @classmethod
    def from_orm_with_agent(cls, call):
        data = {
            "id": call.id,
            "caller_number": call.caller_number,
            "agent_id": call.agent_id,
            "agent_name": call.agent.name if call.agent else None,
            "department": call.department,
            "status": call.status,
            "started_at": call.started_at,
            "ended_at": call.ended_at,
            "duration_seconds": call.duration_seconds,
            "recording_path": call.recording_path,
            "created_at": call.created_at,
            "routes": call.routes,
        }
        return cls(**data)
