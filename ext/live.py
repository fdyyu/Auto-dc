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

# Load config dengan error handling
try:
    with open('config.json') as config_file:
        config = json.load(config_file)
        LIVE_STOCK_CHANNEL_ID = int(config.get('id_live_stock', 0))
except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
    logger.error(f"Failed to load config.json: {e}")
    raise Exception("Config file is missing or invalid")
except Exception as e:
    logger.error(f"Unexpected error loading config: {e}")
    raise

if not LIVE_STOCK_CHANNEL_ID:
    raise Exception("LIVE_STOCK_CHANNEL_ID must be set in config.json")

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

    def clear_category(self, category: str):
        """Clear all items in a specific category"""
        keys_to_delete = [
            key for key, data in self._cache.items()
            if data.get('category') == category
        ]
        for key in keys_to_delete:
            self.delete(key)

class InteractionManager:
    def __init__(self):
        self._cache = SmartCache()
        self._lock = asyncio.Lock()
        self._error_counts = {}
        
    async def can_interact(self, interaction: discord.Interaction) -> bool:
        user_id = str(interaction.user.id)
        
        async with self._lock:
            if self._cache.get(user_id, 'cooldown'):
                try:
                    if not interaction.response.is_done():
                        await interaction.response.send_message(
                            embed=discord.Embed(
                                title="⏳ Cooldown Active",
                                description="Please wait before trying again.",
                                color=discord.Color.yellow()
                            ),
                            ephemeral=True
                        )
                except Exception as e:
                    logger.error(f"Error sending cooldown message: {e}")
                return False
                
            self._cache.set(user_id, True, 'cooldown')
            return True
    
    def reset_cooldown(self, user_id: str):
        self._cache.delete(user_id)

    async def handle_error(self, interaction: discord.Interaction, error: Exception, action: str):
        """Handle interaction errors with retry logic"""
        error_key = f"{interaction.user.id}:{action}"
        self._error_counts[error_key] = self._error_counts.get(error_key, 0) + 1
        
        logger.error(f"Error in {action} ({self._error_counts[error_key]} attempts): {error}")
        
        if self._error_counts[error_key] >= 3:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Service Error",
                    description="An error occurred. Please try again later.",
                    color=discord.Color.red()
                ),
                ephemeral=True
            )
            self._error_counts[error_key] = 0
            return False
            
        return True

class SetGrowIDModal(Modal):
    def __init__(self, bot):
        super().__init__(title="🎮 Set Your GrowID")
        self.bot = bot
        self.balance_manager = BalanceManagerService(bot)
        self._lock = asyncio.Lock()
        self.logger = logging.getLogger("SetGrowIDModal")

    growid = TextInput(
        label="Enter Your GrowID",
        placeholder="Your Growtopia account name",
        min_length=3,
        max_length=20,
        required=True,
        style=discord.TextStyle.short
    )

    async def on_submit(self, interaction: discord.Interaction):
        async with self._lock:
            if getattr(interaction, '_responded', False):
                return
                
            try:
                await interaction.response.defer(ephemeral=True)
                setattr(interaction, '_responded', True)
                
                growid = self.growid.value.strip()
                
                # Validate GrowID format
                if not growid.isalnum():
                    await interaction.followup.send(
                        embed=discord.Embed(
                            title="❌ Invalid GrowID",
                            description="GrowID must contain only letters and numbers.",
                            color=discord.Color.red()
                        ),
                        ephemeral=True
                    )
                    return

                # Check if GrowID already registered
                existing = await self.balance_manager.get_user_by_growid(growid)
                if existing and existing != interaction.user.id:
                    await interaction.followup.send(
                        embed=discord.Embed(
                            title="⚠️ GrowID Already Registered",
                            description="This GrowID is already linked to another account.",
                            color=discord.Color.yellow()
                        ),
                        ephemeral=True
                    )
                    return

                # Check if user already has a GrowID
                current_growid = await self.balance_manager.get_growid(interaction.user.id)
                if current_growid:
                    confirm = discord.Embed(
                        title="⚠️ GrowID Already Set",
                        description=f"Your current GrowID is `{current_growid}`\nDo you want to update it to `{growid}`?",
                        color=discord.Color.yellow()
                    )
                    await interaction.followup.send(embed=confirm, ephemeral=True)
                    return

                success = await self.balance_manager.register_user(interaction.user.id, growid)
                if success:
                    embed = discord.Embed(
                        title="✅ GrowID Successfully Registered!",
                        description=(
                            f"Your GrowID has been set to `{growid}`\n\n"
                            "**What's Next?**\n"
                            "> 💰 Check your balance\n"
                            "> 🛍️ Browse our products\n"
                            "> 🌍 Visit our trading world"
                        ),
                        color=discord.Color.green()
                    )
                    embed.set_footer(text="Thank you for registering!")
                    await interaction.followup.send(embed=embed, ephemeral=True)
                else:
                    raise Exception("Failed to register")

            except Exception as e:
                self.logger.error(f"Error in SetGrowIDModal: {e}")
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="❌ Registration Failed",
                        description="An error occurred. Please try again later.",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )

class BuyModal(Modal):
    def __init__(self, bot, products):
        super().__init__(title="🛍️ Purchase Product")
        self.bot = bot
        self.products = products
        self.balance_manager = BalanceManagerService(bot)
        self.product_manager = ProductManagerService(bot)
        self.trx_manager = TransactionManager(bot)
        self._lock = asyncio.Lock()
        self._cache = SmartCache()
        self.logger = logging.getLogger("BuyModal")

    code = TextInput(
        label="Product Code",
        placeholder="Enter product code (e.g. DL1)",
        min_length=1,
        max_length=10,
        required=True,
        style=discord.TextStyle.short
    )

    quantity = TextInput(
        label="Quantity",
        placeholder="How many do you want to buy? (1-99)",
        min_length=1,
        max_length=2,
        required=True,
        style=discord.TextStyle.short
    )

    async def on_submit(self, interaction: discord.Interaction):
        cache_key = f"{interaction.user.id}:{int(time.time())}"
        if self._cache.get(cache_key, 'purchase'):
            return
            
        async with self._lock:
            if getattr(interaction, '_responded', False):
                return
                
            try:
                await interaction.response.defer(ephemeral=True)
                setattr(interaction, '_responded', True)
                self._cache.set(cache_key, True, 'purchase')
                
                code = self.code.value.upper()
                
                # Validate product exists
                product = next((p for p in self.products if p['code'] == code), None)
                if not product:
                    await interaction.followup.send(
                        embed=discord.Embed(
                            title="❌ Invalid Product",
                            description="This product code doesn't exist.",
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
                stock = await self.product_manager.get_stock_count(code)
                if stock <= 0:
                    await interaction.followup.send(
                        embed=discord.Embed(
                            title="❌ Out of Stock",
                            description="This product is currently out of stock.",
                            color=discord.Color.red()
                        ),
                        ephemeral=True
                    )
                    return
                    
                if stock < quantity:
                    await interaction.followup.send(
                        embed=discord.Embed(
                            title="⚠️ Insufficient Stock",
                            description=f"Only {stock} units available.",
                            color=discord.Color.yellow()
                        ),
                        ephemeral=True
                    )
                    return

                # Check GrowID and balance
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
                                f"Required: `{total_cost:,} WL`\n"
                                f"Your balance: `{balance:,} WL`\n"
                                f"Missing: `{total_cost - balance:,} WL`"
                            ),
                            color=discord.Color.red()
                        ),
                        ephemeral=True
                    )
                    return

                # Process purchase
                result = await self.trx_manager.process_purchase(
                    growid=growid,
                    product_code=code,
                    quantity=quantity
                )

                if result:
                    embed = discord.Embed(
                        title="✅ Purchase Successful!",
                        description=f"Thank you for your purchase, `{growid}`!",
                        color=discord.Color.green()
                    )
                    
                    embed.add_field(
                        name="📦 Order Details",
                        value=(
                            f"```\n"
                            f"Product  : {product['name']}\n"
                            f"Quantity : {quantity}x\n"
                            f"Price    : {product['price']:,} WL each\n"
                            f"Total    : {total_cost:,} WL\n"
                            f"```"
                        ),
                        inline=False
                    )
                    
                    embed.add_field(
                        name="💰 Balance",
                        value=(
                            f"```\n"
                            f"Previous : {balance:,} WL\n"
                            f"Current  : {balance - total_cost:,} WL\n"
                            f"```"
                        ),
                        inline=False
                    )

                    if result.get('items'):
                        items_text = "\n".join(f"> `{item}`" for item in result['items'])
                        embed.add_field(
                            name="🎁 Your Items",
                            value=items_text,
                            inline=False
                        )

                    embed.set_footer(text="Thank you for your purchase!")
                    await interaction.followup.send(embed=embed, ephemeral=True)
                else:
                    raise Exception("Transaction failed")

            except Exception as e:
                self.logger.error(f"Error in BuyModal: {e}")
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="❌ Purchase Failed",
                        description=(
                            "An error occurred during purchase.\n"
                            "Your balance has not been deducted.\n"
                            "Please try again later."
                        ),
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
            finally:
                self._cache.delete(cache_key)

class StockView(View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot
        self.balance_manager = BalanceManagerService(bot)
        self.product_manager = ProductManagerService(bot)
        self.interaction_manager = InteractionManager()
        self.logger = logging.getLogger("StockView")
        self._error_counts = {}
        self._max_retries = 3

    async def handle_response(self, interaction: discord.Interaction, action: str):
        """Centralized interaction handler with error tracking"""
        try:
            if not await self.interaction_manager.can_interact(interaction):
                return False
                
            # Reset error count on successful interaction
            if action in self._error_counts:
                del self._error_counts[action]
                
            return True
            
        except Exception as e:
            self.logger.error(f"Error in {action}: {e}")
            
            # Track errors
            self._error_counts[action] = self._error_counts.get(action, 0) + 1
            
            if self._error_counts[action] >= self._max_retries:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="❌ Service Temporarily Unavailable",
                        description="Please try again later.",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
                return False
                
            return False

    @discord.ui.button(
        label="Balance",
        emoji="💰",
        style=discord.ButtonStyle.primary,
        custom_id="balance:1"
    )
    async def button_balance_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.handle_response(interaction, "balance"):
            return

        try:
            await interaction.response.defer(ephemeral=True)
            
            growid = await self.balance_manager.get_growid(interaction.user.id)
            if not growid:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="❌ GrowID Not Set",
                        description=(
                            "You haven't set your GrowID yet!\n\n"
                            "**How to Set GrowID:**\n"
                            "> 1. Click the `Set GrowID` button\n"
                            "> 2. Enter your Growtopia account name\n"
                            "> 3. Click Submit"
                        ),
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
                return

            balance = await self.balance_manager.get_balance(growid)
            
            embed = discord.Embed(
                title="💰 Balance Information",
                description=f"Account details for `{growid}`",
                color=discord.Color.green(),
                timestamp=datetime.utcnow()
            )
            
            embed.add_field(
                name="Current Balance",
                value=f"```{balance:,} WL```",
                inline=False
            )
            
            transactions = await self.balance_manager.get_recent_transactions(growid, limit=3)
            if transactions:
                recent_txs = "\n".join(
                    f"> {tx['type']}: {tx['amount']:,} WL - {tx['date']}"
                    for tx in transactions
                )
                embed.add_field(
                    name="Recent Transactions",
                    value=recent_txs,
                    inline=False
                )

            embed.set_footer(text=f"Last Updated • {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            self.logger.error(f"Error in balance callback: {e}")
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Error",
                    description="Failed to fetch balance. Please try again.",
                    color=discord.Color.red()
                ),
                ephemeral=True
            )

    @discord.ui.button(
        label="Buy",
        emoji="🛒",
        style=discord.ButtonStyle.success,
        custom_id="buy:1"
    )
    async def button_buy_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.handle_response(interaction, "buy"):
            return

        try:
            growid = await self.balance_manager.get_growid(interaction.user.id)
            if not growid:
                await interaction.response.send_message(
                    embed=discord.Embed(
                        title="❌ GrowID Required",
                        description=(
                            "You need to set your GrowID before making a purchase!\n\n"
                            "**How to Set GrowID:**\n"
                            "> 1. Click the `Set GrowID` button\n"
                            "> 2. Enter your Growtopia account name\n"
                            "> 3. Click Submit"
                        ),
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
                return

            products = await self.product_manager.get_all_products()
            if not products:
                await interaction.response.send_message(
                    embed=discord.Embed(
                        title="❌ No Products Available",
                        description="Our product list is currently empty. Please try again later.",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
                return

            modal = BuyModal(self.bot, products)
            await interaction.response.send_modal(modal)

        except Exception as e:
            self.logger.error(f"Error in buy callback: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    embed=discord.Embed(
                        title="❌ Error",
                        description="An error occurred. Please try again later.",
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
        if not await self.handle_response(interaction, "set_growid"):
            return

        try:
            modal = SetGrowIDModal(self.bot)
            await interaction.response.send_modal(modal)
        except Exception as e:
            self.logger.error(f"Error in set growid callback: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    embed=discord.Embed(
                        title="❌ Error",
                        description="An error occurred. Please try again later.",
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
        if not await self.handle_response(interaction, "world"):
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
                description=(
                    "Visit our trading world!\n\n"
                    "**Trading Rules:**\n"
                    "> 1. Be respectful to others\n"
                    "> 2. No spamming or advertising\n"
                    "> 3. Follow world rules\n"
                    "> 4. Report any issues to moderators"
                ),
                color=discord.Color.blue(),
                timestamp=datetime.utcnow()
            )
            
            embed.add_field(
                name="World Name",
                value=f"```{world_info['world']}```",
                inline=True
            )
            
            if world_info.get('owner'):
                embed.add_field(
                    name="Owner",
                    value=f"```{world_info['owner']}```",
                    inline=True
                )
            
            if world_info.get('bot'):
                embed.add_field(
                    name="Bot Name",
                    value=f"```{world_info['bot']}```",
                    inline=True
                )

            if world_info.get('status'):
                embed.add_field(
                    name="Status",
                    value=f"```{world_info['status']}```",
                    inline=False
                )

            embed.set_footer(text=f"Last Updated • {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            self.logger.error(f"Error in world callback: {e}")
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Error",
                    description="An error occurred. Please try again later.",
                    color=discord.Color.red()
                ),
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
        self._consecutive_errors = 0
        self._max_errors = 5
        self._recovery_delay = 60  # seconds
        
        # Add view persistence
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
                    "> 👥 **24/7 Support**\n\n"
                    "**How to Purchase:**\n"
                    "1. Set your GrowID (🔑)\n"
                    "2. Check your balance (💰)\n"
                    "3. Click Buy (🛒)\n"
                    "4. Enter product code & quantity"
                ),
                color=discord.Color.gold(),
                timestamp=datetime.utcnow()
            )

            if products:
                for product in sorted(products, key=lambda x: x['code']):
                    stock_count = await self.product_manager.get_stock_count(product['code'])
                    status_emoji = "🟢" if stock_count > 0 else "🔴"
                    
                    value = (
                        f"```ml\n"
                        f"Code     : {product['code']}\n"
                        f"Status   : {status_emoji} {'In Stock' if stock_count > 0 else 'Out of Stock'}\n"
                        f"Stock    : {stock_count} units\n"
                        f"Price    : {product['price']:,} WL\n"
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
                embed.add_field(
                    name="No Products Available",
                    value="Products will be added soon!",
                    inline=False
                )

            embed.set_footer(
                text=f"Last Updated • {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
            )
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

            try:
                if self.message_id:
                    try:
                        message = await channel.fetch_message(self.message_id)
                        await message.edit(embed=embed, view=self.stock_view)
                    except discord.NotFound:
                        # Message was deleted, create new one
                        message = await channel.send(embed=embed, view=self.stock_view)
                        self.message_id = message.id
                else:
                    # Clean up old messages if any
                    async for old_message in channel.history(limit=5):
                        if old_message.author == self.bot.user:
                            try:
                                await old_message.delete()
                            except:
                                pass
                    
                    message = await channel.send(embed=embed, view=self.stock_view)
                    self.message_id = message.id

                self.last_update = datetime.utcnow().timestamp()
                self._consecutive_errors = 0  # Reset error counter on success
                
            except Exception as e:
                self.logger.error(f"Error updating message: {e}")
                self._consecutive_errors += 1
                
                if self._consecutive_errors >= self._max_errors:
                    self.update_stock.cancel()
                    self.logger.critical("Update stock task stopped due to too many errors")
                    await asyncio.sleep(self._recovery_delay)
                    self.update_stock.start()  # Restart task after delay
                
                # Try to send a new message if edit failed
                message = await channel.send(embed=embed, view=self.stock_view)
                self.message_id = message.id

        except Exception as e:
            self.logger.error(f"Error in update_stock: {e}")
            self._consecutive_errors += 1
            
            if self._consecutive_errors >= self._max_errors:
                self.logger.critical(f"Stopping update_stock task after {self._consecutive_errors} consecutive errors")
                self.update_stock.cancel()
                # Will attempt to restart after recovery delay
                await asyncio.sleep(self._recovery_delay)
                if not self.update_stock.is_running():
                    self.update_stock.start()

    @update_stock.before_loop
    async def before_update_stock(self):
        """Wait for bot to be ready before starting the loop"""
        await self.bot.wait_until_ready()
        self.logger.info("Starting update_stock loop")

    async def cleanup(self):
        """Cleanup resources when unloading"""
        try:
            if self.message_id:
                channel = self.bot.get_channel(LIVE_STOCK_CHANNEL_ID)
                if channel:
                    try:
                        message = await channel.fetch_message(self.message_id)
                        await message.delete()
                    except discord.NotFound:
                        pass
                    except Exception as e:
                        self.logger.error(f"Error deleting message during cleanup: {e}")
        except Exception as e:
            self.logger.error(f"Error during cleanup: {e}")

    def cog_unload(self):
        """Handle cog unloading"""
        self.logger.info("Unloading LiveStock cog")
        self.update_stock.cancel()
        asyncio.create_task(self.cleanup())

async def setup(bot):
    """Setup the LiveStock cog"""
    try:
        await bot.add_cog(LiveStock(bot))
        logger.info(
            f"LiveStock cog loaded successfully\n"
            f"Bot Version: {getattr(bot, 'version', 'unknown')}\n"
            f"Python Version: {platform.python_version()}\n"
            f"Discord.py Version: {discord.__version__}\n"
            f"Startup Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
            f"Current User: {getattr(bot, 'user', 'unknown')}"
        )
    except Exception as e:
        logger.error(f"Error loading LiveStock cog: {e}")
        raise