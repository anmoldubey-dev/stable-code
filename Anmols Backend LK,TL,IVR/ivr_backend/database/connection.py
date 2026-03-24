# ==========================================================
# APPLICATION FLOW OVERVIEW
# ==========================================================
# 1. engine        -> SQLAlchemy MySQL engine with pool_pre_ping + pool_recycle
# 2. SessionLocal  -> Session factory (autocommit=False, autoflush=False)
# 3. Base          -> DeclarativeBase shared by all ORM models
# 4. get_db()      -> FastAPI dependency: yield session -> close on exit
#
# PIPELINE FLOW
# DATABASE_URL = mysql+pymysql://root:root@127.0.0.1:3306/sr_comsoft_db
#    ||
# create_engine  ->  connection pool (pool_pre_ping, pool_recycle=3600)
#    ||
# get_db()  ->  SessionLocal()  ->  yield db  ->  db.close()
# ==========================================================

"""
ivr_backend/database/connection.py
SQLAlchemy engine + session factory for MySQL (callcentre DB).
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

DATABASE_URL = "mysql+pymysql://root:root@127.0.0.1:3306/sr_comsoft_db"

engine = create_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,       # Reconnect on stale connections
    pool_recycle=3600,        # Recycle connections every hour
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# --------------------------------------------------
# Base -> SQLAlchemy declarative base shared by all ORM models
# --------------------------------------------------
class Base(DeclarativeBase):
    pass


# --------------------------------------------------
# get_db -> FastAPI dependency that yields a DB session and closes on exit
#    ||
# SessionLocal() -> yield db -> db.close()
# --------------------------------------------------
def get_db():
    """FastAPI dependency — yields a DB session and closes it after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
