from typing import Literal, Optional
from fastapi import APIRouter, HTTPException, status, Depends, Query
from sqlalchemy.orm import Session

from app import crud
from app.database import get_db
from app.config import settings
from app.models import UserModel
from app.auth import get_current_user
from app.rate_limiter import limit_ip_rate, limit_api_key_rate
from app.cache import set_cached_url, delete_cached_url
from app.schemas import (
    URLShortenRequest, URLShortenResponse, URLShortenV2Response, 
    URLUpdateDestinationRequest, URLPaginatedResponse
)
from app.services.url_service import (
    create_shortened_url_service, handle_url_redirect_service
)

v1_router = APIRouter(prefix="/v1", tags=["V1 URL API (Legacy Deprecated)"])
v2_router = APIRouter(prefix="/v2", tags=["V2 URL API (Modern Nested Payload)"])
main_url_router = APIRouter(tags=["URL Operations"])

# --- V1 Shorten Endpoint ---
@v1_router.post("/shorten", response_model=URLShortenResponse, status_code=status.HTTP_201_CREATED)
def shorten_url_v1(
    payload: URLShortenRequest, 
    db: Session = Depends(get_db), 
    owner: UserModel = Depends(limit_api_key_rate)
):
    """V1 Shorten Endpoint (Attaches RFC 8594 Sunset & Deprecation headers via middleware)."""
    return create_shortened_url_service(db, payload, owner, is_v2=False)

# --- V2 Shorten Endpoint ---
@v2_router.post("/shorten", response_model=URLShortenV2Response, status_code=status.HTTP_201_CREATED)
def shorten_url_v2(
    payload: URLShortenRequest, 
    db: Session = Depends(get_db), 
    owner: UserModel = Depends(limit_api_key_rate)
):
    """V2 Shorten Endpoint: Reshapes response payload to nest fields under 'data' and 'api_version'."""
    return create_shortened_url_service(db, payload, owner, is_v2=True)

# --- Default Un-prefixed Shorten Endpoint (For Backwards Compatibility) ---
@main_url_router.post("/shorten", response_model=URLShortenResponse, status_code=status.HTTP_201_CREATED)
def shorten_url_default(
    payload: URLShortenRequest, 
    db: Session = Depends(get_db), 
    owner: UserModel = Depends(limit_api_key_rate)
):
    """Programmatic API Endpoint for Developers (Requires X-API-Key Header)."""
    return create_shortened_url_service(db, payload, owner, is_v2=False)

# --- Dashboard & Management Endpoints ---
@main_url_router.get("/urls", response_model=URLPaginatedResponse)
def list_urls(
    skip: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=100),
    min_clicks: Optional[int] = Query(None, ge=0),
    # Literal (not Query(enum=...)) — only Literal is actually validated by FastAPI.
    # Without it any string reaches getattr(URLModel, sort_by) and 500s on e.g. ?sort_by=metadata.
    sort_by: Literal["created_at", "clicks"] = Query("created_at"),
    order: Literal["asc", "desc"] = Query("desc"),
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Paginated, Filtered & Sorted URL list for authenticated Dashboard users."""
    filter_owner = None if user.role == "admin" else user.id
    items, total = crud.get_urls_paginated(
        db, owner_id=filter_owner, skip=skip, limit=limit, 
        min_clicks=min_clicks, sort_by=sort_by, order=order
    )
    
    formatted_items = [
        URLShortenResponse(
            short_code=i.short_code,
            short_url=f"{settings.base_url}/{i.short_code}",
            original_url=i.original_url,
            clicks=i.clicks,
            created_at=i.created_at.isoformat(),
            expires_at=i.expires_at.isoformat() if i.expires_at else None
        ) for i in items
    ]
    return URLPaginatedResponse(items=formatted_items, total_count=total, skip=skip, limit=limit)

@main_url_router.patch("/urls/{short_code}", response_model=URLShortenResponse)
def update_url(
    short_code: str,
    payload: URLUpdateDestinationRequest,
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Update destination URL with strict ownership check."""
    new_url_str = str(payload.new_original_url)
    try:
        updated_url = crud.update_url_destination(db, short_code=short_code, new_original_url=new_url_str, requesting_user=user)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    
    if not updated_url:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="URL not found")
        
    set_cached_url(short_code, new_url_str, updated_url.expires_at)
    return URLShortenResponse(
        short_code=updated_url.short_code,
        short_url=f"{settings.base_url}/{updated_url.short_code}",
        original_url=updated_url.original_url,
        clicks=updated_url.clicks,
        created_at=updated_url.created_at.isoformat(),
        expires_at=updated_url.expires_at.isoformat() if updated_url.expires_at else None
    )

@main_url_router.delete("/urls/{short_code}", status_code=status.HTTP_200_OK)
def delete_url(
    short_code: str,
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Soft-delete a URL resource by setting deleted_at timestamp."""
    try:
        success = crud.soft_delete_url(db, short_code, requesting_user=user)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))

    if not success:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="URL not found or already deleted")

    delete_cached_url(short_code)  # otherwise the hot path keeps redirecting for up to 2h
    return {"message": f"URL {short_code} soft-deleted successfully"}


@main_url_router.get("/{short_code}", dependencies=[Depends(limit_ip_rate)])
def redirect_to_url(short_code: str, db: Session = Depends(get_db)):
    """Public Redirect Endpoint: Fast Redis Cache read + Expiration check + High-throughput Click Buffer."""
    return handle_url_redirect_service(db, short_code)
