"""Gretta AI — Caching layer for improved performance.

Provides in-memory caching with TTL and cache invalidation.
"""
import time
import threading
from typing import Any, Optional, Dict, Callable
from functools import wraps
from collections import OrderedDict

from logger import get_logger

log = get_logger(__name__)


class LRUCache:
    """Thread-safe LRU cache with TTL support."""
    
    def __init__(self, max_size: int = 1000, default_ttl: int = 300):
        """Initialize cache.
        
        Args:
            max_size: Maximum number of entries
            default_ttl: Default time-to-live in seconds
        """
        self.max_size = max_size
        self.default_ttl = default_ttl
        self._cache: OrderedDict = OrderedDict()
        self._timestamps: Dict[str, float] = {}
        self._lock = threading.RLock()
    
    def get(self, key: str) -> Optional[Any]:
        """Get value from cache if not expired."""
        with self._lock:
            if key not in self._cache:
                return None
            
            # Check TTL
            if self._is_expired(key):
                self._remove(key)
                return None
            
            # Move to end (most recently used)
            self._cache.move_to_end(key)
            return self._cache[key]
    
    def set(self, key: str, value: Any, ttl: Optional[int] = None):
        """Set value in cache with optional TTL override."""
        with self._lock:
            # Remove if exists to update position
            if key in self._cache:
                self._remove(key)
            
            # Evict oldest if at capacity
            while len(self._cache) >= self.max_size:
                oldest_key = next(iter(self._cache))
                self._remove(oldest_key)
            
            self._cache[key] = value
            self._timestamps[key] = time.time() + (ttl or self.default_ttl)
    
    def delete(self, key: str):
        """Remove a key from cache."""
        with self._lock:
            self._remove(key)
    
    def clear(self):
        """Clear all cache entries."""
        with self._lock:
            self._cache.clear()
            self._timestamps.clear()
    
    def _is_expired(self, key: str) -> bool:
        """Check if key has expired."""
        if key not in self._timestamps:
            return False
        return time.time() > self._timestamps[key]
    
    def _remove(self, key: str):
        """Remove key from cache and timestamp tracking."""
        self._cache.pop(key, None)
        self._timestamps.pop(key, None)
    
    def invalidate_prefix(self, prefix: str):
        """Invalidate all keys starting with prefix (for pattern-based invalidation)."""
        with self._lock:
            keys_to_remove = [k for k in self._cache.keys() if k.startswith(prefix)]
            for key in keys_to_remove:
                self._remove(key)
    
    def stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        with self._lock:
            return {
                "size": len(self._cache),
                "max_size": self.max_size,
                "hit_rate": self._hit_rate,
            }
    
    @property
    def _hit_rate(self) -> float:
        """Calculate hit rate (simplified)."""
        return 0.0  # Would need hit/miss tracking for real rate


class Cache:
    """Application-wide cache manager."""
    
    def __init__(self):
        # Different caches for different data types
        self.leads = LRUCache(max_size=500, default_ttl=60)
        self.stats = LRUCache(max_size=10, default_ttl=30)
        self.users = LRUCache(max_size=100, default_ttl=120)
        self.ai_responses = LRUCache(max_size=200, default_ttl=300)
    
    def invalidate_leads(self):
        """Invalidate all lead-related caches."""
        self.leads.clear()
        self.stats.clear()
    
    def invalidate_users(self):
        """Invalidate user caches."""
        self.users.clear()


# Global cache instance
cache = Cache()


def cached(cache_instance: LRUCache, key_builder: Optional[Callable] = None):
    """Decorator for caching function results.
    
    Usage:
        @cached(cache.leads, lambda args: f"lead:{args[0]}")
        def get_lead(username):
            ...
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Build cache key
            if key_builder:
                cache_key = key_builder(*args, **kwargs)
            else:
                cache_key = f"{func.__name__}:{str(args)}:{str(kwargs)}"
            
            # Try cache first
            result = cache_instance.get(cache_key)
            if result is not None:
                return result
            
            # Call function and cache result
            result = func(*args, **kwargs)
            cache_instance.set(cache_key, result)
            return result
        
        # Add cache methods to function
        wrapper.invalidate = lambda: cache_instance.delete(cache_key if key_builder else f"{func.__name__}:{str(args)}:{str(kwargs)}")
        return wrapper
    return decorator


def cache_invalidate_on_write(cache_instance: LRUCache):
    """Decorator to invalidate related caches after a write operation."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            result = func(*args, **kwargs)
            # Invalidate the entire cache after writes
            cache_instance.clear()
            return result
        return wrapper
    return decorator