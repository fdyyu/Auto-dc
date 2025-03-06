import discord
from discord.ext import commands
import logging
import json
from datetime import datetime
from typing import Optional, Dict, List, Tuple, Any
from ext.cache_manager import CacheManager

logger = logging.getLogger(__name__)

class CommandAnalytics:
    def __init__(self):
        self.cache_manager = CacheManager()
        
    async def track_command(self, ctx: commands.Context, command: str) -> None:
        """Track command usage statistics"""
        cache_key = f"analytics:command:{command}"
        stats = await self.cache_manager.get(cache_key) or {
            'total_uses': 0,
            'users': set(),
            'channels': set(),
            'last_used': None,
            'peak_hour_usage': [0] * 24
        }
        
        # Convert sets from list if loaded from cache
        if isinstance(stats['users'], list):
            stats['users'] = set(stats['users'])
        if isinstance(stats['channels'], list):
            stats['channels'] = set(stats['channels'])
        
        # Update stats
        now = datetime.utcnow()
        stats['total_uses'] += 1
        stats['users'].add(ctx.author.id)
        stats['channels'].add(ctx.channel.id)
        stats['last_used'] = now.isoformat()
        stats['peak_hour_usage'][now.hour] += 1
        
        # Convert sets to list for JSON serialization
        cache_stats = stats.copy()
        cache_stats['users'] = list(stats['users'])
        cache_stats['channels'] = list(stats['channels'])
        
        # Cache for 1 hour, store permanently
        await self.cache_manager.set(
            cache_key,
            cache_stats,
            expires_in=3600,
            permanent=True
        )

    async def track_error(self, command: str, error: Exception) -> None:
        """Track command errors"""
        cache_key = f"analytics:errors:{command}"
        errors = await self.cache_manager.get(cache_key) or []
        
        errors.append({
            'time': datetime.utcnow().isoformat(),
            'error': str(error),
            'type': type(error).__name__
        })
        
        # Keep only last 100 errors
        if len(errors) > 100:
            errors = errors[-100:]
            
        # Cache for 24 hours
        await self.cache_manager.set(
            cache_key,
            errors,
            expires_in=86400,
            permanent=True
        )

class AdvancedCommandHandler:
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.analytics = CommandAnalytics()
        self.cache_manager = CacheManager()
        
        # Load config
        try:
            with open('config.json', 'r') as f:
                self.config = json.load(f)
        except FileNotFoundError:
            logger.error("config.json not found! Using default values.")
            self.config = {}
        except json.JSONDecodeError:
            logger.error("Invalid config.json! Using default values.")
            self.config = {}
        
        # Setup default values
        self.cooldowns = {}
        self.custom_cooldowns = self.config.get('cooldowns', {
            'default': 3,
            'admin': 1
        })
        self.permissions = self.config.get('permissions', {})
        self.rate_limits = self.config.get('rate_limits', {
            'global': [5, 5],  # [max_commands, time_window]
            'user': [3, 5],
            'channel': [10, 5]
        })
        
        # Setup logging channel
        self.log_channel_id = int(self.config.get('channels', {}).get('logs', 0))

    async def check_rate_limit(self, ctx: commands.Context) -> bool:
        """Check if command exceeds rate limits dengan cache"""
        now = datetime.utcnow()
        
        # Admin bypass
        if str(ctx.author.id) == str(self.config.get('admin_id')):
            return True

        # Get rate limit data from cache
        cache_key = f"rate_limit:{ctx.author.id}"
        rate_data = await self.cache_manager.get(cache_key)
        
        if not rate_data:
            rate_data = {
                'commands': [],
                'last_reset': now.timestamp()
            }

        # Cleanup old commands
        rate_data['commands'] = [
              cmd_time for cmd_time in rate_data['commands']
            if (now - datetime.fromtimestamp(cmd_time)).total_seconds() <= self.rate_limits['user'][1]
        ]

        # Check limit
        if len(rate_data['commands']) >= self.rate_limits['user'][0]:
            return False

        # Update rate limit data
        rate_data['commands'].append(now.timestamp())
        await self.cache_manager.set(
            cache_key,
            rate_data,
            expires_in=self.rate_limits['user'][1]
        )

        return True

    async def check_cooldown(self, user_id: int, command: str) -> Tuple[bool, float]:
        """Check command cooldown dengan cache"""
        cache_key = f"cooldown:{user_id}:{command}"
        
        # Admin bypass
        if str(user_id) == str(self.config.get('admin_id')):
            return True, 0

        # Check cooldown from cache
        last_used = await self.cache_manager.get(cache_key)
        if last_used:
            cooldown_time = self.custom_cooldowns.get(
                command, 
                self.custom_cooldowns.get('default', 3)
            )
            elapsed = (datetime.utcnow() - datetime.fromtimestamp(last_used)).total_seconds()
            
            if elapsed < cooldown_time:
                return False, cooldown_time - elapsed

        # Set new cooldown
        await self.cache_manager.set(
            cache_key,
            datetime.utcnow().timestamp(),
            expires_in=self.custom_cooldowns.get(command, 3)
        )
        return True, 0

    async def check_permissions(self, ctx: commands.Context, command: str) -> bool:
        """Check user permissions for command"""
        # Admin bypass
        if str(ctx.author.id) == str(self.config.get('admin_id')):
            return True
            
        # Check cached permissions
        cache_key = f"perms:{ctx.author.id}:{command}"
        cached_perm = await self.cache_manager.get(cache_key)
        if cached_perm is not None:
            return cached_perm
            
        # Get user roles
        user_roles = [str(role.id) for role in ctx.author.roles]
        
        # Check role permissions
        has_permission = False
        for role_id in user_roles:
            if role_id in self.permissions:
                perms = self.permissions[role_id]
                if 'all' in perms or command in perms:
                    has_permission = True
                    break
        
        # Cache permission result for 5 minutes
        await self.cache_manager.set(
            cache_key,
            has_permission,
            expires_in=300
        )
                    
        return has_permission

    async def log_command(self, ctx: commands.Context, command: str, success: bool, error: Optional[Exception] = None) -> None:
        """Log command execution"""
        if not self.log_channel_id:
            return
            
        channel = self.bot.get_channel(self.log_channel_id)
        if not channel:
            return
            
        # Create embed
        embed = discord.Embed(
            title="Command Log",
            timestamp=datetime.utcnow(),
            color=discord.Color.green() if success else discord.Color.red()
        )
        
        embed.add_field(name="Command", value=command, inline=True)
        embed.add_field(name="User", value=f"{ctx.author} ({ctx.author.id})", inline=True)
        embed.add_field(name="Channel", value=f"{ctx.channel} ({ctx.channel.id})", inline=True)
        
        if error:
            embed.add_field(name="Error", value=str(error), inline=False)
            
        try:
            await channel.send(embed=embed)
        except Exception as e:
            logger.error(f"Failed to send command log: {e}")

        # Cache log entry
        cache_key = f"cmdlog:{ctx.message.id}"
        log_entry = {
            'command': command,
            'user_id': ctx.author.id,
            'channel_id': ctx.channel.id,
            'success': success,
            'error': str(error) if error else None,
            'timestamp': datetime.utcnow().isoformat()
        }
        
        await self.cache_manager.set(
            cache_key,
            log_entry,
            expires_in=86400,  # Keep logs for 24 hours
            permanent=True
        )

    async def handle_command(self, ctx: commands.Context, command_name: str) -> None:
        """Handle command execution with all features"""
        try:
            # Validate command exists
            command = self.bot.get_command(command_name)
            if not command:
                logger.error(f"Command not found: {command_name}")
                return

            # Rate Limit Check
            if not await self.check_rate_limit(ctx):
                await ctx.send("🚫 You're sending commands too fast!", delete_after=5)
                return
                
            # Permission Check
            if not await self.check_permissions(ctx, command_name):
                await ctx.send("❌ You don't have permission to use this command!", delete_after=5)
                return
                
            # Cooldown Check
            can_run, remaining = await self.check_cooldown(ctx.author.id, command_name)
            if not can_run:
                await ctx.send(
                    f"⏰ Please wait {remaining:.1f}s before using this command again!",
                    delete_after=5
                )
                return
                
            # Track Analytics
            await self.analytics.track_command(ctx, command_name)
            
            # Log successful command
            await self.log_command(ctx, command_name, True)
            
        except Exception as e:
            # Error Handling & Tracking
            await self.analytics.track_error(command_name, e)
            await self.log_command(ctx, command_name, False, e)
            
            error_message = "❌ An error occurred while executing the command!"
            if isinstance(e, commands.MissingPermissions):
                error_message = "❌ You don't have permission to use this command!"
            elif isinstance(e, commands.CommandOnCooldown):
                error_message = f"⏰ Please wait {e.retry_after:.1f}s before using this command again!"
            
            logger.error(f"Error in command {command_name}: {e}")
            await ctx.send(error_message, delete_after=5)