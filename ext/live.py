import discord 
from discord import ui
from discord.ext import commands, tasks
from discord.ui import Button, Modal, TextInput, View
import logging
from datetime import datetime
import asyncio
import json
import platform
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

# Smart Cache System
class SmartCache:
    def __init__(self):
        self.cache = OrderedDict()
        self.timeouts = {
            'balance': 30,    # 30 seconds for balance
            'stock': 60,      # 1 minute for stock data
            'world': 300,     # 5 minutes for world info
            'cooldown': COOLDOWN_SECONDS
        }
        self._cleanup_task = None

    def get_cached(self, key: str, category: str = 'default') -> Optional[Any]:
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
        try:
            self.cache[key] = {
                'timestamp': time.time(),
                'data': data,
                'category': category
            }
        except Exception as e:
            logger.error(f"Cache set error: {e}")

    def cleanup(self):
        try:
            current_time = time.time()
            expired_keys = [
                key for key, value in self.cache.items()
                if current_time - value['timestamp'] > self.timeouts.get(value.get('category'), CACHE_TIMEOUT)
            ]
            for key in expired_keys:
                del self.cache[key]
        except Exception as e:
            logger.error(f"Cache cleanup error: {e}")

# Setup logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(), logging.FileHandler('bot.log')]
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
        cache_key = f"stock_embed_{hash(str(products))}"
        cached = self.smart_cache.get_cached(cache_key, 'stock')
        if cached:
            return cached

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

        except Exception as e:
            self.logger.error(f"Error creating stock embed: {e}")
            return None

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
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "⏳ Another transaction is in progress. Please wait...",
                    ephemeral=True
                )
            return

        async with self.modal_lock:
            try:
                await interaction.response.defer(ephemeral=True)
                
                # Validasi kode produk
                product = await self.product_manager.get_product(self.code.value)
                if not product:
                    await interaction.followup.send(
                        embed=discord.Embed(
                            title="❌ Invalid Product Code",
                            description=(
                                "The product code you entered is invalid.\n"
                                "━━━━━━━━━━━━━━━━━━━━━━\n"
                                "> 🔍 Please check available products\n"
                                "> 📝 Make sure to use the correct code"
                            ),
                            color=discord.Color.red()
                        ),
                        ephemeral=True
                    )
                    return

                # Validasi stok
                stock_count = await self.product_manager.get_stock_count(self.code.value)
                if stock_count <= 0:
                    await interaction.followup.send(
                        embed=discord.Embed(
                            title="❌ Out of Stock",
                            description=(
                                f"**{product['name']}** is currently out of stock.\n"
                                "━━━━━━━━━━━━━━━━━━━━━━\n"
                                "> 🔄 Please try again later\n"
                                "> 📢 Stock updates every few minutes"
                            ),
                            color=discord.Color.red()
                        ),
                        ephemeral=True
                    )
                    return

                # Validasi quantity
                try:
                    quantity = int(self.quantity.value)
                    if quantity <= 0 or quantity > 99:
                        raise ValueError()
                except ValueError:
                    await interaction.followup.send(
                        embed=discord.Embed(
                            title="❌ Invalid Quantity",
                            description=(
                                "Please enter a valid quantity between 1-99.\n"
                                "━━━━━━━━━━━━━━━━━━━━━━\n"
                                "> 🔢 Must be a number between 1-99\n"
                                "> ❌ Cannot be zero or negative"
                            ),
                            color=discord.Color.red()
                        ),
                        ephemeral=True
                    )
                    return

                # Cek apakah quantity melebihi stok
                if quantity > stock_count:
                    await interaction.followup.send(
                        embed=discord.Embed(
                            title="❌ Insufficient Stock",
                            description=(
                                f"Requested: **{quantity}** units\n"
                                f"Available: **{stock_count}** units\n"
                                "━━━━━━━━━━━━━━━━━━━━━━\n"
                                "> ℹ️ Please reduce your order quantity\n"
                                "> 📦 Or wait for stock restock"
                            ),
                            color=discord.Color.red()
                        ),
                        ephemeral=True
                    )
                    return

                # Get GrowID
                growid = await self.balance_manager.get_growid(interaction.user.id)
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

                # Cek balance
                balance = await self.balance_manager.get_balance(growid)
                total_price = product['price'] * quantity
                
                if balance < total_price:
                    await interaction.followup.send(
                        embed=discord.Embed(
                            title="❌ Insufficient Balance",
                            description=(
                                f"Your balance: **{balance:,}** WL\n"
                                f"Total price: **{total_price:,}** WL\n"
                                f"Missing: **{total_price - balance:,}** WL\n"
                                "━━━━━━━━━━━━━━━━━━━━━━\n"
                                "> 💰 Please top up your balance\n"
                                "> 📊 Or reduce order quantity"
                            ),
                            color=discord.Color.red()
                        ),
                        ephemeral=True
                    )
                    return

                # Process purchase
                result = await self.trx_manager.process_purchase(
                    growid=growid,
                    product_code=self.code.value,
                    quantity=quantity
                )

                if not result:
                    await interaction.followup.send(
                        embed=discord.Embed(
                            title="❌ Transaction Failed",
                            description="Failed to process purchase. Please try again.",
                            color=discord.Color.red()
                        ),
                        ephemeral=True
                    )
                    return

                # Create success embed
                embed = discord.Embed(
                    title="🎉 Purchase Successful!",
                    description="Your order has been processed successfully!\n━━━━━━━━━━━━━━━━━━━━━━",
                    color=discord.Color.green(),
                    timestamp=datetime.utcnow()
                )
                
                embed.add_field(name="📦 Product", value=f"```yaml\n{result['product_name']}```", inline=True)
                embed.add_field(name="🔢 Quantity", value=f"```yaml\n{quantity} units```", inline=True)
                embed.add_field(name="💎 Total Price", value=f"```yaml\n{result['total_price']:,} WL```", inline=True)
                
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

                # Send DM with items
                dm_sent = await self.trx_manager.send_purchase_result(
                    user=interaction.user,
                    items=result['items'],
                    product_name=result['product_name']
                )

                if dm_sent:
                    embed.add_field(
                        name="📨 Purchase Details",
                        value="> Check your DMs for detailed purchase information!\n> Keep your items safe and secure.",
                        inline=False
                    )
                else:
                    items_text = "\n".join([f"```yaml\n{item['content']}```" for item in result['items']])
                    await interaction.followup.send(
                        f"**Your Items:**\n{items_text}",
                        ephemeral=True
                    )

                embed.set_footer(text=f"Transaction ID: {result.get('transaction_id', 'N/A')}")
                await interaction.followup.send(embed=embed, ephemeral=True)

                # Success embed dan proses selanjutnya tetap sama
                # ... (kode success yang sudah ada)

            except Exception as e:
                self.logger.error(f"Error in BuyModal: {e}")
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        embed=discord.Embed(
                            title="❌ Error",
                            description="An unexpected error occurred. Please try again.",
                            color=discord.Color.red()
                        ),
                        ephemeral=True
                    )
                else:
                    await interaction.followup.send(
                        embed=discord.Embed(
                            title="❌ Error",
                            description="An unexpected error occurred. Please try again.",
                            color=discord.Color.red()
                        ),
                        ephemeral=True
                    )
class SetGrowIDModal(ui.Modal, title="🎮 Set Your GrowID"):
    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.logger = logging.getLogger("SetGrowIDModal")
        self.balance_manager = BalanceManagerService(bot)
        self.modal_lock = asyncio.Lock()

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
            if not interaction.response.is_done():
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
                    
                    embed.set_footer(text="Welcome to our premium store! ✨")
                    
                    await interaction.followup.send(embed=embed, ephemeral=True)
                    self.logger.info(f"Set GrowID for Discord user {interaction.user.id} to {self.growid.value}")
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

class StockView(View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot
        self.balance_manager = BalanceManagerService(bot)
        self.product_manager = ProductManagerService(bot)
        self.trx_manager = TransactionManager(bot)
        self.logger = logging.getLogger("StockView")
        self.smart_cache = SmartCache()
        self._cache_cleanup.start()

    @tasks.loop(minutes=1)
    async def _cache_cleanup(self):
        """Cleanup expired cache entries"""
        self.smart_cache.cleanup()

    async def _check_cooldown(self, interaction: discord.Interaction) -> bool:
        cooldown_key = f"cooldown_{interaction.user.id}"
        cooldown_data = self.smart_cache.get_cached(cooldown_key, 'cooldown')
        
        if cooldown_data:
            remaining = COOLDOWN_SECONDS - (time.time() - cooldown_data['timestamp'])
            if remaining > 0:
                try:
                    if not interaction.response.is_done():
                        await interaction.response.send_message(
                            embed=discord.Embed(
                                title="⏳ Cooldown Active",
                                description=f"Please wait `{remaining:.1f}` seconds...",
                                color=discord.Color.orange()
                            ),
                            ephemeral=True
                        )
                except Exception:
                    pass
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

        try:
            # Check if interaction is already responded
            if interaction.response.is_done():
                return

            await interaction.response.defer(ephemeral=True)
            
            # Get GrowID
            growid = self.smart_cache.get_cached(f"growid_{interaction.user.id}", 'user_data')
            if not growid:
                growid = await self.balance_manager.get_growid(interaction.user.id)
                if growid:
                    self.smart_cache.set_cached(f"growid_{interaction.user.id}", growid, 'user_data')

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

            # Get balance
            balance = await self.balance_manager.get_balance(growid)
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
            
            embed.set_footer(text="Thank you for using our service! ✨")
            
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            self.logger.error(f"Error in balance callback: {e}")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "An error occurred. Please try again.",
                        ephemeral=True
                    )
            except:
                pass



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
            # Check GrowID
            growid = self.smart_cache.get_cached(f"growid_{interaction.user.id}", 'user_data')
            if not growid:
                growid = await self.balance_manager.get_growid(interaction.user.id)
                if growid:
                    self.smart_cache.set_cached(f"growid_{interaction.user.id}", growid, 'user_data')
                    
            if not growid:
                if not interaction.response.is_done():
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
            if not interaction.response.is_done():
                await interaction.response.send_modal(modal)

        except Exception as e:
            self.logger.error(f"Error in buy callback: {e}")
            if not interaction.response.is_done():
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
            # Check if interaction is already responded
            if interaction.response.is_done():
                return
                
            modal = SetGrowIDModal(self.bot)
            await interaction.response.send_modal(modal)

        except Exception as e:
            self.logger.error(f"Error in set growid callback: {e}")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "Failed to open registration. Please try again.",
                        ephemeral=True
                    )
            except:
                pass

    async def _check_cooldown(self, interaction: discord.Interaction) -> bool:
        """Improved cooldown check"""
        try:
            if interaction.response.is_done():
                return False
                
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
            
        except Exception as e:
            self.logger.error(f"Error in cooldown check: {e}")
            return False

    @discord.ui.button(
        label="World",
        emoji="🌍",
        style=discord.ButtonStyle.secondary,
        custom_id="world:1"
    )
    async def button_world_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_cooldown(interaction):
            return

        try:
            await interaction.response.defer(ephemeral=True)
            
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
            
            last_updated = datetime.strptime(world_info['last_updated'], '%Y-%m-%d %H:%M:%S')
            embed.set_footer(
                text=f"Last Updated • {last_updated.strftime('%Y-%m-%d %H:%M:%S')} UTC"
            )
            
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            self.logger.error(f"Error in world callback: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    embed=discord.Embed(
                        title="❌ Error",
                        description="Failed to retrieve world information. Please try again.",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
            else:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="❌ Error",
                        description="Failed to retrieve world information. Please try again.",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )

class LiveStock(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.message_id = None
        self.last_update = datetime.utcnow().timestamp()
        self.service = LiveStockService(bot)
        self.stock_view = StockView(bot)
        self.logger = logging.getLogger("LiveStock")
        self._task = None
        self.smart_cache = SmartCache()
        
        bot.add_view(self.stock_view)

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
        """Update live stock display"""
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

    @live_stock.before_loop
    async def before_live_stock(self):
        """Wait for bot to be ready before starting loop"""
        await self.bot.wait_until_ready()

    @commands.command()
    @commands.has_permissions(administrator=True)
    async def setworld(self, ctx, world: str, owner: str = None, bot: str = None):
        """Set world information"""
        try:
            if await self.service.product_manager.update_world_info(world, owner, bot):
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
                
                embed.set_footer(text=f"Updated by {ctx.author}")
                
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