import discord
from discord.ext import commands
import logging
import json
from datetime import datetime
from typing import Optional, Dict, List, Tuple

logger = logging.getLogger(__name__)

class CommandAnalytics:
    def __init__(self):
        self.usage_stats = {}
        self.error_stats = {}
        
    async def track_command(self, ctx, command: str):
        now = datetime.utcnow()
        
        if command not in self.usage_stats:
            self.usage_stats[command] = {
                'total_uses': 0,
                'users': set(),
                'channels': set(),
                'last_used': None,
                'peak_hour_usage': [0] * 24
            }
        
        stats = self.usage_stats[command]
        stats['total_uses'] += 1
        stats['users'].add(ctx.author.id)
        stats['channels'].add(ctx.channel.id)
        stats['last_used'] = now
        stats['peak_hour_usage'][now.hour] += 1

    async def track_error(self, command: str, error: Exception):
        if command not in self.error_stats:
            self.error_stats[command] = []
        
        self.error_stats[command].append({
            'time': datetime.utcnow(),
            'error': str(error),
            'type': type(error).__name__
        })

class AdvancedCommandHandler:
    def __init__(self, bot):
        self.bot = bot
        self.analytics = CommandAnalytics()
        
        # Load config
        with open('config.json', 'r') as f:
            self.config = json.load(f)
        
        self.cooldowns = {}
        self.custom_cooldowns = self.config.get('cooldowns', {})
        self.permissions = self.config.get('permissions', {})
        self.rate_limits = self.config.get('rate_limits', {
            'global': [5, 5],
            'user': [3, 5],
            'channel': [10, 5]
        })
        
        # Rate limit tracking
        self.rate_usage = {
            'global': [],
            'user': {},
            'channel': {}
        }
        
        # Setup logging channel
        self.log_channel_id = int(self.config['channels']['logs'])

    async def check_rate_limit(self, ctx) -> bool:
        now = datetime.utcnow()
        
        # Global limit
        self.rate_usage['global'] = [t for t in self.rate_usage['global'] 
                                   if (now - t).total_seconds() <= self.rate_limits['global'][1]]
        if len(self.rate_usage['global']) >= self.rate_limits['global'][0]:
            return False
            
        # User limit
        user_id = str(ctx.author.id)
        if user_id not in self.rate_usage['user']:
            self.rate_usage['user'][user_id] = []
            
        self.rate_usage['user'][user_id] = [t for t in self.rate_usage['user'][user_id] 
                                          if (now - t).total_seconds() <= self.rate_limits['user'][1]]
        if len(self.rate_usage['user'][user_id]) >= self.rate_limits['user'][0]:
            return False
            
        # Update usage
        self.rate_usage['global'].append(now)
        self.rate_usage['user'][user_id].append(now)
        return True

    async def check_cooldown(self, user_id: int, command: str) -> Tuple[bool, float]:
        key = f"{user_id}:{command}"
        now = datetime.utcnow()
        
        if key in self.cooldowns:
            cooldown_time = self.custom_cooldowns.get(command, 
                                                    self.custom_cooldowns.get('default', 3))
            elapsed = (now - self.cooldowns[key]).total_seconds()
            
            if elapsed < cooldown_time:
                return False, cooldown_time - elapsed
                
        self.cooldowns[key] = now
        return True, 0

    async def check_permissions(self, ctx, command: str) -> bool:
        # Admin bypass
        if str(ctx.author.id) == self.config['admin_id']:
            return True
            
        # Get user roles
        user_roles = [role.id for role in ctx.author.roles]
        
        # Check role permissions
        for role_id in user_roles:
            role_id = str(role_id)
            if role_id in self.permissions:
                perms = self.permissions[role_id]
                if 'all' in perms or command in perms:
                    return True
                    
        return False

    async def log_command(self, ctx, command: str, success: bool, error: Optional[Exception] = None):
        channel = self.bot.get_channel(self.log_channel_id)
        if not channel:
            return
            
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
            
        await channel.send(embed=embed)

    async def handle_command(self, ctx, command_name: str, *args, **kwargs):
        """Handle command execution with all features"""
        try:
            # 1. Rate Limit Check
            if not await self.check_rate_limit(ctx):
                await ctx.send("🚫 You're sending commands too fast!", delete_after=5)
                return
                
            # 2. Permission Check
            if not await self.check_permissions(ctx, command_name):
                await ctx.send("❌ You don't have permission to use this command!", delete_after=5)
                return
                
            # 3. Cooldown Check
            can_run, remaining = await self.check_cooldown(ctx.author.id, command_name)
            if not can_run:
                await ctx.send(
                    f"⏰ Please wait {remaining:.1f}s before using this command again!",
                    delete_after=5
                )
                return
                
            # 4. Track Analytics
            await self.analytics.track_command(ctx, command_name)
                
            # 5. Execute Command
            command = self.bot.get_command(command_name)
            await command.callback(command.cog, ctx, *args, **kwargs)
            
            # 6. Log successful command
            await self.log_command(ctx, command_name, True)
            
        except Exception as e:
            # 7. Error Handling & Tracking
            await self.analytics.track_error(command_name, e)
            await self.log_command(ctx, command_name, False, e)
            
            if isinstance(e, commands.MissingPermissions):
                await ctx.send("❌ You don't have permission to use this command!", delete_after=5)
            elif isinstance(e, commands.CommandOnCooldown):
                await ctx.send(
                    f"⏰ Please wait {e.retry_after:.1f}s before using this command again!",
                    delete_after=5
                )
            else:
                logger.error(f"Error in command {command_name}: {e}")
                await ctx.send(
                    "❌ An error occurred while executing the command!",
                    delete_after=5
      )
