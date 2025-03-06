import discord
from enum import Enum
from collections import namedtuple
from typing import Dict, List

# Timeouts and Intervals
COOLDOWN_SECONDS = 3
UPDATE_INTERVAL = 55  # seconds
CACHE_TIMEOUT = 60
PAGE_TIMEOUT = 60  # seconds
ADMIN_CONFIRM_TIMEOUT = 30  # seconds
INTERACTION_TIMEOUT = 15.0  # seconds

# Database Status
class Status:
    AVAILABLE = 'available'
    SOLD = 'sold'
    DELETED = 'deleted'
    PENDING = 'pending'

# Transaction Types
class TransactionType:
    PURCHASE = 'PURCHASE'
    REFUND = 'REFUND'
    ADMIN = 'ADMIN'
    DEPOSIT = 'DEPOSIT'
    WITHDRAW = 'WITHDRAW'
    ADMIN_ADD = 'ADMIN_ADD'
    ADMIN_REMOVE = 'ADMIN_REMOVE'
    ADMIN_RESET = 'ADMIN_RESET'

    @classmethod
    def values(cls) -> List[str]:
        return [
            cls.PURCHASE, cls.REFUND, cls.ADMIN,
            cls.DEPOSIT, cls.WITHDRAW,
            cls.ADMIN_ADD, cls.ADMIN_REMOVE, cls.ADMIN_RESET
        ]

# Currency Rates
CURRENCY_RATES: Dict[str, int] = {
    'WL': 1,
    'DL': 100,
    'BGL': 10000
}

# File Limits and Settings
MAX_STOCK_FILE_SIZE = 1024 * 1024  # 1MB
VALID_STOCK_FORMATS = ['txt']
MAX_FILE_SIZES = {
    'stock': 1024 * 1024,  # 1MB
    'backup': 10 * 1024 * 1024  # 10MB
}
ALLOWED_FILE_TYPES = {
    'stock': ['txt'],
    'backup': ['db', 'sqlite', 'backup']
}

# Pagination Settings
DEFAULT_PAGE_SIZE = 5
MAX_PAGE_SIZE = 20
ITEMS_PER_PAGE = 5
PAGINATION_TIMEOUT = 60
PAGINATION_EMOJIS = {
    'previous': '⬅️',
    'next': '➡️',
    'first': '⏮️',
    'last': '⏭️'
}

# Transaction Limits
MIN_TRANSACTION_AMOUNT = 1
MAX_TRANSACTION_AMOUNT = 1000000  # 1M WLs
MIN_PURCHASE_QUANTITY = 1
MAX_PURCHASE_QUANTITY = 100
MAX_TRANSACTION_HISTORY = 50
ADMIN_BULK_UPDATE_CHUNK = 10

# Colors
COLORS = {
    'success': discord.Color.green(),
    'error': discord.Color.red(),
    'info': discord.Color.blue(),
    'warning': discord.Color.yellow()
}

# Messages
MESSAGES = {
    'ERROR_GENERIC': "❌ An error occurred. Please try again later.",
    'NO_PERMISSION': "❌ You don't have permission to use this command.",
    'COOLDOWN': "⚠️ Please wait {seconds} seconds before using this command again.",
    'INVALID_AMOUNT': "❌ Please enter a valid amount.",
    'INSUFFICIENT_BALANCE': "❌ Insufficient balance.",
    'INSUFFICIENT_STOCK': "❌ Insufficient stock available.",
    'SUCCESS_PURCHASE': "✅ Purchase successful! Check your DMs for the items.",
    'SUCCESS_DEPOSIT': "✅ Deposit successful!",
    'SUCCESS_WITHDRAW': "✅ Withdrawal successful!",
    'NO_PRODUCT': "❌ Product not found!",
    'NO_USER': "❌ User not found!",
    'SUCCESS_ADD': "✅ Successfully added!",
    'SUCCESS_REMOVE': "✅ Successfully removed!",
    'SUCCESS_UPDATE': "✅ Successfully updated!",
    'INVALID_CURRENCY': "❌ Invalid currency. Use: WL, DL, or BGL",
    'FILE_TOO_LARGE': "❌ File is too large! Maximum size is 1MB.",
    'INVALID_FILE_FORMAT': "❌ Invalid file format! Please use .txt files only.",
    'NO_ITEMS_FOUND': "❌ No items found in file!",
    'STOCK_ADDED': "✅ Stock items successfully added!",
    'PROCESSING': "⏳ Processing... Please wait..."
}

# Database Settings
DB_FILE = 'shop.db'
DB_BACKUP_DIR = 'backups'

# Logging Settings
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
LOG_DATE_FORMAT = '%Y-%m-%d %H:%M:%S'
LOG_FILE = 'bot.log'

# Permission Levels
PERMISSION_LEVELS = {
    'ADMIN': 3,
    'MOD': 2,
    'USER': 1
}

# Product Settings
VALID_PRODUCT_FIELDS = ['name', 'price', 'description']
MAX_ITEMS_PER_MESSAGE = 10

# Custom Exceptions
class TransactionError(Exception):
    """Custom exception for transaction-related errors"""
    pass

class PermissionError(Exception):
    """Custom exception for permission-related errors"""
    pass

class ValidationError(Exception):
    """Custom exception for validation-related errors"""
    pass

# Balance Class
class Balance:
    def __init__(self, wl: int = 0, dl: int = 0, bgl: int = 0):
        self.wl = wl
        self.dl = dl
        self.bgl = bgl
        self.total_wls = self.to_wls()
    
    def format(self) -> str:
        """Format balance in human readable string"""
        parts = []
        if self.bgl > 0:
            parts.append(f"{self.bgl:,} BGL")
        if self.dl > 0:
            parts.append(f"{self.dl:,} DL")
        if self.wl > 0:
            parts.append(f"{self.wl:,} WL")
        return " + ".join(parts) if parts else "0 WL"
    
    def to_wls(self) -> int:
        """Convert balance to total WLs"""
        return self.wl + (self.dl * CURRENCY_RATES['DL']) + (self.bgl * CURRENCY_RATES['BGL'])
    
    @classmethod
    def from_wls(cls, total_wls: int) -> 'Balance':
        """Create Balance instance from total WLs"""
        bgl = total_wls // CURRENCY_RATES['BGL']
        remaining = total_wls % CURRENCY_RATES['BGL']
        dl = remaining // CURRENCY_RATES['DL']
        wl = remaining % CURRENCY_RATES['DL']
        return cls(wl=wl, dl=dl, bgl=bgl)

    def __str__(self) -> str:
        return self.format()

    def __repr__(self) -> str:
        return f"Balance(wl={self.wl}, dl={self.dl}, bgl={self.bgl})"

# Exports
__all__ = [
    'TransactionType',
    'Status',
    'Balance',
    'TransactionError',
    'PermissionError',
    'ValidationError',
    'CURRENCY_RATES',
    'COLORS',
    'MESSAGES',
    'PERMISSION_LEVELS',
]