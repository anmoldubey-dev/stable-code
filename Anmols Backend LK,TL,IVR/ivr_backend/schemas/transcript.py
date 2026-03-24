# ==========================================================
# APPLICATION FLOW OVERVIEW
# ==========================================================
# 1. TranscriptCreate    -> POST /calls/{id}/transcript request body schema
# 2. TranscriptResponse  -> Serialized transcript in list responses
#
# PIPELINE FLOW
# HTTP request  ->  TranscriptCreate (speaker + text)
#    ||
# add_transcript(db, call_id, speaker, text)
#    ||
# TranscriptResponse  ->  JSON response
# ==========================================================

from pydantic import BaseModel
from datetime import datetime


class TranscriptCreate(BaseModel):
    speaker: str   # "agent" | "caller" | "system"
    text: str


class TranscriptResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    call_id: int
    speaker: str
    text: str
    created_at: datetime
