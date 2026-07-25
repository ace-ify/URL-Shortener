from datetime import datetime, timezone
from sqlalchemy.orm import Session
from sqlalchemy import desc, asc
from app.models import URLModel, UserModel, APIKeyModel
from app.auth import hash_password, generate_api_key_token

# --- USER OPERATIONS ---
def create_user(db: Session, username: str, password_raw: str, role: str = "user") -> UserModel:
    hashed_pwd = hash_password(password_raw)
    db_user = UserModel(username=username, password_hash=hashed_pwd, role=role)
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user

def get_user_by_username(db:Session,username:str)->UserModel:
    return db.query(UserModel).filter(UserModel.username==username).first()

# --- API KEY OPERATIONS ---
def create_api_key_for_user(db: Session, user_id: int, label: str = "Default Key", rate_limit: int = 10) -> tuple[APIKeyModel, str]:
    """Generates an API Key, hashes it, saves hash to DB, and returns (db_record, plain_key)"""
    plain_key, key_hash = generate_api_key_token()
    prefix = plain_key[:7]

    api_key_record = APIKeyModel(
        user_id=user_id,
        key_hash=key_hash,
        prefix=prefix,
        label=label,
        rate_limit=rate_limit,
    )
    db.add(api_key_record)
    db.commit()
    db.refresh(api_key_record)
    return api_key_record, plain_key

def get_user_api_keys(db: Session, user_id: int):
    return db.query(APIKeyModel).filter(APIKeyModel.user_id == user_id).all()

# --- URL CRUD & PAGINATION OPERATIONS ---
def create_short_url(db: Session, short_code: str, original_url: str, owner_id: int = None) -> URLModel:
    db_url = URLModel(short_code=short_code, original_url=original_url, owner_id=owner_id)
    db.add(db_url)
    db.commit()
    db.refresh(db_url)
    return db_url

def get_url_by_code(db: Session, short_code: str) -> URLModel:
    """Fetch URL by code only if it has not been soft-deleted"""
    return db.query(URLModel).filter(
        URLModel.short_code == short_code,
        URLModel.deleted_at.is_(None)
    ).first()

def get_urls_paginated(
    db: Session, 
    owner_id: int = None, 
    skip: int = 0, 
    limit: int = 10,
    min_clicks: int = None,
    sort_by: str = "created_at",
    order: str = "desc"
) -> tuple[list[URLModel], int]:
    """Paginated, Filtered, and Sorted list query for URLs"""
    query = db.query(URLModel).filter(URLModel.deleted_at.is_(None))

    # Ownership Filter
    if owner_id is not None:
        query = query.filter(URLModel.owner_id == owner_id)
    
    # Min Clicks Filter
    if min_clicks is not None:
        query = query.filter(URLModel.clicks >= min_clicks)

    # Total count calculation before applying pagination bounds
    total_count = query.count()

    sort_column = getattr(URLModel, sort_by, URLModel.created_at)
    if order == "asc":
        query = query.order_by(asc(sort_column))
    else:
        query = query.order_by(desc(sort_column))

    # Apply Offset & Limit (Pagination Bounds)
    items = query.offset(skip).limit(limit).all()
    return items, total_count

def update_url_destination(db: Session, short_code: str, new_original_url: str, requesting_user: UserModel) -> URLModel:
    """Update destination URL with strict ownership check"""
    db_url = get_url_by_code(db, short_code)
    if not db_url:
        return None
        
    # Ownership Enforcement (Admin bypass allowed)
    if requesting_user.role != "admin" and db_url.owner_id != requesting_user.id:
        raise PermissionError("Forbidden: You do not own this URL resource")
        
    db_url.original_url = new_original_url
    db.commit()
    db.refresh(db_url)
    return db_url


def soft_delete_url(db: Session, short_code: str, requesting_user: UserModel) -> bool:
    """Soft delete a URL resource by setting deleted_at timestamp"""
    db_url = db.query(URLModel).filter(URLModel.short_code == short_code).first()
    if not db_url or db_url.deleted_at is not None:
        return False
        
    # Ownership Enforcement (Admin bypass allowed)
    if requesting_user.role != "admin" and db_url.owner_id != requesting_user.id:
        raise PermissionError("Forbidden: You do not own this URL resource")
        
    db_url.deleted_at = datetime.now(timezone.utc)
    db.commit()
    return True


def increment_clicks(db: Session, short_code: str) -> URLModel:
    db_url = get_url_by_code(db, short_code)
    if db_url:
        db_url.clicks += 1
        db.commit()
        db.refresh(db_url)
    return db_url