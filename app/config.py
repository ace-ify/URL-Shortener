from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    database_url: str = "sqlite:///./urls.db"
    
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 1

    secret_key: str = "super-secure-saas-secret-key-change-me"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/auth/google/callback"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")



settings = Settings()

