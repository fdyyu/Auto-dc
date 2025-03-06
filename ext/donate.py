import discord
from discord.ext import commands
import logging
from datetime import datetime
import json
import asyncio
from http.server import BaseHTTPRequestHandler, HTTPServer
from database import get_connection
from .constants import (
    Balance,         # Untuk perhitungan balance
    TransactionError,# Untuk error handling
    CURRENCY_RATES, # Untuk konversi mata uang
    MESSAGES,       # Untuk pesan response
    TransactionType # Untuk tipe transaksi DONATION
)
# Load config
with open('config.json') as config_file:
    config = json.load(config_file)

DONATION_LOG_CHANNEL_ID = int(config['id_donation_log'])
PORT = 8081

class DonationManager:
    """Manager class for handling donations"""
    _instance = None

    def __new__(cls, bot):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, bot):
        if not hasattr(self, 'initialized'):
            self.bot = bot
            self.logger = logging.getLogger("DonationManager")
            self.initialized = True

    def parse_deposit(self, deposit: str) -> tuple[int, int, int]:
        """Parse deposit string into WL, DL, BGL amounts"""
        wl = dl = bgl = 0
        
        deposits = deposit.split(',')
        for d in deposits:
            d = d.strip()
            if 'World Lock' in d:
                wl += int(d.split()[0])
            elif 'Diamond Lock' in d:
                dl += int(d.split()[0])
            elif 'Blue Gem Lock' in d:
                bgl += int(d.split()[0])
                
        return wl, dl, bgl

    async def process_donation(self, growid: str, wl: int, dl: int, bgl: int) -> Balance:
        """Process a donation"""
        conn = None
        try:
            conn = get_connection()
            cursor = conn.cursor()
            
            # Get current balance
            cursor.execute("""
                SELECT balance_wl, balance_dl, balance_bgl 
                FROM users 
                WHERE growid = ?
            """, (growid,))
            
            result = cursor.fetchone()
            if not result:
                # Create new user
                cursor.execute("""
                    INSERT INTO users (growid, balance_wl, balance_dl, balance_bgl)
                    VALUES (?, 0, 0, 0)
                """, (growid,))
                current = Balance(0, 0, 0)
            else:
                current = Balance(
                    result['balance_wl'],
                    result['balance_dl'],
                    result['balance_bgl']
                )
            
            # Calculate new balance
            new_balance = Balance(
                current.wl + wl,
                current.dl + dl,
                current.bgl + bgl
            )
            
            # Update balance
            cursor.execute("""
                UPDATE users 
                SET balance_wl = ?,
                    balance_dl = ?,
                    balance_bgl = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE growid = ?
            """, (new_balance.wl, new_balance.dl, new_balance.bgl, growid))
            
            # Log transaction
            total_wls = wl + (dl * CURRENCY_RATES['DL']) + (bgl * CURRENCY_RATES['BGL'])
            
            cursor.execute("""
                INSERT INTO transactions 
                (growid, type, details, old_balance, new_balance, total_price)
                VALUES (?, 'DONATION', ?, ?, ?, ?)
            """, (
                growid,
                f"Donation: {wl} WL, {dl} DL, {bgl} BGL",
                current.format(),
                new_balance.format(),
                total_wls
            ))
            
            conn.commit()
            return new_balance
            
        except Exception as e:
            if conn:
                conn.rollback()
            raise e
            
        finally:
            if conn:
                conn.close()

    async def log_to_discord(self, channel_id: int, growid: str, wl: int, dl: int, bgl: int, new_balance: Balance):
        """Log donation to Discord channel"""
        try:
            channel = self.bot.get_channel(channel_id)
            if not channel:
                self.logger.error("Donation log channel not found")
                return
                
            embed = discord.Embed(
                title="💎 New Donation Received",
                color=discord.Color.green(),
                timestamp=datetime.utcnow()
            )
            
            embed.add_field(name="GrowID", value=growid, inline=True)
            embed.add_field(name="Amount", value=(f"• {wl:,} WL\n" f"• {dl:,} DL\n" f"• {bgl:,} BGL"), inline=True)
            embed.add_field(name="New Balance", value=new_balance.format(), inline=False)
            
            await channel.send(embed=embed)
            
        except Exception as e:
            self.logger.error(f"Error logging to Discord: {e}")

class DonateHandler(BaseHTTPRequestHandler):
    """HTTP handler for donation requests"""
    bot = None
    manager = None
    logger = logging.getLogger("DonateHandler")

    def do_POST(self):
        """Handle POST requests"""
        try:
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            self.logger.info(f"Received donation data: {post_data}")
            
            data = json.loads(post_data)
            growid = data.get('GrowID')
            deposit = data.get('Deposit')
            
            if not growid or not deposit:
                self.send_error_response("Invalid data")
                return
                
            # Parse deposit amounts
            wl, dl, bgl = self.manager.parse_deposit(deposit)
            
            # Process donation using asyncio
            loop = asyncio.get_event_loop()
            new_balance = loop.run_until_complete(
                self.manager.process_donation(growid, wl, dl, bgl)
            )
            
            # Send success response
            self.send_success_response(growid, wl, dl, bgl, new_balance)
            
            # Log to Discord
            loop.run_until_complete(
                self.manager.log_to_discord(
                    DONATION_LOG_CHANNEL_ID,
                    growid, 
                    wl, 
                    dl, 
                    bgl, 
                    new_balance
                )
            )
            
        except json.JSONDecodeError:
            self.send_error_response("Invalid JSON data")
        except Exception as e:
            self.logger.error(f"Error processing donation: {e}")
            self.send_error_response("Internal server error")

    def send_success_response(self, growid: str, wl: int, dl: int, bgl: int, new_balance: Balance):
        """Send success response"""
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        
        response = (
            f"✅ Donation received!\n"
            f"GrowID: {growid}\n"
            f"Amount: {wl} WL, {dl} DL, {bgl} BGL\n"
            f"New Balance:\n{new_balance.format()}"
        )
        self.wfile.write(response.encode())

    def send_error_response(self, message: str):
        """Send error response"""
        self.send_response(400)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(f"❌ Error: {message}".encode())

class Donation(commands.Cog):
    """Cog for donation system"""
    def __init__(self, bot):
        self.bot = bot
        self.logger = logging.getLogger("Donation")
        self.server = None
        self.manager = DonationManager(bot)
        DonateHandler.bot = bot
        DonateHandler.manager = self.manager
        
        # Flag untuk mencegah duplikasi
        if not hasattr(bot, 'donation_initialized'):
            bot.donation_initialized = True
            self._start_server()
            self.logger.info("Donation cog initialized")

    def _start_server(self):
        """Start the donation server"""
        if not self.server:
            try:
                self.server = HTTPServer(('0.0.0.0', PORT), DonateHandler)
                self.logger.info(f'Starting donation server on port {PORT}')
                self.bot.loop.run_in_executor(None, self.server.serve_forever)
            except Exception as e:
                self.logger.error(f"Failed to start donation server: {e}")

    def cog_unload(self):
        """Cleanup when cog is unloaded"""
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        self.logger.info("Donation cog unloaded")

async def setup(bot):
    """Setup the Donation cog"""
    if not hasattr(bot, 'donation_cog_loaded'):
        await bot.add_cog(Donation(bot))
        bot.donation_cog_loaded = True
        logging.info('Donation cog loaded successfully')
