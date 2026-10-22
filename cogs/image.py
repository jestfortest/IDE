import discord
from discord import app_commands
from discord.ext import commands
import logging
from typing import Optional
import base64
import io

logger = logging.getLogger(__name__)

class ImageCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    
    @app_commands.command(name="image", description="Generate images using the image generation model pool")
    @app_commands.describe(
        prompt="Your image generation prompt",
        model="Specific model to use (optional, uses pool if not specified)"
    )
    async def image(
        self, 
        interaction: discord.Interaction, 
        prompt: str,
        model: Optional[str] = None
    ):
        await interaction.response.defer()
        
        try:
            # Check if image pool has models
            image_pool = self.bot.openrouter_service.DEFAULT_IMAGE_GEN_POOL
            if not image_pool and model is None:
                embed = discord.Embed(
                    title="❌ No Image Models Available",
                    description="No image models are available, please contact staff or try again later.",
                    color=0xe74c3c
                )
                await interaction.followup.send(embed=embed)
                return
            
            # Use provided model or select from image generation pool
            if model is None:
                model = self.bot.openrouter_service.select_model_from_pool(image_pool)
            
            # Generate image using OpenRouter
            response = await self.bot.openrouter_service.image_generation(
                prompt=prompt,
                model=model
            )
            
            image_item = next(
                (item for item in response.get("data", []) if isinstance(item, dict)),
                None,
            )
            if image_item is None:
                raise ValueError("OpenRouter returned no generated image.")

            embed = discord.Embed(
                title="🎨 Generated Image",
                description=f"*Prompt: {prompt}*",
                color=0xe91e63  # pink/purple color for creativity
            )
            embed.add_field(name="Model Used", value=model, inline=True)
            embed.set_footer(text=f"Requested by {interaction.user.display_name}")
            
            image_url = image_item.get("url")
            if image_url:
                embed.set_image(url=image_url)
                await interaction.followup.send(embed=embed)
                return

            encoded_image = image_item.get("b64_json")
            if not isinstance(encoded_image, str):
                raise ValueError("OpenRouter's image response did not include image data.")
            image_bytes = base64.b64decode(encoded_image, validate=True)
            media_type = image_item.get("media_type", "image/png").split(";", 1)[0].lower()
            extension = {
                "image/png": "png",
                "image/jpeg": "jpg",
                "image/webp": "webp",
                "image/gif": "gif",
                "image/svg+xml": "svg",
            }.get(media_type, "png")
            filename = f"generated_image.{extension}"
            if media_type != "image/svg+xml":
                embed.set_image(url=f"attachment://{filename}")
            await interaction.followup.send(
                embed=embed,
                file=discord.File(io.BytesIO(image_bytes), filename=filename),
            )
            
        except Exception as e:
            logger.error(f"Error in image command: {e}")
            embed = discord.Embed(
                title="❌ Error",
                description=f"An error occurred while generating the image: {str(e)}",
                color=0xe74c3c  # error color
            )
            await interaction.followup.send(embed=embed)

async def setup(bot):
    await bot.add_cog(ImageCog(bot))