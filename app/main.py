import random
import string
from fastapi import FastAPI, HTTPException, status, Depends
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, HttpUrl
from sqlalchemy.orm import Session

from app.cache import get_cached_url, set_cached_url


from app.database import engine, Base, get_db
from app import crud

from app.rate_limiter import limit_rate
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Secure URL Shortener - Phase 4", 
    dependencies=[Depends(limit_rate)]
)

class URLShortenRequest(BaseModel):
    url: HttpUrl

class URLShortenResponse(BaseModel):
    short_code: str
    short_url: str

def generate_short_code(length: int = 6) -> str:
    """Generates a random alphanumeric string of specified length."""
    characters = string.ascii_letters + string.digits
    return "".join(random.choice(characters) for _ in range(length))

@app.post("/shorten", response_model=URLShortenResponse, status_code=status.HTTP_201_CREATED)
def shorten_url(payload: URLShortenRequest, db: Session = Depends(get_db)):
    """
    Endpoint to receive a long URL, generate a unique short code,
    and save it in our persistent database.
    """
    original_url = str(payload.url)
    
    # Generate unique short code and verify it doesn't already exist in DB
    short_code = generate_short_code()
    while crud.get_url_by_code(db, short_code) is not None:
        short_code = generate_short_code()
    
    crud.create_short_url(db, short_code=short_code, original_url=original_url)
    set_cached_url(short_code,original_url)

    return URLShortenResponse(
        short_code=short_code,
        short_url=f"http://localhost:8000/{short_code}"
    )

@app.get("/{short_code}")
def redirect_to_url(short_code: str, db: Session = Depends(get_db)):
    """
    Endpoint to fetch the short code from database, increment clicks,
    and redirect the client using HTTP 307.
    ```
    """
    cached_url = get_cached_url(short_code)
    if cached_url:
        crud.increment_clicks(db,short_code)
        return RedirectResponse(url=cached_url,status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    db_url = crud.get_url_by_code(db, short_code)
    if not db_url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Short code not found"
        )
    set_cached_url(short_code,db_url.original_url)
    crud.increment_clicks(db, short_code)
    return RedirectResponse(url=db_url.original_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
