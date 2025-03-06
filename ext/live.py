import discord 
from discord import ui
from discord.ext import commands, tasks
from discord.ui import Button, Modal, TextInput, View
import logging
from datetime import datetime
import asyncio
import json
import time
import platform
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

# Setup logging dengan detail lebih baik
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot_debug.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# Load config
with open('config.json') as config_file:
    config = json.load(config_file)
    LIVE_STOCK_CHANNEL_ID = int(config['id_live_stock'])

class SmartCache:
    def __init__(self):
        self._cache = {}
        self._timeouts = {
            'balance': 30,
            'stock': 60,
            'world': 300,
            'cooldown': COOLDOWN_SECONDS,
            'user_data': 3600
        }

    def get(self, key: str, category: str = 'default') -> Optional[Any]:
        if key not in self._cache:
            return None
            
        data = self._cache[key]
        if time.time() - data['timestamp'] > self._timeouts.get(category, CACHE_TIMEOUT):
            del self._cache[key]
            return None
            
        return data['value']

    def set(self, key: str, value: Any, category: str = 'default'):
        self._cache[key] = {
            'value': value,
            'timestamp': time.time(),
            'category': category
        }

    def delete(self, key: str):
        self._cache.pop(key, None)

class BaseModal(ui.Modal):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._responded = False
        self._lock = asyncio.Lock()
        self.logger = logging.getLogger(self.__class__.__name__)

    async def _handle_interaction(self, interaction: discord.Interaction) -> bool:
        """Base interaction handler with safety checks"""
        if self._responded:
            return False

        async with self._lock:
            try:
                if interaction.response.is_done():
                    return False
                    
                await interaction.response.defer(ephemeral=True)
                await asyncio.sleep(0.1)  # Small delay for stability
                self._responded = True
                return True
                
            except Exception as e:
                self.logger.error(f"Error handling interaction: {e}")
                return False

class SetGrowIDModal(BaseModal, title="🎮 Set Your GrowID"):
    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.balance_manager = BalanceManagerService(bot)
        self.cache = SmartCache()

    growid = ui.TextInput(
        label="🎯 Enter GrowID",
        placeholder="Your Growtopia account name...",
        min_length=3,
        max_length=20,
        required=True,
        style=discord.TextStyle.short
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not await self._handle_interaction(interaction):
            return

        try:
            growid = self.growid.value.strip()
            
            # Validate GrowID
            if not self._validate_growid(growid):
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="❌ Invalid GrowID Format",
                        description=(
                            "GrowID must:\n"
                            "━━━━━━━━━━━━━━━━━━━━━━\n"
                            "> 📝 Be 3-20 characters long\n"
                            "> 🔤 Contain only letters, numbers, and underscores"
                        ),
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
                return

            # Check if GrowID is already registered
            existing_user = await self.balance_manager.get_user_by_growid(growid)
            if existing_user and existing_user != interaction.user.id:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="❌ GrowID Already Registered",
                        description="This GrowID is linked to another account.",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
                return

            # Register GrowID
            success = await self.balance_manager.register_user(interaction.user.id, growid)
            
            if success:
                # Update cache
                self.cache.set(f"growid_{interaction.user.id}", growid, 'user_data')
                
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="✅ GrowID Successfully Set!",
                        description=(
                            f"Your GrowID has been set to: `{growid}`\n"
                            "━━━━━━━━━━━━━━━━━━━━━━\n"
                            "> 💰 Check your balance with the Balance button\n"
                            "> 🛍️ You can now make purchases!"
                        ),
                        color=discord.Color.green()
                    ),
                    ephemeral=True
                )
                self.logger.info(f"GrowID set for user {interaction.user.id}: {growid}")
            else:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="❌ Registration Failed",
                        description="Please try again later.",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )

        except Exception as e:
            self.logger.error(f"Error in SetGrowIDModal: {e}")
            await interaction.followup.send(
                "An error occurred. If your GrowID was set, try checking your balance.",
                ephemeral=True
            )

    def _validate_growid(self, growid: str) -> bool:
        import re
        return bool(re.match(r'^[a-zA-Z0-9_]{3,20}$', growid))

class BuyModal(BaseModal, title="🛍️ Premium Purchase"):
    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.balance_manager = BalanceManagerService(bot)
        self.product_manager = ProductManagerService(bot)
        self.trx_manager = TransactionManager(bot)
        self.cache = SmartCache()

    code = ui.TextInput(
        label="🏷️ Product Code",
        placeholder="Enter product code (e.g. DL1)",
        min_length=1,
        max_length=10,
        required=True,
        style=discord.TextStyle.short
    )

    quantity = ui.TextInput(
        label="📦 Quantity",
        placeholder="How many? (1-99)",
        min_length=1,
        max_length=2,
        required=True,
        style=discord.TextStyle.short
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not await self._handle_interaction(interaction):
            return

        try:
            # Get and validate product
            product = await self.product_manager.get_product(self.code.value)
            if not product:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="❌ Invalid Product",
                        description="Please check the product code and try again.",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
                return

            # Validate quantity
            try:
                quantity = int(self.quantity.value)
                if not 1 <= quantity <= 99:
                    raise ValueError()
            except ValueError:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="❌ Invalid Quantity",
                        description="Please enter a number between 1 and 99.",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
                return

            # Check stock
            stock = await self.product_manager.get_stock_count(self.code.value)
            if stock < quantity:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="❌ Insufficient Stock",
                        description=f"Only {stock} units available.",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
                return

            # Get GrowID and check balance
            growid = await self.balance_manager.get_growid(interaction.user.id)
            if not growid:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="❌ GrowID Required",
                        description="Please set your GrowID first!",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
                return

            balance = await self.balance_manager.get_balance(growid)
            total_cost = product['price'] * quantity
            
            if balance < total_cost:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="❌ Insufficient Balance",
                        description=(
                            f"Required: {total_cost:,} WL\n"
                            f"Your balance: {balance:,} WL"
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

            if result:
                embed = discord.Embed(
                    title="✅ Purchase Successful!",
                    description=(
                        f"Successfully purchased {quantity}x {product['name']}\n"
                        "━━━━━━━━━━━━━━━━━━━━━━"
                    ),
                    color=discord.Color.green()
                )
                
                embed.add_field(
                    name="💰 Transaction Details",
                    value=(
                        f"```\n"
                        f"Total Cost : {total_cost:,} WL\n"
                        f"New Balance: {balance - total_cost:,} WL\n"
                        f"```"
                    ),
                    inline=False
                )

                if result.get('items'):
                    items_text = "\n".join(f"```{item}```" for item in result['items'])
                    embed.add_field(
                        name="🎁 Your Items",
                        value=items_text,
                        inline=False
                    )

                await interaction.followup.send(embed=embed, ephemeral=True)
                self.logger.info(f"Successful purchase by {interaction.user.id}: {quantity}x {self.code.value}")
            else:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="❌ Purchase Failed",
                        description="Transaction could not be completed. Please try again.",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )

        except Exception as e:
            self.logger.error(f"Error in BuyModal: {e}")
            await interaction.followup.send(
                "An error occurred. Please check your balance to verify the transaction status.",
                ephemeral=True
            )

class StockView(View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot
        self.balance_manager = BalanceManagerService(bot)
        self.product_manager = ProductManagerService(bot)
        self.cache = SmartCache()
        self.logger = logging.getLogger("StockView")

    async def _handle_interaction(self, interaction: discord.Interaction) -> bool:
        """Common interaction handler"""
        try:
            if interaction.response.is_done():
                return False
                
            # Check cooldown
            cooldown_key = f"cooldown_{interaction.user.id}"
            last_use = self.cache.get(cooldown_key, 'cooldown')
            
            if last_use:
                remaining = COOLDOWN_SECONDS - (time.time() - last_use)
                if remaining > 0:
                    await interaction.response.send_message(
                        f"Please wait {remaining:.1f} seconds.",
                        ephemeral=True
                    )
                    return False

            self.cache.set(cooldown_key, time.time(), 'cooldown')
            return True

        except Exception as e:
            self.logger.error(f"Error handling interaction: {e}")
            return False

    @discord.ui.button(
        label="Balance",
        emoji="💰",
        style=discord.ButtonStyle.primary,
        custom_id="balance:1"
    )
    async def button_balance_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._handle_interaction(interaction):
            return

        try:
            await interaction.response.defer(ephemeral=True)
            
            growid = await self.balance_manager.get_growid(interaction.user.id)
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

            balance = await self.balance_manager.get_balance(growid)
            
            embed = discord.Embed(
                title="💰 Balance Information",
                color=discord.Color.green(),
                timestamp=datetime.utcnow()
            )
            
            embed.add_field(
                name="🎮 GrowID",
                value=f"```{growid}```",
                inline=True
            )
            
            embed.add_field(
                name="💎 Balance",
                value=f"```{balance:,} WL```",
                inline=True
            )
            
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            self.logger.error(f"Error in balance callback: {e}")
            await interaction.followup.send(
                "An error occurred. Please try again.",
                ephemeral=True
            )

    @discord.ui.button(
        label="Buy",
        emoji="🛒",
        style=discord.ButtonStyle.success,
        custom_id="buy:1"
    )
    async def button_buy_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._handle_interaction(interaction):
            return

        try:
            # Check GrowID first
            growid = await self.balance_manager.get_growid(interaction.user.id)
            if not growid:
                await interaction.response.send_message(
                    embed=discord.Embed(
                        title="❌ GrowID Required",
                        description="Please set your GrowID first!",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
                return

            modal = BuyModal(self.bot)
            await interaction.response.send_modal(modal)

        except Exception as e:
            self.logger.error(f"Error in buy callback: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "An error occurred. Please try again.",
                    ephemeral=True
                )

    @discord.ui.button(
        label="Set GrowID",
        emoji="🔑",
        style=discord.ButtonStyle.secondary,
        custom_id="set_growid:1"
    )
    async def button_set_growid_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._handle_interaction(interaction):
            return

        try:
            modal = SetGrowIDModal(self.bot)
            await interaction.response.send_modal(modal)

        except Exception as e:
            self.logger.error(f"Error in set growid callback: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "An error occurred. Please try again.",
                    ephemeral=True
                )

    @discord.ui.button(
        label="World",
        emoji="🌍",
        style=discord.ButtonStyle.secondary,
        custom_id="world:1"
    )
    async def button_world_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._handle_interaction(interaction):
            return

        try:
            await interaction.response.defer(ephemeral=True)
            
            world_info = await self.product_manager.get_world_info()
            if not world_info:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="❌ World Info Unavailable",
                        description="Trading world information is currently unavailable.",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
                return

            embed = discord.Embed(
                title="🌍 Trading World Information",
                description="Current trading world details",
                color=discord.Color.blue(),
                timestamp=datetime.utcnow()
            )
            
            embed.add_field(
                name="🏠 World Name",
                value=f"```{world_info['world']}```",
                inline=True
            )
            
            if world_info.get('owner'):
                embed.add_field(
                    name="👑 Owner",
                    value=f"```{world_info['owner']}```",
                    inline=True
                )
            
            if world_info.get('bot'):
                embed.add_field(
                    name="🤖 Bot Name",
                    value=f"```{world_info['bot']}```",
                    inline=True
                )

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            self.logger.error(f"Error in world callback: {e}")
            await interaction.followup.send(
                "An error occurred. Please try again.",
                ephemeral=True
            )

class LiveStock(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.message_id = None
        self.last_update = datetime.utcnow().timestamp()
        self.product_manager = ProductManagerService(bot)
        self.stock_view = StockView(bot)
        self.logger = logging.getLogger("LiveStock")
        self.cache = SmartCache()
        
        bot.add_view(self.stock_view)
        self.update_stock.start()

    async def create_stock_embed(self) -> discord.Embed:
        try:
            products = await self.product_manager.get_all_products()
            
            embed = discord.Embed(
                title="🏪 Premium Store",
                description=(
                    "Welcome to our premium store!\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    "> 💎 **Premium Products**\n"
                    "> 🔒 **Secure Trading**\n"
                    "> ⚡ **Instant Delivery**\n"
                    "> 👥 **24/7 Support**"
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
                        f"[Code]     : {product['code']}\n"
                        f"[Status]   : {status}\n"
                        f"[Stock]    : {stock_count} units\n"
                        f"[Price]    : {product['price']:,} WL\n"
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
                embed.description += "\n\n**No products available at the moment.**"

            embed.set_footer(text=f"Last Updated • {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
            return embed

        except Exception as e:
            self.logger.error(f"Error creating stock embed: {e}")
            return None

    @tasks.loop(seconds=UPDATE_INTERVAL)
    async def update_stock(self):
        try:
            channel = self.bot.get_channel(LIVE_STOCK_CHANNEL_ID)
            if not channel:
                self.logger.error(f"Could not find channel {LIVE_STOCK_CHANNEL_ID}")
                return

            embed = await self.create_stock_embed()
            if not embed:
                self.logger.error("Failed to create stock embed")
                return

            if self.message_id:
                try:
                    message = await channel.fetch_message(self.message_id)
                    await message.edit(embed=embed, view=self.stock_view)
                except discord.NotFound:
                    message = await channel.send(embed=embed, view=self.stock_view)
                    self.message_id = message.id
            else:
                message = await channel.send(embed=embed, view=self.stock_view)
                self.message_id = message.id

            self.last_update = datetime.utcnow().timestamp()

        except Exception as e:
            self.logger.error(f"Error updating stock: {e}")

    @update_stock.before_loop
    async def before_update_stock(self):
        await self.bot.wait_until_ready()

    def cog_unload(self):
        self.update_stock.cancel()
        self.cache.delete_all()

async def setup(bot):
    try:
        await bot.add_cog(LiveStock(bot))
        logger.info(
            f"LiveStock cog loaded successfully\n"
            f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
            f"Bot Version: {getattr(bot, 'version', 'unknown')}\n"
            f"Python Version: {platform.python_version()}"
        )
    except Exception as e:
        logger.error(f"Error loading LiveStock cog: {e}")
        raise