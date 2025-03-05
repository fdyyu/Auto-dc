import asyncio
from asyncio import Lock
import logging
from typing import Optional, Dict
import time

class BaseLockHandler:
    def __init__(self):
        self._locks: Dict[str, Lock] = {}
        self._response_locks: Dict[str, Lock] = {}
        self._cache = {}
        self._cache_timeout = 30
        self.logger = logging.getLogger(self.__class__.__name__)
        
    async def acquire_lock(self, key: str, timeout: float = 10.0) -> Optional[Lock]:
        """Get or create a lock for the given key"""
        if key not in self._locks:
            self._locks[key] = Lock()
            
        try:
            await asyncio.wait_for(self._locks[key].acquire(), timeout=timeout)
            return self._locks[key]
        except asyncio.TimeoutError:
            self.logger.error(f"Failed to acquire lock for {key} within {timeout} seconds")
            return None
        except Exception as e:
            self.logger.error(f"Error acquiring lock for {key}: {e}")
            return None

    async def acquire_response_lock(self, ctx_or_interaction, timeout: float = 5.0) -> bool:
        """Acquire a response lock for a context or interaction"""
        key = str(ctx_or_interaction.id)
        if key not in self._response_locks:
            self._response_locks[key] = Lock()
            
        try:
            await asyncio.wait_for(self._response_locks[key].acquire(), timeout=timeout)
            return True
        except:
            return False

    def release_lock(self, key: str):
        """Release a lock if it exists"""
        if key in self._locks and self._locks[key].locked():
            self._locks[key].release()

    def release_response_lock(self, ctx_or_interaction):
        """Release a response lock if it exists"""
        key = str(ctx_or_interaction.id)
        if key in self._response_locks and self._response_locks[key].locked():
            self._response_locks[key].release()

    def get_cached(self, key: str):
        """Get cached value if valid"""
        if key in self._cache:
            data = self._cache[key]
            if time.time() - data['timestamp'] < self._cache_timeout:
                return data['value']
            del self._cache[key]
        return None

    def set_cached(self, key: str, value, timeout: Optional[int] = None):
        """Set cache value with timestamp"""
        self._cache[key] = {
            'value': value,
            'timestamp': time.time(),
            'timeout': timeout or self._cache_timeout
        }

    def invalidate_cache(self, key_prefix: str = None):
        """Invalidate cache entries starting with key_prefix"""
        if key_prefix:
            keys_to_delete = [k for k in self._cache.keys() if k.startswith(key_prefix)]
            for key in keys_to_delete:
                del self._cache[key]
        else:
            self._cache.clear()

    def cleanup(self):
        """Cleanup all resources"""
        self._cache.clear()
        self._locks.clear()
        self._response_locks.clear()

class BaseResponseHandler:
    """Mixin for handling responses safely"""
    
    async def send_response_once(self, ctx_or_interaction, **kwargs):
        """Send a response only once"""
        try:
            if hasattr(ctx_or_interaction, 'response'):
                # This is an Interaction
                if not ctx_or_interaction.response.is_done():
                    await ctx_or_interaction.response.send_message(**kwargs)
                else:
                    await ctx_or_interaction.followup.send(**kwargs)
            else:
                # This is a Context
                await ctx_or_interaction.send(**kwargs)
        except Exception as e:
            self.logger.error(f"Error sending response: {e}")