import os
from fastapi import FastAPI, HTTPException, status, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.staticfiles import StaticFiles


from app.database import engine, Base
from app.middleware import LoggingAndRedactionMiddleware, V1DeprecationMiddleware
from app.routers import health, auth, urls

# Ensure database tables are created
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="High-Scale SaaS URL Shortener & Developer Platform",
    description="Production-grade API featuring Dual Auth (JWT + Hashed API Keys), Redis Click Buffer, OAuth 2.0, & Liveness/Readiness Probes.",
    version="2.0.0"
)

# --- STANDARDIZED EXCEPTION HANDLERS ---

@app.exception_handler(HTTPException)
async def custom_http_exception_handler(request: Request, exc: HTTPException):
    """Ensures ALL HTTP exceptions strictly conform to {"error": {"code": STATUS, "message": DETAIL}}."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.status_code, "message": exc.detail}},
        headers=exc.headers
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Ensures Pydantic Request Validation errors return standardized 422 JSON shape."""
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"error": {"code": status.HTTP_422_UNPROCESSABLE_ENTITY, "message": jsonable_encoder(exc.errors())}}
    )


# --- MIDDLEWARE REGISTRATION ---
app.add_middleware(LoggingAndRedactionMiddleware)
app.add_middleware(V1DeprecationMiddleware)

# --- ROUTER MOUNTING ---
app.include_router(health.router)
app.include_router(auth.router)
app.include_router(urls.v1_router)
app.include_router(urls.v2_router)
app.include_router(urls.main_url_router)

# --- STATIC DASHBOARD MOUNT ---
static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
if os.path.exists(static_dir):
    app.mount("/dashboard", StaticFiles(directory=static_dir, html=True), name="static")
