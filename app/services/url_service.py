import secrets
import string
from datetime import datetime, timezone
from fastapi import HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import crud
from app.models import UserModel
from app.config import settings
from app.cache import get_cached_url, set_cached_url, increment_click_buffer
from app.schemas import URLShortenRequest, URLShortenResponse, URLShortenV2Response, URLShortenV2Data

BASE62_CHARACTERS = string.ascii_letters + string.digits

def generate_base62_code(length: int = 6) -> str:
    """Generates a cryptographically secure 6-character Base62 string."""
    return "".join(secrets.choice(BASE62_CHARACTERS) for _ in range(length))

def generate_collision_safe_code(db: Session, max_retries: int = 5) -> str:
    """Bounded collision retry loop for short-code uniqueness guarantee."""
    for _ in range(max_retries):
        code = generate_base62_code()
        if crud.get_url_by_code(db, code) is None:
            return code
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Short-code generation collision limit reached. Please try again."
    )

def create_shortened_url_service(db: Session, payload: URLShortenRequest, owner: UserModel, is_v2: bool = False):
    """Business logic service for custom alias validation, short code generation, and caching."""
    original_url_str = str(payload.url)
    if payload.custom_alias:
        try:
            crud.validate_custom_alias(db, payload.custom_alias)
            short_code = payload.custom_alias
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    else:
        short_code = generate_collision_safe_code(db)

    db_url = crud.create_short_url(
        db,
        short_code=short_code,
        original_url=original_url_str,
        owner_id=owner.id,
        expires_at=payload.expires_at
    )
    set_cached_url(short_code, original_url_str)

    full_short_url = f"{settings.base_url}/{short_code}"
    created_iso = db_url.created_at.isoformat()
    expires_iso = db_url.expires_at.isoformat() if db_url.expires_at else None

    if is_v2:
        return URLShortenV2Response(
            data=URLShortenV2Data(
                short_code=short_code,
                short_url=full_short_url,
                target_url=db_url.original_url,
                clicks=db_url.clicks,
                created_at=created_iso,
                expires_at=expires_iso
            ),
            api_version="v2"
        )

    return URLShortenResponse(
        short_code=short_code,
        short_url=full_short_url,
        original_url=db_url.original_url,
        clicks=db_url.clicks,
        created_at=created_iso,
        expires_at=expires_iso
    )

def handle_url_redirect_service(db: Session, short_code: str) -> RedirectResponse:
    """Service for public link redirects: Cache lookup, expiration check (HTTP 410), & atomic click buffer."""
    db_url = crud.get_url_by_code(db, short_code)
    if not db_url:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Short URL not found")

    # Expiration check (HTTP 410 Gone)
    if db_url.expires_at:
        now_utc = datetime.now(timezone.utc)
        exp_utc = db_url.expires_at.replace(tzinfo=timezone.utc) if db_url.expires_at.tzinfo is None else db_url.expires_at
        if now_utc > exp_utc:
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="This short link has expired."
            )

    # High-throughput atomic click counter buffer (< 0.1ms) & DB count update
    increment_click_buffer(short_code)
    db_url.clicks = db_url.clicks + 1
    db.commit()

    cached_url = get_cached_url(short_code)
    if cached_url:
        return RedirectResponse(url=cached_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    set_cached_url(short_code, db_url.original_url)
    return RedirectResponse(url=db_url.original_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
