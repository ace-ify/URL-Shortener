import time
import json
import logging
from typing import Any
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("api.access")
logger.setLevel(logging.INFO)

# Headers that must NEVER appear in plain-text logs
SENSITIVE_HEADERS = {"authorization", "x-api-key", "cookie", "set-cookie"}

# JSON Body fields that must NEVER appear in plain-text logs
SENSITIVE_BODY_KEYS = {
    "password", "access_token", "refresh_token", 
    "plain_key", "secret", "api_key", "token"
}

def redact_dict(data: Any) -> Any:
    """
    Recursively scans and redacts sensitive keys in JSON payloads.
    Uses a denylist approach combined with strict key matching.
    """
    if isinstance(data, dict):
        redacted = {}
        for key, value in data.items():
            if key.lower() in SENSITIVE_BODY_KEYS:
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact_dict(value)
        return redacted
    elif isinstance(data, list):
        return [redact_dict(item) for item in data]
    return data

def redact_headers(headers: Any) -> dict[str, str]:
    """Redacts sensitive HTTP headers from log outputs."""
    redacted = {}
    for key, value in headers.items():
        if key.lower() in SENSITIVE_HEADERS:
            redacted[key] = "[REDACTED]"
        else:
            redacted[key] = value
    return redacted

class LoggingAndRedactionMiddleware(BaseHTTPMiddleware):
    """
    Production-grade request/response logging middleware.
    Calculates execution latency and redacts sensitive PII & secrets before logging.
    """
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        start_time = time.perf_counter()

        # Capture request body for logging without consuming request stream
        body_bytes = await request.body()
        
        # Starlette request body streams can only be read once. We re-inject it for downstream handlers:
        async def receive():
            return {"type": "http.request", "body": body_bytes}
        
        request = Request(request.scope, receive=receive)

        # Process Request downstream
        response = await call_next(request)
        
        # Calculate execution latency in milliseconds
        process_time_ms = round((time.perf_counter() - start_time) * 1000, 2)
        response.headers["X-Process-Time-Ms"] = str(process_time_ms)

        # Redact headers
        safe_headers = redact_headers(request.headers)

        # Redact Body
        safe_body: Any = "[EMPTY]"
        if body_bytes:
            try:
                parsed_json = json.loads(body_bytes.decode("utf-8"))
                safe_body = redact_dict(parsed_json)
            except Exception:
                safe_body = "[RAW_NON_JSON_BYTES]"

        log_data = {
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "latency_ms": process_time_ms,
            "headers": safe_headers,
            "body": safe_body
        }

        logger.info(f"HTTP {request.method} {request.url.path} {response.status_code} - {process_time_ms}ms | Data: {json.dumps(log_data)}")
        return response

class V1DeprecationMiddleware(BaseHTTPMiddleware):
    """
    Appends RFC 8594 Sunset & Deprecation headers to any HTTP response starting with /v1/.
    Gives machine-readable migration notices to legacy client consumers.
    """
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        if request.url.path.startswith("/v1/"):
            response.headers["Sunset"] = "Wed, 31 Dec 2026 23:59:59 GMT"
            response.headers["X-API-Deprecated"] = "true"
            response.headers["X-API-Migration-Doc"] = "/docs#v2-migration"
        return response

