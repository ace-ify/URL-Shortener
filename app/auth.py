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
    # Bcrypt spec limits input passwords to 72 bytes max
    password_bytes = password.encode('utf-8')[:72].decode('utf-8', errors='ignore')
    return pwd_context.hash(password_bytes)

def verify_password(password: str, hashed_password: str) -> bool:
    # Bcrypt spec limits input passwords to 72 bytes max
    password_bytes = password.encode('utf-8')[:72].decode('utf-8', errors='ignore')
    return pwd_context.verify(password_bytes, hashed_password)


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