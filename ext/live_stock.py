import logging
import asyncio
from typing import Optional, Dict, List
from datetime import datetime

import discord
from discord.ext import commands, tasks
from .constants import (
    Status,          # Untuk status stok
    COLORS,         # Untuk warna embed
    UPDATE_INTERVAL,# Untuk interval update (55 seconds)
    MESSAGES,       # Untuk pesan error/status
    CACHE_TIMEOUT  # Untuk cache message ID
)

from database import get_connection
from .base_handler import BaseLockHandler
from .cache_manager import CacheManager
from .product_manager import ProductManagerService

class LiveStockManager(BaseLockHandler):
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
            self.logger = logging.getLogger("LiveStockManager")
            self.cache_manager = CacheManager()
            self.product_manager = ProductManagerService(bot)
            self.stock_channel_id = int(self.bot.config.get('id_live_stock', 0))
            self.current_stock_message: Optional[discord.Message] = None
            self.initialized = True

    async def create_stock_embed(self) -> discord.Embed:
        """Create a modern looking stock embed"""
        try:
            products = await self.product_manager.get_all_products()
            
            embed = discord.Embed(
                title="🌟 Live Stock Status",
                description=(
                    "```diff\n"
                    "Welcome to our Growtopia Shop!\n"
                    "Real-time stock information updated every minute\n"
                    "```"
                ),
                color=COLORS['info']  # Menggunakan warna dari constants
            )

            # Add server time
            embed.add_field(
                name="🕒 Server Time",
                value=f"```yml\n{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC```",
                inline=False
            )

            # Group products by category
            for product in products:
                stock_count = await self.product_manager.get_stock_count(product['code'])
                
                status_emoji = "🟢" if stock_count > 0 else "🔴"
                status_text = "Available" if stock_count > 0 else "Out of Stock"
                
                field_value = (
                    f"```yml\n"
                    f"Price: {product['price']:,} WL\n"
                    f"Stock: {stock_count} units\n"
                    f"Status: {status_text}\n"
                    f"```"
                )
                
                embed.add_field(
                    name=f"{status_emoji} {product['name']} ({product['code']})",
                    value=field_value,
                    inline=True
                )

            embed.set_footer(
                text="Last Updated",
                icon_url=self.bot.user.display_avatar.url
            )
            embed.timestamp = datetime.utcnow()

            return embed

        except Exception as e:
            self.logger.error(f"Error creating stock embed: {e}")
            raise

    async def get_or_create_stock_message(self) -> Optional[discord.Message]:
        """Get existing stock message or create new one"""
        if not self.stock_channel_id:
            self.logger.error("Stock channel ID not configured!")
            return None

        channel = self.bot.get_channel(self.stock_channel_id)
        if not channel:
            self.logger.error(f"Could not find stock channel {self.stock_channel_id}")
            return None

        try:
            # Check cache first
            message_id = await self.cache_manager.get("live_stock_message_id")
            if message_id:
                try:
                    message = await channel.fetch_message(message_id)
                    self.current_stock_message = message
                    return message
                except discord.NotFound:
                    await self.cache_manager.delete("live_stock_message_id")
                except Exception as e:
                    self.logger.error(f"Error fetching stock message: {e}")

            # If no cached message or message not found, create new
            embed = await self.create_stock_embed()
            message = await channel.send(embed=embed)
            self.current_stock_message = message
            
            # Cache the message ID
            await self.cache_manager.set(
                "live_stock_message_id", 
                message.id,
                expires_in=CACHE_TIMEOUT,  # Menggunakan CACHE_TIMEOUT dari constants
                permanent=True
            )
            
            return message

        except Exception as e:
            self.logger.error(f"Error in get_or_create_stock_message: {e}")
            return None

    async def update_stock_display(self) -> bool:
        """Update the live stock display"""
        try:
            message = await self.get_or_create_stock_message()
            if not message:
                return False

            embed = await self.create_stock_embed()
            await message.edit(embed=embed)
            return True

        except Exception as e:
            self.logger.error(f"Error updating stock display: {e}")
            return False

    async def cleanup(self):
        """Cleanup resources"""
        try:
            if self.current_stock_message:
                await self.current_stock_message.edit(
                    content="Shop is currently offline. Please wait...",
                    color=COLORS['warning']  # Menggunakan warna warning dari constants
                )
        except Exception as e:
            self.logger.error(f"Error in cleanup: {e}")

class LiveStockCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.stock_manager = LiveStockManager(bot)
        self.logger = logging.getLogger("LiveStockCog")
        self.update_stock.start()

    @tasks.loop(seconds=UPDATE_INTERVAL)  # Menggunakan UPDATE_INTERVAL dari constants
    async def update_stock(self):
        """Update stock display periodically"""
        try:
            await self.stock_manager.update_stock_display()
        except Exception as e:
            self.logger.error(f"Error in stock update loop: {e}")

    @update_stock.before_loop
    async def before_update_stock(self):
        """Wait until bot is ready before starting the loop"""
        await self.bot.wait_until_ready()

    async def cog_unload(self):
        """Cleanup when unloading cog"""
        self.update_stock.cancel()
        await self.stock_manager.cleanup()
        self.logger.info("LiveStockCog unloaded")

async def setup(bot):
    if not hasattr(bot, 'live_stock_loaded'):
        await bot.add_cog(LiveStockCog(bot))
        bot.live_stock_loaded = True
        logging.info(
            f'LiveStock cog loaded successfully at '
            f'{datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")} UTC'
        )