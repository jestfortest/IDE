import discord
from discord import app_commands
from discord.ext import commands
import logging
from typing import Optional
import asyncio
from types import SimpleNamespace

logger = logging.getLogger(__name__)

class ChatCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    
    @app_commands.command(name="chat", description="Chat with AI using the text model pool")
    @app_commands.describe(
        message="Your message to the AI",
        model="Specific model to use (optional, uses pool if not specified)"
    )
    async def chat(
        self, 
        interaction: discord.Interaction, 
        message: str,
        model: Optional[str] = None
    ):
        setup_cog = self.bot.get_cog("SetupCog")
        guild_config = (
            setup_cog._get_guild_config(interaction.guild.id)
            if setup_cog and interaction.guild
            else {}
        )
        private_chat_enabled = bool(interaction.guild and guild_config.get("ai_chat", {}).get("enabled"))
        await interaction.response.defer(ephemeral=private_chat_enabled)
        
        try:
            service = self.bot.openrouter_service
            if model is None:
                if not service.DEFAULT_TEXT_POOL:
                    raise RuntimeError("No text models are currently configured.")
                model = service.select_model_from_pool(service.DEFAULT_TEXT_POOL)

            if private_chat_enabled and setup_cog:
                ai_chat = guild_config.get("ai_chat", {})
                owner_channels = ai_chat.get("channels", {}).get(str(interaction.user.id), [])
                if not isinstance(owner_channels, list):
                    owner_channels = [owner_channels] if owner_channels else []
                owner_channel_ids = {int(channel_id) for channel_id in owner_channels if str(channel_id).isdigit()}
                if interaction.channel_id in owner_channel_ids and isinstance(interaction.channel, discord.TextChannel):
                    target_channel = interaction.channel
                else:
                    target_channel, _ = await setup_cog.create_private_ai_channel(
                        interaction.guild,
                        interaction.user,
                    )

                question_embed = discord.Embed(
                    title=f"Message from {interaction.user.display_name}",
                    description=message[:4000],
                    color=0x3498db,
                )
                await target_channel.send(embed=question_embed, allowed_mentions=discord.AllowedMentions.none())
                ai_cog = self.bot.get_cog("AICog")
                if ai_cog is None:
                    raise RuntimeError("The AI chat service is unavailable.")
                await ai_cog.on_message(SimpleNamespace(
                    guild=interaction.guild,
                    author=interaction.user,
                    channel=target_channel,
                    content=message,
                    attachments=[],
                    model_override=model,
                ))
                await interaction.followup.send(f"Your message was sent to {target_channel.mention}.", ephemeral=True)
                return
            
            # Prepare messages
            messages = [{"role": "user", "content": message}]
            
            response = await service.chat_completion_with_fallback(
                messages=messages,
                model=model,
                max_tokens=1500,
                temperature=0.7,
            )
            model = response.get("_used_model", model)
            
            # Extract the response
            ai_message = response["choices"][0]["message"]["content"]
            
            # Create embed
            embed = discord.Embed(
                title="💬 AI Chat Response",
                description=ai_message,
                color=0x3498db  # info color
            )
            embed.add_field(name="Model Used", value=model, inline=True)
            embed.set_footer(text=f"Requested by {interaction.user.display_name}")
            
            await interaction.followup.send(embed=embed, ephemeral=private_chat_enabled)
            
        except Exception as e:
            logger.error(f"Error in chat command: {e}")
            embed = discord.Embed(
                title="❌ Error",
                description=f"An error occurred while processing your request: {str(e)}",
                color=0xe74c3c  # error color
            )
            await interaction.followup.send(embed=embed)

async def setup(bot):
    await bot.add_cog(ChatCog(bot))