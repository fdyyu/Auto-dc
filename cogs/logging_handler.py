import discord
from discord.ext import commands
import logging
from datetime import datetime
from .utils import Embed, event_dispatcher

class LoggingHandler(commands.Cog):
    """📝 Centralized Logging System"""
    
    def __init__(self, bot):
        self.bot = bot
        self.setup_logging()
        self.register_events()

    def setup_logging(self):
        """Setup logging configuration"""
        self.logger = logging.getLogger('discord')
        self.logger.setLevel(logging.INFO)
        
        # File handler
        file_handler = logging.FileHandler(
            filename='logs/discord.log', 
            encoding='utf-8', 
            mode='a'
        )
        file_handler.setFormatter(
            logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s')
        )
        self.logger.addHandler(file_handler)
        
        # Activity logger
        self.activity_logger = logging.getLogger('activity')
        activity_handler = logging.FileHandler(
            filename='logs/activity.log',
            encoding='utf-8',
            mode='a'
        )
        activity_handler.setFormatter(
            logging.Formatter('%(asctime)s:%(message)s')
        )
        self.activity_logger.addHandler(activity_handler)

    def register_events(self):
        """Register event handlers"""
        event_dispatcher.register('message', self.log_message)
        event_dispatcher.register('command', self.log_command)
        event_dispatcher.register('error', self.log_error)
        event_dispatcher.register('voice', self.log_voice)

    async def log_message(self, message):
        """Log message activity"""
        if message.author.bot:
            return
            
        self.activity_logger.info(
            f"Message by {message.author} (ID: {message.author.id}) "
            f"in #{message.channel.name} ({message.guild.name})"
        )

    async def log_command(self, ctx):
        """Log command usage"""
        self.logger.info(
            f"Command '{ctx.command}' used by {ctx.author} "
            f"(ID: {ctx.author.id}) in #{ctx.channel.name}"
        )

    async def log_error(self, ctx, error):
        """Log command errors"""
        self.logger.error(
            f"Error in command '{ctx.command}' by {ctx.author}: {error}"
        )

    async def log_voice(self, member, before, after):
        """Log voice state changes"""
        if before.channel != after.channel:
            if after.channel:
                action = f"joined {after.channel.name}"
            else:
                action = f"left {before.channel.name}"
                
            self.activity_logger.info(
                f"Voice: {member} (ID: {member.id}) {action}"
            )

    @commands.Cog.listener()
    async def on_command(self, ctx):
        await event_dispatcher.dispatch('command', ctx)

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        await event_dispatcher.dispatch('error', ctx, error)

    @commands.Cog.listener()
    async def on_message(self, message):
        await event_dispatcher.dispatch('message', message)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        await event_dispatcher.dispatch('voice', member, before, after)

async def setup(bot):
    await bot.add_cog(LoggingHandler(bot))