from datetime import datetime
from typing import Optional, List, Any
from pydantic import BaseModel, HttpUrl, field_validator

# --- AUTH & USER SCHEMAS ---
class UserSignupRequest(BaseModel):
    username: str
    password: str

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, v: str) -> str:
        if len(v) < 8 or len(v) > 64:
            raise ValueError("Password must be between 8 and 64 characters long")
        return v


class UserLoginRequest(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"

class UserProfileResponse(BaseModel):
    id: int
    username: str
    email: Optional[str] = None
    google_sub: Optional[str] = None
    role: str

class APIKeyCreateRequest(BaseModel):
    label: Optional[str] = None
    rate_limit: int = 10

class APIKeyResponse(BaseModel):
    id: int
    prefix: str
    label: Optional[str] = None
    rate_limit: int
    plain_key: Optional[str] = None

# --- URL SCHEMAS ---
class URLShortenRequest(BaseModel):
    url: HttpUrl
    custom_alias: Optional[str] = None
    expires_at: Optional[datetime] = None

    @field_validator("url")
    @classmethod
    def validate_url_scheme(cls, v: HttpUrl) -> HttpUrl:
        url_str = str(v).lower()
        if v.scheme not in ["http", "https"]:
            raise ValueError("Only http and https URL schemes are allowed")
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
    expires_at: Optional[str] = None

class URLShortenV2Data(BaseModel):
    short_code: str
    short_url: str
    target_url: str
    clicks: int
    created_at: str
    expires_at: Optional[str] = None

class URLShortenV2Response(BaseModel):
    data: URLShortenV2Data
    api_version: str = "v2"

class URLUpdateDestinationRequest(BaseModel):
    new_original_url: HttpUrl

class URLPaginatedResponse(BaseModel):
    items: List[URLShortenResponse]
    total_count: int
    skip: int
    limit: int

# --- STANDARDIZED ERROR PAYLOAD SCHEMAS ---
class ErrorDetailPayload(BaseModel):
    code: int
    message: Any

class StandardErrorResponse(BaseModel):
    error: ErrorDetailPayload
