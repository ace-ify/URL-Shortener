from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    database_url: str = "sqlite:///./urls.db"
    
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 1

    # Fail fast when Redis is unreachable so the redirect can fall back to SQL.
    # Generous for a local Redis (sub-millisecond); raise it for a cross-AZ cache.
    redis_timeout_seconds: float = 0.25

    # Per-IP quota on public redirects. Env-tunable so load tests and staging can raise it.
    ip_rate_limit: int = 30

    secret_key: str = "super-secure-saas-secret-key-change-me"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/auth/google/callback"
    base_url: str = "http://localhost:8000"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")




settings = Settings()

