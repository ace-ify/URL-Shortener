from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    database_url: str = "sqlite:///./urls.db"
    
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 1

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
