import secrets
import string
from fastapi.encoders import jsonable_encoder
from typing import Optional, List
from fastapi import FastAPI, HTTPException, status, Depends, Request, Query
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, HttpUrl, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db
from app import crud
from app.cache import get_cached_url, set_cached_url
from app.rate_limiter import limit_ip_rate, limit_api_key_rate
from app.auth import (
    hash_password, verify_password, create_access_token, 
    get_current_user, get_api_key_owner
)
from app.models import UserModel, APIKeyModel
from app.queue import push_click_event

app = FastAPI(
    title="SaaS-Grade URL Shortener Platform",
    description="Multi-tenant shortener with Dual Auth (JWT & API Keys), Redis Caching, Base62 Collision Protection & Async Click Queue",
    version="2.0.0"
)

# --- 1. CENTRALIZED EXCEPTION HANDLERS (Standard JSON Error Shape) ---

@app.exception_handler(HTTPException)
async def custom_http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.status_code, "message": exc.detail}}
    )

@app.exception_handler(PermissionError)
async def custom_permission_error_handler(request: Request, exc: PermissionError):
    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN,
        content={"error": {"code": 403, "message": str(exc)}}
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"error": {"code": 422, "message": "Validation Error", "details": jsonable_encoder(exc.errors())}}
    )

# --- 2. PYDANTIC V2 SCHEMAS ---

class UserSignupRequest(BaseModel):
    username: str
    password: str

    @field_validator("password")
    @classmethod
    def validate_password_length(cls, v: str) -> str:
        if len(v.encode("utf-8")) > 72:
            raise ValueError("Password must not exceed 72 UTF-8 bytes")
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters long")
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain at least one uppercase letter (A-Z)")
        if not any(c.islower() for c in v):
            raise ValueError("Password must contain at least one lowercase letter (a-z)")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one number (0-9)")
        if not any(c in "!@#$%^&*()_+-=[]{}|;:,.<>?" for c in v):
            raise ValueError("Password must contain at least one special character (!@#$)")
        return v

class UserLoginRequest(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"

class APIKeyCreateRequest(BaseModel):
    label: Optional[str] = "Default Developer Key"
    rate_limit: Optional[int] = 10

class APIKeyResponse(BaseModel):
    id: int
    prefix: str
    label: Optional[str]
    rate_limit: int
    plain_key: Optional[str] = None  # Returned ONLY ONCE on creation

class URLShortenRequest(BaseModel):
    url: HttpUrl

    @field_validator("url")
    @classmethod
    def validate_url_scheme(cls, v: HttpUrl) -> HttpUrl:
        url_str = str(v).lower()
    
        # 1. Scheme Check
        if v.scheme not in ["http", "https"]:
            raise ValueError("Only http and https URL schemes are allowed")
            
        # 2. Self-Loop & SSRF Protection
        forbidden_hosts = ["localhost", "127.0.0.1", "0.0.0.0", "169.254.169.254"]
        if v.host in forbidden_hosts:
            raise ValueError("Shortening internal or localhost URLs is forbidden for security")
            
        return v

class URLShortenResponse(BaseModel):
    short_code: str
    short_url: str
    original_url: str
    clicks: int
    created_at: str

class URLUpdateDestinationRequest(BaseModel):
    new_original_url: HttpUrl

class URLPaginatedResponse(BaseModel):
    items: List[URLShortenResponse]
    total_count: int
    skip: int
    limit: int

# --- 3. BASE62 COLLISION-SAFE SHORT CODE GENERATOR ---

BASE62_CHARACTERS = string.ascii_letters + string.digits  # 62 alphanumeric chars

def generate_base62_code(length: int = 6) -> str:
    """Generates a cryptographically secure 6-character Base62 string"""
    return "".join(secrets.choice(BASE62_CHARACTERS) for _ in range(length))

def generate_collision_safe_short_code(db: Session, max_retries: int = 5) -> str:
    """Bounded collision retry loop for short-code uniqueness guarantee"""
    for attempt in range(max_retries):
        code = generate_base62_code()
        # Verify code does not exist in DB
        if crud.get_url_by_code(db, code) is None:
            return code
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Short-code generation collision limit reached. Please try again."
    )

# --- 4. AUTH & DEVELOPER API ROUTES ---

@app.get("/health")
def health_check(db: Session = Depends(get_db)):
    """Verify dependencies required by the API are reachable."""
    try:
        db.execute(text("SELECT 1"))
        from app.cache import r
        r.ping()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service dependency unavailable",
        )
    return {"status": "ok"}

@app.post("/auth/signup", status_code=status.HTTP_201_CREATED)
def signup(payload: UserSignupRequest, db: Session = Depends(get_db)):
    existing = crud.get_user_by_username(db, payload.username)
    if existing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Username already registered")
    user = crud.create_user(db, payload.username, payload.password)
    return {"message": "User registered successfully", "username": user.username}

@app.post("/auth/login", response_model=TokenResponse)
def login(payload: UserLoginRequest, db: Session = Depends(get_db)):
    user = crud.get_user_by_username(db, payload.username)
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")
    
    token = create_access_token(data={"sub": user.username})
    return TokenResponse(access_token=token)

@app.post("/auth/keys", response_model=APIKeyResponse, status_code=status.HTTP_201_CREATED)
def create_api_key(
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

@app.get("/auth/keys", response_model=List[APIKeyResponse])
def list_api_keys(user: UserModel = Depends(get_current_user), db: Session = Depends(get_db)):
    records = crud.get_user_api_keys(db, user.id)
    return [
        APIKeyResponse(id=r.id, prefix=r.prefix, label=r.label, rate_limit=r.rate_limit)
        for r in records
    ]

# --- 5. URL CORE ENDPOINTS ---

@app.post("/shorten", response_model=URLShortenResponse, status_code=status.HTTP_201_CREATED)
def shorten_url(
    payload: URLShortenRequest, 
    db: Session = Depends(get_db), 
    owner: UserModel = Depends(limit_api_key_rate)  # Enforces API Key Auth & Per-Key Rate Limiting
):
    """Programmatic API Endpoint for Developers (Requires X-API-Key Header)"""
    original_url_str = str(payload.url)
    short_code = generate_collision_safe_short_code(db)
    
    db_url = crud.create_short_url(db, short_code=short_code, original_url=original_url_str, owner_id=owner.id)
    set_cached_url(short_code, original_url_str)

    return URLShortenResponse(
        short_code=short_code,
        short_url=f"http://localhost:8000/{short_code}",
        original_url=db_url.original_url,
        clicks=db_url.clicks,
        created_at=db_url.created_at.isoformat()
    )

@app.get("/urls", response_model=URLPaginatedResponse)
def list_urls(
    skip: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=100),
    min_clicks: Optional[int] = Query(None, ge=0),
    sort_by: str = Query("created_at", enum=["created_at", "clicks"]),
    order: str = Query("desc", enum=["asc", "desc"]),
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Paginated, Filtered & Sorted URL list for authenticated Dashboard users"""
    # Non-admin users can only view their own URLs
    filter_owner = None if user.role == "admin" else user.id
    items, total = crud.get_urls_paginated(
        db, owner_id=filter_owner, skip=skip, limit=limit, 
        min_clicks=min_clicks, sort_by=sort_by, order=order
    )
    
    formatted_items = [
        URLShortenResponse(
            short_code=i.short_code,
            short_url=f"http://localhost:8000/{i.short_code}",
            original_url=i.original_url,
            clicks=i.clicks,
            created_at=i.created_at.isoformat()
        ) for i in items
    ]
    return URLPaginatedResponse(items=formatted_items, total_count=total, skip=skip, limit=limit)

@app.patch("/urls/{short_code}", response_model=URLShortenResponse)
def update_url(
    short_code: str,
    payload: URLUpdateDestinationRequest,
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Update destination URL with strict ownership check"""
    new_url_str = str(payload.new_original_url)
    updated_url = crud.update_url_destination(db, short_code=short_code, new_original_url=new_url_str, requesting_user=user)
    
    if not updated_url:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="URL not found")
        
    set_cached_url(short_code, new_url_str)
    return URLShortenResponse(
        short_code=updated_url.short_code,
        short_url=f"http://localhost:8000/{updated_url.short_code}",
        original_url=updated_url.original_url,
        clicks=updated_url.clicks,
        created_at=updated_url.created_at.isoformat()
    )

@app.delete("/urls/{short_code}", status_code=status.HTTP_200_OK)
def delete_url(
    short_code: str,
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Soft-delete a URL resource by setting deleted_at timestamp"""
    success = crud.soft_delete_url(db, short_code, requesting_user=user)
    if not success:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="URL not found or already deleted")
    return {"message": f"URL {short_code} soft-deleted successfully"}

@app.get("/{short_code}", dependencies=[Depends(limit_ip_rate)])
def redirect_to_url(short_code: str, db: Session = Depends(get_db)):
    """Public Redirect Endpoint: Fast Redis Cache read + Async Click Analytics Queue"""
    cached_url = get_cached_url(short_code)
    if cached_url:
        push_click_event(short_code)  # Non-blocking async queue push (< 0.2ms)
        return RedirectResponse(url=cached_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
        
    db_url = crud.get_url_by_code(db, short_code)
    if not db_url:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Short URL not found")
        
    set_cached_url(short_code, db_url.original_url)
    push_click_event(short_code)  # Non-blocking async queue push (< 0.2ms)
    return RedirectResponse(url=db_url.original_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
