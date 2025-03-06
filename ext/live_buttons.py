import logging
import asyncio
from typing import Optional, List, Dict
from datetime import datetime

import discord
from discord.ext import commands
from discord.ui import Button, View, Modal, TextInput, Select

from .constants import TransactionType, COLORS
from .base_handler import BaseLockHandler
from .cache_manager import CacheManager
from .product_manager import ProductManagerService
from .balance_manager import BalanceManagerService
from .trx import TransactionManager

class SetGrowIDModal(Modal):
    def __init__(self):
        super().__init__(title="📝 Register Your GrowID")
        
        self.growid = TextInput(
            label="Enter your GrowID",
            placeholder="Your Growtopia ID...",
            min_length=3,
            max_length=20,
            style=discord.TextStyle.short,
            required=True
        )
        self.add_item(self.growid)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            balance_manager = BalanceManagerService(interaction.client)
            await balance_manager.register_user(
                str(interaction.user.id),
                self.growid.value
            )
            
            embed = discord.Embed(
                title="✅ Registration Successful",
                description=(
                    f"```yaml\n"
                    f"GrowID: {self.growid.value}\n"
                    f"Status: Registered Successfully\n"
                    f"```"
                ),
                color=COLORS['success']
            )
            embed.set_footer(text="You can now use all shop features!")
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            error_embed = discord.Embed(
                title="❌ Registration Failed",
                description=f"```diff\n- Error: {str(e)}```",
                color=COLORS['error']
            )
            await interaction.followup.send(embed=error_embed, ephemeral=True)

class PurchaseModal(Modal):
    def __init__(self, product: Dict):
        super().__init__(title=f"🛒 Purchase {product['name']}")
        self.product = product
        
        self.quantity = TextInput(
            label=f"Quantity (Max: {product.get('stock', 0)})",
            placeholder="Enter amount to buy...",
            min_length=1,
            max_length=3,
            style=discord.TextStyle.short,
            required=True
        )
        self.add_item(self.quantity)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            quantity = int(self.quantity.value)
            if quantity <= 0:
                raise ValueError("Quantity must be positive")
            
            trx_manager = TransactionManager(interaction.client)
            result = await trx_manager.process_purchase(
                str(interaction.user.id),
                self.product['code'],
                quantity
            )
            
            # Create stylish success embed
            embed = discord.Embed(
                title="🎉 Purchase Successful!",
                color=COLORS['success']
            )
            
            # Add purchase details
            embed.add_field(
                name="📦 Product Details",
                value=(
                    f"```yml\n"
                    f"Item: {self.product['name']}\n"
                    f"Quantity: {quantity}x\n"
                    f"Total Paid: {result['total_paid']:,} WL\n"
                    f"```"
                ),
                inline=False
            )
            
            # Add content details in spoiler
            content_text = "\n".join([f"• ||{content}||" for content in result['content']])
            embed.add_field(
                name="🔐 Your Items (Click to Reveal)",
                value=content_text,
                inline=False
            )
            
            # Add footer with timestamp
            embed.set_footer(text="Thank you for your purchase!")
            embed.timestamp = datetime.utcnow()
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            error_embed = discord.Embed(
                title="❌ Purchase Failed",
                description=f"```diff\n- Error: {str(e)}```",
                color=COLORS['error']
            )
            await interaction.followup.send(embed=error_embed, ephemeral=True)

class ProductSelect(Select):
    def __init__(self, products: List[Dict]):
        options = [
            discord.SelectOption(
                label=f"{p['name']} ({p['price']:,} WL)",
                value=p['code'],
                description=f"Stock: {p.get('stock', 0)} | {p.get('description', 'No description')}",
                emoji="🏷️"
            ) for p in products
        ]
        super().__init__(
            placeholder="Select a product to purchase...",
            min_values=1,
            max_values=1,
            options=options
        )
        self.products = {p['code']: p for p in products}

    async def callback(self, interaction: discord.Interaction):
        product = self.products[self.values[0]]
        modal = PurchaseModal(product)
        await interaction.response.send_modal(modal)

class ShopView(View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot
        self.cache_manager = CacheManager()
        self.product_manager = ProductManagerService(bot)
        self.balance_manager = BalanceManagerService(bot)
        self.trx_manager = TransactionManager(bot)
        
        # Add styled buttons
        self.add_item(
            Button(
                style=discord.ButtonStyle.primary,
                custom_id="register",
                label="Register GrowID",
                emoji="📝"
            )
        )
        self.add_item(
            Button(
                style=discord.ButtonStyle.success,
                custom_id="balance",
                label="My Balance",
                emoji="💰"
            )
        )
        self.add_item(
            Button(
                style=discord.ButtonStyle.success,
                custom_id="buy",
                label="Shop Items",
                emoji="🛒"
            )
        )
        self.add_item(
            Button(
                style=discord.ButtonStyle.secondary,
                custom_id="history",
                label="History",
                emoji="📋"
            )
        )

    @discord.ui.button(custom_id="register")
    async def register_callback(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_modal(SetGrowIDModal())

    @discord.ui.button(custom_id="balance")
    async def balance_callback(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        try:
            growid = await self.balance_manager.get_growid(str(interaction.user.id))
            if not growid:
                raise ValueError("Please register your GrowID first!")

            balance = await self.balance_manager.get_balance(growid)
            if not balance:
                raise ValueError("Could not retrieve balance")

            embed = discord.Embed(
                title="💎 Your Balance",
                color=COLORS['info']
            )
            
            # Add user info
            embed.add_field(
                name="👤 Account Info",
                value=(
                    f"```yml\n"
                    f"GrowID: {growid}\n"
                    f"Discord: {interaction.user.name}\n"
                    f"```"
                ),
                inline=False
            )
            
            # Add balance info with currency icons
            embed.add_field(
                name="💰 Current Balance",
                value=(
                    f"```yml\n"
                    f"World Locks: {balance.wl:,} WL\n"
                    f"Diamond Locks: {balance.dl:,} DL\n"
                    f"Blue Gem Locks: {balance.bgl:,} BGL\n"
                    f"```"
                ),
                inline=False
            )
            
            # Add total in WL
            embed.add_field(
                name="💵 Total Value",
                value=f"```fix\n{balance.total_wl():,} WL```",
                inline=False
            )
            
            embed.set_thumbnail(url=interaction.user.display_avatar.url)
            embed.timestamp = datetime.utcnow()
            
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            error_embed = discord.Embed(
                title="❌ Error",
                description=f"```diff\n- {str(e)}```",
                color=COLORS['error']
            )
            await interaction.followup.send(embed=error_embed, ephemeral=True)

    @discord.ui.button(custom_id="buy")
    async def buy_callback(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        try:
            # Check registration
            growid = await self.balance_manager.get_growid(str(interaction.user.id))
            if not growid:
                raise ValueError("Please register your GrowID first!")

            # Get available products
            products = await self.product_manager.get_all_products()
            available_products = []
            
            for product in products:
                stock_count = await self.product_manager.get_stock_count(product['code'])
                if stock_count > 0:
                    product['stock'] = stock_count
                    available_products.append(product)

            if not available_products:
                raise ValueError("No products available at the moment")

            embed = discord.Embed(
                title="🏪 Shop Items",
                description="Select a product from the menu below to purchase",
                color=COLORS['info']
            )

            # Add product showcase
            for product in available_products:
                embed.add_field(
                    name=f"{product['name']} ({product['code']})",
                    value=(
                        f"```yml\n"
                        f"Price: {product['price']:,} WL\n"
                        f"Stock: {product['stock']} units\n"
                        f"```"
                        f"{product.get('description', 'No description')}"
                    ),
                    inline=True
                )

            view = View(timeout=300)
            view.add_item(ProductSelect(available_products))
            
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

        except Exception as e:
            error_embed = discord.Embed(
                title="❌ Error",
                description=f"```diff\n- {str(e)}```",
                color=COLORS['error']
            )
            await interaction.followup.send(embed=error_embed, ephemeral=True)

    @discord.ui.button(custom_id="history")
    async def history_callback(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        try:
            growid = await self.balance_manager.get_growid(str(interaction.user.id))
            if not growid:
                raise ValueError("Please register your GrowID first!")

            history = await self.balance_manager.get_transaction_history(growid, limit=5)
            if not history:
                raise ValueError("No transaction history found")

            embed = discord.Embed(
                title="📊 Transaction History",
                description=f"Recent transactions for `{growid}`",
                color=COLORS['info']
            )

            for i, trx in enumerate(history, 1):
                # Get transaction emoji
                emoji = "💰" if trx['type'] == TransactionType.DEPOSIT.value else "🛒" if trx['type'] == TransactionType.PURCHASE.value else "💸"
                
                # Format timestamp
                timestamp = datetime.fromisoformat(trx['created_at'].replace('Z', '+00:00'))
                
                embed.add_field(
                    name=f"{emoji} Transaction #{i}",
                    value=(
                        f"```yml\n"
                        f"Type: {trx['type']}\n"
                        f"Date: {timestamp.strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
                        f"Details: {trx['details']}\n"
                        f"Old Balance: {trx['old_balance']}\n"
                        f"New Balance: {trx['new_balance']}\n"
                        f"```"
                    ),
                    inline=False
                )

            embed.set_footer(text="Showing last 5 transactions")
            embed.timestamp = datetime.utcnow()
            
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            error_embed = discord.Embed(
                title="❌ Error",
                description=f"```diff\n- {str(e)}```",
                color=COLORS['error']
            )
            await interaction.followup.send(embed=error_embed, ephemeral=True)

class LiveButtonManager(BaseLockHandler):
    _instance = None
    _instance_lock = asyncio.Lock()

    def __new__(cls, bot):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.initialized = False
        return cls._instance

    def __init__(self, bot):
        if not self.initialized:
            super().__init__()
            self.bot = bot
            self.logger = logging.getLogger("LiveButtonManager")
            self.cache_manager = CacheManager()
            self.stock_channel_id = int(self.bot.config.get('id_live_stock', 0))
            self.current_button_message: Optional[discord.Message] = None
            self.initialized = True

    async def get_or_create_button_message(self) -> Optional[discord.Message]:
        """Get existing button message or create new one"""
        if not self.stock_channel_id:
            self.logger.error("Stock channel ID not configured!")
            return None

        channel = self.bot.get_channel(self.stock_channel_id)
        if not channel:
            self.logger.error(f"Could not find stock channel {self.stock_channel_id}")
            return None

        try:
            message_id = await self.cache_manager.get("live_buttons_message_id")
            if message_id:
                try:
                    message = await channel.fetch_message(message_id)
                    self.current_button_message = message
                    return message
                except discord.NotFound:
                    await self.cache_manager.delete("live_buttons_message_id")
                except Exception as e:
                    self.logger.error(f"Error fetching button message: {e}")

            # Create new message with modern embed
            embed = discord.Embed(
                title="🎮 Shop Controls",
                description=(
                    "```yml\n"
                    "Welcome to our Growtopia Shop!\n"
                    "Use the buttons below to interact\n"
                    "```"
                ),
                color=0x2b2d31
            )
            
            # Add quick guide
            embed.add_field(
                name="📝 Quick Guide",
                value=(
                    "```md\n"
                    "1. Register your GrowID\n"
                    "2. Check your balance\n"
                    "3. Browse available items\n"
                    "4. Make a purchase\n"
                    "5. Track your transactions\n"
                    "```"
                ),
                inline=False
            )
            
            # Add support info
            embed.add_field(
                name="📞 Need Help?",
                value=(
                    "```yml\n"
                    "Contact our support team for assistance\n"
                    "Available 24/7\n"
                    "```"
                ),
                inline=False
            )
            
            embed.set_footer(
                text="Shop System v2.0",
                icon_url=self.bot.user.display_avatar.url
            )
            embed.timestamp = datetime.utcnow()
            
            message = await channel.send(
                embed=embed,
                view=ShopView(self.bot)
            )
            
            self.current_button_message = message
            
            # Cache the message ID
            await self.cache_manager.set(
                "live_buttons_message_id", 
                message.id,
                expires_in=86400,  # 24 hours
                permanent=True
            )
            
            return message

        except Exception as e:
            self.logger.error(f"Error creating button message: {e}")
            return None

    async def update_buttons(self) -> bool:
        """Update the button message"""
        try:
            message = await self.get_or_create_button_message()
            if not message:
                return False

            # Update view with fresh buttons
            await message.edit(view=ShopView(self.bot))
            return True

        except Exception as e:
            self.logger.error(f"Error updating buttons: {e}")
            return False

    async def cleanup(self):
        """Cleanup resources"""
        try:
            if self.current_button_message:
                embed = discord.Embed(
                    title="🛠️ Shop Maintenance",
                    description="```diff\n- Shop is currently offline\n- Please wait for maintenance to complete\n```",
                    color=COLORS['warning']
                )
                await self.current_button_message.edit(
                    embed=embed,
                    view=None
                )
        except Exception as e:
            self.logger.error(f"Error in cleanup: {e}")

class LiveButtonsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.button_manager = LiveButtonManager(bot)
        self.logger = logging.getLogger("LiveButtonsCog")

    @commands.Cog.listener()
    async def on_ready(self):
        """Setup buttons when bot is ready"""
        await self.button_manager.update_buttons()

    async def cog_load(self):
        self.logger.info("LiveButtonsCog loading...")

    async def cog_unload(self):
        await self.button_manager.cleanup()
        self.logger.info("LiveButtonsCog unloaded")

async def setup(bot):
    if not hasattr(bot, 'live_buttons_loaded'):
        await bot.add_cog(LiveButtonsCog(bot))
        bot.live_buttons_loaded = True
        logging.info(
            f'LiveButtons cog loaded successfully at '
            f'{datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")} UTC'
        )