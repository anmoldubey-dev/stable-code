# ==========================================================
# APPLICATION FLOW OVERVIEW
# ==========================================================
# 1. seed_demo_agents()    -> Insert demo agents if table empty (idempotent)
# 2. create_call()         -> INSERT calls row, return Call ORM
# 3. get_call()            -> SELECT call by id with relationships
# 4. get_active_calls()    -> SELECT non-ended calls list
# 5. get_call_history()    -> SELECT ended/transferred paginated
# 6. end_call()            -> UPDATE status=ended + ended_at + duration
# 7. transfer_call()       -> INSERT CallRoute + UPDATE call status
# 8. add_transcript()      -> INSERT Transcript row
# 9. get_transcripts()     -> SELECT ordered transcripts for call
# 10. delete_call()        -> DELETE call (cascade routes + transcripts)
# 11. update_recording()   -> UPDATE recording_path on Call
#
# PIPELINE FLOW
# Route handler  ->  get_db (SQLAlchemy session)
#    ||
# call_service function  ->  ORM query / mutation
#    ||
# db.commit()  ->  db.refresh()
#    ||
# Return ORM object to route
# ==========================================================
"""
ivr_backend/services/call_service.py
Business logic for call lifecycle + demo data seeding.
"""
from datetime import datetime, timedelta
from typing import List, Optional
from sqlalchemy.orm import Session

from ..models.call import Call
from ..models.call_route import CallRoute
from ..models.transcript import Transcript
from ..models.user import Agent


# ── Call CRUD ─────────────────────────────────────────────────────────────────

def create_call(
    db: Session,
    caller_number: str,
    department: str = "General",
    agent_id: Optional[int] = None,
) -> Call:
    call = Call(
        caller_number=caller_number,
        agent_id=agent_id,
        department=department,
        status="connected",
        started_at=datetime.utcnow(),
    )
    db.add(call)
    db.commit()
    db.refresh(call)
    return call


def end_call(db: Session, call_id: int) -> Optional[Call]:
    call = db.query(Call).filter(Call.id == call_id).first()
    if not call:
        return None
    now = datetime.utcnow()
    call.ended_at = now
    call.status   = "ended"
    if call.started_at:
        call.duration_seconds = int((now - call.started_at).total_seconds())
    db.commit()
    db.refresh(call)
    return call


def get_active_calls(db: Session) -> List[Call]:
    active_statuses = ("dialing", "ringing", "connected", "on_hold", "conference")
    return (
        db.query(Call)
        .filter(Call.status.in_(active_statuses))
        .order_by(Call.started_at.desc())
        .all()
    )


def get_call_history(db: Session, page: int = 1, limit: int = 20) -> List[Call]:
    offset = (page - 1) * limit
    return (
        db.query(Call)
        .filter(Call.status.in_(("ended", "transferred")))
        .order_by(Call.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def get_call_by_id(db: Session, call_id: int) -> Optional[Call]:
    return db.query(Call).filter(Call.id == call_id).first()


def delete_call(db: Session, call_id: int) -> bool:
    call = db.query(Call).filter(Call.id == call_id).first()
    if not call:
        return False
    db.delete(call)
    db.commit()
    return True


# ── Transfer ──────────────────────────────────────────────────────────────────

def transfer_call(
    db: Session,
    call_id: int,
    to_department: Optional[str],
    to_agent_id: Optional[int],
    from_agent_id: Optional[int] = None,
    action_type: str = "transfer",
) -> Optional[CallRoute]:
    call = db.query(Call).filter(Call.id == call_id).first()
    if not call:
        return None

    route = CallRoute(
        call_id=call_id,
        from_agent_id=from_agent_id,
        to_agent_id=to_agent_id,
        from_department=call.department,
        to_department=to_department,
        action_type=action_type,
    )
    db.add(route)

    call.status = "transferred"
    if to_department:
        call.department = to_department
    if to_agent_id:
        call.agent_id = to_agent_id
    db.commit()
    db.refresh(route)
    return route


# ── Transcripts ───────────────────────────────────────────────────────────────

def add_transcript(db: Session, call_id: int, speaker: str, text: str) -> Optional[Transcript]:
    call = db.query(Call).filter(Call.id == call_id).first()
    if not call:
        return None
    entry = Transcript(call_id=call_id, speaker=speaker, text=text)
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def get_transcripts(db: Session, call_id: int) -> List[Transcript]:
    return (
        db.query(Transcript)
        .filter(Transcript.call_id == call_id)
        .order_by(Transcript.created_at.asc())
        .all()
    )


# ── Demo data seeding ─────────────────────────────────────────────────────────

def seed_demo_data(db: Session) -> None:
    """
    Seed agents and sample call history on first run.
    Idempotent — checks agents count before inserting.
    Does NOT touch the users table (existing users are preserved).
    """
    if db.query(Agent).count() > 0:
        return  # Already seeded

    # ── Agents (no user account required) ─────────────────────────────────────
    # Link all demo agents to user_id=1 (Anmol Dubey) — agents.user_id is NOT NULL in MySQL
    agents = [
        Agent(user_id=1, name="Angela",     persona="Friendly English support agent", voice_model="en", is_active=True),
        Agent(user_id=1, name="Priyamvada", persona="Hindi language support agent",   voice_model="hi", is_active=True),
        Agent(user_id=1, name="Raj",        persona="Billing and accounts specialist", voice_model="hi", is_active=True),
    ]
    db.add_all(agents)
    db.flush()

    # ── Historical calls ───────────────────────────────────────────────────────
    now = datetime.utcnow()
    demo_calls = [
        Call(caller_number="+91-98765-43210", agent_id=agents[0].id, department="Sales",
             status="ended", started_at=now - timedelta(hours=2),
             ended_at=now - timedelta(hours=1, minutes=45), duration_seconds=900),
        Call(caller_number="+91-87654-32109", agent_id=agents[1].id, department="Support",
             status="ended", started_at=now - timedelta(hours=3),
             ended_at=now - timedelta(hours=2, minutes=50), duration_seconds=600),
        Call(caller_number="+1-555-234-5678",  agent_id=agents[0].id, department="Sales",
             status="ended", started_at=now - timedelta(hours=5),
             ended_at=now - timedelta(hours=4, minutes=45), duration_seconds=180),
        Call(caller_number="+91-76543-21098", agent_id=agents[2].id, department="Billing",
             status="ended", started_at=now - timedelta(hours=6),
             ended_at=now - timedelta(hours=5, minutes=50), duration_seconds=420),
        Call(caller_number="+91-65432-10987", agent_id=agents[1].id, department="Support",
             status="transferred", started_at=now - timedelta(hours=8),
             ended_at=now - timedelta(hours=7, minutes=50), duration_seconds=310),
        Call(caller_number="+1-555-345-6789",  agent_id=agents[0].id, department="Sales",
             status="ended", started_at=now - timedelta(days=1),
             ended_at=now - timedelta(days=1) + timedelta(minutes=8), duration_seconds=480),
        Call(caller_number="+91-54321-09876", agent_id=agents[1].id, department="Support",
             status="ended", started_at=now - timedelta(days=1, hours=3),
             ended_at=now - timedelta(days=1, hours=2, minutes=50), duration_seconds=270),
    ]
    db.add_all(demo_calls)
    db.flush()

    # ── Transcripts for first 2 calls ─────────────────────────────────────────
    transcripts = [
        Transcript(call_id=demo_calls[0].id, speaker="system", text="Call connected — Sales department"),
        Transcript(call_id=demo_calls[0].id, speaker="agent",  text="Hello, thank you for calling SR Comsoft Sales. I'm Angela. How can I help you today?"),
        Transcript(call_id=demo_calls[0].id, speaker="caller", text="Hi, I wanted to enquire about your enterprise plan pricing."),
        Transcript(call_id=demo_calls[0].id, speaker="agent",  text="Our enterprise plan starts at ₹5000 per month. Let me walk you through the features."),
        Transcript(call_id=demo_calls[0].id, speaker="caller", text="Can you also tell me about the onboarding process?"),
        Transcript(call_id=demo_calls[0].id, speaker="agent",  text="Onboarding takes 2-3 business days. We provide full support throughout."),
        Transcript(call_id=demo_calls[0].id, speaker="system", text="Call ended"),

        Transcript(call_id=demo_calls[1].id, speaker="system", text="Call connected — Support department"),
        Transcript(call_id=demo_calls[1].id, speaker="agent",  text="नमस्ते, SR Comsoft सपोर्ट में आपका स्वागत है। मैं प्रियमवदा हूँ।"),
        Transcript(call_id=demo_calls[1].id, speaker="caller", text="मेरे अकाउंट में लॉगिन नहीं हो रहा है।"),
        Transcript(call_id=demo_calls[1].id, speaker="agent",  text="मैं आपकी मदद करूँगी। कृपया अपना रजिस्टर्ड मोबाइल नंबर बताइए।"),
        Transcript(call_id=demo_calls[1].id, speaker="caller", text="87654-32109"),
        Transcript(call_id=demo_calls[1].id, speaker="agent",  text="आपका अकाउंट मिल गया। मैं अभी पासवर्ड रीसेट लिंक भेज देती हूँ।"),
        Transcript(call_id=demo_calls[1].id, speaker="system", text="Call ended"),
    ]
    db.add_all(transcripts)

    # ── Sample transfer route ──────────────────────────────────────────────────
    db.add(CallRoute(
        call_id=demo_calls[4].id,
        from_agent_id=agents[1].id,
        to_agent_id=agents[0].id,
        from_department="Support",
        to_department="Sales",
        action_type="transfer",
    ))

    db.commit()
