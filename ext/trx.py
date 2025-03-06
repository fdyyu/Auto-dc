import logging
import asyncio
from typing import Optional, Dict, List, Union
from datetime import datetime

import discord
from discord.ext import commands
from .constants import (
    Status,          # Untuk STATUS_AVAILABLE, STATUS_SOLD
    TransactionType, # Untuk tipe transaksi
    Balance,        # Untuk manajemen balance
    TransactionError, # Untuk error handling
    MESSAGES        # Untuk pesan error/success
)
from database import get_connection
from .base_handler import BaseLockHandler
from .cache_manager import CacheManager
from .product_manager import ProductManagerService
from .balance_manager import BalanceManagerService

class TransactionManager(BaseLockHandler):
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
            self.logger = logging.getLogger("TransactionManager")
            self.cache_manager = CacheManager()
            self.product_manager = ProductManagerService(bot)
            self.balance_manager = BalanceManagerService(bot)
            self.initialized = True

    async def process_purchase(
        self, 
        buyer_id: str, 
        product_code: str, 
        quantity: int = 1
    ) -> Dict[str, Union[str, List[str], int]]:
        """
        Process a purchase transaction with proper locking and validation
        Returns dict with status, message, and content list if successful
        """
        lock = await self.acquire_lock(f"purchase_{buyer_id}_{product_code}")
        if not lock:
            raise TransactionError("System is busy processing another transaction")

        conn = None
        try:
            # Get buyer's GrowID and verify registration
            growid = await self.balance_manager.get_growid(buyer_id)
            if not growid:
                raise TransactionError("You need to register your GrowID first!")

            # Verify product exists and get details
            product = await self.product_manager.get_product(product_code)
            if not product:
                raise TransactionError(f"Product {product_code} not found")

            # Check stock availability
            available_stock = await self.product_manager.get_available_stock(product_code, quantity)
            if not available_stock or len(available_stock) < quantity:
                raise TransactionError(
                    f"Insufficient stock! Only {len(available_stock)} available"
                )

            # Calculate total price
            total_price = product['price'] * quantity

            # Get current balance
            balance = await self.balance_manager.get_balance(growid)
            if not balance:
                raise TransactionError("Could not retrieve balance")

            # Convert price to WL if needed and check balance
            total_wl = total_price
            if total_wl > balance.total_wl():
                raise TransactionError(
                    f"Insufficient balance! Need {total_wl:,} WL, you have {balance.total_wl():,} WL"
                )

            conn = get_connection()
            cursor = conn.cursor()
            
            try:
                # Begin transaction
                conn.execute("BEGIN TRANSACTION")

                # Update stock status
                stock_ids = [item['id'] for item in available_stock[:quantity]]
                content_list = [item['content'] for item in available_stock[:quantity]]

                cursor.executemany(
                    """
                    UPDATE stock 
                    SET status = ?, buyer_id = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    [(STATUS_SOLD, buyer_id, stock_id) for stock_id in stock_ids]
                )

                # Update balance
                new_balance = await self.balance_manager.update_balance(
                    growid=growid,
                    wl=-total_wl,
                    details=f"Purchase {quantity}x {product['name']}",
                    transaction_type=TransactionType.PURCHASE.value
                )

                # Record transaction
                cursor.execute(
                    """
                    INSERT INTO transactions 
                    (growid, type, details, old_balance, new_balance, created_at)
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (
                        growid,
                        TransactionType.PURCHASE.value,
                        f"Purchased {quantity}x {product['name']} for {total_wl:,} WL",
                        balance.format(),
                        new_balance.format()
                    )
                )

                conn.commit()

                # Invalidate relevant caches
                await self.cache_manager.delete(f"stock_count_{product_code}")
                await self.cache_manager.delete(f"stock_{product_code}")
                await self.cache_manager.delete(f"balance_{growid}")
                await self.cache_manager.delete(f"trx_history_{growid}")

                self.logger.info(
                    f"Purchase successful: {growid} bought {quantity}x {product_code}"
                )

                return {
                    'status': 'success',
                    'message': (
                        f"Successfully purchased {quantity}x {product['name']}\n"
                        f"Total paid: {total_wl:,} WL\n"
                        f"New balance: {new_balance.format()}"
                    ),
                    'content': content_list,
                    'total_paid': total_wl
                }

            except Exception as e:
                conn.rollback()
                raise TransactionError(f"Transaction failed: {str(e)}")

        except TransactionError as e:
            raise
        except Exception as e:
            self.logger.error(f"Error processing purchase: {e}")
            raise TransactionError("An unexpected error occurred")
        finally:
            if conn:
                conn.close()
            self.release_lock(f"purchase_{buyer_id}_{product_code}")

    async def process_deposit(
        self, 
        user_id: str, 
        wl: int = 0, 
        dl: int = 0, 
        bgl: int = 0,
        admin_id: Optional[str] = None
    ) -> Dict[str, Union[str, Balance]]:
        """Process a deposit transaction with proper locking"""
        lock = await self.acquire_lock(f"deposit_{user_id}")
        if not lock:
            raise TransactionError("System is busy processing another transaction")

        try:
            # Verify user registration
            growid = await self.balance_manager.get_growid(user_id)
            if not growid:
                raise TransactionError("You need to register your GrowID first!")

            # Calculate total deposit in WL
            total_wl = wl + (dl * 100) + (bgl * 10000)
            if total_wl <= 0:
                raise TransactionError("Deposit amount must be greater than 0")

            # Process deposit
            details = f"Deposit: {wl} WL"
            if dl > 0:
                details += f", {dl} DL"
            if bgl > 0:
                details += f", {bgl} BGL"
            if admin_id:
                admin_name = self.bot.get_user(int(admin_id))
                details += f" (by {admin_name})"

            new_balance = await self.balance_manager.update_balance(
                growid=growid,
                wl=wl,
                dl=dl,
                bgl=bgl,
                details=details,
                transaction_type=TransactionType.DEPOSIT.value
            )

            self.logger.info(
                f"Deposit successful: {growid} deposited {total_wl:,} WL"
            )

            return {
                'status': 'success',
                'message': (
                    f"Successfully deposited:\n"
                    f"{wl:,} WL{f', {dl:,} DL' if dl > 0 else ''}"
                    f"{f', {bgl:,} BGL' if bgl > 0 else ''}\n"
                    f"New balance: {new_balance.format()}"
                ),
                'new_balance': new_balance
            }

        except TransactionError as e:
            raise
        except Exception as e:
            self.logger.error(f"Error processing deposit: {e}")
            raise TransactionError("An unexpected error occurred")
        finally:
            self.release_lock(f"deposit_{user_id}")

    async def process_withdrawal(
        self, 
        user_id: str, 
        wl: int = 0, 
        dl: int = 0, 
        bgl: int = 0,
        admin_id: Optional[str] = None
    ) -> Dict[str, Union[str, Balance]]:
        """Process a withdrawal transaction with proper locking"""
        lock = await self.acquire_lock(f"withdrawal_{user_id}")
        if not lock:
            raise TransactionError("System is busy processing another transaction")

        try:
            # Verify user registration
            growid = await self.balance_manager.get_growid(user_id)
            if not growid:
                raise TransactionError("You need to register your GrowID first!")

            # Get current balance
            current_balance = await self.balance_manager.get_balance(growid)
            if not current_balance:
                raise TransactionError("Could not retrieve balance")

            # Calculate total withdrawal in WL
            total_wl = wl + (dl * 100) + (bgl * 10000)
            if total_wl <= 0:
                raise TransactionError("Withdrawal amount must be greater than 0")

            # Check if sufficient balance
            if total_wl > current_balance.total_wl():
                raise TransactionError(
                    f"Insufficient balance! You have {current_balance.total_wl():,} WL"
                )

            # Process withdrawal
            details = f"Withdrawal: {wl} WL"
            if dl > 0:
                details += f", {dl} DL"
            if bgl > 0:
                details += f", {bgl} BGL"
            if admin_id:
                admin_name = self.bot.get_user(int(admin_id))
                details += f" (by {admin_name})"

            new_balance = await self.balance_manager.update_balance(
                growid=growid,
                wl=-wl,
                dl=-dl,
                bgl=-bgl,
                details=details,
                transaction_type=TransactionType.WITHDRAWAL.value
            )

            self.logger.info(
                f"Withdrawal successful: {growid} withdrew {total_wl:,} WL"
            )

            return {
                'status': 'success',
                'message': (
                    f"Successfully withdrew:\n"
                    f"{wl:,} WL{f', {dl:,} DL' if dl > 0 else ''}"
                    f"{f', {bgl:,} BGL' if bgl > 0 else ''}\n"
                    f"New balance: {new_balance.format()}"
                ),
                'new_balance': new_balance
            }

        except TransactionError as e:
            raise
        except Exception as e:
            self.logger.error(f"Error processing withdrawal: {e}")
            raise TransactionError("An unexpected error occurred")
        finally:
            self.release_lock(f"withdrawal_{user_id}")

class TransactionCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.trx_manager = TransactionManager(bot)
        self.logger = logging.getLogger("TransactionCog")

    async def cog_load(self):
        self.logger.info("TransactionCog loading...")

    async def cog_unload(self):
        self.logger.info("TransactionCog unloaded")

async def setup(bot):
    if not hasattr(bot, 'transaction_manager_loaded'):
        await bot.add_cog(TransactionCog(bot))
        bot.transaction_manager_loaded = True
        logging.info(
            f'Transaction Manager cog loaded successfully at '
            f'{datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")} UTC'
        )