"""Gretta AI — Rate limiting middleware.

Implements in-memory rate limiting for API endpoints to prevent
abuse and brute force attacks.
"""
import time
import threading
from collections import defaultdict
from typing import Dict, Tuple, Optional
from functools import wraps

from logger import get_logger

log = get_logger(__name__)


class RateLimiter:
    """Thread-safe in-memory rate limiter using sliding window."""
    
    def __init__(
        self,
        max_requests: int = 100,
        window_seconds: int = 60,
        cleanup_interval: int = 1000
    ):
        """Initialize rate limiter.
        
        Args:
            max_requests: Maximum requests allowed in window
            window_seconds: Time window in seconds
            cleanup_interval: Clean old entries every N requests
        """
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.cleanup_interval = cleanup_interval
        self._requests: Dict[str, list] = defaultdict(list)
        self._lock = threading.Lock()
        self._request_count = 0
    
    def _cleanup_if_needed(self):
        """Remove old entries to prevent memory leak."""
        self._request_count += 1
        if self._request_count >= self.cleanup_interval:
            self._request_count = 0
            cutoff = time.time() - self.window_seconds
            for key in list(self._requests.keys()):
                self._requests[key] = [
                    t for t in self._requests[key] if t > cutoff
                ]
                if not self._requests[key]:
                    del self._requests[key]
    
    def is_allowed(self, key: str) -> Tuple[bool, int, int]:
        """Check if request is allowed for given key.
        
        Args:
            key: Identifier (typically IP or user ID)
        
        Returns:
            Tuple of (allowed, remaining_requests, retry_after_seconds)
        """
        now = time.time()
        cutoff = now - self.window_seconds
        
        with self._lock:
            # Remove old requests
            self._requests[key] = [
                t for t in self._requests[key] if t > cutoff
            ]
            
            current_count = len(self._requests[key])
            
            if current_count >= self.max_requests:
                oldest = min(self._requests[key]) if self._requests[key] else now
                retry_after = int(oldest + self.window_seconds - now) + 1
                return False, 0, max(1, retry_after)
            
            self._requests[key].append(now)
            self._cleanup_if_needed()
            
            return True, self.max_requests - current_count - 1, 0
    
    def reset(self, key: str):
        """Reset rate limit for a key."""
        with self._lock:
            self._requests.pop(key, None)


# Global rate limiters
api_limiter = RateLimiter(max_requests=100, window_seconds=60)
auth_limiter = RateLimiter(max_requests=5, window_seconds=300)


def get_client_ip(request) -> str:
    """Extract client IP from request, handling proxies."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    if hasattr(request, "client") and request.client:
        return request.client.host
    return "unknown"


def rate_limit(limiter: Optional[RateLimiter] = None):
    """Decorator for rate limiting FastAPI endpoints.
    
    Usage:
        @app.post("/api/endpoint")
        @rate_limit(api_limiter)
        async def endpoint(request: Request):
            ...
    """
    if limiter is None:
        limiter = api_limiter
    
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Find request object in args/kwargs
            request = None
            for arg in args:
                if hasattr(arg, "headers"):
                    request = arg
                    break
            if not request:
                request = kwargs.get("request")
            
            if request:
                key = get_client_ip(request)
                allowed, remaining, retry_after = limiter.is_allowed(key)
                
                if not allowed:
                    from fastapi.responses import JSONResponse
                    return JSONResponse(
                        status_code=429,
                        content={
                            "ok": False,
                            "error": "Rate limit exceeded",
                            "retry_after": retry_after
                        },
                        headers={
                            "Retry-After": str(retry_after),
                            "X-RateLimit-Remaining": "0"
                        }
                    )
            
            return await func(*args, **kwargs)
        return wrapper
    return decorator
