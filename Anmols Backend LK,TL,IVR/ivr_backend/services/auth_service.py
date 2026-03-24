# ==========================================================
# APPLICATION FLOW OVERVIEW
# ==========================================================
# 1. create_access_token() -> Build signed HS256 JWT with expiry
# 2. authenticate()        -> Verify username + sha256_crypt password
# 3. get_current_user()    -> Decode JWT Bearer token -> User ORM
#
# PIPELINE FLOW
# POST /auth/login  ->  authenticate(username, password, db)
#    ||
# db query User by username  ->  sha256_crypt.verify(password, hash)
#    ||
# create_access_token({sub: username})  ->  JWT string
#    ||
# Protected routes  ->  get_current_user  ->  decode JWT  ->  User
# ==========================================================
"""
ivr_backend/services/auth_service.py
JWT creation / verification + password hashing.
"""
import os
from datetime import datetime, timedelta
from typing import Optional

from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from ..database.connection import get_db
from ..models.user import User

SECRET_KEY  = os.getenv("IVR_JWT_SECRET", "ivr-super-secret-key-change-in-prod-2024")
ALGORITHM   = "HS256"
TOKEN_EXPIRE_HOURS = 8

# sha256_crypt avoids bcrypt 5.x / passlib version incompatibility
pwd_ctx = CryptContext(schemes=["sha256_crypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def hash_password(plain: str) -> str:
    return pwd_ctx.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_ctx.verify(plain, hashed)


def create_token(user_id: int, username: str, role: str) -> str:
    expire = datetime.utcnow() + timedelta(hours=TOKEN_EXPIRE_HOURS)
    payload = {"sub": str(user_id), "username": username, "role": role, "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def authenticate_user(db: Session, username: str, password: str) -> Optional[User]:
    user = db.query(User).filter(User.username == username, User.is_active == True).first()
    if not user or not verify_password(password, user.hashed_password):
        return None
    return user


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    payload = decode_token(token)
    user_id = int(payload.get("sub", 0))
    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return current_user
