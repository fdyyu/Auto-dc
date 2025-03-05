import discord
from discord.ext import commands
import asyncio
from datetime import datetime
import json
import sqlite3
from typing import Optional, Dict
from .utils import Embed, get_connection, logger

class TicketSystem(commands.Cog):
    """🎫 Advanced Ticket Support System"""
    
    def __init__(self, bot):
        self.bot = bot
        self.active_tickets = {}

    def setup_tables(self):
        """Setup necessary database tables"""
        try:
            conn = get_connection()
            cursor = conn.cursor()

            # Ticket settings table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS ticket_settings (
                    guild_id TEXT PRIMARY KEY,
                    category_id TEXT,
                    log_channel_id TEXT,
                    support_role_id TEXT,
                    max_tickets INTEGER DEFAULT 1,
                    ticket_format TEXT DEFAULT 'ticket-{user}-{number}',
                    auto_close_hours INTEGER DEFAULT 48,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Tickets table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    reason TEXT,
                    status TEXT DEFAULT 'open' CHECK (status IN ('open', 'closed')),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    closed_at TIMESTAMP,
                    closed_by TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Ticket responses table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS ticket_responses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id INTEGER,
                    user_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (ticket_id) REFERENCES tickets (id) ON DELETE CASCADE
                )
            """)

            # Create triggers for timestamp updates
            cursor.execute("""
                CREATE TRIGGER IF NOT EXISTS update_ticket_settings_timestamp 
                AFTER UPDATE ON ticket_settings
                BEGIN
                    UPDATE ticket_settings SET updated_at = CURRENT_TIMESTAMP
                    WHERE guild_id = NEW.guild_id;
                END;
            """)

            cursor.execute("""
                CREATE TRIGGER IF NOT EXISTS update_tickets_timestamp 
                AFTER UPDATE ON tickets
                BEGIN
                    UPDATE tickets SET updated_at = CURRENT_TIMESTAMP
                    WHERE id = NEW.id;
                END;
            """)

            cursor.execute("""
                CREATE TRIGGER IF NOT EXISTS update_ticket_responses_timestamp 
                AFTER UPDATE ON ticket_responses
                BEGIN
                    UPDATE ticket_responses SET updated_at = CURRENT_TIMESTAMP
                    WHERE id = NEW.id;
                END;
            """)

            # Create indexes
            indexes = [
                ("idx_tickets_guild", "tickets(guild_id)"),
                ("idx_tickets_channel", "tickets(channel_id)"),
                ("idx_tickets_user", "tickets(user_id)"),
                ("idx_tickets_status", "tickets(status)"),
                ("idx_ticket_responses_ticket", "ticket_responses(ticket_id)"),
                ("idx_ticket_responses_user", "ticket_responses(user_id)")
            ]

            for idx_name, idx_cols in indexes:
                cursor.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {idx_cols}")

            conn.commit()
            logger.info("Ticket system tables setup completed successfully")

        except sqlite3.Error as e:
            logger.error(f"Failed to setup ticket tables: {e}")
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.close()

    def get_guild_settings(self, guild_id: int) -> Dict:
        """Get ticket settings for a guild"""
        try:
            conn = get_connection()
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT * FROM ticket_settings WHERE guild_id = ?
            """, (str(guild_id),))
            
            data = cursor.fetchone()
            
            if not data:
                return {
                    'category_id': None,
                    'log_channel_id': None,
                    'support_role_id': None,
                    'max_tickets': 1,
                    'ticket_format': 'ticket-{user}-{number}',
                    'auto_close_hours': 48
                }
                
            return dict(data)

        except sqlite3.Error as e:
            logger.error(f"Error fetching guild settings: {e}")
            return {}
        finally:
            if conn:
                conn.close()

    async def create_ticket_channel(self, ctx, reason: str, settings: Dict) -> Optional[discord.TextChannel]:
        """Create a new ticket channel"""
        try:
            conn = get_connection()
            cursor = conn.cursor()

            # Check max tickets
            cursor.execute("""
                SELECT COUNT(*) FROM tickets 
                WHERE guild_id = ? AND user_id = ? AND status = 'open'
            """, (str(ctx.guild.id), str(ctx.author.id)))
            
            count = cursor.fetchone()[0]
            
            if count >= settings['max_tickets']:
                await ctx.send("❌ You have reached the maximum number of open tickets!")
                return None

            # Get category
            category_id = settings.get('category_id')
            category = ctx.guild.get_channel(int(category_id)) if category_id else None
            
            if not category:
                category = await ctx.guild.create_category("Tickets")
                cursor.execute("""
                    INSERT OR REPLACE INTO ticket_settings (guild_id, category_id)
                    VALUES (?, ?)
                """, (str(ctx.guild.id), str(category.id)))
                conn.commit()

            # Create channel
            ticket_number = count + 1
            channel_name = settings['ticket_format'].format(
                user=ctx.author.name.lower(),
                number=ticket_number
            )

            # Set permissions
            overwrites = {
                ctx.guild.default_role: discord.PermissionOverwrite(read_messages=False),
                ctx.author: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                ctx.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
            }

            # Add support role permissions
            if settings['support_role_id']:
                support_role = ctx.guild.get_role(int(settings['support_role_id']))
                if support_role:
                    overwrites[support_role] = discord.PermissionOverwrite(
                        read_messages=True,
                        send_messages=True
                    )

            # Create the channel
            channel = await category.create_text_channel(
                channel_name,
                overwrites=overwrites
            )

            # Save ticket to database
            cursor.execute("""
                INSERT INTO tickets (guild_id, channel_id, user_id, reason)
                VALUES (?, ?, ?, ?)
            """, (str(ctx.guild.id), str(channel.id), str(ctx.author.id), reason))
            
            ticket_id = cursor.lastrowid
            conn.commit()

            self.active_tickets[channel.id] = ticket_id
            
            # Log creation in admin_logs
            cursor.execute("""
                INSERT INTO admin_logs (admin_id, action, target, details)
                VALUES (?, ?, ?, ?)
            """, (
                str(ctx.author.id),
                'ticket_create',
                str(channel.id),
                f"Ticket created: {reason}"
            ))
            conn.commit()
            
            return channel

        except sqlite3.Error as e:
            logger.error(f"Error creating ticket channel: {e}")
            if conn:
                conn.rollback()
            return None
        finally:
            if conn:
                conn.close()

    @commands.group(name="ticket")
    async def ticket(self, ctx):
        """🎫 Ticket management commands"""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @ticket.command(name="create")
    async def create_ticket(self, ctx, *, reason: str = "No reason provided"):
        """Create a new support ticket"""
        settings = self.get_guild_settings(ctx.guild.id)
        
        channel = await self.create_ticket_channel(ctx, reason, settings)
        if not channel:
            return

        embed = Embed.create(
            title="🎫 Support Ticket",
            description=f"Ticket created by {ctx.author.mention}",
            color=discord.Color.blue()
        )
        embed.add_field(name="Reason", value=reason)
        embed.add_field(name="Instructions", value="React with 🔒 to close the ticket\nSupport team will assist you shortly.")

        msg = await channel.send(embed=embed)
        await msg.add_reaction("🔒")

    @ticket.command(name="close")
    async def close_ticket(self, ctx):
        """Close the current ticket"""
        if ctx.channel.id not in self.active_tickets:
            return await ctx.send("❌ This is not a ticket channel!")

        try:
            conn = get_connection()
            cursor = conn.cursor()

            ticket_id = self.active_tickets[ctx.channel.id]
            
            # Update database
            cursor.execute("""
                UPDATE tickets 
                SET status = 'closed', 
                    closed_at = CURRENT_TIMESTAMP,
                    closed_by = ?
                WHERE id = ?
            """, (str(ctx.author.id), ticket_id))

            # Log closure in admin_logs
            cursor.execute("""
                INSERT INTO admin_logs (admin_id, action, target, details)
                VALUES (?, ?, ?, ?)
            """, (
                str(ctx.author.id),
                'ticket_close',
                str(ctx.channel.id),
                f"Ticket {ticket_id} closed"
            ))

            conn.commit()

            # Create and save transcript
            transcript = await self.create_transcript(ctx.channel)
            
            # Delete channel
            await ctx.send("🔒 Closing ticket in 5 seconds...")
            await asyncio.sleep(5)
            await ctx.channel.delete()

            del self.active_tickets[ctx.channel.id]

        except sqlite3.Error as e:
            logger.error(f"Error closing ticket: {e}")
            await ctx.send("❌ An error occurred while closing the ticket")
        finally:
            if conn:
                conn.close()

    @ticket.command(name="add")
    async def add_user(self, ctx, user: discord.Member):
        """Add a user to the current ticket"""
        if ctx.channel.id not in self.active_tickets:
            return await ctx.send("❌ This is not a ticket channel!")

        await ctx.channel.set_permissions(user, read_messages=True, send_messages=True)
        
        try:
            conn = get_connection()
            cursor = conn.cursor()
            
            # Log user addition in admin_logs
            cursor.execute("""
                INSERT INTO admin_logs (admin_id, action, target, details)
                VALUES (?, ?, ?, ?)
            """, (
                str(ctx.author.id),
                'ticket_add_user',
                str(user.id),
                f"Added to ticket channel {ctx.channel.id}"
            ))
            conn.commit()
            
            await ctx.send(f"✅ Added {user.mention} to the ticket")
            
        except sqlite3.Error as e:
            logger.error(f"Error adding user to ticket: {e}")
        finally:
            if conn:
                conn.close()

    @ticket.command(name="remove")
    async def remove_user(self, ctx, user: discord.Member):
        """Remove a user from the current ticket"""
        if ctx.channel.id not in self.active_tickets:
            return await ctx.send("❌ This is not a ticket channel!")

        await ctx.channel.set_permissions(user, overwrite=None)
        
        try:
            conn = get_connection()
            cursor = conn.cursor()
            
            # Log user removal in admin_logs
            cursor.execute("""
                INSERT INTO admin_logs (admin_id, action, target, details)
                VALUES (?, ?, ?, ?)
            """, (
                str(ctx.author.id),
                'ticket_remove_user',
                str(user.id),
                f"Removed from ticket channel {ctx.channel.id}"
            ))
            conn.commit()
            
            await ctx.send(f"✅ Removed {user.mention} from the ticket")
            
        except sqlite3.Error as e:
            logger.error(f"Error removing user from ticket: {e}")
        finally:
            if conn:
                conn.close()

    @commands.group(name="ticketset")
    @commands.has_permissions(administrator=True)
    async def ticketset(self, ctx):
        """⚙️ Ticket system settings"""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @ticketset.command(name="supportrole")
    async def set_support_role(self, ctx, role: discord.Role):
        """Set the support team role"""
        try:
            conn = get_connection()
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT OR REPLACE INTO ticket_settings (guild_id, support_role_id)
                VALUES (?, ?)
            """, (str(ctx.guild.id), str(role.id)))
            
            # Log setting change in admin_logs
            cursor.execute("""
                INSERT INTO admin_logs (admin_id, action, target, details)
                VALUES (?, ?, ?, ?)
            """, (
                str(ctx.author.id),
                'ticket_settings_update',
                'support_role',
                f"Set to {role.name} ({role.id})"
            ))
            
            conn.commit()
            await ctx.send(f"✅ Support role set to {role.mention}")
            
        except sqlite3.Error as e:
            logger.error(f"Error setting support role: {e}")
            await ctx.send("❌ An error occurred while setting the support role")
        finally:
            if conn:
                conn.close()

    @ticketset.command(name="maxtickets")
    async def set_max_tickets(self, ctx, amount: int):
        """Set maximum open tickets per user"""
        if amount < 1:
            return await ctx.send("❌ Amount must be at least 1!")

        try:
            conn = get_connection()
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT OR REPLACE INTO ticket_settings (guild_id, max_tickets)
                VALUES (?, ?)
            """, (str(ctx.guild.id), amount))
            
            # Log setting change in admin_logs
            cursor.execute("""
                INSERT INTO admin_logs (admin_id, action, target, details)
                VALUES (?, ?, ?, ?)
            """, (
                str(ctx.author.id),
                'ticket_settings_update',
                'max_tickets',
                f"Set to {amount}"
            ))
            
            conn.commit()
            await ctx.send(f"✅ Maximum tickets per user set to {amount}")
            
        except sqlite3.Error as e:
            logger.error(f"Error setting max tickets: {e}")
            await ctx.send("❌ An error occurred while setting the maximum tickets")
        finally:
            if conn:
                conn.close()

    @ticketset.command(name="logchannel")
    async def set_log_channel(self, ctx, channel: discord.TextChannel):
        """Set the ticket log channel"""
        try:
            conn = get_connection()
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT OR REPLACE INTO ticket_settings (guild_id, log_channel_id)
                VALUES (?, ?)
            """, (str(ctx.guild.id), str(channel.id)))
            
            # Log setting change in admin_logs
            cursor.execute("""
                INSERT INTO admin_logs (admin_id, action, target, details)
                VALUES (?, ?, ?, ?)
            """, (
                str(ctx.author.id),
                'ticket_settings_update',
                'log_channel',
                f"Set to {channel.name} ({channel.id})"
            ))
            
            conn.commit()
            await ctx.send(f"✅ Log channel set to {channel.mention}")
            
        except sqlite3.Error as e:
            logger.error(f"Error setting log channel: {e}")
            await ctx.send("❌ An error occurred while setting the log channel")
        finally:
            if conn:
                conn.close()

    async def create_transcript(self, channel: discord.TextChannel) -> str:
        """Create a transcript of the ticket"""
        messages = []
        async for message in channel.history(limit=None, oldest_first=True):
            messages.append({
                'author': str(message.author),
                'content': message.content,
                'timestamp': message.created_at.strftime('%Y-%m-%d %H:%M:%S')
            })

        try:
            conn = get_connection()
            cursor = conn.cursor()
            
            # Save transcript in ticket_responses
            for msg in messages:
                cursor.execute("""
                    INSERT INTO ticket_responses (ticket_id, user_id, content)
                    VALUES (?, ?, ?)
                """, (
                    self.active_tickets[channel.id],
                    msg['author'],
                    msg['content']
                ))
            
            conn.commit()
            
        except sqlite3.Error as e:
            logger.error(f"Error saving ticket transcript: {e}")
        finally:
            if conn:
                conn.close()

        return json.dumps(messages, indent=2)

    def get_ticket_duration(self, ticket_id: int) -> str:
        """Get the duration of a ticket"""
        try:
            conn = get_connection()
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT created_at, closed_at FROM tickets WHERE id = ?
            """, (ticket_id,))
            
            data = cursor.fetchone()
            
            if not data or not data['closed_at']:
                return "Unknown"
                
            created = datetime.strptime(data['created_at'], '%Y-%m-%d %H:%M:%S')
            closed = datetime.strptime(data['closed_at'], '%Y-%m-%d %H:%M:%S')
            duration = closed - created
            
            return str(duration).split('.')[0]
            
        except sqlite3.Error as e:
            logger.error(f"Error getting ticket duration: {e}")
            return "Unknown"
        finally:
            if conn:
                conn.close()

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        """Handle ticket reactions"""
        if payload.user_id == self.bot.user.id:
            return

        if str(payload.emoji) != "🔒":
            return

        channel = self.bot.get_channel(payload.channel_id)
        if not channel.id in self.active_tickets:
            return

        ctx = await self.bot.get_context(await channel.fetch_message(payload.message_id))
        await self.close_ticket(ctx)

    @ticketset.command(name="format")
    async def set_ticket_format(self, ctx, *, format_string: str):
        """Set the ticket channel name format
        Available variables: {user}, {number}
        Example: ticket-{user}-{number}"""
        
        if not any(var in format_string for var in ['{user}', '{number}']):
            return await ctx.send("❌ Format must include at least {user} or {number}!")

        try:
            conn = get_connection()
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT OR REPLACE INTO ticket_settings (guild_id, ticket_format)
                VALUES (?, ?)
            """, (str(ctx.guild.id), format_string))
            
            # Log setting change
            cursor.execute("""
                INSERT INTO admin_logs (admin_id, action, target, details)
                VALUES (?, ?, ?, ?)
            """, (
                str(ctx.author.id),
                'ticket_settings_update',
                'ticket_format',
                f"Set to {format_string}"
            ))
            
            conn.commit()
            await ctx.send(f"✅ Ticket format set to: {format_string}")
            
        except sqlite3.Error as e:
            logger.error(f"Error setting ticket format: {e}")
            await ctx.send("❌ An error occurred while setting the ticket format")
        finally:
            if conn:
                conn.close()

    @ticketset.command(name="settings")
    async def view_settings(self, ctx):
        """View current ticket system settings"""
        settings = self.get_guild_settings(ctx.guild.id)
        
        embed = discord.Embed(
            title="🎫 Ticket System Settings",
            color=discord.Color.blue(),
            timestamp=datetime.utcnow()
        )
        
        # Format settings for display
        support_role = ctx.guild.get_role(int(settings['support_role_id'])) if settings['support_role_id'] else None
        log_channel = ctx.guild.get_channel(int(settings['log_channel_id'])) if settings['log_channel_id'] else None
        category = ctx.guild.get_channel(int(settings['category_id'])) if settings['category_id'] else None
        
        embed.add_field(
            name="Support Role",
            value=support_role.mention if support_role else "Not set",
            inline=False
        )
        embed.add_field(
            name="Log Channel",
            value=log_channel.mention if log_channel else "Not set",
            inline=False
        )
        embed.add_field(
            name="Ticket Category",
            value=category.name if category else "Not set",
            inline=False
        )
        embed.add_field(
            name="Max Tickets per User",
            value=settings['max_tickets'],
            inline=True
        )
        embed.add_field(
            name="Ticket Format",
            value=f"`{settings['ticket_format']}`",
            inline=True
        )
        embed.add_field(
            name="Auto Close Hours",
            value=settings['auto_close_hours'],
            inline=True
        )
        
        await ctx.send(embed=embed)

async def setup(bot):
    """Setup the Ticket cog"""
    cog = TicketSystem(bot)
    cog.setup_tables()  # Setup tables before adding cog
    await bot.add_cog(cog)