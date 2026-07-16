from sqlalchemy.orm import Session
from app.models import URLModel

def get_url_by_code(db: Session, short_code: str) -> URLModel:
    return db.query(URLModel).filter(URLModel.short_code == short_code).first()

def create_short_url(db: Session, short_code: str, original_url: str) -> URLModel:
    db_url = URLModel(short_code=short_code, original_url=original_url)
    db.add(db_url)
    db.commit()
    db.refresh(db_url)
    return db_url

def increment_clicks(db: Session, short_code: str) -> URLModel:
    db_url = db.query(URLModel).filter(URLModel.short_code == short_code).first()
    if db_url:
        db_url.clicks += 1
        db.commit()
        db.refresh(db_url)
    return db_url
