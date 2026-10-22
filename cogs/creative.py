import discord
from discord import app_commands
from discord.ext import commands
import logging
from typing import Optional
import asyncio

logger = logging.getLogger(__name__)

class CreativeCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    
    @app_commands.command(name="creative", description="Generate creative content using the creative model pool")
    @app_commands.describe(
        prompt="Your creative writing prompt",
        genre="Genre or style (optional, e.g., poetry, story, song lyrics)",
        model="Specific model to use (optional, uses pool if not specified)"
    )
    async def creative(
        self, 
        interaction: discord.Interaction, 
        prompt: str,
        genre: Optional[str] = None,
        model: Optional[str] = None
    ):
        await interaction.response.defer()
        
        try:
            # Use provided model or select from creative pool
            if model is None:
                model = self.bot.openrouter_service.select_model_from_pool(
                    self.bot.openrouter_service.DEFAULT_CREATIVE_POOL
                )
            
            # Prepare the prompt
            full_prompt = f"Create a {genre} about: {prompt}" if genre else f"Create creative content about: {prompt}"
            full_prompt += "\n\nBe imaginative, engaging, and showcase your creative abilities."
            
            # Prepare messages
            messages = [{"role": "user", "content": full_prompt}]
            
            # Get response from OpenRouter
            response = await self.bot.openrouter_service.chat_completion_with_fallback(
                messages=messages,
                model=model,
                max_tokens=1500,
                temperature=0.8  # Higher temperature for more creativity
            )
            model = response.get("_used_model", model)
            
            # Extract the response
            ai_message = response["choices"][0]["message"]["content"]
            
            # Create embed
            embed = discord.Embed(
                title="🎨 Creative Generation",
                description=ai_message,
                color=0x9b59b6  # modlog color (using as creative color)
            )
            embed.add_field(name="Model Used", value=model, inline=True)
            if genre:
                embed.add_field(name="Genre/Style", value=genre, inline=True)
            embed.set_footer(text=f"Requested by {interaction.user.display_name}")
            
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Error in creative command: {e}")
            embed = discord.Embed(
                title="❌ Error",
                description=f"An error occurred while generating creative content: {str(e)}",
                color=0xe74c3c  # error color
            )
            await interaction.followup.send(embed=embed)

async def setup(bot):
    await bot.add_cog(CreativeCog(bot))