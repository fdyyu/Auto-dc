import logging
import asyncio
from typing import Dict, List, Optional
from datetime import datetime

import discord
from discord.ext import commands

from .constants import (
    Status,          # Untuk status produk
    TransactionError,# Untuk error handling
    CACHE_TIMEOUT,  # Untuk cache produk
    MESSAGES        # Untuk pesan error/success
)
from database import get_connection
from .base_handler import BaseLockHandler
from .cache_manager import CacheManager

class ProductManagerService(BaseLockHandler):
    _instance = None
    _instance_lock = asyncio.Lock()

    def __new__(cls, bot):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.initialized = False
        return cls._instance

    def __init__(self, bot):
        if not self.initialized:
            super().__init__()  # Initialize BaseLockHandler
            self.bot = bot
            self.logger = logging.getLogger("ProductManagerService")
            self.cache_manager = CacheManager()
            self.initialized = True

    async def create_product(self, code: str, name: str, price: int, description: str = None) -> Dict:
        """Create a new product with proper locking and cache invalidation"""
        lock = await self.acquire_lock(f"product_create_{code}")
        if not lock:
            raise TransactionError("System is busy, please try again later")

        conn = None
        try:
            # Check if product already exists
            existing = await self.get_product(code)
            if existing:
                raise TransactionError(f"Product with code '{code}' already exists")

            conn = get_connection()
            cursor = conn.cursor()
            
            cursor.execute(
                """
                INSERT INTO products (code, name, price, description)
                VALUES (?, ?, ?, ?)
                """,
                (code, name, price, description)
            )
            
            conn.commit()
            
            result = {
                'code': code,
                'name': name,
                'price': price,
                'description': description
            }
            
            # Update cache with new system
            await self.cache_manager.set(f"product_{code}", result)
            await self.cache_manager.delete("all_products")  # Invalidate all products cache
            
            self.logger.info(f"Product created: {code}")
            return result

        except Exception as e:
            self.logger.error(f"Error creating product: {e}")
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.close()
            self.release_lock(f"product_create_{code}")

    async def get_product(self, code: str) -> Optional[Dict]:
        """Get product with caching"""
        cache_key = f"product_{code}"
        cached = await self.cache_manager.get(cache_key)
        if cached:
            return cached

        lock = await self.acquire_lock(f"product_get_{code}")
        if not lock:
            self.logger.warning(f"Failed to acquire lock for getting product {code}")
            return None

        try:
            conn = get_connection()
            cursor = conn.cursor()
            
            cursor.execute(
                "SELECT * FROM products WHERE code = ? COLLATE NOCASE",
                (code,)
            )
            
            result = cursor.fetchone()
            if result:
                product = dict(result)
                await self.cache_manager.set(cache_key, product, expires_in=3600)  # Cache for 1 hour
                return product
            return None

        except Exception as e:
            self.logger.error(f"Error getting product: {e}")
            return None
        finally:
            if conn:
                conn.close()
            self.release_lock(f"product_get_{code}")

    async def get_all_products(self) -> List[Dict]:
        """Get all products with caching"""
        cached = await self.cache_manager.get("all_products")
        if cached:
            return cached

        lock = await self.acquire_lock("products_getall")
        if not lock:
            self.logger.warning("Failed to acquire lock for getting all products")
            return []

        try:
            conn = get_connection()
            cursor = conn.cursor()
            
            cursor.execute("SELECT * FROM products ORDER BY code")
            
            products = [dict(row) for row in cursor.fetchall()]
            await self.cache_manager.set("all_products", products, expires_in=300)  # Cache for 5 minutes
            return products

        except Exception as e:
            self.logger.error(f"Error getting all products: {e}")
            return []
        finally:
            if conn:
                conn.close()
            self.release_lock("products_getall")

    async def add_stock_item(self, product_code: str, content: str, added_by: str) -> bool:
        """Add stock item with proper locking"""
        lock = await self.acquire_lock(f"stock_add_{product_code}")
        if not lock:
            raise TransactionError("System is busy, please try again later")

        conn = None
        try:
            conn = get_connection()
            cursor = conn.cursor()
            
            # Verify product exists
            cursor.execute(
                "SELECT code FROM products WHERE code = ? COLLATE NOCASE",
                (product_code,)
            )
            if not cursor.fetchone():
                raise TransactionError(f"Product {product_code} not found")
            
            cursor.execute(
                """
                INSERT INTO stock (product_code, content, added_by, status)
                VALUES (?, ?, ?, ?)
                """,
                (product_code, content, added_by, STATUS_AVAILABLE)
            )
            
            conn.commit()
            
            # Invalidate relevant caches
            await self.cache_manager.delete(f"stock_count_{product_code}")
            await self.cache_manager.delete(f"stock_{product_code}")
            
            self.logger.info(f"Stock added for {product_code}")
            return True

        except Exception as e:
            self.logger.error(f"Error adding stock item: {e}")
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.close()
            self.release_lock(f"stock_add_{product_code}")

    async def get_available_stock(self, product_code: str, quantity: int = 1) -> List[Dict]:
        """Get available stock with proper locking"""
        cache_key = f"stock_{product_code}_q{quantity}"
        cached = await self.cache_manager.get(cache_key)
        if cached:
            return cached

        lock = await self.acquire_lock(f"stock_get_{product_code}")
        if not lock:
            raise TransactionError("System is busy, please try again later")

        try:
            conn = get_connection()
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT id, content, added_at
                FROM stock
                WHERE product_code = ? AND status = ?
                ORDER BY added_at ASC
                LIMIT ?
            """, (product_code, STATUS_AVAILABLE, quantity))
            
            result = [{
                'id': row['id'],
                'content': row['content'],
                'added_at': row['added_at']
            } for row in cursor.fetchall()]

            # Cache for a short time since this is frequently changing data
            await self.cache_manager.set(cache_key, result, expires_in=30)
            return result

        except Exception as e:
            self.logger.error(f"Error getting available stock: {e}")
            raise
        finally:
            if conn:
                conn.close()
            self.release_lock(f"stock_get_{product_code}")

    async def get_stock_count(self, product_code: str) -> int:
        """Get stock count with caching"""
        cache_key = f"stock_count_{product_code}"
        cached = await self.cache_manager.get(cache_key)
        if cached is not None:
            return cached

        lock = await self.acquire_lock(f"stock_count_{product_code}")
        if not lock:
            self.logger.warning(f"Failed to acquire lock for stock count {product_code}")
            return 0

        try:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) as count 
                FROM stock 
                WHERE product_code = ? AND status = ?
            """, (product_code, STATUS_AVAILABLE))
            
            result = cursor.fetchone()['count']
            await self.cache_manager.set(cache_key, result, expires_in=30)  # Cache for 30 seconds
            return result

        except Exception as e:
            self.logger.error(f"Error getting stock count: {e}")
            return 0
        finally:
            if conn:
                conn.close()
            self.release_lock(f"stock_count_{product_code}")

    async def update_stock_status(self, stock_id: int, status: str, buyer_id: str = None) -> bool:
        """Update stock status with proper locking"""
        lock = await self.acquire_lock(f"stock_update_{stock_id}")
        if not lock:
            raise TransactionError("System is busy, please try again later")

        conn = None
        try:
            conn = get_connection()
            cursor = conn.cursor()
            
            # Get product code first for cache invalidation
            cursor.execute("SELECT product_code FROM stock WHERE id = ?", (stock_id,))
            product_result = cursor.fetchone()
            if not product_result:
                raise TransactionError(f"Stock item {stock_id} not found")
            
            product_code = product_result['product_code']
            
            update_query = """
                UPDATE stock 
                SET status = ?, updated_at = CURRENT_TIMESTAMP
            """
            params = [status]

            if buyer_id:
                update_query += ", buyer_id = ?"
                params.append(buyer_id)

            update_query += " WHERE id = ?"
            params.append(stock_id)

            cursor.execute(update_query, params)
            conn.commit()
            
            # Invalidate relevant caches
            await self.cache_manager.delete(f"stock_count_{product_code}")
            await self.cache_manager.delete(f"stock_{product_code}")
            # Also invalidate any quantity specific caches
            for i in range(1, 101):  # Reasonable range for quantities
                await self.cache_manager.delete(f"stock_{product_code}_q{i}")
            
            self.logger.info(f"Stock {stock_id} status updated to {status}")
            return True

        except Exception as e:
            self.logger.error(f"Error updating stock status: {e}")
            if conn:
                conn.rollback()
            return False
        finally:
            if conn:
                conn.close()
            self.release_lock(f"stock_update_{stock_id}")

    async def get_world_info(self) -> Optional[Dict]:
        """Get world info with caching"""
        cached = await self.cache_manager.get("world_info")
        if cached:
            return cached

        lock = await self.acquire_lock("world_info_get")
        if not lock:
            self.logger.warning("Failed to acquire lock for world info")
            return None

        try:
            conn = get_connection()
            cursor = conn.cursor()
            
            cursor.execute("SELECT * FROM world_info WHERE id = 1")
            result = cursor.fetchone()
            
            if result:
                info = dict(result)
                await self.cache_manager.set("world_info", info, expires_in=300)  # Cache for 5 minutes
                return info
            return None

        except Exception as e:
            self.logger.error(f"Error getting world info: {e}")
            return None
        finally:
            if conn:
                conn.close()
            self.release_lock("world_info_get")

    async def update_world_info(self, world: str, owner: str, bot: str) -> bool:
        """Update world info with proper locking"""
        lock = await self.acquire_lock("world_info_update")
        if not lock:
            raise TransactionError("System is busy, please try again later")

        conn = None
        try:
            conn = get_connection()
            cursor = conn.cursor()
            
            cursor.execute("""
                UPDATE world_info 
                SET world = ?, owner = ?, bot = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = 1
            """, (world, owner, bot))
            
            conn.commit()
            
            # Invalidate cache
            await self.cache_manager.delete("world_info")
            
            self.logger.info("World info updated")
            return True

        except Exception as e:
            self.logger.error(f"Error updating world info: {e}")
            if conn:
                conn.rollback()
            return False
        finally:
            if conn:
                conn.close()
            self.release_lock("world_info_update")

class ProductManagerCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.product_service = ProductManagerService(bot)
        self.logger = logging.getLogger("ProductManagerCog")

    async def cog_load(self):
        self.logger.info("ProductManagerCog loading...")

    async def cog_unload(self):
        await self.product_service.cleanup()
        self.logger.info("ProductManagerCog unloaded")

async def setup(bot):
    if not hasattr(bot, 'product_manager_loaded'):
        await bot.add_cog(ProductManagerCog(bot))
        bot.product_manager_loaded = True
        logging.info(
            f'ProductManager cog loaded successfully at '
            f'{datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")} UTC'
        )