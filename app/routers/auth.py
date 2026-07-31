import urllib.parse
from typing import List
from fastapi import APIRouter, HTTPException, status, Depends, Request
from sqlalchemy.orm import Session

from app import crud
from app.database import get_db
from app.config import settings
from app.models import UserModel
from app.auth import get_current_user, generate_oauth_state
from app.schemas import (
    UserSignupRequest, UserLoginRequest, TokenResponse, 
    UserProfileResponse, APIKeyCreateRequest, APIKeyResponse
)
from app.services.auth_service import (
    authenticate_user_credentials_service, process_google_oauth_callback_service
)

router = APIRouter(prefix="/auth", tags=["Authentication & Developer Keys"])

@router.post("/signup", status_code=status.HTTP_201_CREATED)
def signup(payload: UserSignupRequest, db: Session = Depends(get_db)):
    existing = crud.get_user_by_username(db, payload.username)
    if existing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Username already registered")
    user = crud.create_user(db, username=payload.username, password_raw=payload.password)
    return {"message": "User created successfully", "id": user.id, "username": user.username}

@router.post("/login", response_model=TokenResponse)
def login(payload: UserLoginRequest, db: Session = Depends(get_db)):
    token = authenticate_user_credentials_service(db, payload.username, payload.password)
    return TokenResponse(access_token=token)

@router.get("/me", response_model=UserProfileResponse)
def get_current_user_profile(user: UserModel = Depends(get_current_user)):
    """Returns the authenticated user profile including email and Google OAuth linkage details."""
    return UserProfileResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        google_sub=user.google_sub,
        role=user.role
    )

@router.post("/keys", response_model=APIKeyResponse, status_code=status.HTTP_201_CREATED)
def generate_api_key(
    payload: APIKeyCreateRequest, 
    user: UserModel = Depends(get_current_user), 
    db: Session = Depends(get_db)
):
    """Generate a new Developer API Key for authenticated JWT user"""
    record, plain_key = crud.create_api_key_for_user(
        db, user_id=user.id, label=payload.label, rate_limit=payload.rate_limit
    )
    return APIKeyResponse(
        id=record.id,
        prefix=record.prefix,
        label=record.label,
        rate_limit=record.rate_limit,
        plain_key=plain_key
    )

@router.get("/keys", response_model=List[APIKeyResponse])
def list_api_keys(user: UserModel = Depends(get_current_user), db: Session = Depends(get_db)):
    records = crud.get_user_api_keys(db, user.id)
    return [
        APIKeyResponse(id=r.id, prefix=r.prefix, label=r.label, rate_limit=r.rate_limit)
        for r in records
    ]

# --- GOOGLE OAUTH 2.0 ENDPOINTS ---

@router.get("/google/login")
def google_login():
    """
    1. Generates cryptographic CSRF state token.
    2. Constructs Google Authorization URL.
    3. Redirects client to accounts.google.com authorization screen.
    """
    state = generate_oauth_state()

    # Fallback demo auth URL when Google credentials are not set in .env
    if not settings.google_client_id:
        auth_url = f"{settings.google_redirect_uri}?state={state}&code=demo_code"
    else:
        params = {
            "client_id": settings.google_client_id,
            "redirect_uri": settings.google_redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "access_type": "offline",
            "prompt": "select_account"
        }
        auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"

    return {
        "authorization_url": auth_url,
        "state": state,
        "message": "Navigate to authorization_url in browser to initiate Google SSO"
    }

@router.get("/google/callback")
def google_callback(code: str, state: str, request: Request, db: Session = Depends(get_db)):
    """
    1. Validates and consumes single-use CSRF state token.
    2. Exchanges code for Google access token.
    3. Fetches user identity (sub & email) from Google userinfo API.
    4. Executes account linking and issues platform JWT token.
    """
    return process_google_oauth_callback_service(db, code, state, request)
