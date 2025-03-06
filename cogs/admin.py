import discord
from discord.ext import commands
import logging
from datetime import datetime, timedelta
import json
import asyncio
from typing import Optional, List
import io
import psutil
import platform
import aiohttp

# Import dari file yang sudah diperbarui
from ext.constants import (
    COLORS,
    CURRENCY_RATES,
    TRANSACTION_TYPES,
    MAX_STOCK_FILE_SIZE,
    VALID_STOCK_FORMATS,
    STATUS_AVAILABLE,
    STATUS_SOLD
)
from ext.balance_manager import BalanceManagerService
from ext.product_manager import ProductManagerService
from ext.trx import TransactionManager
from ext.base_handler import BaseLockHandler, BaseResponseHandler
from ext.cache_manager import CacheManager
from database import get_connection

class AdminCog(commands.Cog, BaseLockHandler, BaseResponseHandler):
    def __init__(self, bot):
        super().__init__()  # Initialize BaseLockHandler dan BaseResponseHandler
        self.bot = bot
        self.logger = logging.getLogger("AdminCog")
        
        # Initialize services dengan cache manager
        self.cache_manager = CacheManager()
        self.balance_service = BalanceManagerService(bot)
        self.product_service = ProductManagerService(bot)
        self.trx_manager = TransactionManager(bot)
        
        # Load admin configuration dengan proper error handling
        try:
            with open('config.json') as f:
                config = json.load(f)
                self.admin_id = int(config.get('admin_id'))
                if not self.admin_id:
                    raise ValueError("admin_id not found in config.json")
                self.logger.info(f"Admin ID loaded: {self.admin_id}")
        except Exception as e:
            self.logger.critical(f"Failed to load admin configuration: {e}")
            raise

    async def _check_admin(self, ctx) -> bool:
        """Check if user has admin permissions with cache"""
        cache_key = f"admin_check_{ctx.author.id}"
        is_admin = await self.cache_manager.get(cache_key)
        
        if is_admin is None:
            is_admin = ctx.author.id == self.admin_id
            await self.cache_manager.set(cache_key, is_admin, expires_in=3600)
            
        if not is_admin:
            embed = discord.Embed(
                title="❌ Access Denied",
                description="```diff\n- You don't have permission to use admin commands!```",
                color=COLORS['error']
            )
            await self.send_response_once(ctx, embed=embed)
            self.logger.warning(
                f"Unauthorized access attempt by {ctx.author} (ID: {ctx.author.id})"
            )
        return is_admin

    async def _process_command(self, ctx, command_name: str, callback) -> bool:
        """Process command with proper locking, caching, and response handling"""
        if hasattr(ctx, 'is_being_processed'):
            return False

        ctx.is_being_processed = True
        cache_key = f"admin_command_{command_name}_{ctx.author.id}"
        lock = await self.acquire_lock(cache_key)

        if not lock:
            await self.send_response_once(
                ctx, 
                embed=discord.Embed(
                    title="⏳ System Busy",
                    description="```diff\n- System is busy, please try again later```",
                    color=COLORS['warning']
                )
            )
            return False

        try:
            if not await self._check_admin(ctx):
                return False

            response_lock = await self.acquire_response_lock(ctx)
            if not response_lock:
                return False

            try:
                await callback()
                return True
            finally:
                self.release_response_lock(ctx)

        except Exception as e:
            self.logger.error(f"Error in {command_name}: {str(e)}", exc_info=True)
            error_embed = discord.Embed(
                title="❌ Error Occurred",
                description=f"```diff\n- {str(e)}```",
                color=COLORS['error']
            )
            await self.send_response_once(ctx, embed=error_embed)
            return False
        finally:
            self.release_lock(cache_key)
            delattr(ctx, 'is_being_processed')

    async def _process_stock_file(self, attachment) -> List[str]:
        """Process uploaded stock file with improved validation"""
        if attachment.size > MAX_STOCK_FILE_SIZE:
            raise ValueError(
                f"File too large! Maximum size is {MAX_STOCK_FILE_SIZE/1024:.0f}KB"
            )
            
        file_ext = attachment.filename.split('.')[-1].lower()
        if file_ext not in VALID_STOCK_FORMATS:
            raise ValueError(
                f"Invalid file format! Supported formats: {', '.join(VALID_STOCK_FORMATS)}"
            )
            
        content = await attachment.read()
        try:
            text = content.decode('utf-8').strip()
        except UnicodeDecodeError:
            raise ValueError("Invalid file encoding. Please use UTF-8")
        
        items = [line.strip() for line in text.split('\n') if line.strip()]
        if not items:
            raise ValueError("No valid items found in file!")
            
        return items

    async def _confirm_action(self, ctx, message: str, timeout: int = 30) -> bool:
        """Get confirmation for dangerous actions"""
        embed = discord.Embed(
            title="⚠️ Confirmation Required",
            description=message,
            color=COLORS['warning']
        )
        confirm_msg = await ctx.send(embed=embed)
        
        await confirm_msg.add_reaction('✅')
        await confirm_msg.add_reaction('❌')

        try:
            reaction, user = await self.bot.wait_for(
                'reaction_add',
                timeout=timeout,
                check=lambda r, u: u == ctx.author and str(r.emoji) in ['✅', '❌']
            )
            return str(reaction.emoji) == '✅'
        except asyncio.TimeoutError:
            await self.send_response_once(
                ctx, 
                embed=discord.Embed(
                    title="⏰ Timeout",
                    description="```diff\n- Operation cancelled due to timeout```",
                    color=COLORS['error']
                )
            )
            return False

    @commands.command(name="adminhelp")
    async def admin_help(self, ctx):
        """Show admin commands"""
        async def execute():
            embed = discord.Embed(
                title="🛠️ Admin Commands",
                description="Available administrative commands",
                color=COLORS['blue'],
                timestamp=datetime.utcnow()
            )

            command_categories = {
                "Product Management": [
                    "`addproduct <code> <name> <price> [description]`\nAdd new product",
                    "`editproduct <code> <field> <value>`\nEdit product details",
                    "`deleteproduct <code>`\nDelete product",
                    "`addstock <code>`\nAdd stock with file attachment",
                    "`addworld <name> [description]`\nAdd world information"
                ],
                "Balance Management": [
                    "`addbal <growid> <amount> <WL/DL/BGL>`\nAdd balance",
                    "`removebal <growid> <amount> <WL/DL/BGL>`\nRemove balance",
                    "`checkbal <growid>`\nCheck balance",
                    "`resetuser <growid>`\nReset balance"
                ],
                "Transaction Management": [
                    "`trxhistory <growid> [limit]`\nView transactions",
                    "`stockhistory <code> [limit]`\nView stock history"
                ],
                "System Management": [
                    "`systeminfo`\nShow bot system information",
                    "`announcement <message>`\nSend announcement to all users",
                    "`maintenance <on/off>`\nToggle maintenance mode",
                    "`blacklist <add/remove> <growid>`\nManage blacklisted users",
                    "`backup`\nCreate database backup"
                ]
            }

            for category, commands in command_categories.items():
                embed.add_field(
                    name=f"📋 {category}",
                    value="\n\n".join(commands),
                    inline=False
                )

            embed.set_footer(text=f"Requested by {ctx.author}")
            await self.send_response_once(ctx, embed=embed)

        await self._process_command(ctx, "adminhelp", execute)

    @commands.command(name="addworld")
    async def add_world(self, ctx, world_name: str, *, description: str = None):
        """Add world info"""
        async def execute():
            # Validation
            if not world_name or not world_name.isalnum():
                raise ValueError("World name must be alphanumeric")
                
            world_name = world_name.upper()
            
            # Add world using product service
            result = await self.product_service.add_world(
                world_name=world_name,
                description=description or "No description",
                added_by=str(ctx.author.id)
            )
            
            embed = discord.Embed(
                title="✅ World Added",
                color=COLORS['success'],
                timestamp=datetime.utcnow()
            )
            
            embed.add_field(
                name="🌍 World Details",
                value=(
                    f"```yml\n"
                    f"Name: {result['name']}\n"
                    f"Description: {result['description']}\n"
                    f"Status: {result['status']}\n"
                    f"Added By: {ctx.author.name}\n"
                    f"```"
                ),
                inline=False
            )
            
            await self.send_response_once(ctx, embed=embed)
            self.logger.info(f"World {world_name} added by {ctx.author}")
                
        await self._process_command(ctx, "addworld", execute)

    @commands.command(name="addproduct")
    async def add_product(self, ctx, code: str, name: str, price: int, *, description: Optional[str] = None):
        """Add new product with improved validation"""
        async def execute():
            result = await self.product_service.create_product(
                code=code,
                name=name,
                price=price,
                description=description,
                added_by=str(ctx.author.id)
            )
            
            embed = discord.Embed(
                title="✅ Product Added",
                color=COLORS['success'],
                timestamp=datetime.utcnow()
            )
            
            embed.add_field(
                name="📦 Product Details",
                value=(
                    f"```yml\n"
                    f"Code: {result['code']}\n"
                    f"Name: {result['name']}\n"
                    f"Price: {result['price']:,} WLs\n"
                    f"Description: {result['description'] or 'No description'}\n"
                    f"Added By: {ctx.author.name}\n"
                    f"```"
                ),
                inline=False
            )
            
            await self.send_response_once(ctx, embed=embed)
            self.logger.info(f"Product {code} added by {ctx.author}")
            
            # Invalidate product cache
            await self.cache_manager.delete(f"product_{code}")
            
        await self._process_command(ctx, "addproduct", execute)

    @commands.command(name="addstock")
    async def add_stock(self, ctx, code: str):
        """Add stock from file with improved validation and progress tracking"""
        async def execute():
            if not ctx.message.attachments:
                raise ValueError("Please attach a text file containing the stock items!")

            # Verify product exists
            product = await self.product_service.get_product(code)
            if not product:
                raise ValueError(f"Product code `{code}` not found!")

            # Process stock file
            items = await self._process_stock_file(ctx.message.attachments[0])

            # Add stock with progress updates
            progress_msg = await ctx.send(
                embed=discord.Embed(
                    title="⏳ Adding Stock",
                    description="Processing items...",
                    color=COLORS['info']
                )
            )
            
            added_count = 0
            failed_count = 0
            error_items = []

            for i, item in enumerate(items, 1):
                try:
                    await self.product_service.add_stock_item(
                        code,
                        item,
                        str(ctx.author.id)
                    )
                    added_count += 1
                except Exception as e:
                    failed_count += 1
                    error_items.append(f"Item {i}: {str(e)}")

                if i % 10 == 0:
                    await progress_msg.edit(
                        embed=discord.Embed(
                            title="⏳ Adding Stock",
                            description=f"Processing... {i}/{len(items)} items",
                            color=COLORS['info']
                        )
                    )

            await progress_msg.delete()

            # Create result embed
            embed = discord.Embed(
                title="✅ Stock Added",
                color=COLORS['success'],
                timestamp=datetime.utcnow()
            )
            
            embed.add_field(
                name="📦 Stock Summary",
                value=(
                    f"```yml\n"
                    f"Product: {product['name']} ({code})\n"
                    f"Total Items: {len(items)}\n"
                    f"Added: {added_count}\n"
                    f"Failed: {failed_count}\n"
                    f"```"
                ),
                inline=False
            )

            if error_items:
                embed.add_field(
                    name="❌ Errors",
                    value="\n".join(error_items[:5]) + (
                        "\n..." if len(error_items) > 5 else ""
                    ),
                    inline=False
                )

            await self.send_response_once(ctx, embed=embed)
            self.logger.info(
                f"Stock added for {code} by {ctx.author}: "
                f"{added_count} success, {failed_count} failed"
            )
            
            # Invalidate stock cache
            await self.cache_manager.delete(f"stock_{code}")

        await self._process_command(ctx, "addstock", execute)

    @commands.command(name="addbal")
    async def add_balance(self, ctx, growid: str, amount: int, currency: str):
        """Add balance to user"""
        async def execute():
            currency = currency.upper()
            if currency not in CURRENCY_RATES:
                raise ValueError(f"Invalid currency. Use: {', '.join(CURRENCY_RATES.keys())}")

            if amount <= 0:
                raise ValueError("Amount must be positive!")

            # Convert to WLs
            wls = amount if currency == "WL" else amount * CURRENCY_RATES[currency]

            new_balance = await self.balance_service.update_balance(
                growid=growid,
                wl=wls,
                details=f"Added by admin {ctx.author}",
                transaction_type=TRANSACTION_TYPES['ADMIN_ADD']
            )

            embed = discord.Embed(
                title="✅ Balance Added",
                color=COLORS['success'],
                timestamp=datetime.utcnow()
            )
            
            embed.add_field(
                name="💰 Balance Details",
                value=(
                    f"```yml\n"
                    f"GrowID: {growid}\n"
                    f"Added: {amount:,} {currency}\n"
                    f"New Balance: {new_balance.format()}\n"
                    f"```"
                ),
                inline=False
            )
            embed.set_footer(text=f"Added by {ctx.author}")

            await self.send_response_once(ctx, embed=embed)
            self.logger.info(f"Balance added for {growid} by {ctx.author}")

            # Invalidate balance cache
            await self.cache_manager.delete(f"balance_{growid}")

        await self._process_command(ctx, "addbal", execute)

    @commands.command(name="removebal")
    async def remove_balance(self, ctx, growid: str, amount: int, currency: str):
        """Remove balance from user"""
        async def execute():
            currency = currency.upper()
            if currency not in CURRENCY_RATES:
                raise ValueError(f"Invalid currency. Use: {', '.join(CURRENCY_RATES.keys())}")

            if amount <= 0:
                raise ValueError("Amount must be positive!")

            # Convert to negative WLs
            wls = -(amount if currency == "WL" else amount * CURRENCY_RATES[currency])

            new_balance = await self.balance_service.update_balance(
                growid=growid,
                wl=wls,
                details=f"Removed by admin {ctx.author}",
                transaction_type=TRANSACTION_TYPES['ADMIN_REMOVE']
            )

            embed = discord.Embed(
                title="✅ Balance Removed",
                color=COLORS['error'],
                timestamp=datetime.utcnow()
            )
            
            embed.add_field(
                name="💰 Balance Details",
                value=(
                    f"```yml\n"
                    f"GrowID: {growid}\n"
                    f"Removed: {amount:,} {currency}\n"
                    f"New Balance: {new_balance.format()}\n"
                    f"```"
                ),
                inline=False
            )
            embed.set_footer(text=f"Removed by {ctx.author}")

            await self.send_response_once(ctx, embed=embed)
            self.logger.info(f"Balance removed from {growid} by {ctx.author}")

            # Invalidate balance cache
            await self.cache_manager.delete(f"balance_{growid}")

        await self._process_command(ctx, "removebal", execute)

    @commands.command(name="checkbal")
    async def check_balance(self, ctx, growid: str):
        """Check user balance"""
        async def execute():
            balance = await self.balance_service.get_balance(growid)
            if not balance:
                raise ValueError(f"User {growid} not found!")

            transactions = await self.trx_manager.get_transaction_history(growid, limit=5)

            embed = discord.Embed(
                title=f"👤 User Information - {growid}",
                color=COLORS['info'],
                timestamp=datetime.utcnow()
            )
            
            embed.add_field(
                name="💰 Current Balance",
                value=f"```yml\n{balance.format()}\n```",
                inline=False
            )

            if transactions:
                recent_tx = "\n".join([
                    f"• {tx['type']} - {tx['timestamp']}: {tx['details']}"
                    for tx in transactions
                ])
                embed.add_field(
                    name="📝 Recent Transactions",
                    value=f"```yml\n{recent_tx}\n```",
                    inline=False
                )

            embed.set_footer(text=f"Checked by {ctx.author}")
            await self.send_response_once(ctx, embed=embed)

        await self._process_command(ctx, "checkbal", execute)

    @commands.command(name="resetuser")
    async def reset_user(self, ctx, growid: str):
        """Reset user balance"""
        async def execute():
            if not await self._confirm_action(
                ctx, 
                f"Are you sure you want to reset {growid}'s balance? This action cannot be undone."
            ):
                raise ValueError("Operation cancelled by user")

            current_balance = await self.balance_service.get_balance(growid)
            if not current_balance:
                raise ValueError(f"User {growid} not found!")

            # Reset balance
            new_balance = await self.balance_service.update_balance(
                growid=growid,
                wl=-current_balance.wl,
                dl=-current_balance.dl,
                bgl=-current_balance.bgl,
                details=f"Balance reset by admin {ctx.author}",
                transaction_type=TRANSACTION_TYPES['ADMIN_RESET']
            )

            embed = discord.Embed(
                title="✅ Balance Reset",
                color=COLORS['error'],
                timestamp=datetime.utcnow()
            )
            
            embed.add_field(
                name="Previous Balance",
                value=f"```yml\n{current_balance.format()}\n```",
                inline=False
            )
            
            embed.add_field(
                name="New Balance",
                value=f"```yml\n{new_balance.format()}\n```",
                inline=False
            )
            
            embed.set_footer(text=f"Reset by {ctx.author}")

            await self.send_response_once(ctx, embed=embed)
            self.logger.info(f"Balance reset for {growid} by {ctx.author}")

            # Invalidate balance cache
            await self.cache_manager.delete(f"balance_{growid}")

        await self._process_command(ctx, "resetuser", execute)

    @commands.command(name="systeminfo")
    async def system_info(self, ctx):
        """Show bot system information"""
        async def execute():
            # Get system info
            cpu_usage = psutil.cpu_percent(interval=1)
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage('/')
            
            # Get bot info
            uptime = datetime.utcnow() - self.bot.startup_time
            
            embed = discord.Embed(
                title="🤖 System Information",
                color=COLORS['info'],
                timestamp=datetime.utcnow()
            )
            
            # System Stats
            embed.add_field(
                name="💻 System Resources",
                value=(
                    f"```yml\n"
                    f"OS: {platform.system()} {platform.release()}\n"
                    f"CPU Usage: {cpu_usage}%\n"
                    f"Memory: {memory.used/1024/1024/1024:.1f}GB/{memory.total/1024/1024/1024:.1f}GB ({memory.percent}%)\n"
                    f"Disk: {disk.used/1024/1024/1024:.1f}GB/{disk.total/1024/1024/1024:.1f}GB ({disk.percent}%)\n"
                    f"Python: {platform.python_version()}\n"
                    f"```"
                ),
                inline=False
            )
            
            # Bot Stats
            embed.add_field(
                name="🤖 Bot Status",
                value=(
                    f"```yml\n"
                    f"Uptime: {str(uptime).split('.')[0]}\n"
                    f"Latency: {round(self.bot.latency * 1000)}ms\n"
                    f"Servers: {len(self.bot.guilds)}\n"
                    f"Commands: {len(self.bot.commands)}\n"
                    f"```"
                ),
                inline=False
            )
            
            # Cache Stats
            cache_stats = await self.cache_manager.get_stats()
            embed.add_field(
                name="📊 Cache Statistics",
                value=(
                    f"```yml\n"
                    f"Items: {cache_stats.get('items', 0)}\n"
                    f"Hit Rate: {cache_stats.get('hit_rate', 0):.1f}%\n"
                    f"Memory Usage: {cache_stats.get('memory_usage', 0):.1f}MB\n"
                    f"```"
                ),
                inline=False
            )
            
            await self.send_response_once(ctx, embed=embed)

        await self._process_command(ctx, "systeminfo", execute)

    @commands.command(name="announcement")
    async def announcement(self, ctx, *, message: str):
        """Send announcement to all users"""
        async def execute():
            if not await self._confirm_action(
                ctx,
                "Are you sure you want to send this announcement to all users?"
            ):
                raise ValueError("Announcement cancelled by user")

            # Get all users from database
            users = await self.balance_service.get_all_users()

            embed = discord.Embed(
                title="📢 Announcement",
                description=message,
                color=COLORS['warning'],
                timestamp=datetime.utcnow()
            )
            embed.set_footer(text=f"Sent by {ctx.author}")

            sent_count = 0
            failed_count = 0

            progress_msg = await ctx.send(
                embed=discord.Embed(
                    title="⏳ Sending Announcement",
                    description="Processing...",
                    color=COLORS['info']
                )
            )

            for user_data in users:
                try:
                    user = await self.bot.fetch_user(int(user_data['discord_id']))
                    if user:
                        await user.send(embed=embed)
                        sent_count += 1
                        if sent_count % 10 == 0:
                            await progress_msg.edit(
                                embed=discord.Embed(
                                    title="⏳ Sending Announcement",
                                    description=f"Progress: {sent_count}/{len(users)} users",
                                    color=COLORS['info']
                                )
                            )
                except Exception as e:
                    failed_count += 1
                    self.logger.error(
                        f"Failed to send announcement to user ID {user_data['discord_id']}: {e}"
                    )

            await progress_msg.delete()

            result_embed = discord.Embed(
                title="📢 Announcement Results",
                color=COLORS['success'],
                timestamp=datetime.utcnow()
            )
            
            result_embed.add_field(
                name="📊 Statistics",
                value=(
                    f"```yml\n"
                    f"Total Users: {len(users)}\n"
                    f"Sent Successfully: {sent_count}\n"
                    f"Failed: {failed_count}\n"
                    f"```"
                ),
                inline=False
            )
            
            await self.send_response_once(ctx, embed=result_embed)

        await self._process_command(ctx, "announcement", execute)

    @commands.command(name="maintenance")
    async def maintenance(self, ctx, mode: str):
        """Toggle maintenance mode"""
        async def execute():
            mode_lower = mode.lower()
            if mode_lower not in ['on', 'off']:
                raise ValueError("Please specify 'on' or 'off'")

            if mode_lower == 'on' and not await self._confirm_action(
                ctx,
                "Are you sure you want to enable maintenance mode? This will restrict user access."
            ):
                raise ValueError("Operation cancelled by user")

            # Update maintenance status
            conn = None
            try:
                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
                    ("maintenance_mode", "1" if mode_lower == "on" else "0")
                )
                conn.commit()

                embed = discord.Embed(
                    title="🔧 Maintenance Mode",
                    description=(
                        f"Maintenance mode has been turned "
                        f"**{mode_lower.upper()}**"
                    ),
                    color=COLORS['warning'] if mode_lower == "on" else COLORS['success'],
                    timestamp=datetime.utcnow()
                )
                embed.set_footer(text=f"Changed by {ctx.author}")
                
                await self.send_response_once(ctx, embed=embed)
                self.logger.info(f"Maintenance mode {mode_lower} by {ctx.author}")

                if mode_lower == "on":
                    # Notify online users
                    for guild in self.bot.guilds:
                        for member in guild.members:
                            if not member.bot and member.status != discord.Status.offline:
                                try:
                                    await member.send(
                                        embed=discord.Embed(
                                            title="⚠️ Maintenance Mode",
                                            description=(
                                                "The bot is entering maintenance mode. "
                                                "Some features may be unavailable. "
                                                "We'll notify you when service is restored."
                                            ),
                                            color=COLORS['warning']
                                        )
                                    )
                                except Exception as e:
                                    self.logger.error(f"Failed to notify member {member.id}: {e}")

            finally:
                if conn:
                    conn.close()

        await self._process_command(ctx, "maintenance", execute)

    @commands.command(name="blacklist")
    async def blacklist(self, ctx, action: str, growid: str):
        """Manage blacklisted users"""
        async def execute():
            action_lower = action.lower()
            if action_lower not in ['add', 'remove']:
                raise ValueError("Please specify 'add' or 'remove'")

            if action_lower == 'add' and not await self._confirm_action(
                ctx,
                f"Are you sure you want to blacklist {growid}?"
            ):
                raise ValueError("Operation cancelled by user")

            conn = None
            try:
                conn = get_connection()
                cursor = conn.cursor()
                
                if action_lower == "add":
                    # Check if user exists
                    cursor.execute(
                        "SELECT growid FROM users WHERE growid = ?",
                        (growid,)
                    )
                    if not cursor.fetchone():
                        raise ValueError(f"User {growid} not found!")

                    # Add to blacklist
                    cursor.execute(
                        """
                        INSERT OR REPLACE INTO blacklist 
                        (growid, added_by, added_at) VALUES (?, ?, ?)
                        """,
                        (
                            growid,
                            str(ctx.author.id),
                            datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
                        )
                    )
                else:
                    # Remove from blacklist
                    cursor.execute(
                        "DELETE FROM blacklist WHERE growid = ?",
                        (growid,)
                    )

                conn.commit()

                embed = discord.Embed(
                    title="⛔ Blacklist Updated",
                    description=(
                        f"User {growid} has been "
                        f"{'added to' if action_lower == 'add' else 'removed from'} "
                        f"the blacklist."
                    ),
                    color=COLORS['error'] if action_lower == 'add' else COLORS['success'],
                    timestamp=datetime.utcnow()
                )
                embed.set_footer(text=f"Updated by {ctx.author}")
                
                await self.send_response_once(ctx, embed=embed)
                self.logger.info(f"User {growid} {action_lower}ed to blacklist by {ctx.author}")
                
                # Invalidate blacklist cache
                await self.cache_manager.delete("blacklist_users")
                
            finally:
                if conn:
                    conn.close()

        await self._process_command(ctx, "blacklist", execute)

    @commands.command(name="backup")
    async def backup(self, ctx):
        """Create database backup"""
        async def execute():
            backup_file = f"backup_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.db"
            
            conn = None
            try:
                conn = get_connection()
                
                # Create backup file
                with open(backup_file, 'wb') as f:
                    for line in conn.iterdump():
                        f.write(f"{line}\n".encode('utf-8'))
                
                # Create backup info embed
                embed = discord.Embed(
                    title="💾 Database Backup",
                    color=COLORS['success'],
                    timestamp=datetime.utcnow()
                )
                
                embed.add_field(
                    name="📁 Backup Details",
                    value=(
                        f"```yml\n"
                        f"Filename: {backup_file}\n"
                        f"Size: {os.path.getsize(backup_file)/1024/1024:.2f} MB\n"
                        f"Created: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
                        f"```"
                    ),
                    inline=False
                )
                
                # Attach backup file
                file = discord.File(backup_file, filename=backup_file)
                embed.set_footer(text=f"Backup created by {ctx.author}")
                
                await ctx.send(
                    embed=embed,
                    file=file
                )
                
                self.logger.info(f"Database backup created: {backup_file}")
                
            except Exception as e:
                raise ValueError(f"Failed to create backup: {str(e)}")
            finally:
                if conn:
                    conn.close()
                # Clean up backup file after sending
                if os.path.exists(backup_file):
                    os.remove(backup_file)

        await self._process_command(ctx, "backup", execute)

async def setup(bot):
    """Setup the Admin cog with improved logging"""
    if not hasattr(bot, 'admin_cog_loaded'):
        try:
            await bot.add_cog(AdminCog(bot))
            bot.admin_cog_loaded = True
            logging.info(
                f'Admin cog loaded successfully at '
                f'{datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")} UTC'
            )
        except Exception as e:
            logging.error(f"Failed to load Admin cog: {e}")
            raise