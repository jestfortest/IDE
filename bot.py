import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiohttp
import asyncio
import logging
import io
from typing import Optional, List, Dict, Any, Tuple
import os
import json
import re
import wave
from pathlib import Path
from urllib.parse import urlsplit
from datetime import date, datetime, time, timedelta, timezone
from config import (
    DISCORD_TOKEN, 
    OPENROUTER_API_KEY,
    EMBED_COLORS,
    DEFAULT_TEXT_POOL,
    DEFAULT_CODE_POOL,
    DEFAULT_REASONING_POOL,
    DEFAULT_CREATIVE_POOL,
    DEFAULT_IMAGE_GEN_POOL,
    DEFAULT_AUDIO_GEN_POOL,
    DEFAULT_MODEL_POOL,
    MODEL_POOLS,
)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Dedicated audit log for API key/provider actions (keys never written)
audit_logger = logging.getLogger("api_audit")
_audit_handler_path = Path(__file__).resolve().parent / "api_audit.log"
if not audit_logger.handlers:
    _audit_handler = logging.FileHandler(_audit_handler_path, encoding="utf-8")
    _audit_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    audit_logger.addHandler(_audit_handler)
    audit_logger.setLevel(logging.INFO)
    audit_logger.propagate = False

class OpenRouterService:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key if api_key and api_key.strip() else None
        self.base_url = "https://openrouter.ai/api/v1"
        self.session: Optional[aiohttp.ClientSession] = None
        self.DEFAULT_TEXT_POOL = DEFAULT_TEXT_POOL
        self.DEFAULT_CODE_POOL = DEFAULT_CODE_POOL
        self.DEFAULT_REASONING_POOL = DEFAULT_REASONING_POOL
        self.DEFAULT_CREATIVE_POOL = DEFAULT_CREATIVE_POOL
        self.DEFAULT_IMAGE_GEN_POOL = DEFAULT_IMAGE_GEN_POOL
        self.DEFAULT_AUDIO_GEN_POOL = DEFAULT_AUDIO_GEN_POOL
        self.DEFAULT_MODEL_POOL = DEFAULT_MODEL_POOL
        self.MODEL_POOLS = MODEL_POOLS
        self.providers_file = Path(__file__).resolve().parent / "api_providers.json"
        self.api_providers = self._load_api_providers()
        self.pool_overrides_file = Path(__file__).resolve().parent / "model_pool_overrides.json"
        self._apply_pool_overrides()
        self._warned_model_expirations = set()
        self._model_catalog_cache: Dict[str, Dict[str, Any]] = {}
        self._model_catalog_checked_at = 0.0
        self._model_catalog_error: Optional[str] = None

    def _apply_pool_overrides(self) -> None:
        """Load per-pool model overrides saved by /setup ai pool-edit."""
        try:
            if self.pool_overrides_file.exists():
                with self.pool_overrides_file.open("r", encoding="utf-8") as f:
                    overrides = json.load(f)
                pool_attr_map = {
                    "default": "DEFAULT_MODEL_POOL",
                    "text": "DEFAULT_TEXT_POOL",
                    "code": "DEFAULT_CODE_POOL",
                    "reason": "DEFAULT_REASONING_POOL",
                    "reasoning": "DEFAULT_REASONING_POOL",
                    "creative": "DEFAULT_CREATIVE_POOL",
                    "image": "DEFAULT_IMAGE_GEN_POOL",
                    "audio": "DEFAULT_AUDIO_GEN_POOL",
                }
                for pool_name, models in overrides.items():
                    if not isinstance(models, list) or not models:
                        continue
                    if pool_name in self.MODEL_POOLS:
                        self.MODEL_POOLS[pool_name] = list(models)
                    attr = pool_attr_map.get(pool_name)
                    if attr and hasattr(self, attr):
                        setattr(self, attr, list(models))
                logger.info("Applied model pool overrides from %s", self.pool_overrides_file.name)
        except (OSError, json.JSONDecodeError) as error:
            logger.warning("Could not load model pool overrides: %s", error)

    def save_pool_override(self, pool_name: str, models: List[str], actor_id: Optional[int] = None, actor_name: Optional[str] = None) -> None:
        """Persist a pool's model list so it survives restarts."""
        # Update in-memory
        pool_attr_map = {
            "default": "DEFAULT_MODEL_POOL",
            "text": "DEFAULT_TEXT_POOL",
            "code": "DEFAULT_CODE_POOL",
            "reason": "DEFAULT_REASONING_POOL",
            "reasoning": "DEFAULT_REASONING_POOL",
            "creative": "DEFAULT_CREATIVE_POOL",
            "image": "DEFAULT_IMAGE_GEN_POOL",
            "audio": "DEFAULT_AUDIO_GEN_POOL",
        }
        if pool_name in self.MODEL_POOLS:
            self.MODEL_POOLS[pool_name] = list(models)
        attr = pool_attr_map.get(pool_name)
        if attr and hasattr(self, attr):
            setattr(self, attr, list(models))

        # Persist
        overrides = {}
        try:
            if self.pool_overrides_file.exists():
                with self.pool_overrides_file.open("r", encoding="utf-8") as f:
                    overrides = json.load(f)
        except (OSError, json.JSONDecodeError):
            overrides = {}
        overrides[pool_name] = list(models)
        try:
            with self.pool_overrides_file.open("w", encoding="utf-8") as f:
                json.dump(overrides, f, indent=2)
        except OSError as error:
            logger.warning("Could not save model pool overrides: %s", error)

        actor_part = f" by {actor_name} ({actor_id})" if actor_id else ""
        audit_logger.info("MODEL POOL UPDATED: pool=%s models=%d%s", pool_name, len(models), actor_part)

    def _load_api_providers(self) -> Dict[str, Dict[str, str]]:
        try:
            with self.providers_file.open("r", encoding="utf-8") as provider_file:
                providers = json.load(provider_file)
            if isinstance(providers, dict):
                return {
                    name: provider
                    for name, provider in providers.items()
                    if isinstance(provider, dict)
                    and isinstance(provider.get("api_key"), str)
                    and isinstance(provider.get("base_url"), str)
                }
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as error:
            logger.error("Could not load API-compatible providers: %s", error)
        return {}

    def add_api_provider(self, alias: str, base_url: str, api_key: str, actor_id: Optional[int] = None, actor_name: Optional[str] = None) -> None:
        alias = alias.strip().lower()
        base_url = base_url.strip().rstrip("/")
        api_key = api_key.strip()
        reserved_names = set(self.MODEL_POOLS)
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", alias):
            raise ValueError("Use a unique alias of 1-32 lowercase letters, numbers, hyphens, or underscores.")
        if alias in reserved_names:
            raise ValueError(f"'{alias}' is a reserved name (it's a pool name). Pick a different alias.")
        if alias in self.api_providers:
            raise ValueError(f"Alias '{alias}' is already in use. Pick a different one or remove it first.")
        parsed_url = urlsplit(base_url)
        if (
            parsed_url.scheme != "https"
            or not parsed_url.hostname
            or parsed_url.username
            or parsed_url.password
            or parsed_url.query
            or parsed_url.fragment
            or parsed_url.path.rstrip("/").endswith("/chat/completions")
        ):
            raise ValueError("Enter an HTTPS OpenAI-compatible API base URL, not a chat-completions URL.")
        if not api_key:
            raise ValueError("API key cannot be blank.")

        self.api_providers[alias] = {"base_url": base_url, "api_key": api_key}
        self._save_api_providers()

        # Audit log: keys are never written to log
        actor_part = f" by {actor_name} ({actor_id})" if actor_id else ""
        audit_logger.info("API PROVIDER ADDED: alias=%s base_url=%s%s", alias, base_url, actor_part)

    def remove_api_provider(self, alias: str, actor_id: Optional[int] = None, actor_name: Optional[str] = None) -> bool:
        if self.api_providers.pop(alias.strip().lower(), None) is None:
            return False
        self._save_api_providers()
        actor_part = f" by {actor_name} ({actor_id})" if actor_id else ""
        audit_logger.info("API PROVIDER REMOVED: alias=%s%s", alias, actor_part)
        return True

    def _save_api_providers(self) -> None:
        serialized = json.dumps(self.api_providers, indent=2)
        file_descriptor = os.open(
            self.providers_file,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as provider_file:
            provider_file.write(serialized)
        os.chmod(self.providers_file, 0o600)

    def _get_request_provider(self, model: str) -> Tuple[str, str, str]:
        if "::" not in model:
            if not self.api_key:
                raise ValueError(
                    "No API key configured. Add one via /setup providers add "
                    "or set OPENROUTER_API_KEY in .env."
                )
            return self.api_key, self.base_url, model
        alias, provider_model = model.split("::", 1)
        provider = self.api_providers.get(alias)
        if provider is None or not provider_model.strip():
            raise ValueError("Use a configured provider alias and model as alias::model-id.")
        return provider["api_key"], provider["base_url"], provider_model.strip()
    
    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session
    
    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    @staticmethod
    def _parse_model_expiration(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            if len(value) == 10:
                return datetime.combine(date.fromisoformat(value), time(hour=13), tzinfo=timezone.utc)
            expiration = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if expiration.tzinfo is None:
                expiration = expiration.replace(tzinfo=timezone.utc)
            return expiration.astimezone(timezone.utc)
        except (TypeError, ValueError):
            logger.warning("Ignoring invalid model expiration date: %r", value)
            return None

    async def get_model_catalog(self, *, force_refresh: bool = False) -> Dict[str, Dict[str, Any]]:
        """Return OpenRouter model metadata, cached for one hour."""
        now = asyncio.get_running_loop().time()
        if not force_refresh and now - self._model_catalog_checked_at < 3600:
            if self._model_catalog_cache:
                return self._model_catalog_cache
            if self._model_catalog_error:
                raise RuntimeError(self._model_catalog_error)

        session = await self._get_session()
        self._model_catalog_checked_at = now
        try:
            async with session.get(
                f"{self.base_url}/models",
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                if response.status != 200:
                    raise RuntimeError(f"OpenRouter model catalog request failed with HTTP {response.status}.")
                payload = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError, RuntimeError) as error:
            self._model_catalog_error = str(error)
            raise

        models = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            raise RuntimeError("OpenRouter returned an invalid model catalog.")

        self._model_catalog_cache = {
            item["id"]: item
            for item in models
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        self._model_catalog_error = None
        return self._model_catalog_cache

    async def get_model_context_length(self, model: str) -> Optional[int]:
        """Return the published context window for an OpenRouter model ID."""
        if "::" in model:
            return None
        metadata = (await self.get_model_catalog()).get(model)
        context_length = metadata.get("context_length") if metadata else None
        return context_length if isinstance(context_length, int) and context_length > 0 else None

    async def refresh_model_pool_deprecations(self, now: Optional[datetime] = None) -> None:
        """Warn for upcoming OpenRouter model expirations and remove expired models."""
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)

        try:
            session = await self._get_session()
            async with session.get(
                f"{self.base_url}/models",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                if response.status != 200:
                    logger.warning("Model deprecation check failed with HTTP %s; keeping current pools", response.status)
                    return
                catalog = await response.json()
            if not isinstance(catalog, dict) or not isinstance(catalog.get("data"), list):
                logger.warning("Model deprecation check returned an unexpected response; keeping current pools")
                return
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as error:
            logger.warning("Model deprecation check failed; keeping current pools: %s", error)
            return

        expiration_dates = {
            item["id"]: expiration
            for item in catalog["data"]
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and (expiration := self._parse_model_expiration(item.get("expiration_date"))) is not None
        }
        warning_deadline = now + timedelta(days=30)
        checked_pools = set()
        for pool_name, pool in self.MODEL_POOLS.items():
            if id(pool) in checked_pools:
                continue
            checked_pools.add(id(pool))
            active_models = []
            for model_id in pool:
                expiration = expiration_dates.get(model_id)
                if expiration is not None and expiration <= now:
                    logger.warning("Removed expired model %s from pool %s (expired %s)", model_id, pool_name, expiration.isoformat())
                    continue
                if expiration is not None and expiration <= warning_deadline:
                    warning_key = (model_id, expiration)
                    if warning_key not in self._warned_model_expirations:
                        logger.warning("Model %s in pool %s is scheduled to expire on %s", model_id, pool_name, expiration.isoformat())
                        self._warned_model_expirations.add(warning_key)
                active_models.append(model_id)
            pool[:] = active_models
    
    async def chat_completion(
        self, 
        messages: List[Dict[str, str]], 
        model: str,
        max_tokens: int = 1000,
        temperature: float = 0.7
    ) -> Dict[str, Any]:
        """Send a chat completion request to OpenRouter or a compatible provider."""
        api_key, base_url, provider_model = self._get_request_provider(model)
        session = await self._get_session()
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": provider_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature
        }
        
        async with session.post(
            f"{base_url.rstrip('/')}/chat/completions",
            json=payload,
            headers=headers
        ) as response:
            if response.status == 200:
                result = await response.json()
                if not isinstance(result, dict):
                    raise RuntimeError("OpenRouter returned an invalid chat response.")

                api_error = result.get("error")
                if api_error:
                    if isinstance(api_error, dict):
                        message = api_error.get("message", "Unknown provider error")
                        code = api_error.get("code")
                        detail = f"{message} (code {code})" if code else str(message)
                    else:
                        detail = str(api_error)
                    raise RuntimeError(f"OpenRouter chat error: {detail[:500]}")

                choices = result.get("choices")
                if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                    logger.error("OpenRouter returned a chat response without choices: %s", result)
                    raise RuntimeError("The model returned no chat choices. Check the model ID and provider availability.")

                message = choices[0].get("message")
                if not isinstance(message, dict) or not isinstance(message.get("content"), str):
                    logger.error("OpenRouter returned a chat choice without text content: %s", choices[0])
                    raise RuntimeError("The model returned no text content for this message.")
                return result

            error_text = await response.text()
            logger.error("OpenRouter API error: %s - %s", response.status, error_text)
            raise RuntimeError(f"OpenRouter API error: {response.status} - {error_text[:500]}")

    @staticmethod
    def _is_retryable_chat_error(error: Exception) -> bool:
        if isinstance(error, asyncio.TimeoutError):
            return True
        message = str(error).lower()
        return any(
            marker in message
            for marker in (
                "resourceexhausted",
                "worker local total request limit",
                "rate limit",
                "rate_limit",
                "code 429",
                "code 502",
                "code 503",
                "api error: 429",
                "api error: 502",
                "api error: 503",
            )
        )

    async def chat_completion_with_fallback(
        self,
        messages: List[Dict[str, str]],
        model: str,
        max_tokens: int = 1000,
        temperature: float = 0.7,
    ) -> Dict[str, Any]:
        """Try the selected model, then its configured pool on transient failures."""
        pool = next((models for models in self.MODEL_POOLS.values() if model in models), None)
        candidates = [model]
        if pool is not None:
            candidates.extend(candidate for candidate in dict.fromkeys(pool) if candidate != model)

        last_error = None
        for index, candidate in enumerate(candidates):
            try:
                result = await asyncio.wait_for(
                    self.chat_completion(
                        messages=messages,
                        model=candidate,
                        max_tokens=max_tokens,
                        temperature=temperature,
                    ),
                    timeout=90,
                )
                result["_used_model"] = candidate
                return result
            except Exception as error:
                last_error = error
                if index + 1 >= len(candidates) or not self._is_retryable_chat_error(error):
                    raise
                logger.warning(
                    "Chat model %s failed transiently; trying pool fallback %s: %s",
                    candidate,
                    candidates[index + 1],
                    error,
                )

        raise last_error or RuntimeError("No chat model could complete this request.")
    
    async def image_generation(self, prompt: str, model: str) -> Dict[str, Any]:
        """Generate an image using OpenRouter or custom provider"""
        session = await self._get_session()
        api_key, base_url, target_model = self._get_request_provider(model)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

        payload = {"model": target_model, "prompt": prompt}

        async with session.post(
            f"{base_url}/images/generations" if "::" in model else f"{base_url}/images",
            json=payload,
            headers=headers
        ) as response:
            if response.status == 200:
                return await response.json()
            else:
                error_text = await response.text()
                logger.error(f"Image generation error: {response.status} - {error_text}")
                raise Exception(f"Image generation error: {response.status} - {error_text[:200]}")
    
    async def audio_generation(self, text: str, model: str, voice: Optional[str] = None) -> Dict[str, Any]:
        """Generate audio using OpenRouter or custom provider"""
        session = await self._get_session()
        api_key, base_url, target_model = self._get_request_provider(model)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": target_model,
            "input": text,
            "voice": voice or "alloy"
        }

        async with session.post(
            f"{base_url}/audio/speech",
            json=payload,
            headers=headers
        ) as response:
            if response.status == 200:
                audio_bytes = await response.read()
                content_type = response.headers.get("Content-Type", "application/octet-stream")
                mime_type = content_type.split(";", 1)[0].strip().lower()

                if mime_type == "audio/pcm":
                    parameters = {}
                    for parameter in content_type.split(";")[1:]:
                        key, separator, value = parameter.strip().partition("=")
                        if separator:
                            parameters[key.strip().lower()] = value.strip()
                    sample_rate = int(parameters.get("rate", 44100))
                    channels = int(parameters.get("channels", 1))
                    sample_width = int(parameters.get("bits", 16)) // 8
                    wav_buffer = io.BytesIO()
                    with wave.open(wav_buffer, "wb") as wav_file:
                        wav_file.setnchannels(channels)
                        wav_file.setsampwidth(sample_width)
                        wav_file.setframerate(sample_rate)
                        wav_file.writeframes(audio_bytes)
                    audio_bytes = wav_buffer.getvalue()
                    mime_type = "audio/wav"

                return {"audio_bytes": audio_bytes, "content_type": mime_type}
            else:
                error_text = await response.text()
                logger.error(f"Audio generation error: {response.status} - {error_text}")
                raise Exception(f"Audio generation error: {response.status} - {error_text[:200]}")

    def select_model_from_pool(self, pool: List[str]) -> str:
        """Select a model from the pool (simple random selection for now)"""
        import random
        return random.choice(pool) if pool else []

class InfinityLoungeBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self.openrouter_service: Optional[OpenRouterService] = None

    @tasks.loop(hours=24)
    async def refresh_model_deprecations(self):
        if self.openrouter_service:
            await self.openrouter_service.refresh_model_pool_deprecations()
            # Also refresh model pools from OpenRouter API to account for new/removed models
            try:
                import subprocess
                result = subprocess.run(
                    ["python3", "/Users/aidenchang/Documents/important/projects/Discord Bots/Infinity_lounge_bot/update_model_pools.py"],
                    capture_output=True,
                    text=True,
                    timeout=120
                )
                if result.returncode == 0:
                    import importlib
                    import config
                    importlib.reload(config)
                    self.openrouter_service.MODEL_POOLS = config.MODEL_POOLS
                    self.openrouter_service.DEFAULT_MODEL_POOL = config.DEFAULT_MODEL_POOL
                    logger.info("Model catalog refreshed successfully from OpenRouter API")
                else:
                    logger.warning("Model catalog refresh failed: %s", result.stderr[:500])
            except Exception as error:
                logger.warning("Failed to refresh model catalog: %s", error)

    @refresh_model_deprecations.before_loop
    async def wait_for_ready_before_model_check(self):
        await self.wait_until_ready()
    
    async def setup_hook(self):
        """Initialize services when the bot starts"""
        self.openrouter_service = OpenRouterService(OPENROUTER_API_KEY)
        logger.info("OpenRouter service initialized")
        self.refresh_model_deprecations.start()

        # Set up global tree error handler
        @self.tree.error
        async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
            if isinstance(error, app_commands.MissingPermissions):
                missing = [perm.replace("_", " ").title() for perm in error.missing_permissions]
                if "Administrator" in missing:
                    msg = "❌ You don't have **Administrator** permissions to use this command."
                else:
                    msg = f"❌ You don't have the required permissions to use this command: **{', '.join(missing)}**"
            elif isinstance(error, app_commands.BotMissingPermissions):
                missing = [perm.replace("_", " ").title() for perm in error.missing_permissions]
                msg = f"❌ I (the bot) am missing the required permissions: **{', '.join(missing)}**. Please check my role permissions."
            elif isinstance(error, app_commands.CheckFailure):
                msg = str(error) if str(error) else "❌ You don't have permission to use this command."
            elif isinstance(error, app_commands.CommandOnCooldown):
                msg = f"⏳ This command is on cooldown. Try again in {error.retry_after:.1f} seconds."
            else:
                logger.error("Unhandled slash command error: %s", error, exc_info=error)
                msg = f"❌ An unexpected error occurred: {error}"

            try:
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
            except Exception as send_err:
                logger.error("Failed to send error message to interaction: %s", send_err)

        # Load all cogs
        await self.load_extension("cogs.chat")
        await self.load_extension("cogs.code")
        await self.load_extension("cogs.reason")
        await self.load_extension("cogs.creative")
        await self.load_extension("cogs.image")
        await self.load_extension("cogs.audio")
        await self.load_extension("cogs.moderator")
        await self.load_extension("cogs.setup")
        await self.load_extension("cogs.ai")
        logger.info("All cogs loaded")
    
    async def on_ready(self):
        logger.info(f"{self.user} has connected to Discord!")
        try:
            synced = await self.tree.sync()
            logger.info(f"Synced {len(synced)} command(s)")
        except Exception as e:
            logger.error(f"Failed to sync commands: {e}")
    
    async def close(self):
        if self.refresh_model_deprecations.is_running():
            self.refresh_model_deprecations.cancel()
        if self.openrouter_service:
            await self.openrouter_service.close()
        await super().close()

# Create bot instance
bot = InfinityLoungeBot()

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)