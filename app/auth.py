import secrets
import hashlib
from datetime import datetime, timedelta, timezone
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
import jwt # Standard pyjwt fallback
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import UserModel, APIKeyModel
from app.config import settings
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

SECRET_KEY = settings.secret_key
ALGORITHM = settings.algorithm
ACCESS_TOKEN_EXPIRE_MINUTES = settings.access_token_expire_minutes

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login",auto_error=False)

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(password: str, hashed_password: str) -> bool:
    return pwd_context.verify(password, hashed_password)


# JWT helper funcs
def create_access_token(data:dict,expires_delta:timedelta=None)->str:
    to_encode=data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))    
    to_encode.update({"exp":int(expire.timestamp())})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(token:str= Depends(oauth2_scheme), db:Session = Depends(get_db))-> UserModel:
    """FastAPI dependency to extract human user from JWT token"""
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing auth token"
        )
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except Exception:
        raise credentials_exception
        
    user = db.query(UserModel).filter(UserModel.username == username).first()
    if user is None:
        raise credentials_exception
    return user

def generate_api_key_token()-> tuple[str,str]:
    """Generates a plain API Key and its SHA-256 hash.
    Returns: (plain_key, key_hash)
    """
    raw_token = secrets.token_urlsafe(32)
    plain_key = f"sk_live_{raw_token}"
    key_hash = hashlib.sha256(plain_key.encode()).hexdigest()
    return plain_key, key_hash

from fastapi.security.api_key import APIKeyHeader
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def get_api_key_owner(api_key:str = Depends(api_key_header), db:Session = Depends(get_db))->UserModel:
    """FastAPI dependency to validate X-API-Key header and retrieve the owning User"""
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header"
        )
    hashed_incoming = hashlib.sha256(api_key.encode()).hexdigest()
    api_key_record = db.query(APIKeyModel).filter(APIKeyModel.key_hash==hashed_incoming).first()
    if not api_key_record:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API Key"
        )


# JWT helper funcs
def create_access_token(data:dict,expires_delta:timedelta=None)->str:
    to_encode=data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))    
    to_encode.update({"exp":int(expire.timestamp())})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(token:str= Depends(oauth2_scheme), db:Session = Depends(get_db))-> UserModel:
    """FastAPI dependency to extract human user from JWT token"""
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing auth token"
        )
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except Exception:
        raise credentials_exception
        
    user = db.query(UserModel).filter(UserModel.username == username).first()
    if user is None:
        raise credentials_exception
    return user

def generate_api_key_token()-> tuple[str,str]:
    """Generates a plain API Key and its SHA-256 hash.
    Returns: (plain_key, key_hash)
    """
    raw_token = secrets.token_urlsafe(32)
    plain_key = f"sk_live_{raw_token}"
    key_hash = hashlib.sha256(plain_key.encode()).hexdigest()
    return plain_key, key_hash

from fastapi.security.api_key import APIKeyHeader
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def get_api_key_owner(api_key:str = Depends(api_key_header), db:Session = Depends(get_db))->UserModel:
    """FastAPI dependency to validate X-API-Key header and retrieve the owning User"""
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header"
        )
    hashed_incoming = hashlib.sha256(api_key.encode()).hexdigest()
    api_key_record = db.query(APIKeyModel).filter(APIKeyModel.key_hash==hashed_incoming).first()
    if not api_key_record:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API Key"
        )
    user = db.query(UserModel).filter(UserModel.id == api_key_record.user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API Key owner user not found"
        )
    user.active_api_key = api_key_record
    return user

# --- OAUTH 2.0 STATE & GOOGLE PROFILE HELPERS ---

OAUTH_STATE_STORE: set[str] = set()

def generate_oauth_state() -> str:
    """Generates a cryptographically random state parameter to prevent CSRF on OAuth callback."""
    state = secrets.token_urlsafe(32)
    OAUTH_STATE_STORE.add(state)
    return state

def verify_and_consume_oauth_state(state: str) -> bool:
    """Validates and consumes state parameter to ensure single-use CSRF protection."""
    if state and state in OAUTH_STATE_STORE:
        OAUTH_STATE_STORE.remove(state)
        return True
    return False

import urllib.request
import urllib.parse
import urllib.error

def fetch_google_user_profile(code: str) -> dict:
    """
    Exchanges authorization code for Google access token and fetches user profile.
    If GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are set in settings/.env, executes
    real HTTP request to Google's token and userinfo endpoints. Otherwise falls back to dev mode.
    """
    if settings.google_client_id and settings.google_client_secret and code != "demo_code":
        try:
            token_url = "https://oauth2.googleapis.com/token"
            data = urllib.parse.urlencode({
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": "http://localhost:8000/auth/google/callback",
                "grant_type": "authorization_code"
            }).encode("utf-8")

            req = urllib.request.Request(token_url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req) as resp:
                token_data = json.loads(resp.read().decode("utf-8"))
                access_token = token_data.get("access_token")

            userinfo_url = "https://www.googleapis.com/oauth2/v2/userinfo"
            req_profile = urllib.request.Request(userinfo_url, headers={"Authorization": f"Bearer {access_token}"})
            with urllib.request.urlopen(req_profile) as resp_profile:
                return json.loads(resp_profile.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8") if e.fp else ""
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Google OAuth Exchange Failed: {error_body or e.reason}"
            )
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Google OAuth Connection Error: {str(e)}"
            )

    # Local dev mode fallback when real Client ID/Secret are not set or when using demo_code
    return {
        "email": "user@gmail.com",
        "sub": "google_sub_109283019283",
        "email_verified": True
    }


