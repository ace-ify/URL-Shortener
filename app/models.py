from datetime import datetime, timezone
from sqlalchemy import Column, String, Integer, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from app.database import Base

class UserModel(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    role = Column(String, default="user", nullable=False)
    created_at = Column(
        DateTime, 
        default=lambda: datetime.now(timezone.utc), 
        nullable=False
    )
    urls = relationship("URLModel", back_populates="owner")
    api_keys = relationship("APIKeyModel", back_populates="user")

class APIKeyModel(Base):
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    key_hash = Column(String, unique=True, index=True, nullable=False)
    prefix = Column(String, nullable=False)
    label = Column(String, nullable=True)
    rate_limit = Column(Integer, default=10, nullable=False) # max requests per minute
    created_at = Column(
        DateTime, 
        default=lambda: datetime.now(timezone.utc), 
        nullable=False
    )
    user = relationship("UserModel", back_populates="api_keys")

class URLModel(Base):
    __tablename__ = "urls"

    short_code = Column(String, primary_key=True, index=True)
    original_url = Column(String, nullable=False)
    clicks = Column(Integer, default=0, nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(
        DateTime, 
        default=lambda: datetime.now(timezone.utc), 
        nullable=False
    )
    deleted_at = Column(DateTime, nullable=True)

    owner = relationship("UserModel", back_populates="urls")
