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

from ext.constants import (
    CURRENCY_RATES,
    TRANSACTION_ADMIN_ADD,
    TRANSACTION_ADMIN_REMOVE,
    TRANSACTION_ADMIN_RESET,
    MAX_STOCK_FILE_SIZE,
    VALID_STOCK_FORMATS
)
from ext.balance_manager import BalanceManagerService
from ext.product_manager import ProductManagerService
from ext.trx import TransactionManager
from ext.base_handler import BaseLockHandler, BaseResponseHandler

class AdminCog(commands.Cog, BaseLockHandler, BaseResponseHandler):
    def __init__(self, bot):
        super().__init__()  # Initialize BaseLockHandler and BaseResponseHandler
        self.bot = bot
        self.logger = logging.getLogger("AdminCog")
        
        # Initialize services
        self.balance_service = BalanceManagerService(bot)
        self.product_service = ProductManagerService(bot)
        self.trx_manager = TransactionManager(bot)
        
        # Load admin configuration
        try:
            with open('config.json') as f:
                config = json.load(f)
                self.admin_id = int(config['admin_id'])
                self.logger.info(f"Admin ID loaded: {self.admin_id}")
        except Exception as e:
            self.logger.error(f"Failed to load admin_id: {e}")
            raise

    async def _check_admin(self, ctx) -> bool:
        """Check if user has admin permissions"""
        is_admin = ctx.author.id == self.admin_id
        if not is_admin:
            await self.send_response_once(
                ctx, 
                content="❌ You don't have permission to use admin commands!"
            )
            self.logger.warning(
                f"Unauthorized access attempt by {ctx.author} (ID: {ctx.author.id})"
            )
        return is_admin

    async def _process_command(self, ctx, command_name: str, callback) -> bool:
        """Process command with proper locking and response handling"""
        # Prevent multiple executions
        if hasattr(ctx, 'is_being_processed'):
            return False
        ctx.is_being_processed = True

        lock = await self.acquire_lock(f"admin_command_{command_name}_{ctx.author.id}")
        if not lock:
            await self.send_response_once(
                ctx, 
                content="❌ System is busy, please try again later"
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
            self.logger.error(f"Error in {command_name}: {e}")
            await self.send_response_once(ctx, content=f"❌ Error: {str(e)}")
            return False
        finally:
            self.release_lock(f"admin_command_{command_name}_{ctx.author.id}")
            delattr(ctx, 'is_being_processed')

    async def _process_stock_file(self, attachment) -> List[str]:
        """Process uploaded stock file"""
        if attachment.size > MAX_STOCK_FILE_SIZE:
            raise ValueError(f"File too large! Maximum size is {MAX_STOCK_FILE_SIZE/1024:.0f}KB")
            
        file_ext = attachment.filename.split('.')[-1].lower()
        if file_ext not in VALID_STOCK_FORMATS:
            raise ValueError(f"Invalid file format! Supported formats: {', '.join(VALID_STOCK_FORMATS)}")
            
        content = await attachment.read()
        text = content.decode('utf-8').strip()
        
        items = [line.strip() for line in text.split('\n') if line.strip()]
        if not items:
            raise ValueError("No valid items found in file!")
            
        return items

    async def _confirm_action(self, ctx, message: str, timeout: int = 30) -> bool:
        """Get confirmation for dangerous actions"""
        lock = await self.acquire_lock(f"confirm_{ctx.author.id}")
        if not lock:
            return False

        try:
            confirm_msg = await ctx.send(
                f"⚠️ **WARNING**\n{message}\nReact with ✅ to confirm or ❌ to cancel."
            )
            
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
                await self.send_response_once(ctx, content="❌ Operation timed out!")
                return False
        finally:
            self.release_lock(f"confirm_{ctx.author.id}")

    @commands.command(name="adminhelp")
    async def admin_help(self, ctx):
        """Show admin commands"""
        async def execute():
            embed = discord.Embed(
                title="🛠️ Admin Commands",
                description="Available administrative commands",
                color=discord.Color.blue(),
                timestamp=datetime.utcnow()
            )

            command_categories = {
                "Product Management": [
                    "`addproduct <code> <name> <price> [description]`\nAdd new product",
                    "`editproduct <code> <field> <value>`\nEdit product details",
                    "`deleteproduct <code>`\nDelete product",
                    "`addstock <code>`\nAdd stock with file attachment"
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

    @commands.command(name="addproduct")
    async def add_product(self, ctx, code: str, name: str, price: int, *, description: Optional[str] = None):
        """Add new product"""
        async def execute():
            result = await self.product_service.create_product(
                code=code,
                name=name,
                price=price,
                description=description
            )
            
            embed = discord.Embed(
                title="✅ Product Added",
                color=discord.Color.green(),
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Code", value=result['code'], inline=True)
            embed.add_field(name="Name", value=result['name'], inline=True)
            embed.add_field(name="Price", value=f"{result['price']:,} WLs", inline=True)
            if result['description']:
                embed.add_field(name="Description", value=result['description'], inline=False)
            
            await self.send_response_once(ctx, embed=embed)
            self.logger.info(f"Product {code} added by {ctx.author}")

        await self._process_command(ctx, "addproduct", execute)

    @commands.command(name="addstock")
    async def add_stock(self, ctx, code: str):
        """Add stock from file"""
        async def execute():
            if not ctx.message.attachments:
                await self.send_response_once(
                    ctx,
                    content="❌ Please attach a text file containing the stock items!"
                )
                return

            # Verify product exists
            product = await self.product_service.get_product(code)
            if not product:
                await self.send_response_once(
                    ctx,
                    content=f"❌ Product code `{code}` not found!"
                )
                return

            # Process stock file
            items = await self._process_stock_file(ctx.message.attachments[0])

            # Add stock with progress updates
            progress_msg = await ctx.send("⏳ Adding stock items...")
            added_count = 0
            failed_count = 0

            for i, item in enumerate(items, 1):
                try:
                    await self.product_service.add_stock_item(
                        code,
                        item,
                        str(ctx.author.id)
                    )
                    added_count += 1
                except:
                    failed_count += 1

                if i % 10 == 0:
                    await progress_msg.edit(
                        content=f"⏳ Processing... {i}/{len(items)} items"
                    )

            embed = discord.Embed(
                title="✅ Stock Added",
                color=discord.Color.green(),
                timestamp=datetime.utcnow()
            )
            embed.add_field(
                name="Product",
                value=f"{product['name']} ({code})",
                inline=False
            )
            embed.add_field(name="Total Items", value=len(items), inline=True)
            embed.add_field(name="Added", value=added_count, inline=True)
            embed.add_field(name="Failed", value=failed_count, inline=True)

            await progress_msg.delete()
            await self.send_response_once(ctx, embed=embed)
            self.logger.info(
                f"Stock added for {code} by {ctx.author}: "
                f"{added_count} success, {failed_count} failed"
            )

        await self._process_command(ctx, "addstock", execute)

    @commands.command(name="addbal")
    async def add_balance(self, ctx, growid: str, amount: int, currency: str):
        """Add balance to user"""
        async def execute():
            if currency.upper() not in CURRENCY_RATES:
                await self.send_response_once(
                    ctx,
                    content=f"❌ Invalid currency. Use: {', '.join(CURRENCY_RATES.keys())}"
                )
                return

            if amount <= 0:
                await self.send_response_once(
                    ctx,
                    content="❌ Amount must be positive!"
                )
                return

            # Convert to WLs
            wls = amount if currency == "WL" else amount * CURRENCY_RATES[currency.upper()]

            new_balance = await self.balance_service.update_balance(
                growid=growid,
                wl=wls,
                details=f"Added by admin {ctx.author}",
                transaction_type=TRANSACTION_ADMIN_ADD
            )

            embed = discord.Embed(
                title="✅ Balance Added",
                color=discord.Color.green(),
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="GrowID", value=growid, inline=True)
            embed.add_field(name="Added", value=f"{amount:,} {currency.upper()}", inline=True)
            embed.add_field(name="New Balance", value=new_balance.format(), inline=False)
            embed.set_footer(text=f"Added by {ctx.author}")

            await self.send_response_once(ctx, embed=embed)
            self.logger.info(f"Balance added for {growid} by {ctx.author}")

        await self._process_command(ctx, "addbal", execute)

    @commands.command(name="removebal")
    async def remove_balance(self, ctx, growid: str, amount: int, currency: str):
        """Remove balance from user"""
        async def execute():
            if currency.upper() not in CURRENCY_RATES:
                await self.send_response_once(
                    ctx,
                    content=f"❌ Invalid currency. Use: {', '.join(CURRENCY_RATES.keys())}"
                )
                return

            if amount <= 0:
                await self.send_response_once(
                    ctx,
                    content="❌ Amount must be positive!"
                )
                return

            # Convert to negative WLs
            wls = -(amount if currency == "WL" else amount * CURRENCY_RATES[currency.upper()])

            new_balance = await self.balance_service.update_balance(
                growid=growid,
                wl=wls,
                details=f"Removed by admin {ctx.author}",
                transaction_type=TRANSACTION_ADMIN_REMOVE
            )

            embed = discord.Embed(
                title="✅ Balance Removed",
                color=discord.Color.red(),
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="GrowID", value=growid, inline=True)
            embed.add_field(name="Removed", value=f"{amount:,} {currency.upper()}", inline=True)
            embed.add_field(name="New Balance", value=new_balance.format(), inline=False)
            embed.set_footer(text=f"Removed by {ctx.author}")

            await self.send_response_once(ctx, embed=embed)
            self.logger.info(f"Balance removed from {growid} by {ctx.author}")

        await self._process_command(ctx, "removebal", execute)

    @commands.command(name="checkbal")
    async def check_balance(self, ctx, growid: str):
        """Check user balance"""
        async def execute():
            balance = await self.balance_service.get_balance(growid)
            if not balance:
                await self.send_response_once(
                    ctx,
                    content=f"❌ User {growid} not found!"
                )
                return

            transactions = await self.trx_manager.get_transaction_history(growid, limit=5)

            embed = discord.Embed(
                title=f"👤 User Information - {growid}",
                color=discord.Color.blue(),
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Current Balance", value=balance.format(), inline=False)

            if transactions:
                recent_tx = "\n".join([
                    f"• {tx['type']} - {tx['timestamp']}: {tx['details']}"
                    for tx in transactions
                ])
                embed.add_field(name="Recent Transactions", value=recent_tx, inline=False)

            embed.set_footer(text=f"Checked by {ctx.author}")
            await self.send_response_once(ctx, embed=embed)

        await self._process_command(ctx, "checkbal", execute)

    @commands.command(name="resetuser")
    async def reset_user(self, ctx, growid: str):
        """Reset user balance"""
        async def execute():
            if not await self._confirm_action(
                ctx, 
                f"Are you sure you want to reset {growid}'s balance?"
            ):
                await self.send_response_once(ctx, content="❌ Operation cancelled.")
                return

            current_balance = await self.balance_service.get_balance(growid)
            if not current_balance:
                await self.send_response_once(
                    ctx,
                    content=f"❌ User {growid} not found!"
                )
                return

            # Reset balance
            new_balance = await self.balance_service.update_balance(
                growid=growid,
                wl=-current_balance.wl,
                dl=-current_balance.dl,
                bgl=-current_balance.bgl,
                details=f"Balance reset by admin {ctx.author}",
                transaction_type=TRANSACTION_ADMIN_RESET
            )

            embed = discord.Embed(
                title="✅ Balance Reset",
                description=f"User {growid}'s balance has been reset.",
                color=discord.Color.red(),
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Previous Balance", value=current_balance.format(), inline=False)
            embed.add_field(name="New Balance", value=new_balance.format(), inline=False)
            embed.set_footer(text=f"Reset by {ctx.author}")

            await self.send_response_once(ctx, embed=embed)
            self.logger.info(f"Balance reset for {growid} by {ctx.author}")

        await self._process_command(ctx, "resetuser", execute)

    @commands.command(name="systeminfo")
    async def system_info(self, ctx):
        """Show bot system information"""
        async def execute():
            # Get system info
            cpu_usage = psutil.cpu_percent()
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage('/')
            
            # Get bot info
            uptime = datetime.utcnow() - self.bot.startup_time
            
            embed = discord.Embed(
                title="🤖 Bot System Information",
                color=discord.Color.blue(),
                timestamp=datetime.utcnow()
            )
            
            # System Stats
            sys_info = (
                f"OS: {platform.system()} {platform.release()}\n"
                f"CPU Usage: {cpu_usage}%\n"
                f"RAM: {memory.used/1024/1024/1024:.1f}GB/{memory.total/1024/1024/1024:.1f}GB ({memory.percent}%)\n"
                f"Disk: {disk.used/1024/1024/1024:.1f}GB/{disk.total/1024/1024/1024:.1f}GB ({disk.percent}%)"
            )
            embed.add_field(name="💻 System", value=sys_info, inline=False)
            
            # Bot Stats
            bot_stats = (
                f"Uptime: {str(uptime).split('.')[0]}\n"
                f"Latency: {round(self.bot.latency * 1000)}ms\n"
                f"Servers: {len(self.bot.guilds)}\n"
                f"Commands: {len(self.bot.commands)}"
            )
            embed.add_field(name="🤖 Bot", value=bot_stats, inline=False)
            
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
                await self.send_response_once(ctx, content="❌ Announcement cancelled.")
                return

            # Get all users from database
            conn = None
            try:
                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute("SELECT DISTINCT discord_id FROM user_growid")
                users = cursor.fetchall()
            finally:
                if conn:
                    conn.close()

            embed = discord.Embed(
                title="📢 Announcement",
                description=message,
                color=discord.Color.gold(),
                timestamp=datetime.utcnow()
            )
            embed.set_footer(text=f"Sent by {ctx.author}")

            sent_count = 0
            failed_count = 0

            progress_msg = await ctx.send("⏳ Sending announcement...")

            for user_data in users:
                try:
                    user = await self.bot.fetch_user(int(user_data['discord_id']))
                    if user:
                        await user.send(embed=embed)
                        sent_count += 1
                        if sent_count % 10 == 0:
                            await progress_msg.edit(
                                content=f"⏳ Sending announcement... {sent_count}/{len(users)} users"
                            )
                except Exception as e:
                    failed_count += 1
                    self.logger.error(f"Failed to send announcement to user ID {user_data['discord_id']}: {e}")

            await progress_msg.delete()

            result_msg = (
                f"✅ Announcement sent to {sent_count} users.\n"
                f"❌ Failed to send to {failed_count} users."
            )
            await self.send_response_once(ctx, content=result_msg)

        await self._process_command(ctx, "announcement", execute)

    @commands.command(name="maintenance")
    async def maintenance(self, ctx, mode: str):
        """Toggle maintenance mode"""
        async def execute():
            mode_lower = mode.lower()
            if mode_lower not in ['on', 'off']:
                await self.send_response_once(
                    ctx,
                    content="❌ Please specify 'on' or 'off'"
                )
                return

            # Update maintenance status in database
            conn = None
            try:
                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
                    ("maintenance_mode", "1" if mode_lower == "on" else "0")
                )
                conn.commit()
            finally:
                if conn:
                    conn.close()

            embed = discord.Embed(
                title="🔧 Maintenance Mode",
                description=f"Maintenance mode has been turned **{mode_lower}**",
                color=discord.Color.orange() if mode_lower == "on" else discord.Color.green(),
                timestamp=datetime.utcnow()
            )
            embed.set_footer(text=f"Changed by {ctx.author}")
            
            await self.send_response_once(ctx, embed=embed)
            self.logger.info(f"Maintenance mode {mode_lower} by {ctx.author}")

            if mode_lower == "on":
                # Notify all online users
                for guild in self.bot.guilds:
                    for member in guild.members:
                        if not member.bot and member.status != discord.Status.offline:
                            try:
                                await member.send(
                                    "⚠️ The bot is entering maintenance mode. "
                                    "Some features may be unavailable. "
                                    "We'll notify you when service is restored."
                                )
                            except Exception as e:
                                self.logger.error(f"Failed to notify member {member.id}: {e}")

        await self._process_command(ctx, "maintenance", execute)

    @commands.command(name="blacklist")
    async def blacklist(self, ctx, action: str, growid: str):
        """Manage blacklisted users"""
        async def execute():
            action_lower = action.lower()
            if action_lower not in ['add', 'remove']:
                await self.send_response_once(
                    ctx,
                    content="❌ Please specify 'add' or 'remove'"
                )
                return

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
                        await self.send_response_once(
                            ctx,
                            content=f"❌ User {growid} not found!"
                        )
                        return

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
                    color=discord.Color.red() if action_lower == 'add' else discord.Color.green(),
                    timestamp=datetime.utcnow()
                )
                embed.set_footer(text=f"Updated by {ctx.author}")
                
                await self.send_response_once(ctx, embed=embed)
                self.logger.info(f"User {growid} {action_lower}ed to blacklist by {ctx.author}")
                
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
                with open(backup_file, 'wb') as f:
                    for line in conn.iterdump():
                        f.write(f"{line}\n".encode('utf-8'))
                await self.send_response_once(ctx, content=f"✅ Backup created: `{backup_file}`")
                self.logger.info(f"Backup created: {backup_file}")
            except Exception as e:
                await self.send_response_once(ctx, content=f"❌ Failed to create backup: {e}")
                self.logger.error(f"Failed to create backup: {e}")
            finally:
                if conn:
                    conn.close()

        await self._process_command(ctx, "backup", execute)

async def setup(bot):
    """Setup the Admin cog"""
    if not hasattr(bot, 'admin_cog_loaded'):
        await bot.add_cog(AdminCog(bot))
        bot.admin_cog_loaded = True
        logging.info(
            f'Admin cog loaded successfully at '
            f'{datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")} UTC'
        )