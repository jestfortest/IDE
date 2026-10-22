import discord
from discord import app_commands
from discord.ext import commands
import logging
from typing import Optional
import io

logger = logging.getLogger(__name__)

class AudioCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    
    @app_commands.command(name="audio", description="Generate audio/speech using the audio generation model pool")
    @app_commands.describe(
        text="Text to convert to speech",
        model="Specific model to use (optional, uses pool if not specified)",
        voice="Voice ID for the TTS model (optional, e.g. 'b347db033a6549378b48d00acb0d06cd' for fish-audio)"
    )
    async def audio(
        self, 
        interaction: discord.Interaction, 
        text: str,
        model: Optional[str] = None,
        voice: Optional[str] = None
    ):
        await interaction.response.defer()
        
        try:
            # Check if audio pool has models
            audio_pool = self.bot.openrouter_service.DEFAULT_AUDIO_GEN_POOL
            if not audio_pool and model is None:
                embed = discord.Embed(
                    title="❌ No Audio Models Available",
                    description="No audio models are available, please contact staff or try again later.",
                    color=0xe74c3c
                )
                await interaction.followup.send(embed=embed)
                return
            
            # Use provided model or select from audio generation pool
            if model is None:
                model = self.bot.openrouter_service.select_model_from_pool(audio_pool)
            
            # Limit text length for TTS
            if len(text) > 1000:
                text = text[:1000] + "..."
            
            # Generate audio using OpenRouter
            response = await self.bot.openrouter_service.audio_generation(
                text=text,
                model=model,
                voice=voice,
            )
            
            audio_bytes = response.get("audio_bytes")
            if not isinstance(audio_bytes, bytes) or not audio_bytes:
                raise ValueError("OpenRouter returned an empty audio response.")
            content_type = response.get("content_type", "audio/wav")
            extension = {
                "audio/wav": "wav",
                "audio/x-wav": "wav",
                "audio/mpeg": "mp3",
                "audio/ogg": "ogg",
                "audio/flac": "flac",
            }.get(content_type, "bin")
            audio_file = discord.File(
                io.BytesIO(audio_bytes),
                filename=f"generated_audio.{extension}",
            )
            
            # Create embed
            embed = discord.Embed(
                title="🔊 Generated Audio",
                description=f"*Text: {text[:100]}{'...' if len(text) > 100 else ''}*",
                color=0xe67e22  # orange color for audio
            )
            embed.add_field(name="Model Used", value=model, inline=True)
            embed.add_field(name="Text Length", value=f"{len(text)} characters", inline=True)
            
            await interaction.followup.send(
                embed=embed,
                file=audio_file
            )
            
        except Exception as e:
            logger.error(f"Error in audio command: {e}")
            embed = discord.Embed(
                title="❌ Error",
                description=f"An error occurred while generating audio: {str(e)}",
                color=0xe74c3c  # error color
            )
            await interaction.followup.send(embed=embed)

async def setup(bot):
    await bot.add_cog(AudioCog(bot))