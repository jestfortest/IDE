import discord
from discord import app_commands
from discord.ext import commands
import logging
from typing import Optional
import asyncio
import base64

logger = logging.getLogger(__name__)

class VisionCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    
    @app_commands.command(name="vision", description="Analyze images using the vision model pool")
    @app_commands.describe(
        question="Your question about the image",
        image="Upload an image to analyze",
        model="Specific model to use (optional, uses pool if not specified)"
    )
    async def vision(
        self, 
        interaction: discord.Interaction, 
        question: str,
        image: discord.Attachment,
        model: Optional[str] = None
    ):
        await interaction.response.defer()
        
        try:
            # Check if the attachment is an image
            if not image.content_type or not image.content_type.startswith('image/'):
                embed = discord.Embed(
                    title="❌ Invalid File",
                    description="Please upload a valid image file (PNG, JPG, JPEG, GIF, etc.)",
                    color=0xe74c3c  # error color
                )
                await interaction.followup.send(embed=embed)
                return
            
            # Use the first default model, which supports image input.
            if model is None:
                model = self.bot.openrouter_service.DEFAULT_MODEL_POOL[0]
            
            # Download and encode the image
            image_data = await image.read()
            base64_image = base64.b64encode(image_data).decode('utf-8')
            
            # Prepare messages with image
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{image.content_type};base64,{base64_image}"
                            }
                        }
                    ]
                }
            ]
            
            # Get response from OpenRouter
            response = await self.bot.openrouter_service.chat_completion(
                messages=messages,
                model=model,
                max_tokens=1000,
                temperature=0.4
            )
            
            # Extract the response
            ai_message = response["choices"][0]["message"]["content"]
            
            # Create embed
            embed = discord.Embed(
                title="👁️ Image Analysis Result",
                description=ai_message,
                color=0x3498db  # info color
            )
            embed.add_field(name="Model Used", value=model, inline=True)
            embed.add_field(name="Image", value=f"[{image.filename}]({image.url})", inline=True)
            embed.set_image(url=image.url)
            embed.set_footer(text=f"Requested by {interaction.user.display_name}")
            
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Error in vision command: {e}")
            embed = discord.Embed(
                title="❌ Error",
                description=f"An error occurred while analyzing the image: {str(e)}",
                color=0xe74c3c  # error color
            )
            await interaction.followup.send(embed=embed)

async def setup(bot):
    await bot.add_cog(VisionCog(bot))