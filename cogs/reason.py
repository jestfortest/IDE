import discord
from discord import app_commands
from discord.ext import commands
import logging
from typing import Optional
import asyncio

logger = logging.getLogger(__name__)

class ReasonCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    
    @app_commands.command(name="reason", description="Solve complex problems using the reasoning model pool")
    @app_commands.describe(
        question="Your complex question or problem to solve",
        model="Specific model to use (optional, uses pool if not specified)"
    )
    async def reason(
        self, 
        interaction: discord.Interaction, 
        question: str,
        model: Optional[str] = None
    ):
        await interaction.response.defer()
        
        try:
            # Use provided model or select from reasoning pool
            if model is None:
                model = self.bot.openrouter_service.select_model_from_pool(
                    self.bot.openrouter_service.DEFAULT_REASONING_POOL
                )
            
            # Prepare messages with reasoning prompt
            messages = [
                {"role": "system", "content": "You are an AI assistant specialized in logical reasoning and problem-solving. Break down complex problems step by step, show your work, and provide clear explanations."},
                {"role": "user", "content": question}
            ]
            
            # Get response from OpenRouter
            response = await self.bot.openrouter_service.chat_completion_with_fallback(
                messages=messages,
                model=model,
                max_tokens=2000,
                temperature=0.3  # Lower temperature for more logical reasoning
            )
            model = response.get("_used_model", model)
            
            # Extract the response
            ai_message = response["choices"][0]["message"]["content"]
            
            # Create embed
            embed = discord.Embed(
                title="🧠 Reasoning Result",
                description=ai_message,
                color=0xf39c12  # warning color (using as reasoning color)
            )
            embed.add_field(name="Model Used", value=model, inline=True)
            embed.set_footer(text=f"Requested by {interaction.user.display_name}")
            
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Error in reason command: {e}")
            embed = discord.Embed(
                title="❌ Error",
                description=f"An error occurred while reasoning: {str(e)}",
                color=0xe74c3c  # error color
            )
            await interaction.followup.send(embed=embed)

async def setup(bot):
    await bot.add_cog(ReasonCog(bot))