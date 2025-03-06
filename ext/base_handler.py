import asyncio
from asyncio import Lock
import logging
from typing import Optional, Dict
from discord.ext import commands
import discord

class BaseLockHandler:
    """Handler untuk sistem locking"""
    
    def __init__(self):
        self._locks: Dict[str, Lock] = {}
        self._response_locks: Dict[str, Lock] = {}
        self.logger = logging.getLogger(self.__class__.__name__)
        
    async def acquire_lock(self, key: str, timeout: float = 10.0) -> Optional[Lock]:
        """
        Dapatkan atau buat lock untuk key tertentu
        
        Args:
            key: Unique identifier untuk lock
            timeout: Waktu maksimum menunggu lock dalam detik
            
        Returns:
            Lock object jika berhasil, None jika gagal
        """
        if key not in self._locks:
            self._locks[key] = Lock()
            
        try:
            await asyncio.wait_for(self._locks[key].acquire(), timeout=timeout)
            return self._locks[key]
        except asyncio.TimeoutError:
            self.logger.error(f"Failed to acquire lock for {key} within {timeout} seconds")
            return None
        except Exception as e:
            self.logger.error(f"Error acquiring lock for {key}: {e}")
            return None

    async def acquire_response_lock(self, ctx_or_interaction, timeout: float = 5.0) -> bool:
        """
        Acquire lock untuk response context/interaction
        
        Args:
            ctx_or_interaction: Context atau Interaction object
            timeout: Waktu maksimum menunggu lock dalam detik
            
        Returns:
            True jika berhasil acquire lock, False jika gagal
        """
        key = str(ctx_or_interaction.id)
        if key not in self._response_locks:
            self._response_locks[key] = Lock()
            
        try:
            await asyncio.wait_for(self._response_locks[key].acquire(), timeout=timeout)
            return True
        except:
            return False

    def release_lock(self, key: str):
        """Release lock untuk key tertentu"""
        if key in self._locks and self._locks[key].locked():
            try:
                self._locks[key].release()
            except RuntimeError:
                self.logger.warning(f"Attempted to release an unlocked lock for {key}")

    def release_response_lock(self, ctx_or_interaction):
        """Release response lock untuk context/interaction"""
        key = str(ctx_or_interaction.id)
        if key in self._response_locks and self._response_locks[key].locked():
            try:
                self._response_locks[key].release()
            except RuntimeError:
                self.logger.warning(f"Attempted to release an unlocked response lock for {key}")

    def cleanup(self):
        """Bersihkan semua resources"""
        self._locks.clear()
        self._response_locks.clear()

    async def __aenter__(self):
        """Support untuk async context manager"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Cleanup saat exit context"""
        self.cleanup()

class BaseResponseHandler:
    """Handler untuk mengirim response dengan aman"""
    
    async def send_response_once(self, ctx_or_interaction, **kwargs):
        """
        Kirim response sekali saja, mendukung Context dan Interaction
        
        Args:
            ctx_or_interaction: Context atau Interaction object
            **kwargs: Argument untuk send/response.send_message
        """
        try:
            if isinstance(ctx_or_interaction, discord.Interaction):
                # Handling untuk Interaction
                if not ctx_or_interaction.response.is_done():
                    await ctx_or_interaction.response.send_message(**kwargs)
                else:
                    await ctx_or_interaction.followup.send(**kwargs)
            else:
                # Handling untuk Context
                await ctx_or_interaction.send(**kwargs)
                
        except discord.errors.NotFound:
            self.logger.warning("Attempted to respond to a deleted message/interaction")
        except discord.errors.Forbidden:
            self.logger.warning("Bot doesn't have permission to send message")
        except Exception as e:
            self.logger.error(f"Error sending response: {e}")

    async def edit_response_safely(self, ctx_or_interaction, **kwargs):
        """
        Edit response dengan aman
        
        Args:
            ctx_or_interaction: Context atau Interaction object
            **kwargs: Argument untuk edit
        """
        try:
            if isinstance(ctx_or_interaction, discord.Interaction):
                # Handling untuk Interaction
                if ctx_or_interaction.response.is_done():
                    await ctx_or_interaction.edit_original_response(**kwargs)
                else:
                    await ctx_or_interaction.response.send_message(**kwargs)
            else:
                # Handling untuk Context
                if hasattr(ctx_or_interaction, 'message'):
                    await ctx_or_interaction.message.edit(**kwargs)
                    
        except discord.errors.NotFound:
            self.logger.warning("Attempted to edit a deleted message/interaction")
        except discord.errors.Forbidden:
            self.logger.warning("Bot doesn't have permission to edit message")
        except Exception as e:
            self.logger.error(f"Error editing response: {e}")