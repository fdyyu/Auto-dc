from typing import Optional, Dict, Any, Set
from datetime import datetime
import time
import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass

@dataclass
class CacheItem:
    value: Any
    timestamp: float
    priority: int
    hits: int = 0
    last_access: float = time.time()

class SmartCacheManager:
    _instance = None
    _lock = asyncio.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.initialized = False
        return cls._instance

    def __init__(self):
        if not self.initialized:
            self.logger = logging.getLogger("SmartCacheManager")
            
            # Cache storage dengan prioritas
            self.cache: Dict[str, CacheItem] = OrderedDict()
            
            # Konfigurasi timeout per kategori
            self.timeouts = {
                'balance': 30,      # Balance: update cepat
                'stock': 60,        # Stock: moderate update
                'product': 300,     # Product: jarang update
                'world': 600,       # World info: update lambat
                'user': 1800,       # User data: sangat stabil
                'default': 120      # Default timeout
            }
            
            # Prioritas kategori (1-5, 5 tertinggi)
            self.priorities = {
                'balance': 5,       # High priority - financial data
                'stock': 4,         # High priority - inventory
                'product': 3,       # Medium priority
                'world': 2,         # Low priority
                'user': 1,          # Lowest priority
                'default': 1
            }
            
            # Cache size limits
            self.max_size = 1000
            self.cleanup_threshold = 0.9  # 90% full trigger cleanup
            
            # Metrics collection
            self.metrics = {
                'hits': 0,
                'misses': 0,
                'evictions': 0,
                'cleanups': 0
            }
            
            self.initialized = True

    async def get(self, key: str, category: str = 'default') -> Optional[Any]:
        """Get item from cache with smart tracking"""
        try:
            if key in self.cache:
                item = self.cache[key]
                current_time = time.time()
                timeout = self.timeouts.get(category, self.timeouts['default'])
                
                if current_time - item.timestamp < timeout:
                    # Update metrics
                    item.hits += 1
                    item.last_access = current_time
                    self.metrics['hits'] += 1
                    
                    # Adaptive timeout for frequently accessed items
                    if item.hits > 100:
                        self.timeouts[category] = min(timeout * 1.2, 3600)
                    
                    return item.value
                    
                # Item expired
                await self._remove(key)
                self.metrics['evictions'] += 1
                
            self.metrics['misses'] += 1
            return None
            
        except Exception as e:
            self.logger.error(f"Cache get error: {e}")
            return None

    async def set(self, key: str, value: Any, category: str = 'default'):
        """Set cache item with smart prioritization"""
        async with self._lock:
            try:
                # Check cache size and cleanup if needed
                if len(self.cache) >= self.max_size * self.cleanup_threshold:
                    await self._smart_cleanup()
                
                # Set item with priority
                self.cache[key] = CacheItem(
                    value=value,
                    timestamp=time.time(),
                    priority=self.priorities.get(category, self.priorities['default'])
                )
                
            except Exception as e:
                self.logger.error(f"Cache set error: {e}")

    async def _smart_cleanup(self):
        """Smart cleanup based on priority, access time, and hits"""
        try:
            if len(self.cache) < self.max_size * self.cleanup_threshold:
                return

            self.metrics['cleanups'] += 1
            
            # Calculate score for each item
            scores = {}
            current_time = time.time()
            
            for key, item in self.cache.items():
                age = current_time - item.timestamp
                last_access = current_time - item.last_access
                
                # Score formula:
                # Higher priority items get higher scores
                # Recently accessed items get higher scores
                # Items with more hits get higher scores
                score = (
                    item.priority * 1000 +
                    item.hits * 100 +
                    (1 / (last_access + 1)) * 10 -
                    (age / 3600)  # Age in hours
                )
                scores[key] = score
            
            # Remove lowest scoring items until we're below threshold
            sorted_items = sorted(scores.items(), key=lambda x: x[1])
            items_to_remove = len(self.cache) - int(self.max_size * 0.7)  # Remove until 70% full
            
            for key, _ in sorted_items[:items_to_remove]:
                await self._remove(key)
                self.metrics['evictions'] += 1
                
        except Exception as e:
            self.logger.error(f"Cache cleanup error: {e}")

    async def _remove(self, key: str):
        """Remove item from cache"""
        try:
            if key in self.cache:
                del self.cache[key]
        except Exception as e:
            self.logger.error(f"Cache remove error: {e}")

    def get_metrics(self) -> Dict[str, Any]:
        """Get cache performance metrics"""
        try:
            total_requests = self.metrics['hits'] + self.metrics['misses']
            hit_rate = (self.metrics['hits'] / total_requests * 100) if total_requests > 0 else 0
            
            return {
                **self.metrics,
                'hit_rate': f"{hit_rate:.2f}%",
                'cache_size': len(self.cache),
                'max_size': self.max_size
            }
        except Exception as e:
            self.logger.error(f"Error getting metrics: {e}")
            return {}