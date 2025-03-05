import logging
import asyncio
import time
import io
from typing import Dict, List, Optional
from datetime import datetime

import discord
from discord.ext import commands

from .constants import STATUS_AVAILABLE, STATUS_SOLD, TransactionError
from database import get_connection

class TransactionManager:
    _instance = None
    _lock = asyncio.Lock()

    def __new__(cls, bot):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.initialized = False
        return cls._instance

    def __init__(self, bot):
        if not self.initialized:
            self.bot = bot
            self.logger = logging.getLogger("TransactionManager")
            self._cache = {}
            self._cache_timeout = 30
            self._transaction_locks = {}
            self._response_locks = {}
            self.initialized = True

    def _get_cached(self, key: str):
        """Get cached value if valid"""
        if key in self._cache:
            data = self._cache[key]
            if time.time() - data['timestamp'] < self._cache_timeout:
                return data['value']
            del self._cache[key]
        return None

    def _set_cached(self, key: str, value):
        """Set cache value with timestamp"""
        self._cache[key] = {
            'value': value,
            'timestamp': time.time()
        }

    async def _get_transaction_lock(self, key: str) -> asyncio.Lock:
        """Get or create a transaction lock"""
        async with self._lock:
            if key not in self._transaction_locks:
                self._transaction_locks[key] = asyncio.Lock()
            return self._transaction_locks[key]

    async def _get_response_lock(self, key: str) -> asyncio.Lock:
        """Get or create a response lock"""
        async with self._lock:
            if key not in self._response_locks:
                self._response_locks[key] = asyncio.Lock()
            return self._response_locks[key]

    async def send_purchase_result(self, user: discord.User, items: list, product_name: str) -> bool:
        """Send purchase result to user via DM as txt file"""
        response_lock = await self._get_response_lock(f"dm_{user.id}")
        async with response_lock:
            try:
                # Create txt content
                content = (
                    f"Purchase Result for {user.name}\n"
                    f"Date: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
                    f"Product: {product_name}\n"
                    f"{'-' * 50}\n\n"
                )
                
                for idx, item in enumerate(items, 1):
                    content += f"Item {idx}:\n{item['content']}\n\n"
                
                # Create txt file with timestamp
                filename = f"result_{user.name}_{int(time.time())}.txt"
                file = discord.File(
                    io.StringIO(content),
                    filename=filename
                )
                
                await user.send("Here is your purchase result:", file=file)
                self.logger.info(f"Purchase result sent to user {user.name} ({user.id})")
                return True
                
            except discord.Forbidden:
                self.logger.warning(f"Cannot send DM to user {user.name} ({user.id})")
                return False
            except Exception as e:
                self.logger.error(f"Error sending purchase result: {e}")
                return False

    async def process_purchase(self, growid: str, product_code: str, quantity: int = 1) -> Optional[Dict]:
        """Process a purchase with proper locking and validation"""
        lock_key = f"purchase_{growid}_{product_code}"
        transaction_lock = await self._get_transaction_lock(lock_key)
        
        async with transaction_lock:
            conn = None
            try:
                conn = get_connection()
                cursor = conn.cursor()
                
                # Get product details with cache
                cache_key = f"product_{product_code}"
                product = self._get_cached(cache_key)
                
                if not product:
                    cursor.execute(
                        "SELECT price, name FROM products WHERE code = ? COLLATE NOCASE",
                        (product_code,)
                    )
                    product = cursor.fetchone()
                    if not product:
                        raise TransactionError(f"Product {product_code} not found")
                    self._set_cached(cache_key, dict(product))
                
                total_price = product['price'] * quantity
                
                # Get available stock
                cursor.execute("""
                    SELECT id, content 
                    FROM stock 
                    WHERE product_code = ? AND status = ?
                    ORDER BY added_at ASC
                    LIMIT ?
                """, (product_code, STATUS_AVAILABLE, quantity))
                
                stock_items = cursor.fetchall()
                if len(stock_items) < quantity:
                    raise TransactionError(f"Insufficient stock for {product_code}")
                
                # Get user balance - case-sensitive
                cursor.execute(
                    "SELECT balance_wl FROM users WHERE growid = ? COLLATE binary",
                    (growid,)
                )
                user = cursor.fetchone()
                if not user:
                    raise TransactionError(f"User {growid} not found")
                
                if user['balance_wl'] < total_price:
                    raise TransactionError("Insufficient balance")
                
                # Update stock status
                stock_ids = [item['id'] for item in stock_items]
                placeholders = ','.join('?' * len(stock_ids))
                cursor.execute(f"""
                    UPDATE stock 
                    SET status = ?, buyer_id = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id IN ({placeholders})
                """, [STATUS_SOLD, growid] + stock_ids)
                
                # Update user balance
                new_balance = user['balance_wl'] - total_price
                cursor.execute(
                    "UPDATE users SET balance_wl = ? WHERE growid = ? COLLATE binary",
                    (new_balance, growid)
                )
                
                # Record transaction
                cursor.execute(
                    """
                    INSERT INTO transactions 
                    (growid, type, details, old_balance, new_balance, items_count, total_price)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        growid,
                        'PURCHASE',
                        f"Purchased {quantity} {product_code}",
                        f"{user['balance_wl']} WL",
                        f"{new_balance} WL",
                        quantity,
                        total_price
                    )
                )
                
                conn.commit()
                
                # Invalidate relevant caches
                self._cache.pop(f"balance_{growid}", None)
                self._cache.pop(f"stock_{product_code}", None)
                
                return {
                    'success': True,
                    'items': [dict(item) for item in stock_items],
                    'total_price': total_price,
                    'new_balance': new_balance,
                    'product_name': product['name']
                }

            except Exception as e:
                self.logger.error(f"Error processing purchase: {e}")
                if conn:
                    conn.rollback()
                raise
            finally:
                if conn:
                    conn.close()

    async def get_transaction_history(self, growid: str, limit: int = 10) -> List[Dict]:
        """Get transaction history with caching"""
        cache_key = f"trx_history_{growid}_{limit}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        async with await self._get_transaction_lock(f"history_{growid}"):
            try:
                conn = get_connection()
                cursor = conn.cursor()
                
                cursor.execute("""
                    SELECT * FROM transactions 
                    WHERE growid = ? COLLATE binary
                    ORDER BY created_at DESC
                    LIMIT ?
                """, (growid, limit))
                
                results = [dict(row) for row in cursor.fetchall()]
                self._set_cached(cache_key, results)
                return results

            except Exception as e:
                self.logger.error(f"Error getting transaction history: {e}")
                return []
            finally:
                if conn:
                    conn.close()

    async def get_stock_history(self, product_code: str, limit: int = 10) -> List[Dict]:
        """Get stock history with caching"""
        cache_key = f"stock_history_{product_code}_{limit}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        async with await self._get_transaction_lock(f"stock_{product_code}"):
            try:
                conn = get_connection()
                cursor = conn.cursor()
                
                cursor.execute("""
                    SELECT * FROM stock 
                    WHERE product_code = ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                """, (product_code, limit))
                
                results = [dict(row) for row in cursor.fetchall()]
                self._set_cached(cache_key, results)
                return results

            except Exception as e:
                self.logger.error(f"Error getting stock history: {e}")
                return []
            finally:
                if conn:
                    conn.close()

    async def cleanup(self):
        """Cleanup resources"""
        self._cache.clear()
        self._transaction_locks.clear()
        self._response_locks.clear()
        
class TransactionCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.trx_manager = TransactionManager(bot)
        self.logger = logging.getLogger("TransactionCog")

    @commands.Cog.listener()
    async def on_ready(self):
        self.logger.info(f"TransactionCog is ready at {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")

async def setup(bot):
    """Setup the Transaction cog"""
    if not hasattr(bot, 'transaction_cog_loaded'):
        await bot.add_cog(TransactionCog(bot))
        bot.transaction_cog_loaded = True
        logging.info(f'Transaction cog loaded successfully at {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC')