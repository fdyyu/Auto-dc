import discord
from discord.ext import commands
import os
import json
import logging
import asyncio
import aiohttp
import sqlite3
from pathlib import Path
from database import setup_database, get_connection
from datetime import datetime
from utils.command_handler import AdvancedCommandHandler
from ext.base_handler import BaseLockHandler, BaseResponseHandler

# Setup logging dengan file handler
log_dir = Path('logs')
log_dir.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_dir / 'bot.log'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

# Load config dengan validasi
def load_config():
    required_keys = {
        'token': str,
        'guild_id': (int, str),
        'admin_id': (int, str),
        'id_live_stock': (int, str),
        'id_log_purch': (int, str),
        'id_donation_log': (int, str),
        'id_history_buy': (int, str),
        'channels': dict,
        'roles': dict,
        'cooldowns': dict,
        'permissions': dict,
        'rate_limits': dict
    }
    
    try:
        with open('config.json', 'r') as config_file:
            config = json.load(config_file)

        # Validate and convert types
        for key, expected_type in required_keys.items():
            if key not in config:
                raise KeyError(f"Missing required key: {key}")
            
            # Handle multiple allowed types
            if isinstance(expected_type, tuple):
                if not isinstance(config[key], expected_type):
                    config[key] = expected_type[0](config[key])
            else:
                if not isinstance(config[key], expected_type):
                    config[key] = expected_type(config[key])

        return config

    except FileNotFoundError:
        logger.error("config.json file not found!")
        raise
    except json.JSONDecodeError:
        logger.error("config.json is not valid JSON!")
        raise
    except (KeyError, ValueError) as e:
        logger.error(f"Configuration error: {e}")
        raise

# Load config
config = load_config()
TOKEN = config['token']
GUILD_ID = int(config['guild_id'])
ADMIN_ID = int(config['admin_id'])
LIVE_STOCK_CHANNEL_ID = int(config['id_live_stock'])
LOG_PURCHASE_CHANNEL_ID = int(config['id_log_purch'])
DONATION_LOG_CHANNEL_ID = int(config['id_donation_log'])
HISTORY_BUY_CHANNEL_ID = int(config['id_history_buy'])

class MyBot(commands.Bot, BaseLockHandler, BaseResponseHandler):
    def __init__(self):
        intents = discord.Intents.all()
        commands.Bot.__init__(self, command_prefix='!', intents=intents, help_command=commands.DefaultHelpCommand())
        BaseLockHandler.__init__(self)
        BaseResponseHandler.__init__(self)
        self.session = None
        self.admin_id = ADMIN_ID
        self.guild_id = GUILD_ID
        self.live_stock_channel_id = LIVE_STOCK_CHANNEL_ID
        self.log_purchase_channel_id = LOG_PURCHASE_CHANNEL_ID
        self.donation_log_channel_id = DONATION_LOG_CHANNEL_ID
        self.history_buy_channel_id = HISTORY_BUY_CHANNEL_ID
        self.config = config
        self.startup_time = datetime.utcnow()
        self.command_handler = AdvancedCommandHandler(self)

    async def setup_hook(self):
        """Initialize bot components"""
        self.session = aiohttp.ClientSession()
        
        # Load extensions with proper error handling
        extensions = [
            'cogs.admin',
            'ext.live',
            'ext.trx',
            'ext.donate',
            'ext.balance_manager',
            'ext.product_manager',
            # New cogs
            'cogs.stats',
            'cogs.automod',
            'cogs.tickets',
            'cogs.welcome',
            'cogs.leveling',
        ]
        
        loaded_extensions = set()  # Track loaded extensions
        
        for ext in extensions:
            try:
                if ext not in loaded_extensions:  # Check if not already loaded
                    await self.load_extension(ext)
                    loaded_extensions.add(ext)
                    logger.info(f'✅ Loaded extension: {ext}')
            except Exception as e:
                logger.error(f'❌ Failed to load {ext}: {e}')
                # Log detailed error info
                logger.exception(f"Detailed error loading {ext}:")
                continue  # Continue loading other extensions

    async def close(self):
        """Cleanup when bot shuts down"""
        logger.info("Bot shutting down...")
        if self.session:
            await self.session.close()
        await super().close()

    async def on_ready(self):
        """Event when bot is ready"""
        logger.info(f'Bot {self.user.name} is ready!')
        logger.info(f'Bot ID: {self.user.id}')
        logger.info(f'Guild ID: {self.guild_id}')
        logger.info(f'Admin ID: {self.admin_id}')
        
        # Verify channels exist
        guild = self.get_guild(self.guild_id)
        if not guild:
            logger.error(f"Could not find guild with ID {self.guild_id}")
            return

        channels = {
            'Live Stock': self.live_stock_channel_id,
            'Purchase Log': self.log_purchase_channel_id,
            'Donation Log': self.donation_log_channel_id,
            'History Buy': self.history_buy_channel_id,
            'Music': int(self.config['channels']['music']),
            'Logs': int(self.config['channels']['logs'])
        }

        for name, channel_id in channels.items():
            channel = guild.get_channel(channel_id)
            if not channel:
                logger.error(f"Could not find {name} channel with ID {channel_id}")
            else:
                logger.info(f"✅ Found {name} channel: {channel.name}")

        # Set custom status
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Growtopia Shop | !help"
            ),
            status=discord.Status.online
        )

    async def on_message(self, message):
        """Handle message events"""
        if message.author.bot:
            return

        # Log messages from specific channels
        if message.channel.id in [
            self.live_stock_channel_id,
            self.log_purchase_channel_id,
            self.donation_log_channel_id,
            self.history_buy_channel_id
        ]:
            logger.info(
                f'Channel {message.channel.name}: '
                f'{message.author}: {message.content}'
            )

        await self.process_commands(message)

    async def on_command(self, ctx):
        """Event when command is triggered"""
        try:
            await self.command_handler.handle_command(
                ctx, 
                ctx.command.name,
                *ctx.args[2:],
                **ctx.kwargs
            )
        except Exception as e:
            logger.error(f"Command handling error: {e}")
            logger.exception("Detailed command handling error:")

    async def on_command_error(self, ctx, error):
        """Global error handler"""
        if isinstance(error, commands.errors.CheckFailure):
            await self.send_response_once(ctx, "❌ You don't have permission to use this command!", delete_after=5)
        elif isinstance(error, commands.errors.CommandNotFound):
            pass  # Ignore command not found
        elif isinstance(error, commands.errors.MissingRequiredArgument):
            await self.send_response_once(ctx, f"❌ Missing required argument: {error.param.name}", delete_after=5)
        elif isinstance(error, commands.errors.BadArgument):
            await self.send_response_once(ctx, "❌ Invalid argument provided!", delete_after=5)
        else:
            error_msg = f'Error in command {ctx.command}: {error}'
            logger.error(error_msg)
            await self.send_response_once(ctx, "❌ An error occurred! The administrator has been notified.", delete_after=5)
            
            # Notify admin if serious error
            if not isinstance(error, (commands.errors.CheckFailure, commands.errors.CommandNotFound)):
                admin = self.get_user(self.admin_id)
                if admin:
                    await admin.send(f"⚠️ Bot Error:\n```{error_msg}```")

    async def on_guild_join(self, guild):
        """Event when bot joins a new guild"""
        logger.info(f"Bot joined new guild: {guild.name} (ID: {guild.id})")
        if guild.id != self.guild_id:
            logger.warning(f"Bot joined unauthorized guild: {guild.name} (ID: {guild.id})")
            await guild.leave()
            
    @commands.is_owner()
    async def reload_extension(self, ctx, extension):
        """Reload a specific extension"""
        try:
            await self.unload_extension(extension)
            await self.load_extension(extension)
            await ctx.send(f"✅ Reloaded extension: {extension}")
        except Exception as e:
            await ctx.send(f"❌ Error reloading {extension}: {e}")
            logger.error(f"Error reloading {extension}: {e}")

bot = MyBot()

async def main():
    """Main function to run the bot"""
    try:
        # Initialize database
        setup_database()
        logger.info("Database initialized successfully")
        
        # Start bot
        async with bot:
            await bot.start(TOKEN)
    except Exception as e:
        logger.error(f'Fatal error: {e}')
        logger.exception("Detailed fatal error:")
        raise
    finally:
        if not bot.is_closed():
            await bot.close()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info('Bot stopped by user')
    except Exception as e:
        logger.error(f'Fatal error occurred: {e}')
        logger.exception("Detailed fatal error on startup:")