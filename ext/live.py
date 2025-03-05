import discord 
from discord import ui
from discord.ext import commands, tasks
from discord.ui import Button, Modal, TextInput, View
import logging
from datetime import datetime
import asyncio
import json
import time
from typing import Optional, Dict, Any

from ext.product_manager import ProductManagerService
from ext.balance_manager import BalanceManagerService
from ext.trx import TransactionManager
from ext.base_handler import BaseLockHandler, BaseResponseHandler
from ext.constants import (
    STATUS_AVAILABLE, 
    STATUS_SOLD,
    TRANSACTION_PURCHASE,
    COOLDOWN_SECONDS,
    UPDATE_INTERVAL,
    CACHE_TIMEOUT
)

# Setup logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log')
    ]
)
logger = logging.getLogger(__name__)

# Load config
with open('config.json') as config_file:
    config = json.load(config_file)
    LIVE_STOCK_CHANNEL_ID = int(config['id_live_stock'])

class LiveStockService(BaseLockHandler):
    _instance = None
    _init_lock = asyncio.Lock()

    def __new__(cls, bot):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.initialized = False
        return cls._instance

    def __init__(self, bot):
        if not self.initialized:
            super().__init__()
            self.bot = bot
            self.logger = logging.getLogger("LiveStockService")
            self.product_manager = ProductManagerService(bot)
            self.initialized = True

    async def create_stock_embed(self, products: list) -> discord.Embed:
        cache_key = f"stock_embed_{hash(str(products))}"
        cached = self.get_cached(cache_key)
        if cached:
            return cached

        lock = await self.acquire_lock("create_stock_embed")
        if not lock:
            self.logger.error("Failed to acquire lock for create_stock_embed")
            return None

        try:
            embed = discord.Embed(
                title="🏪 Store Stock Status",
                color=discord.Color.blue(),
                timestamp=datetime.utcnow()
            )

            if products:
                for product in sorted(products, key=lambda x: x['code']):
                    stock_count = await self.product_manager.get_stock_count(product['code'])
                    value = (
                        f"💎 Code: `{product['code']}`\n"
                        f"📦 Stock: `{stock_count}`\n"
                        f"💰 Price: `{product['price']:,} WL`\n"
                    )
                    if product.get('description'):
                        value += f"📝 Info: {product['description']}\n"
                    
                    embed.add_field(
                        name=f"🔸 {product['name']} 🔸",
                        value=value,
                        inline=False
                    )
            else:
                embed.description = "No products available."

            embed.set_footer(text=f"Last Update: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
            
            self.set_cached(cache_key, embed, timeout=30)  # Cache for 30 seconds
            return embed

        finally:
            self.release_lock("create_stock_embed")

class BuyModal(ui.Modal, BaseResponseHandler, title="Buy Product"):
    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.logger = logging.getLogger("BuyModal")
        self.balance_manager = BalanceManagerService(bot)
        self.product_manager = ProductManagerService(bot)
        self.trx_manager = TransactionManager(bot)
        self.modal_lock = asyncio.Lock()

    code = ui.TextInput(
        label="Product Code",
        placeholder="Enter product code...",
        min_length=1,
        max_length=10,
        required=True
    )

    quantity = ui.TextInput(
        label="Quantity",
        placeholder="Enter quantity...",
        min_length=1,
        max_length=2,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        if self.modal_lock.locked():
            await interaction.response.send_message(
                "🔒 Another transaction is in progress. Please wait...",
                ephemeral=True
            )
            return

        async with self.modal_lock:
            try:
                await interaction.response.defer(ephemeral=True)
        
                # Get user's GrowID
                growid = await self.balance_manager.get_growid(interaction.user.id)
                if not growid:
                    await self.send_response_once(
                        interaction,
                        content="❌ Please set your GrowID first!",
                        ephemeral=True
                    )
                    return
        
                # Validate product
                product = await self.product_manager.get_product(self.code.value)
                if not product:
                    await self.send_response_once(
                        interaction,
                        content="❌ Invalid product code!",
                        ephemeral=True
                    )
                    return
        
                # Validate quantity
                try:
                    quantity = int(self.quantity.value)
                    if quantity <= 0:
                        raise ValueError()
                except ValueError:
                    await self.send_response_once(
                        interaction,
                        content="❌ Invalid quantity!",
                        ephemeral=True
                    )
                    return
        
                # Process purchase
                result = await self.trx_manager.process_purchase(
                    growid=growid,
                    product_code=self.code.value,
                    quantity=quantity
                )
        
                embed = discord.Embed(
                    title="✅ Purchase Successful",
                    color=discord.Color.green(),
                    timestamp=datetime.utcnow()
                )
                embed.add_field(name="Product", value=f"`{result['product_name']}`", inline=True)
                embed.add_field(name="Quantity", value=str(quantity), inline=True)
                embed.add_field(name="Total Price", value=f"{result['total_price']:,} WL", inline=True)
                embed.add_field(name="New Balance", value=f"{result['new_balance']:,} WL", inline=False)
        
                # Send purchase result via DM
                dm_sent = await self.trx_manager.send_purchase_result(
                    user=interaction.user,
                    items=result['items'],
                    product_name=result['product_name']
                )
        
                if dm_sent:
                    embed.add_field(
                        name="Purchase Details",
                        value="✉️ Check your DM for the detailed purchase result!",
                        inline=False
                    )
                else:
                    embed.add_field(
                        name="Purchase Details",
                        value="⚠️ Could not send DM. Please enable DMs from server members.",
                        inline=False
                    )
        
                # Show items in channel if DM failed
                content_msg = None
                if not dm_sent:
                    content_msg = "**Your Items:**\n"
                    for item in result['items']:
                        content_msg += f"```{item['content']}```\n"
        
                await self.send_response_once(
                    interaction,
                    embed=embed,
                    content=content_msg,
                    ephemeral=True
                )
        
            except Exception as e:
                error_msg = str(e) if str(e) else "An error occurred during purchase"
                await self.send_response_once(
                    interaction,
                    content=f"❌ {error_msg}",
                    ephemeral=True
                )
                self.logger.error(f"Error in BuyModal: {e}")

class SetGrowIDModal(ui.Modal, BaseResponseHandler, title="Set GrowID"):
    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.logger = logging.getLogger("SetGrowIDModal")
        self.balance_manager = BalanceManagerService(bot)
        self.modal_lock = asyncio.Lock()

    growid = ui.TextInput(
        label="GrowID",
        placeholder="Enter your GrowID...",
        min_length=3,
        max_length=20,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        if self.modal_lock.locked():
            await interaction.response.send_message(
                "🔒 Please wait...",
                ephemeral=True
            )
            return

        async with self.modal_lock:
            try:
                await interaction.response.defer(ephemeral=True)
                
                if await self.balance_manager.register_user(
                    interaction.user.id,
                    self.growid.value
                ):
                    embed = discord.Embed(
                        title="✅ GrowID Set Successfully",
                        description=f"Your GrowID has been set to: `{self.growid.value}`",
                        color=discord.Color.green(),
                        timestamp=datetime.utcnow()
                    )
                    await self.send_response_once(
                        interaction,
                        embed=embed,
                        ephemeral=True
                    )
                    self.logger.info(
                        f"Set GrowID for Discord user {interaction.user.id} to {self.growid.value}"
                    )
                else:
                    await self.send_response_once(
                        interaction,
                        content="❌ Failed to set GrowID",
                        ephemeral=True
                    )

            except Exception as e:
                self.logger.error(f"Error in SetGrowIDModal: {e}")
                await self.send_response_once(
                    interaction,
                    content="❌ An error occurred",
                    ephemeral=True
                )

class StockView(View, BaseLockHandler, BaseResponseHandler):
    def __init__(self, bot):
        View.__init__(self, timeout=None)
        BaseLockHandler.__init__(self)
        self.bot = bot
        self.balance_manager = BalanceManagerService(bot)
        self.product_manager = ProductManagerService(bot)
        self.trx_manager = TransactionManager(bot)
        self.logger = logging.getLogger("StockView")
        self._cache_cleanup.start()

    @tasks.loop(minutes=5)
    async def _cache_cleanup(self):
        """Cleanup expired cache entries"""
        self.cleanup()

    async def _check_cooldown(self, interaction: discord.Interaction) -> bool:
        cooldown_key = f"cooldown_{interaction.user.id}"
        cooldown_data = self.get_cached(cooldown_key)
        if cooldown_data:
            remaining = COOLDOWN_SECONDS - (time.time() - cooldown_data['timestamp'])
            if remaining > 0:
                await self.send_response_once(
                    interaction,
                    content=f"⏳ Please wait {remaining:.1f} seconds...",
                    ephemeral=True
                )
                return False
        
        self.set_cached(cooldown_key, {'timestamp': time.time()}, timeout=COOLDOWN_SECONDS)
        return True

    @discord.ui.button(
        label="Balance",
        emoji="💰",
        style=discord.ButtonStyle.primary,
        custom_id="balance:1"
    )
    async def button_balance_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_cooldown(interaction):
            return

        lock = await self.acquire_lock(f"balance_{interaction.user.id}")
        if not lock:
            await self.send_response_once(
                interaction,
                content="🔒 System is busy, please try again later",
                ephemeral=True
            )
            return

        try:
            await interaction.response.defer(ephemeral=True)
            
            growid = await self.balance_manager.get_growid(interaction.user.id)
            if not growid:
                await self.send_response_once(
                    interaction,
                    content="❌ Please set your GrowID first!",
                    ephemeral=True
                )
                return

            balance = await self.balance_manager.get_balance(growid)
            if not balance:
                await self.send_response_once(
                    interaction,
                    content="❌ Balance not found!",
                    ephemeral=True
                )
                return

            embed = discord.Embed(
                title="💰 Balance Information",
                color=discord.Color.green(),
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="GrowID", value=f"`{growid}`", inline=False)
            embed.add_field(name="Balance", value=balance.format(), inline=False)
            
            await self.send_response_once(
                interaction,
                embed=embed,
                ephemeral=True
            )

        except Exception as e:
            self.logger.error(f"Error in balance callback: {e}")
            await self.send_response_once(
                interaction,
                content="❌ An error occurred",
                ephemeral=True
            )
        finally:
            self.release_lock(f"balance_{interaction.user.id}")

    @discord.ui.button(
        label="Buy",
        emoji="🛒",
        style=discord.ButtonStyle.success,
        custom_id="buy:1"
    )
    async def button_buy_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_cooldown(interaction):
            return

        try:
            growid = await self.balance_manager.get_growid(interaction.user.id)
            if not growid:
                await self.send_response_once(
                    interaction,
                    content="❌ Please set your GrowID first!",
                    ephemeral=True
                )
                return
            
            modal = BuyModal(self.bot)
            await interaction.response.send_modal(modal)

        except Exception as e:
            self.logger.error(f"Error in buy callback: {e}")
            await self.send_response_once(
                interaction,
                content="❌ An error occurred",
                ephemeral=True
            )

    @discord.ui.button(
        label="Set GrowID",
        emoji="🔑",
        style=discord.ButtonStyle.secondary,
        custom_id="set_growid:1"
    )
    async def button_set_growid_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_cooldown(interaction):
            return

        try:
            modal = SetGrowIDModal(self.bot)
            await interaction.response.send_modal(modal)

        except Exception as e:
            self.logger.error(f"Error in set growid callback: {e}")
            await self.send_response_once(
                interaction,
                content="❌ An error occurred",
                ephemeral=True
            )

    @discord.ui.button(
        label="Check GrowID",
        emoji="🔍",
        style=discord.ButtonStyle.secondary,
        custom_id="check_growid:1"
    )
    async def button_check_growid_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_cooldown(interaction):
            return

        lock = await self.acquire_lock(f"check_growid_{interaction.user.id}")
        if not lock:
            await self.send_response_once(
                interaction,
                content="🔒 System is busy, please try again later",
                ephemeral=True
            )
            return

        try:
            await interaction.response.defer(ephemeral=True)
            
            growid = await self.balance_manager.get_growid(interaction.user.id)
            if not growid:
                await self.send_response_once(
                    interaction,
                    content="❌ You haven't set your GrowID yet!",
                    ephemeral=True
                )
                return

            embed = discord.Embed(
                title="🔍 GrowID Information",
                description=f"Your registered GrowID: `{growid}`",
                color=discord.Color.blue(),
                timestamp=datetime.utcnow()
            )
            
            await self.send_response_once(
                interaction,
                embed=embed,
                ephemeral=True
            )

        except Exception as e:
            self.logger.error(f"Error in check growid callback: {e}")
            await self.send_response_once(
                interaction,
                content="❌ An error occurred",
                ephemeral=True
            )
        finally:
            self.release_lock(f"check_growid_{interaction.user.id}")

    @discord.ui.button(
        label="World",
        emoji="🌍",
        style=discord.ButtonStyle.secondary,
        custom_id="world:1"
    )
    async def button_world_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_cooldown(interaction):
            return

        lock = await self.acquire_lock(f"world_{interaction.user.id}")
        if not lock:
            await self.send_response_once(
                interaction,
                content="🔒 System is busy, please try again later",
                ephemeral=True
            )
            return

        try:
            await interaction.response.defer(ephemeral=True)
            
            world_info = await self.product_manager.get_world_info()
            if not world_info:
                await self.send_response_once(
                    interaction,
                    content="❌ World information not available.",
                    ephemeral=True
                )
                return

            embed = discord.Embed(
                title="🌍 World Information",
                color=discord.Color.blue(),
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="World", value=f"`{world_info['world']}`", inline=True)
            if world_info.get('owner'):
                embed.add_field(name="Owner", value=f"`{world_info['owner']}`", inline=True)
            if world_info.get('bot'):
                embed.add_field(name="Bot", value=f"`{world_info['bot']}`", inline=True)
            
            await self.send_response_once(
                interaction,
                embed=embed,
                ephemeral=True
            )

        except Exception as e:
            self.logger.error(f"Error in world callback: {e}")
            await self.send_response_once(
                interaction,
                content="❌ An error occurred",
                ephemeral=True
            )
        finally:
            self.release_lock(f"world_{interaction.user.id}")

class LiveStock(commands.Cog, BaseLockHandler):
    def __init__(self, bot):
        super().__init__()
        if not hasattr(bot, 'live_stock_instance'):
            self.bot = bot
            self.message_id = None
            self.last_update = datetime.utcnow().timestamp()
            self.service = LiveStockService(bot)
            self.stock_view = StockView(bot)
            self.logger = logging.getLogger("LiveStock")
            self._task = None
            
            bot.add_view(self.stock_view)
            bot.live_stock_instance = self

    async def cog_load(self):
        """Called when cog is being loaded"""
        self.live_stock.start()
        self.logger.info("LiveStock cog loaded and task started")

    def cog_unload(self):
        """Called when cog is being unloaded"""
        if self._task and not self._task.done():
            self._task.cancel()
        if hasattr(self, 'live_stock') and self.live_stock.is_running():
            self.live_stock.cancel()
        self.cleanup()  # Cleanup locks and cache
        self.logger.info("LiveStock cog unloaded")

    @tasks.loop(seconds=UPDATE_INTERVAL)
    async def live_stock(self):
        lock = await self.acquire_lock("live_stock_update")
        if not lock:
            self.logger.warning("Failed to acquire lock for live stock update")
            return

        try:
            channel = self.bot.get_channel(LIVE_STOCK_CHANNEL_ID)
            if not channel:
                self.logger.error(f"Could not find channel with ID {LIVE_STOCK_CHANNEL_ID}")
                return

            products = await self.service.product_manager.get_all_products()
            embed = await self.service.create_stock_embed(products)
            if not embed:
                self.logger.error("Failed to create stock embed")
                return

            if self.message_id:
                try:
                    message = await channel.fetch_message(self.message_id)
                    await message.edit(embed=embed, view=self.stock_view)
                    self.logger.debug(f"Updated existing message {self.message_id}")
                except discord.NotFound:
                    message = await channel.send(embed=embed, view=self.stock_view)
                    self.message_id = message.id
                    self.logger.info(f"Created new message {self.message_id} (old not found)")
            else:
                message = await channel.send(embed=embed, view=self.stock_view)
                self.message_id = message.id
                self.logger.info(f"Created initial message {self.message_id}")

            self.last_update = datetime.utcnow().timestamp()

        except Exception as e:
            self.logger.error(f"Error updating live stock: {e}")
        finally:
            self.release_lock("live_stock_update")

    @live_stock.before_loop
    async def before_live_stock(self):
        await self.bot.wait_until_ready()

async def setup(bot):
    """Setup the LiveStock cog"""
    try:
        await bot.add_cog(LiveStock(bot))
        logger.info(f'LiveStock cog loaded successfully at {datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")} UTC')
    except Exception as e:
        logger.error(f"Error loading LiveStock cog: {e}")
        raise