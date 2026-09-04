"""Gretta AI — Security middleware for FastAPI dashboard.

Provides CSRF protection, security headers, and request validation.
"""
import secrets
import hashlib
import hmac
import time
from typing import Optional
from functools import wraps

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.datastructures import Headers

from config import AUTH_COOKIE_SECURE, AUTH_ENABLED, AUTH_COOKIE_NAME
from logger import get_logger

log = get_logger(__name__)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to all responses."""
    
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        
        # Security headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        
        # Content Security Policy (allow self + necessary origins)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https:; "
            "connect-src 'self' https://openrouter.ai https://api.groq.com"
        )
        
        return response


class CSRFProtection:
    """CSRF token generation and validation."""
    
    def __init__(self, secret_key: Optional[str] = None):
        self.secret_key = secret_key or secrets.token_hex(32)
    
    def generate_token(self, salt: str = "") -> str:
        """Generate a CSRF token."""
        timestamp = str(int(time.time()))
        message = f"{salt}:{timestamp}"
        signature = hmac.new(
            self.secret_key.encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest()
        return f"{timestamp}-{signature}"
    
    def validate_token(self, token: str, salt: str = "", max_age: int = 3600) -> bool:
        """Validate a CSRF token."""
        try:
            timestamp_str, signature = token.split("-", 1)
            timestamp = int(timestamp_str)
            
            # Check expiration
            if time.time() - timestamp > max_age:
                return False
            
            # Verify signature
            message = f"{salt}:{timestamp}"
            expected = hmac.new(
                self.secret_key.encode(),
                message.encode(),
                hashlib.sha256
            ).hexdigest()
            
            return hmac.compare_digest(signature, expected)
        except (ValueError, AttributeError):
            return False


# Global CSRF instance
csrf_protection = CSRFProtection()


def require_auth(func):
    """Decorator to require authentication for endpoints."""
    @wraps(func)
    async def wrapper(request: Request, *args, **kwargs):
        if not AUTH_ENABLED:
            return await func(request, *args, **kwargs)
        
        cookie = request.cookies.get(AUTH_COOKIE_NAME)
        if not cookie:
            return JSONResponse(
                status_code=401,
                content={"ok": False, "error": "Authentication required"}
            )
        
        return await func(request, *args, **kwargs)
    return wrapper


def validate_origin(request: Request) -> bool:
    """Validate request origin for CSRF protection."""
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    
    # Allow same-origin requests
    if origin and "localhost" not in origin and "127.0.0.1" not in origin:
        allowed_origins = [
            "vercel.app",
            "render.com",
            "huggingface.co",
        ]
        if not any(allowed in origin for allowed in allowed_origins):
            log.warning(f"Rejected request from origin: {origin}")
            return False
    
    return True


def sanitize_input(value: str, max_length: int = 4000) -> str:
    """Sanitize user input to prevent injection attacks."""
    if not value:
        return ""
    
    # Truncate to max length
    value = value[:max_length]
    
    # Remove null bytes and control characters (except newlines/tabs)
    value = "".join(
        char for char in value
        if ord(char) >= 32 or char in "\n\t"
    )
    
    return value.strip()