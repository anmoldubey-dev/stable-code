# ==========================================================
# APPLICATION FLOW OVERVIEW
# ==========================================================
# 1. LoginRequest   -> POST /auth/login request body (username + password)
# 2. TokenResponse  -> JWT + role + username response model
# 3. UserResponse   -> GET /auth/me user profile response model
#
# PIPELINE FLOW
# LoginRequest (username + password)
#    ||
# authenticate_user  ->  create_token (HS256 JWT)
#    ||
# TokenResponse (access_token, role, username, user_id)
#    ||
# GET /auth/me  ->  get_current_user (JWT decode)  ->  UserResponse
# ==========================================================

from pydantic import BaseModel
from typing import Optional


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    username: str
    user_id: int


class UserResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    username: str
    email: Optional[str]
    role: str
    is_active: bool
