from datetime import datetime, timezone
from sqlalchemy import Column, String, Integer, DateTime
from app.database import Base

class URLModel(Base):
    __tablename__ = "urls"

    short_code = Column(String, primary_key=True, index=True)
    original_url = Column(String, nullable=False)
    clicks = Column(Integer, default=0, nullable=False)
    created_at = Column(
        DateTime, 
        default=lambda: datetime.now(timezone.utc), 
        nullable=False
    )
