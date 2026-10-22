import discord
from discord import app_commands
from discord.ext import commands
import logging
from typing import Optional
import asyncio

logger = logging.getLogger(__name__)

class CodeCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    
    @app_commands.command(name="code", description="Generate code using the code model pool")
    @app_commands.describe(
        prompt="Your code generation request",
        language="Programming language (optional)",
        model="Specific model to use (optional, uses pool if not specified)"
    )
    async def code(
        self, 
        interaction: discord.Interaction, 
        prompt: str,
        language: Optional[str] = None,
        model: Optional[str] = None
    ):
        await interaction.response.defer()
        
        try:
            # Use provided model or select from code pool
            if model is None:
                model = self.bot.openrouter_service.select_model_from_pool(
                    self.bot.openrouter_service.DEFAULT_CODE_POOL
                )
            
            # Prepare the prompt
            full_prompt = f"Generate {language} code for: {prompt}" if language else f"Generate code for: {prompt}"
            full_prompt += "\n\nProvide only the code without explanations unless specifically asked."
            
            # Prepare messages
            messages = [{"role": "user", "content": full_prompt}]
            
            # Get response from OpenRouter
            response = await self.bot.openrouter_service.chat_completion_with_fallback(
                messages=messages,
                model=model,
                max_tokens=2000,
                temperature=0.2  # Lower temperature for more focused code generation
            )
            model = response.get("_used_model", model)
            
            # Extract the response
            ai_message = response["choices"][0]["message"]["content"]
            
            # Create embed
            embed = discord.Embed(
                title="💻 Generated Code",
                description=f"```{language or ''}\n{ai_message}\n```",
                color=0x2ecc71  # success color
            )
            embed.add_field(name="Model Used", value=model, inline=True)
            if language:
                embed.add_field(name="Language", value=language, inline=True)
            embed.set_footer(text=f"Requested by {interaction.user.display_name}")
            
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Error in code command: {e}")
            embed = discord.Embed(
                title="❌ Error",
                description=f"An error occurred while generating code: {str(e)}",
                color=0xe74c3c  # error color
            )
            await interaction.followup.send(embed=embed)

async def setup(bot):
    await bot.add_cog(CodeCog(bot))