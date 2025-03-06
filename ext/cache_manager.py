import logging
import time
import json
from typing import Optional, Any, Dict
from datetime import datetime, timedelta
from sqlite3 import Connection, Error as SQLiteError
from database import get_connection
import asyncio
from functools import wraps

logger = logging.getLogger(__name__)

class CacheManager:
    """
    Enhanced Cache Manager dengan Database Integration
    """
    _instance = None
    _lock = asyncio.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if not hasattr(self, 'initialized'):
            self.memory_cache: Dict[str, Dict] = {}
            self.logger = logging.getLogger('CacheManager')
            self.initialized = True
    
    async def get(self, key: str, default: Any = None) -> Optional[Any]:
        """
        Ambil data dari cache (memory atau database)
        """
        try:
            # Cek memory cache dulu
            if key in self.memory_cache:
                cache_data = self.memory_cache[key]
                if self._is_valid(cache_data):
                    self.logger.debug(f"Cache hit (memory): {key}")
                    return cache_data['value']
                else:
                    # Hapus cache yang expired
                    del self.memory_cache[key]
            
            # Jika tidak ada di memory, cek database
            async with self._lock:
                conn = get_connection()
                try:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT value, expires_at FROM cache_table WHERE key = ?",
                        (key,)
                    )
                    result = cursor.fetchone()
                    
                    if result:
                        value, expires_at = result
                        expires_at = datetime.fromisoformat(expires_at)
                        
                        if expires_at > datetime.utcnow():
                            # Cache masih valid
                            try:
                                decoded_value = json.loads(value)
                                # Simpan ke memory cache
                                self.memory_cache[key] = {
                                    'value': decoded_value,
                                    'expires_at': expires_at
                                }
                                self.logger.debug(f"Cache hit (database): {key}")
                                return decoded_value
                            except json.JSONDecodeError:
                                self.logger.warning(f"Failed to decode cache value for key: {key}")
                                return value
                        else:
                            # Hapus cache yang expired
                            cursor.execute("DELETE FROM cache_table WHERE key = ?", (key,))
                            conn.commit()
                    
                    return default
                    
                except SQLiteError as e:
                    self.logger.error(f"Database error in get: {e}")
                    return default
                finally:
                    conn.close()
        
        except Exception as e:
            self.logger.error(f"Error in get: {e}")
            return default
    
    async def set(self, 
                  key: str, 
                  value: Any, 
                  expires_in: int = 3600,
                  permanent: bool = False) -> bool:
        """
        Simpan data ke cache
        
        Args:
            key: Kunci cache
            value: Nilai yang akan disimpan
            expires_in: Waktu kadaluarsa dalam detik (default 1 jam)
            permanent: Jika True, simpan ke database (default False)
        """
        try:
            expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
            
            # Simpan ke memory cache
            self.memory_cache[key] = {
                'value': value,
                'expires_at': expires_at
            }
            
            # Jika permanent, simpan juga ke database
            if permanent:
                async with self._lock:
                    conn = get_connection()
                    try:
                        cursor = conn.cursor()
                        
                        # Konversi value ke JSON jika perlu
                        if not isinstance(value, (str, int, float, bool)):
                            value = json.dumps(value)
                            
                        cursor.execute("""
                            INSERT OR REPLACE INTO cache_table (key, value, expires_at)
                            VALUES (?, ?, ?)
                        """, (key, value, expires_at.isoformat()))
                        
                        conn.commit()
                        self.logger.debug(f"Cache set (permanent): {key}")
                        return True
                        
                    except SQLiteError as e:
                        self.logger.error(f"Database error in set: {e}")
                        return False
                    finally:
                        conn.close()
            
            self.logger.debug(f"Cache set (memory): {key}")
            return True
            
        except Exception as e:
            self.logger.error(f"Error in set: {e}")
            return False
    
    async def delete(self, key: str) -> bool:
        """Hapus item dari cache"""
        try:
            # Hapus dari memory cache
            if key in self.memory_cache:
                del self.memory_cache[key]
            
            # Hapus dari database
            async with self._lock:
                conn = get_connection()
                try:
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM cache_table WHERE key = ?", (key,))
                    conn.commit()
                    return True
                except SQLiteError as e:
                    self.logger.error(f"Database error in delete: {e}")
                    return False
                finally:
                    conn.close()
                    
        except Exception as e:
            self.logger.error(f"Error in delete: {e}")
            return False
    
    async def clear(self) -> bool:
        """Bersihkan semua cache"""
        try:
            # Bersihkan memory cache
            self.memory_cache.clear()
            
            # Bersihkan database cache
            async with self._lock:
                conn = get_connection()
                try:
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM cache_table")
                    conn.commit()
                    return True
                except SQLiteError as e:
                    self.logger.error(f"Database error in clear: {e}")
                    return False
                finally:
                    conn.close()
                    
        except Exception as e:
            self.logger.error(f"Error in clear: {e}")
            return False
    
    async def cleanup(self) -> None:
        """Bersihkan cache yang expired"""
        try:
            # Bersihkan memory cache
            current_time = datetime.utcnow()
            expired_keys = [
                key for key, data in self.memory_cache.items()
                if data['expires_at'] <= current_time
            ]
            for key in expired_keys:
                del self.memory_cache[key]
            
            # Bersihkan database cache
            async with self._lock:
                conn = get_connection()
                try:
                    cursor = conn.cursor()
                    cursor.execute(
                        "DELETE FROM cache_table WHERE expires_at < ?",
                        (current_time.isoformat(),)
                    )
                    conn.commit()
                except SQLiteError as e:
                    self.logger.error(f"Database error in cleanup: {e}")
                finally:
                    conn.close()
                    
        except Exception as e:
            self.logger.error(f"Error in cleanup: {e}")
    
    def _is_valid(self, cache_data: Dict) -> bool:
        """Cek apakah cache masih valid"""
        return cache_data['expires_at'] > datetime.utcnow()

    async def get_stats(self) -> Dict:
        """Dapatkan statistik cache"""
        try:
            memory_cache_size = len(self.memory_cache)
            memory_cache_valid = sum(
                1 for data in self.memory_cache.values()
                if self._is_valid(data)
            )
            
            async with self._lock:
                conn = get_connection()
                try:
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM cache_table")
                    db_cache_size = cursor.fetchone()[0]
                    
                    cursor.execute(
                        "SELECT COUNT(*) FROM cache_table WHERE expires_at > ?",
                        (datetime.utcnow().isoformat(),)
                    )
                    db_cache_valid = cursor.fetchone()[0]
                    
                    return {
                        'memory_cache': {
                            'total': memory_cache_size,
                            'valid': memory_cache_valid,
                            'expired': memory_cache_size - memory_cache_valid
                        },
                        'db_cache': {
                            'total': db_cache_size,
                            'valid': db_cache_valid,
                            'expired': db_cache_size - db_cache_valid
                        }
                    }
                finally:
                    conn.close()
                    
        except Exception as e:
            self.logger.error(f"Error getting cache stats: {e}")
            return {}

# Decorator untuk caching
def cached(expires_in: int = 3600, permanent: bool = False):
    """
    Decorator untuk caching fungsi
    
    Args:
        expires_in: Waktu kadaluarsa dalam detik (default 1 jam)
        permanent: Jika True, simpan ke database (default False)
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            cache_key = f"{func.__name__}:{hash(str(args))}-{hash(str(kwargs))}"
            cache_manager = CacheManager()
            
            # Coba ambil dari cache
            cached_value = await cache_manager.get(cache_key)
            if cached_value is not None:
                return cached_value
            
            # Jika tidak ada di cache, eksekusi fungsi
            result = await func(*args, **kwargs) if asyncio.iscoroutinefunction(func) else func(*args, **kwargs)
            
            # Simpan ke cache
            await cache_manager.set(cache_key, result, expires_in, permanent)
            
            return result
        return wrapper
    return decorator