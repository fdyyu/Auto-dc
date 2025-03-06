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
from collections import OrderedDict

from ext.product_manager import ProductManagerService
from ext.balance_manager import BalanceManagerService
from ext.trx import TransactionManager
from ext.base_handler import BaseLockHandler
from ext.constants import (
    STATUS_AVAILABLE, 
    STATUS_SOLD,
    TRANSACTION_PURCHASE,
    COOLDOWN_SECONDS,
    UPDATE_INTERVAL,
    CACHE_TIMEOUT
)

# Improved Smart Cache System
class SmartCache:
    def __init__(self):
        self.cache = OrderedDict()
        self.timeouts = {
            'balance': 30,    # 30 seconds for balance
            'stock': 60,      # 1 minute for stock data
            'world': 300,     # 5 minutes for world info
            'cooldown': COOLDOWN_SECONDS  # From constants
        }
        self._cleanup_task = None

    def get_cached(self, key: str, category: str = 'default') -> Optional[Any]:
        """Get cached data with category-based timeout"""
        try:
            if key in self.cache:
                data = self.cache[key]
                current_time = time.time()
                timeout = self.timeouts.get(category, CACHE_TIMEOUT)
                
                if current_time - data['timestamp'] < timeout:
                    return data['data']
                else:
                    del self.cache[key]
            return None
        except Exception as e:
            logger.error(f"Cache get error: {e}")
            return None

    def set_cached(self, key: str, data: Any, category: str = 'default'):
        """Set cached data with category"""
        try:
            self.cache[key] = {
                'timestamp': time.time(),
                'data': data,
                'category': category
            }
        except Exception as e:
            logger.error(f"Cache set error: {e}")

    def cleanup(self):
        """Remove expired cache entries"""
        try:
            current_time = time.time()
            expired_keys = []
            
            for key, value in self.cache.items():
                timeout = self.timeouts.get(value.get('category'), CACHE_TIMEOUT)
                if current_time - value['timestamp'] > timeout:
                    expired_keys.append(key)
                    
            for key in expired_keys:
                del self.cache[key]
        except Exception as e:
            logger.error(f"Cache cleanup error: {e}")

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
            self.smart_cache = SmartCache()
            self.initialized = True

    async def create_stock_embed(self, products: list) -> discord.Embed:
        """Create an elegant stock embed with improved caching"""
        cache_key = f"stock_embed_{hash(str(products))}"
        cached = self.smart_cache.get_cached(cache_key, 'stock')
        if cached:
            return cached

        lock = await self.acquire_lock("create_stock_embed")
        if not lock:
            self.logger.error("Failed to acquire lock for create_stock_embed")
            return None

        try:
            embed = discord.Embed(
                title="🏪 Premium Store Status",
                description=(
                    "Welcome to our exclusive store!\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    "> 💎 **Premium Products**\n"
                    "> 🔒 **Secure Trading**\n"
                    "> ⚡ **Instant Delivery**\n"
                    "> 👥 **24/7 Support**\n"
                    "━━━━━━━━━━━━━━━━━━━━━━"
                ),
                color=discord.Color.gold(),
                timestamp=datetime.utcnow()
            )

            if products:
                for product in sorted(products, key=lambda x: x['code']):
                    stock_count = await self.product_manager.get_stock_count(product['code'])
                    status = "🟢 In Stock" if stock_count > 0 else "🔴 Out of Stock"
                    
                    value = (
                        f"```ini\n"
                        f"[Product Code] : {product['code']}\n"
                        f"[Stock Status] : {status}\n"
                        f"[Available]    : {stock_count} units\n"
                        f"[Price]        : {product['price']:,} WL\n"
                        f"```"
                    )
                    
                    if product.get('description'):
                        value += f"\n📝 **Details:**\n> {product['description']}\n"
                    
                    embed.add_field(
                        name=f"『 {product['name']} 』",
                        value=value,
                        inline=False
                    )
            else:
                embed.description += "\n\n**No products available at the moment.**\n*Please check back later!*"

            current_time = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
            embed.set_footer(
                text=f"Last Updated • {current_time} UTC\n━━━━━━━━━━━━━━━━━━━━━━"
            )
            
            self.smart_cache.set_cached(cache_key, embed, 'stock')
            return embed

        finally:
            self.release_lock("create_stock_embed")
# ... (kode sebelumnya)

class BuyModal(ui.Modal, title="🛍️ Premium Purchase"):
    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.logger = logging.getLogger("BuyModal")
        self.balance_manager = BalanceManagerService(bot)
        self.product_manager = ProductManagerService(bot)
        self.trx_manager = TransactionManager(bot)
        self.modal_lock = asyncio.Lock()
        self.smart_cache = SmartCache()

    code = ui.TextInput(
        label="🏷️ Product Code",
        placeholder="Enter the product code (e.g. DL1, VIP1)",
        min_length=1,
        max_length=10,
        required=True,
        style=discord.TextStyle.short
    )

    quantity = ui.TextInput(
        label="📦 Quantity",
        placeholder="How many would you like to buy? (1-99)",
        min_length=1,
        max_length=2,
        required=True,
        style=discord.TextStyle.short
    )

    async def on_submit(self, interaction: discord.Interaction):
        if self.modal_lock.locked():
            await interaction.response.send_message(
                "⏳ Another transaction is in progress. Please wait...",
                ephemeral=True
            )
            return

        async with self.modal_lock:
            try:
                await interaction.response.defer(ephemeral=True)
                
                # Check cached GrowID first
                growid_cache_key = f"growid_{interaction.user.id}"
                growid = self.smart_cache.get_cached(growid_cache_key, 'user_data')
                
                if not growid:
                    growid = await self.balance_manager.get_growid(interaction.user.id)
                    if growid:
                        self.smart_cache.set_cached(growid_cache_key, growid, 'user_data')
                
                if not growid:
                    await interaction.followup.send(
                        embed=discord.Embed(
                            title="❌ GrowID Not Set",
                            description="Please set your GrowID first using the `Set GrowID` button!",
                            color=discord.Color.red()
                        ),
                        ephemeral=True
                    )
                    return

                # Get product with cache
                product_cache_key = f"product_{self.code.value}"
                product = self.smart_cache.get_cached(product_cache_key, 'product')
                
                if not product:
                    product = await self.product_manager.get_product(self.code.value)
                    if product:
                        self.smart_cache.set_cached(product_cache_key, product, 'product')

                if not product:
                    await interaction.followup.send(
                        embed=discord.Embed(
                            title="❌ Invalid Product",
                            description="The product code you entered is invalid. Please check and try again.",
                            color=discord.Color.red()
                        ),
                        ephemeral=True
                    )
                    return

                try:
                    quantity = int(self.quantity.value)
                    if quantity <= 0:
                        raise ValueError()
                except ValueError:
                    await interaction.followup.send(
                        embed=discord.Embed(
                            title="❌ Invalid Quantity",
                            description="Please enter a valid quantity (1-99).",
                            color=discord.Color.red()
                        ),
                        ephemeral=True
                    )
                    return

                # Process purchase with improved error handling
                result = await self.trx_manager.process_purchase(
                    growid=growid,
                    product_code=self.code.value,
                    quantity=quantity
                )

                # Create beautiful success embed
                embed = discord.Embed(
                    title="🎉 Purchase Successful!",
                    description=(
                        "Your order has been processed successfully!\n"
                        "━━━━━━━━━━━━━━━━━━━━━━"
                    ),
                    color=discord.Color.green(),
                    timestamp=datetime.utcnow()
                )
                
                # Order details
                embed.add_field(
                    name="📦 Product",
                    value=f"```yaml\n{result['product_name']}```",
                    inline=True
                )
                
                embed.add_field(
                    name="🔢 Quantity",
                    value=f"```yaml\n{quantity} units```",
                    inline=True
                )
                
                embed.add_field(
                    name="💎 Total Price",
                    value=f"```yaml\n{result['total_price']:,} WL```",
                    inline=True
                )
                
                # Financial details
                embed.add_field(
                    name="💰 Balance Update",
                    value=(
                        f"```diff\n"
                        f"- Cost: {result['total_price']:,} WL\n"
                        f"+ Remaining: {result['new_balance']:,} WL\n"
                        f"```"
                    ),
                    inline=False
                )

                # Send purchase details via DM
                dm_sent = await self.trx_manager.send_purchase_result(
                    user=interaction.user,
                    items=result['items'],
                    product_name=result['product_name']
                )

                if dm_sent:
                    embed.add_field(
                        name="📨 Purchase Details",
                        value=(
                            "> Check your DMs for detailed purchase information!\n"
                            "> Keep your items safe and secure."
                        ),
                        inline=False
                    )
                else:
                    embed.add_field(
                        name="⚠️ Important Notice",
                        value=(
                            "> Could not send DM. Please enable DMs from server members.\n"
                            "> Your items will be displayed below."
                        ),
                        inline=False
                    )

                content_msg = None
                if not dm_sent:
                    content_msg = "**Your Items:**\n"
                    for item in result['items']:
                        content_msg += f"```yaml\n{item['content']}```\n"

                # Add nice footer
                embed.set_footer(
                    text="Thank you for your purchase! | Transaction ID: " + 
                         f"{result.get('transaction_id', 'N/A')}"
                )

                await interaction.followup.send(
                    embed=embed,
                    content=content_msg,
                    ephemeral=True
                )

            except Exception as e:
                error_msg = str(e) if str(e) else "An unexpected error occurred"
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="❌ Transaction Failed",
                        description=f"Error: {error_msg}\nPlease try again or contact support.",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
                self.logger.error(f"Error in BuyModal: {e}")
# ... (kode sebelumnya)

class SetGrowIDModal(ui.Modal, title="🎮 Set Your GrowID"):
    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.logger = logging.getLogger("SetGrowIDModal")
        self.balance_manager = BalanceManagerService(bot)
        self.modal_lock = asyncio.Lock()
        self.smart_cache = SmartCache()

    growid = ui.TextInput(
        label="🎯 Enter GrowID",
        placeholder="Your Growtopia account name...",
        min_length=3,
        max_length=20,
        required=True,
        style=discord.TextStyle.short
    )

    async def on_submit(self, interaction: discord.Interaction):
        if self.modal_lock.locked():
            await interaction.response.send_message(
                "⏳ Please wait...",
                ephemeral=True
            )
            return

        async with self.modal_lock:
            try:
                await interaction.response.defer(ephemeral=True)
                
                success = await self.balance_manager.register_user(
                    interaction.user.id,
                    self.growid.value
                )
                
                if success:
                    # Cache the new GrowID
                    self.smart_cache.set_cached(
                        f"growid_{interaction.user.id}", 
                        self.growid.value,
                        'user_data'
                    )
                    
                    embed = discord.Embed(
                        title="✨ GrowID Registration Successful!",
                        description=(
                            "Your account has been linked successfully.\n"
                            "━━━━━━━━━━━━━━━━━━━━━━\n"
                            "> 🎮 Account is now activated\n"
                            "> 💰 You can now check balance\n"
                            "> 🛍️ Ready to make purchases\n"
                            "━━━━━━━━━━━━━━━━━━━━━━"
                        ),
                        color=discord.Color.green(),
                        timestamp=datetime.utcnow()
                    )
                    
                    embed.add_field(
                        name="🎮 GrowID",
                        value=f"```yaml\n{self.growid.value}```",
                        inline=True
                    )
                    
                    embed.add_field(
                        name="👤 Discord",
                        value=f"```yaml\n{interaction.user}```",
                        inline=True
                    )
                    
                    embed.add_field(
                        name="📝 Next Steps",
                        value=(
                            "> 1. Check your balance with 💰\n"
                            "> 2. Browse available items\n"
                            "> 3. Make your first purchase!"
                        ),
                        inline=False
                    )
                    
                    embed.set_footer(text="Welcome to our premium store! ✨")
                    
                    await interaction.followup.send(
                        embed=embed,
                        ephemeral=True
                    )
                    self.logger.info(
                        f"Set GrowID for Discord user {interaction.user.id} to {self.growid.value}"
                    )
                else:
                    await interaction.followup.send(
                        embed=discord.Embed(
                            title="❌ Registration Failed",
                            description="Failed to set GrowID. Please try again or contact support.",
                            color=discord.Color.red()
                        ),
                        ephemeral=True
                    )

            except Exception as e:
                self.logger.error(f"Error in SetGrowIDModal: {e}")
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="❌ Error",
                        description="An unexpected error occurred. Please try again.",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )

class StockView(View, BaseLockHandler):
    def __init__(self, bot):
        View.__init__(self, timeout=None)
        BaseLockHandler.__init__(self)
        self.bot = bot
        self.balance_manager = BalanceManagerService(bot)
        self.product_manager = ProductManagerService(bot)
        self.trx_manager = TransactionManager(bot)
        self.logger = logging.getLogger("StockView")
        self.smart_cache = SmartCache()
        self._cache_cleanup.start()

    @tasks.loop(minutes=5)
    async def _cache_cleanup(self):
        """Cleanup expired cache entries"""
        self.smart_cache.cleanup()

    async def _check_cooldown(self, interaction: discord.Interaction) -> bool:
        cooldown_key = f"cooldown_{interaction.user.id}"
        cooldown_data = self.smart_cache.get_cached(cooldown_key, 'cooldown')
        
        if cooldown_data:
            remaining = COOLDOWN_SECONDS - (time.time() - cooldown_data['timestamp'])
            if remaining > 0:
                await interaction.response.send_message(
                    embed=discord.Embed(
                        title="⏳ Cooldown Active",
                        description=f"Please wait `{remaining:.1f}` seconds...",
                        color=discord.Color.orange()
                    ),
                    ephemeral=True
                )
                return False
        
        self.smart_cache.set_cached(
            cooldown_key, 
            {'timestamp': time.time()}, 
            'cooldown'
        )
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
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="🔒 System Busy",
                    description="Please try again in a few moments.",
                    color=discord.Color.orange()
                ),
                ephemeral=True
            )
            return

        try:
            await interaction.response.defer(ephemeral=True)
            
            # Try to get GrowID from cache first
            growid = self.smart_cache.get_cached(f"growid_{interaction.user.id}", 'user_data')
            if not growid:
                growid = await self.balance_manager.get_growid(interaction.user.id)
                if growid:
                    self.smart_cache.set_cached(
                        f"growid_{interaction.user.id}", 
                        growid, 
                        'user_data'
                    )

            if not growid:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="❌ GrowID Not Set",
                        description="Please set your GrowID first!",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
                return

            # Try to get balance from cache
            balance_key = f"balance_{growid}"
            balance = self.smart_cache.get_cached(balance_key, 'balance')
            if balance is None:
                balance = await self.balance_manager.get_balance(growid)
                if balance is not None:
                    self.smart_cache.set_cached(balance_key, balance, 'balance')

            if balance is None:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="❌ Balance Not Found",
                        description="Could not retrieve your balance. Please try again.",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
                return

            embed = discord.Embed(
                title="💰 Balance Information",
                description=(
                    "Your current account balance and details\n"
                    "━━━━━━━━━━━━━━━━━━━━━━"
                ),
                color=discord.Color.gold(),
                timestamp=datetime.utcnow()
            )
            
            embed.add_field(
                name="🎮 GrowID",
                value=f"```yaml\n{growid}```",
                inline=True
            )
            
            embed.add_field(
                name="💎 Current Balance",
                value=f"```yaml\n{balance:,} WL```",
                inline=True
            )
            
            embed.add_field(
                name="📝 Quick Actions",
                value=(
                    "> 💸 `/donate` - Add more balance\n"
                    "> 🛍️ `Buy` - Purchase items\n"
                    "> 🌍 `World` - Trading world info"
                ),
                inline=False
            )
            
            embed.set_footer(
                text="Thank you for using our service! ✨"
            )
            
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            self.logger.error(f"Error in balance callback: {e}")
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Error",
                    description="An unexpected error occurred. Please try again.",
                    color=discord.Color.red()
                ),
                ephemeral=True
            )
        finally:
            self.release_lock(f"balance_{interaction.user.id}")
# ... (kode sebelumnya)

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
            # Check GrowID from cache first
            growid = self.smart_cache.get_cached(f"growid_{interaction.user.id}", 'user_data')
            if not growid:
                growid = await self.balance_manager.get_growid(interaction.user.id)
                if growid:
                    self.smart_cache.set_cached(f"growid_{interaction.user.id}", growid, 'user_data')
                    
            if not growid:
                await interaction.response.send_message(
                    embed=discord.Embed(
                        title="❌ GrowID Required",
                        description=(
                            "Please set your GrowID first!\n"
                            "━━━━━━━━━━━━━━━━━━━━━━\n"
                            "> Use the `Set GrowID` button to register"
                        ),
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
                return
            
            modal = BuyModal(self.bot)
            await interaction.response.send_modal(modal)

        except Exception as e:
            self.logger.error(f"Error in buy callback: {e}")
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="❌ Error",
                    description="Failed to open purchase menu. Please try again.",
                    color=discord.Color.red()
                ),
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
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="❌ Error",
                    description="Failed to open GrowID registration. Please try again.",
                    color=discord.Color.red()
                ),
                ephemeral=True
            )

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
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="🔒 System Busy",
                    description="Please try again in a few moments.",
                    color=discord.Color.orange()
                ),
                ephemeral=True
            )
            return

        try:
            await interaction.response.defer(ephemeral=True)
            
            # Try to get world info from cache
            world_info = self.smart_cache.get_cached('world_info', 'world')
            if not world_info:
                world_info = await self.product_manager.get_world_info()
                if world_info:
                    self.smart_cache.set_cached('world_info', world_info, 'world')

            if not world_info:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="❌ World Info Unavailable",
                        description="Trading world information is not available at the moment.",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
                return

            embed = discord.Embed(
                title="🌍 Trading World Information",
                description=(
                    "Current trading world details and status\n"
                    "━━━━━━━━━━━━━━━━━━━━━━"
                ),
                color=discord.Color.blue(),
                timestamp=datetime.utcnow()
            )
            
            # World Info Fields
            embed.add_field(
                name="🏠 World Name",
                value=f"```yaml\n{world_info['world']}```",
                inline=True
            )
            
            if world_info.get('owner'):
                embed.add_field(
                    name="👑 Owner",
                    value=f"```yaml\n{world_info['owner']}```",
                    inline=True
                )
                
            if world_info.get('bot'):
                embed.add_field(
                    name="🤖 Bot",
                    value=f"```yaml\n{world_info['bot']}```",
                    inline=True
                )
            
            # Trading Information
            embed.add_field(
                name="📝 Trading Information",
                value=(
                    "```ini\n"
                    "[Trading Hours] : 24/7 Active\n"
                    "[Security]      : Full Protection\n"
                    "[Support]       : Live Assistance\n"
                    "```"
                ),
                inline=False
            )
            
            # Additional Information
            embed.add_field(
                name="ℹ️ Important Notes",
                value=(
                    "> 🕒 Trading is available 24/7\n"
                    "> 🔒 Safe and secure environment\n"
                    "> 👥 Trusted middleman service\n"
                    "> 💬 Support always available"
                ),
                inline=False
            )
            
            last_updated = datetime.strptime(world_info['last_updated'], '%Y-%m-%d %H:%M:%S')
            embed.set_footer(
                text=f"Last Updated • {last_updated.strftime('%Y-%m-%d %H:%M:%S')} UTC"
            )
            
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            self.logger.error(f"Error in world callback: {e}")
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Error",
                    description="Failed to retrieve world information. Please try again.",
                    color=discord.Color.red()
                ),
                ephemeral=True
            )
        finally:
            self.release_lock(f"world_{interaction.user.id}")
# ... (kode sebelumnya)

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
            self.smart_cache = SmartCache()
            
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
        self.smart_cache.cleanup()
        self.logger.info("LiveStock cog unloaded")

    @tasks.loop(seconds=UPDATE_INTERVAL)
    async def live_stock(self):
        """Update live stock display with smart caching"""
        lock = await self.acquire_lock("live_stock_update")
        if not lock:
            self.logger.warning("Failed to acquire lock for live stock update")
            return

        try:
            channel = self.bot.get_channel(LIVE_STOCK_CHANNEL_ID)
            if not channel:
                self.logger.error(f"Could not find channel with ID {LIVE_STOCK_CHANNEL_ID}")
                return

            # Try to get products from cache
            products = self.smart_cache.get_cached('all_products', 'stock')
            if not products:
                products = await self.service.product_manager.get_all_products()
                if products:
                    self.smart_cache.set_cached('all_products', products, 'stock')

            embed = await self.service.create_stock_embed(products)
            if not embed:
                self.logger.error("Failed to create stock embed")
                return

            # Add store status section
            embed.add_field(
                name="🏪 Store Status",
                value=(
                    "```ini\n"
                    f"[Status]     : {'🟢 Online' if products else '🔴 Maintenance'}\n"
                    f"[Last Update]: {datetime.utcnow().strftime('%H:%M:%S UTC')}\n"
                    f"[Products]   : {len(products) if products else 0} items\n"
                    "```"
                ),
                inline=False
            )

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
        """Wait for bot to be ready before starting loop"""
        await self.bot.wait_until_ready()

    @commands.command()
    @commands.has_permissions(administrator=True)
    async def setworld(self, ctx, world: str, owner: str = None, bot: str = None):
        """Set world information with improved feedback"""
        try:
            if await self.service.product_manager.update_world_info(world, owner, bot):
                # Invalidate world info cache
                self.smart_cache.set_cached('world_info', None, 'world')
                
                embed = discord.Embed(
                    title="✅ World Information Updated",
                    description=(
                        "Trading world details have been updated successfully!\n"
                        "━━━━━━━━━━━━━━━━━━━━━━"
                    ),
                    color=discord.Color.green(),
                    timestamp=datetime.utcnow()
                )
                
                embed.add_field(
                    name="🏠 World",
                    value=f"```yaml\n{world}```",
                    inline=True
                )
                
                if owner:
                    embed.add_field(
                        name="👑 Owner",
                        value=f"```yaml\n{owner}```",
                        inline=True
                    )
                    
                if bot:
                    embed.add_field(
                        name="🤖 Bot",
                        value=f"```yaml\n{bot}```",
                        inline=True
                    )
                
                embed.add_field(
                    name="📝 Status",
                    value=(
                        "> ✅ World info updated\n"
                        "> 🔄 Cache refreshed\n"
                        "> 📢 Changes are now live"
                    ),
                    inline=False
                )
                
                embed.set_footer(text=f"Updated by {ctx.author} • {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
                
                await ctx.send(embed=embed)
            else:
                await ctx.send(
                    embed=discord.Embed(
                        title="❌ Update Failed",
                        description="Failed to update world information. Please try again.",
                        color=discord.Color.red()
                    )
                )
        except Exception as e:
            self.logger.error(f"Error in setworld command: {e}")
            await ctx.send(
                embed=discord.Embed(
                    title="❌ Error",
                    description="An unexpected error occurred while updating world information.",
                    color=discord.Color.red()
                )
            )

async def setup(bot):
    """Setup the LiveStock cog with improved logging"""
    try:
        await bot.add_cog(LiveStock(bot))
        logger.info(
            f'LiveStock cog loaded successfully\n'
            f'Time: {datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")} UTC\n'
            f'Bot Version: {getattr(bot, "version", "unknown")}\n'
            f'Python Version: {platform.python_version()}'
        )
    except Exception as e:
        logger.error(
            f"Error loading LiveStock cog: {e}\n"
            f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
        )
        raise