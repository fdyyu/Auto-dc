from typing import Optional, Dict, Any, Set, Union
from datetime import datetime, timedelta
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
    max_memory: int = 0  # Batasan memory dalam bytes
    category: str = 'default'

class EnhancedSmartCacheManager:
    _instance = None
    _lock = asyncio.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.initialized = False
        return cls._instance

    def __init__(self):
        if not self.initialized:
            self.logger = logging.getLogger("EnhancedSmartCacheManager")
            
            # Cache storage dengan prioritas
            self.cache: Dict[str, CacheItem] = OrderedDict()
            
            # Konfigurasi timeout yang lebih detail
            self.timeouts = {
                'balance': 30,      # Data saldo: update cepat
                'stock': 60,        # Stok: update menengah
                'product': 300,     # Produk: jarang update
                'world': 600,       # Info world: update lambat
                'user': 1800,       # Data pengguna: sangat stabil
                'transaction': 45,  # Transaksi: perlu update cepat
                'default': 120      # Default timeout
            }
            
            # Sistem prioritas yang lebih detail
            self.priorities = {
                'balance': 5,    # Prioritas tertinggi - data keuangan
                'stock': 4,      # Prioritas tinggi - inventori
                'product': 3,    # Prioritas menengah
                'world': 2,      # Prioritas rendah
                'user': 1,       # Prioritas terendah
                'transaction': 5, # Prioritas tertinggi - transaksi
                'default': 1
            }
            
            # Batasan cache yang dapat dikonfigurasi
            self.max_size = 1000
            self.cleanup_threshold = 0.85  # Cleanup pada 85% kapasitas
            self.memory_limit = 512 * 1024 * 1024  # 512MB limit
            
            # Metrics yang lebih detail
            self.metrics = {
                'hits': 0,
                'misses': 0,
                'evictions': 0,
                'cleanups': 0,
                'memory_usage': 0,
                'last_cleanup': time.time(),
                'category_stats': {}
            }
            
            # Background task untuk auto cleanup
            self.cleanup_task = None
            self.initialized = True

    async def start_background_tasks(self):
        """Memulai background task untuk maintenance cache"""
        self.cleanup_task = asyncio.create_task(self._periodic_cleanup())

    async def _periodic_cleanup(self):
        """Task periodic untuk membersihkan cache"""
        while True:
            try:
                await asyncio.sleep(300)  # Cleanup setiap 5 menit
                await self._smart_cleanup()
            except Exception as e:
                self.logger.error(f"Error dalam periodic cleanup: {e}")

    async def get(self, key: str, category: str = 'default') -> Optional[Any]:
        """Mengambil item dari cache dengan tracking pintar"""
        try:
            if key in self.cache:
                item = self.cache[key]
                current_time = time.time()
                timeout = self.timeouts.get(category, self.timeouts['default'])
                
                if current_time - item.timestamp < timeout:
                    # Update metrics dan statistik
                    item.hits += 1
                    item.last_access = current_time
                    self.metrics['hits'] += 1
                    
                    # Update statistik kategori
                    if category not in self.metrics['category_stats']:
                        self.metrics['category_stats'][category] = {'hits': 0, 'misses': 0}
                    self.metrics['category_stats'][category]['hits'] += 1
                    
                    # Adaptive timeout
                    if item.hits > 100 and item.hits % 100 == 0:
                        self._adjust_timeout(category, item.hits)
                    
                    return item.value
                    
                await self._remove(key)
                self.metrics['evictions'] += 1
                
            self.metrics['misses'] += 1
            if category in self.metrics['category_stats']:
                self.metrics['category_stats'][category]['misses'] += 1
            return None
            
        except Exception as e:
            self.logger.error(f"Error saat mengambil cache: {e}")
            return None

    def _adjust_timeout(self, category: str, hits: int):
        """Menyesuaikan timeout berdasarkan pola penggunaan"""
        current_timeout = self.timeouts.get(category, self.timeouts['default'])
        if hits > 1000:
            # Untuk item yang sangat sering diakses
            new_timeout = min(current_timeout * 1.5, 7200)  # Max 2 jam
        elif hits > 500:
            new_timeout = min(current_timeout * 1.2, 3600)  # Max 1 jam
        else:
            new_timeout = current_timeout
        
        self.timeouts[category] = new_timeout

    async def set(self, 
                 key: str, 
                 value: Any, 
                 category: str = 'default',
                 max_memory: int = None):
        """Set item cache dengan kontrol memory"""
        async with self._lock:
            try:
                # Cek ukuran value
                value_size = self._estimate_size(value)
                if max_memory and value_size > max_memory:
                    raise ValueError(f"Ukuran value ({value_size} bytes) melebihi batas ({max_memory} bytes)")

                # Cek dan lakukan cleanup jika perlu
                current_memory = self._calculate_total_memory()
                if current_memory + value_size > self.memory_limit:
                    await self._smart_cleanup()

                self.cache[key] = CacheItem(
                    value=value,
                    timestamp=time.time(),
                    priority=self.priorities.get(category, self.priorities['default']),
                    max_memory=max_memory or 0,
                    category=category
                )
                
                self.metrics['memory_usage'] = self._calculate_total_memory()
                
            except Exception as e:
                self.logger.error(f"Error saat menyimpan ke cache: {e}")

    def _estimate_size(self, obj: Any) -> int:
        """Estimasi ukuran objek dalam bytes"""
        try:
            import sys
            return sys.getsizeof(obj)
        except Exception:
            return 0

    def _calculate_total_memory(self) -> int:
        """Menghitung total penggunaan memory cache"""
        return sum(self._estimate_size(item.value) for item in self.cache.values())

    def get_detailed_metrics(self) -> Dict[str, Any]:
        """Mendapatkan metrics detail tentang penggunaan cache"""
        try:
            total_requests = self.metrics['hits'] + self.metrics['misses']
            hit_rate = (self.metrics['hits'] / total_requests * 100) if total_requests > 0 else 0
            
            current_memory = self._calculate_total_memory()
            memory_usage_percent = (current_memory / self.memory_limit * 100) if self.memory_limit > 0 else 0
            
            return {
                **self.metrics,
                'hit_rate': f"{hit_rate:.2f}%",
                'cache_size': len(self.cache),
                'max_size': self.max_size,
                'memory_usage_bytes': current_memory,
                'memory_usage_percent': f"{memory_usage_percent:.2f}%",
                'memory_limit_bytes': self.memory_limit,
                'category_statistics': self.metrics['category_stats']
            }
        except Exception as e:
            self.logger.error(f"Error saat mengambil metrics: {e}")
            return {}