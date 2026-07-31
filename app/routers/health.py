from fastapi import APIRouter, HTTPException, status, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db

router = APIRouter(tags=["Health & Telemetry Probes"])

@router.get("/health/live", status_code=status.HTTP_200_OK)
def health_live():
    """
    Liveness Probe: Confirms the application process is running and event loop is responsive.
    Returns 200 OK immediately without querying DB or Redis.
    """
    return {"status": "alive"}

@router.get("/health/ready")
def health_ready(db: Session = Depends(get_db)):
    """
    Readiness Probe: Validates external infrastructure dependencies (DB, Redis).
    Returns 200 OK if all dependencies respond, or 503 Service Unavailable if any dependency fails.
    """
    checks = {"database": "unhealthy", "redis": "unhealthy"}
    is_healthy = True

    # 1. Database Check (SQL SELECT 1)
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = "healthy"
    except Exception as e:
        is_healthy = False
        checks["database"] = f"unhealthy: {str(e)}"

    # 2. Redis Cache Check (PING)
    try:
        from app.cache import r
        r.ping()
        checks["redis"] = "healthy"
    except Exception as e:
        is_healthy = False
        checks["redis"] = f"unhealthy: {str(e)}"

    if not is_healthy:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "unready", "checks": checks}
        )

    return {"status": "ready", "checks": checks}

@router.get("/health")
def health_legacy_alias(db: Session = Depends(get_db)):
    """Legacy alias pointing to readiness check for backwards compatibility."""
    return health_ready(db=db)
