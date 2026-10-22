import discord
from discord import app_commands
from discord.ext import commands, tasks
import logging
from typing import Optional
import asyncio
import datetime
import io
from collections import deque
from datetime import timedelta
from types import SimpleNamespace
import uuid
from config import DB_PATH, TASK_CLASSIFIER_MODEL
from services.ai_sessions import AIChatSessionStore

logger = logging.getLogger(__name__)


class ChatSessionPanel(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Delete session",
        style=discord.ButtonStyle.danger,
        custom_id="ai_chat:archive_and_delete_session",
    )
    async def delete_session(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.archive_and_clear_channel_session(interaction)


class AICog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # In-memory storage for conversation history (in production, use a database or Redis)
        self.conversations = {}  # {user_id: [{"role": "...", "content": "...", "timestamp": "..."}]}
        self.channel_conversations = {}
        self.channel_locks = {}
        self.channel_context_stats = {}
        self.channel_models = {}  # {channel_id: model_name} - per-channel model override
        self.session_store = AIChatSessionStore(DB_PATH)
        self.session_retention_days = 30
        self.channel_request_times = {}
        self.channel_rate_limit_notices = {}
        self.context_safety_margin_tokens = 256
        self.channel_requests_per_hour = 30
        self.channel_request_interval_seconds = 2
        self.model_check_cooldowns = {}
        self.model_check_lock = asyncio.Lock()
        self.max_history_length = 10  # Maximum number of messages to keep in history
        self.regular_session_locks = {}  # Locks for regular user+channel sessions

    async def cog_load(self):
        self.bot.add_view(ChatSessionPanel(self))
        await self.session_store.initialize()
        self.prune_inactive_sessions.start()

    def cog_unload(self):
        self.prune_inactive_sessions.cancel()

    async def _restore_active_sessions(self) -> None:
        for session in await self.session_store.get_all_active():
            guild = self.bot.get_guild(session["guild_id"])
            channel = guild.get_channel(session["channel_id"]) if guild else None
            if channel is None:
                continue
            self.channel_conversations[session["channel_id"]] = session["messages"]
            self.channel_context_stats[session["channel_id"]] = {
                "model": session.get("model"),
                "context_length": session.get("context_length"),
                "prompt_tokens": session.get("prompt_tokens"),
                "tokens_per_character": 0.5,
                "updated_at": session.get("last_activity"),
            }
            if session.get("model"):
                self.channel_models[session["channel_id"]] = session["model"]

    async def _restore_or_initialize_channel(self, guild: discord.Guild, channel: discord.TextChannel, ai_chat: dict) -> None:
        if channel.id in self.channel_conversations:
            return
        session = await self.session_store.get_by_channel(guild.id, channel.id)
        if session is not None:
            self.channel_conversations[channel.id] = session["messages"]
            self.channel_context_stats[channel.id] = {
                "model": session.get("model"),
                "context_length": session.get("context_length"),
                "prompt_tokens": session.get("prompt_tokens"),
                "tokens_per_character": 0.5,
                "updated_at": session.get("last_activity"),
            }
            if session.get("model"):
                self.channel_models[channel.id] = session["model"]
            return

        owner_id = self._get_channel_owner_id(ai_chat, channel.id, 0)
        await self.session_store.save_active_session(
            session_id=uuid.uuid4().hex,
            guild_id=guild.id,
            owner_id=owner_id,
            channel_id=channel.id,
            channel_name=channel.name,
            created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            last_activity=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            model=None,
            context_length=None,
            prompt_tokens=None,
            messages=[],
            members=ai_chat.get("channel_members", {}).get(str(channel.id), []),
        )
        self.channel_conversations[channel.id] = []

    async def register_channel_session(
        self,
        guild: discord.Guild,
        owner: discord.Member,
        channel: discord.TextChannel,
        additional_members: list[discord.Member],
    ) -> None:
        created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        ai_chat = self.bot.get_cog("SetupCog")._get_guild_config(guild.id).get("ai_chat", {})
        configured_members = ai_chat.setdefault("channel_members", {})
        configured_members[str(channel.id)] = [member.id for member in additional_members]
        self.bot.get_cog("SetupCog")._save_config()
        self.channel_conversations[channel.id] = []
        await self.session_store.save_active_session(
            session_id=uuid.uuid4().hex,
            guild_id=guild.id,
            owner_id=owner.id,
            channel_id=channel.id,
            channel_name=channel.name,
            created_at=created_at,
            last_activity=created_at,
            model=None,
            context_length=None,
            prompt_tokens=None,
            messages=[],
            members=configured_members[str(channel.id)],
        )

    def _get_channel_owner_id(self, ai_chat: dict, channel_id: int, fallback_user_id: int) -> int:
        for owner_id, channel_ids in ai_chat.get("channels", {}).items():
            if not isinstance(channel_ids, list):
                channel_ids = [channel_ids]
            try:
                if channel_id in {int(value) for value in channel_ids}:
                    return int(owner_id)
            except (TypeError, ValueError):
                continue
        return fallback_user_id

    async def _persist_channel_session(
        self,
        guild_id: int,
        channel: discord.TextChannel,
        owner_id: int,
        *,
        model: Optional[str],
        context_length: Optional[int],
        prompt_tokens: Optional[int],
    ) -> None:
        existing = await self.session_store.get_by_channel(guild_id, channel.id)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        guild_config = self.bot.get_cog("SetupCog")._get_guild_config(guild_id)
        ai_chat = guild_config.get("ai_chat", {})
        member_ids = ai_chat.get("channel_members", {}).get(str(channel.id), [])
        await self.session_store.save_active_session(
            session_id=existing["session_id"] if existing else uuid.uuid4().hex,
            guild_id=guild_id,
            owner_id=owner_id,
            channel_id=channel.id,
            channel_name=channel.name,
            created_at=existing.get("created_at", now) if existing else now,
            last_activity=now,
            model=model,
            context_length=context_length,
            prompt_tokens=prompt_tokens,
            messages=self.channel_conversations.get(channel.id, []),
            members=member_ids,
        )

    @tasks.loop(hours=1)
    async def prune_inactive_sessions(self):
        cutoff = (datetime.datetime.now(datetime.timezone.utc) - timedelta(days=self.session_retention_days)).isoformat()
        expired_sessions = await self.session_store.get_expired_active(cutoff)
        for session in expired_sessions:
            guild = self.bot.get_guild(session["guild_id"])
            if guild is None:
                continue
            channel = guild.get_channel(session["channel_id"])
            setup_cog = self.bot.get_cog("SetupCog")
            guild_config = setup_cog._get_guild_config(guild.id) if setup_cog else {}
            transcript_config = guild_config.get("transcripts", {})
            transcript_channel = guild.get_channel(transcript_config.get("channel_id"))
            if transcript_config.get("enabled") and isinstance(transcript_channel, discord.TextChannel):
                transcript_lines = [
                    f"Archived inactive AI session {session['session_id']}",
                    f"Owner ID: {session['owner_id']}",
                    f"Last activity: {session['last_activity']}",
                    "",
                ]
                for item in session["messages"]:
                    role = "AI" if item["role"] == "assistant" else "User"
                    transcript_lines.extend((f"{role}:", item["content"], ""))
                archive_file = discord.File(
                    io.BytesIO("\n".join(transcript_lines).encode("utf-8")),
                    filename=f"ai-session-{session['session_id']}-inactive.txt",
                )
                try:
                    await transcript_channel.send(
                        content=f"AI chat session `{session['session_id']}` was archived after {self.session_retention_days} days of inactivity.",
                        file=archive_file,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException as error:
                    logger.warning("Could not post inactive session transcript %s: %s", session["session_id"], error)

            if channel is not None:
                try:
                    await channel.delete(reason=f"AI session inactive for {self.session_retention_days} days")
                except discord.HTTPException as error:
                    logger.warning("Could not prune inactive AI channel %s: %s", channel.id, error)
                    continue

            await self.session_store.archive(session["session_id"])
            ai_chat = guild_config.setdefault("ai_chat", {})
            channels = ai_chat.setdefault("channels", {})
            owner_key = str(session["owner_id"])
            owner_channels = channels.get(owner_key, [])
            if not isinstance(owner_channels, list):
                owner_channels = [owner_channels] if owner_channels else []
            channels[owner_key] = [channel_id for channel_id in owner_channels if int(channel_id) != session["channel_id"]]
            ai_chat.setdefault("channel_members", {}).pop(str(session["channel_id"]), None)
            if setup_cog:
                setup_cog._save_config()
            self.channel_conversations.pop(session["channel_id"], None)
            self.channel_context_stats.pop(session["channel_id"], None)

    @prune_inactive_sessions.before_loop
    async def before_prune_inactive_sessions(self):
        await self.bot.wait_until_ready()
        await self._restore_active_sessions()

    def _trim_channel_context(
        self,
        history: list,
        *,
        context_length: int,
        max_output_tokens: int,
        tokens_per_character: float,
        current_prompt: str,
        system_prompt: str,
    ) -> None:
        input_budget = max(1, context_length - max_output_tokens - self.context_safety_margin_tokens)

        def estimate_tokens() -> int:
            character_count = len(system_prompt) + len(current_prompt)
            character_count += sum(len(item["content"]) for item in history)
            message_count = len(history) + 2
            return int(character_count * tokens_per_character) + message_count * 8

        while history and estimate_tokens() > input_budget:
            del history[:2]

    def _allow_channel_request(self, channel_id: int, user_id: int) -> bool:
        now = asyncio.get_running_loop().time()
        timestamps = self.channel_request_times.setdefault(user_id, deque())
        while timestamps and now - timestamps[0] >= 3600:
            timestamps.popleft()
        if timestamps and now - timestamps[-1] < self.channel_request_interval_seconds:
            return False
        if len(timestamps) >= self.channel_requests_per_hour:
            return False
        timestamps.append(now)
        return True

    async def create_chat_panel(self, channel: discord.TextChannel) -> None:
        embed = discord.Embed(
            title="Private AI Chat",
            description=(
                "Send a message in this channel to continue the AI session. "
                "Use the button below to archive the transcript and clear the session."
            ),
            color=0x3498db,
        )
        panel = await channel.send(
            embed=embed,
            view=ChatSessionPanel(self),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        try:
            await panel.pin(reason="Keep the AI chat session controls at the top of the channel")
        except discord.Forbidden:
            logger.warning("Cannot pin AI chat panel in channel %s; it was still posted", channel.id)

    async def _get_ai_chat_command_config(self, interaction: discord.Interaction) -> Optional[dict]:
        if interaction.guild is None:
            await interaction.response.send_message("This command is only available in an AI chat channel.", ephemeral=True)
            return None
        setup_cog = self.bot.get_cog("SetupCog")
        guild_config = setup_cog._get_guild_config(interaction.guild.id) if setup_cog else {}
        ai_chat = guild_config.get("ai_chat", {})
        channel_owner_id = None
        for owner_id, channel_id in ai_chat.get("channels", {}).items():
            channel_ids = channel_id if isinstance(channel_id, list) else [channel_id]
            try:
                if interaction.channel_id in {int(value) for value in channel_ids}:
                    channel_owner_id = int(owner_id)
                    break
            except (TypeError, ValueError):
                continue
        if channel_owner_id is None and ai_chat.get("channel_id") == interaction.channel_id:
            channel_owner_id = ai_chat.get("owner_id")
        if not ai_chat.get("enabled") or channel_owner_id is None:
            await interaction.response.send_message("Use this command in your configured AI chat channel.", ephemeral=True)
            return None
        is_admin = isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator
        if not is_admin and interaction.user.id != channel_owner_id:
            await interaction.response.send_message("Only the channel owner or a server administrator can manage this AI session.", ephemeral=True)
            return None
        return ai_chat

    async def archive_and_clear_channel_session(self, interaction: discord.Interaction) -> None:
        ai_chat = await self._get_ai_chat_command_config(interaction)
        if ai_chat is None:
            return
        await self._restore_or_initialize_channel(interaction.guild, interaction.channel, ai_chat)
        setup_cog = self.bot.get_cog("SetupCog")
        guild_config = setup_cog._get_guild_config(interaction.guild.id)
        transcripts = guild_config.get("transcripts", {})
        transcript_channel = interaction.guild.get_channel(transcripts.get("channel_id"))
        if not transcripts.get("enabled") or not isinstance(transcript_channel, discord.TextChannel):
            await interaction.response.send_message(
                "Session transcripts are not configured. Enable them in `/setup all` before deleting a session.",
                ephemeral=True,
            )
            return
        bot_member = interaction.guild.me
        if bot_member is None:
            await interaction.response.send_message("I can't access this server right now.", ephemeral=True)
            return
        transcript_permissions = transcript_channel.permissions_for(bot_member)
        if not transcript_permissions.send_messages or not transcript_permissions.attach_files:
            await interaction.response.send_message(
                f"I need Send Messages and Attach Files permission in {transcript_channel.mention} to archive this session.",
                ephemeral=True,
            )
            return

        channel_id = interaction.channel_id
        lock = self.channel_locks.setdefault(channel_id, asyncio.Lock())
        await interaction.response.defer(ephemeral=True)
        async with lock:
            history = self.channel_conversations.get(channel_id, [])
            if not history:
                await interaction.followup.send("There is no active session to archive.", ephemeral=True)
                return
            transcript_lines = [
                f"Guild: {interaction.guild.name}",
                f"AI channel: {interaction.channel.name}",
                f"Archived by: {interaction.user}",
                "",
            ]
            for item in history:
                role = "AI" if item["role"] == "assistant" else "User"
                timestamp = item.get("timestamp", "")
                transcript_lines.extend((f"[{timestamp}] {role}:", item["content"], ""))
            filename = f"ai-session-{interaction.guild.id}-{channel_id}-{int(datetime.datetime.now().timestamp())}.txt"
            transcript_file = discord.File(
                io.BytesIO("\n".join(transcript_lines).encode("utf-8")),
                filename=filename,
            )
            try:
                await transcript_channel.send(
                    content=f"Archived AI chat session from {interaction.channel.mention}.",
                    file=transcript_file,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException as error:
                logger.error("Could not archive AI chat session from channel %s: %s", channel_id, error)
                await interaction.followup.send("The transcript could not be saved; the session was not deleted.", ephemeral=True)
                return
            stored_session = await self.session_store.get_by_channel(interaction.guild.id, channel_id)
            if stored_session:
                await self.session_store.archive(stored_session["session_id"])
            self.channel_conversations.pop(channel_id, None)
            self.channel_context_stats.pop(channel_id, None)
            await interaction.followup.send(
                f"Transcript saved to {transcript_channel.mention}; this AI channel's session was cleared.",
                ephemeral=True,
            )

    def _record_ai_chat_usage(self, guild_id: int, user_id: int, model: str, response: dict) -> None:
        setup_cog = self.bot.get_cog("SetupCog")
        if setup_cog is None:
            return
        guild_config = setup_cog._get_guild_config(guild_id)
        usage_by_user = guild_config.setdefault("ai_chat", {}).setdefault("usage", {})
        user_usage = usage_by_user.setdefault(
            str(user_id),
            {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_usd": 0.0},
        )
        usage = response.get("usage", {})
        if not isinstance(usage, dict):
            usage = {}
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        prompt_tokens = prompt_tokens if isinstance(prompt_tokens, int) and prompt_tokens > 0 else 0
        completion_tokens = completion_tokens if isinstance(completion_tokens, int) and completion_tokens > 0 else 0
        total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)
        total_tokens = total_tokens if isinstance(total_tokens, int) and total_tokens > 0 else prompt_tokens + completion_tokens
        user_usage["requests"] += 1
        user_usage["prompt_tokens"] += prompt_tokens
        user_usage["completion_tokens"] += completion_tokens
        user_usage["total_tokens"] += total_tokens
        cost = usage.get("cost")
        if isinstance(cost, (int, float)) and cost > 0:
            user_usage["cost_usd"] += float(cost)
        user_usage["last_model"] = model
        user_usage["last_used"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        setup_cog._save_config()

    @app_commands.command(name="ai-chat-create", description="Create a new private numbered AI chat session")
    @app_commands.guild_only()
    async def ai_chat_create(self, interaction: discord.Interaction):
        setup_cog = self.bot.get_cog("SetupCog")
        if setup_cog is None or interaction.guild is None:
            await interaction.response.send_message("AI chat setup is unavailable right now.", ephemeral=True)
            return
        ai_chat = setup_cog._get_guild_config(interaction.guild.id).get("ai_chat", {})
        if not ai_chat.get("enabled"):
            await interaction.response.send_message(
                "An administrator must enable AI chat with `/setup all` first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            channel, created = await setup_cog.create_private_ai_channel(
                interaction.guild,
                interaction.user,
            )
        except (ValueError, discord.Forbidden) as error:
            await interaction.followup.send(f"Could not create your AI channel: {error}", ephemeral=True)
            return
        except discord.HTTPException as error:
            logger.error("Could not create a private AI channel in guild %s: %s", interaction.guild.id, error)
            await interaction.followup.send("Discord couldn't create your AI channel. Please try again later.", ephemeral=True)
            return

        response = f"Created your private AI channel: {channel.mention}." if created else f"Your AI channel is {channel.mention}."
        await interaction.followup.send(response, ephemeral=True)

    @app_commands.command(name="ai-chat-usage", description="View AI-chat usage for yourself or a member")
    @app_commands.describe(member="Only administrators can view another member's usage")
    @app_commands.guild_only()
    async def ai_chat_usage(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        if interaction.guild is None:
            await interaction.response.send_message("This command is only available in a server.", ephemeral=True)
            return
        is_admin = isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator
        if member is not None and member.id != interaction.user.id and not is_admin:
            await interaction.response.send_message("Only administrators can view another member's usage.", ephemeral=True)
            return
        target = member or interaction.user
        setup_cog = self.bot.get_cog("SetupCog")
        guild_config = setup_cog._get_guild_config(interaction.guild.id) if setup_cog else {}
        usage = guild_config.get("ai_chat", {}).get("usage", {}).get(str(target.id), {})
        embed = discord.Embed(title=f"AI Chat Usage: {target.display_name}", color=0x3498db)
        embed.add_field(name="Completed requests", value=f"{usage.get('requests', 0):,}", inline=True)
        embed.add_field(name="Prompt tokens", value=f"{usage.get('prompt_tokens', 0):,}", inline=True)
        embed.add_field(name="Completion tokens", value=f"{usage.get('completion_tokens', 0):,}", inline=True)
        embed.add_field(name="Total tokens", value=f"{usage.get('total_tokens', 0):,}", inline=True)
        if usage.get("cost_usd", 0):
            embed.add_field(name="Reported cost", value=f"${usage['cost_usd']:.6f} USD", inline=True)
        if usage.get("last_model"):
            embed.add_field(name="Last model", value=f"`{usage['last_model']}`", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="ai-chat-search", description="Search your archived AI chat sessions")
    @app_commands.describe(
        query="Words or phrase to find in archived transcripts",
        member="Administrators may search another member's sessions",
    )
    @app_commands.guild_only()
    async def ai_chat_search(
        self,
        interaction: discord.Interaction,
        query: str,
        member: Optional[discord.Member] = None,
    ):
        if interaction.guild is None:
            await interaction.response.send_message("This command is only available in a server.", ephemeral=True)
            return
        is_admin = isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator
        if member is not None and member.id != interaction.user.id and not is_admin:
            await interaction.response.send_message("Only administrators can search another member's sessions.", ephemeral=True)
            return
        owner_id = member.id if member else (None if is_admin else interaction.user.id)
        results = await self.session_store.search_archived(
            interaction.guild.id,
            query.strip(),
            owner_id=owner_id,
            limit=10,
        )
        if not results:
            await interaction.response.send_message("No archived sessions matched that search.", ephemeral=True)
            return
        embed = discord.Embed(title="Archived AI Sessions", color=0x3498db)
        for session in results:
            preview = " ".join(session.get("preview", "").split()) or "No text preview available"
            if len(preview) > 700:
                preview = preview[:697] + "..."
            embed.add_field(
                name=f"{session['channel_name']} · `{session['session_id']}`",
                value=f"Owner: <@{session['owner_id']}> · Archived: {session.get('archived_at') or 'unknown'}\n{preview}\nReopen with `/ai-chat-reopen session_id:{session['session_id']}`",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="ai-chat-reopen", description="Reopen one of your archived AI sessions")
    @app_commands.describe(session_id="Session ID shown by /ai-chat-search")
    @app_commands.guild_only()
    async def ai_chat_reopen(self, interaction: discord.Interaction, session_id: str):
        if interaction.guild is None:
            await interaction.response.send_message("This command is only available in a server.", ephemeral=True)
            return
        archived = await self.session_store.get_archived(interaction.guild.id, session_id.strip())
        if archived is None:
            await interaction.response.send_message("That archived session could not be found.", ephemeral=True)
            return
        is_admin = isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator
        if not is_admin and interaction.user.id != archived["owner_id"]:
            await interaction.response.send_message("You can only reopen your own archived sessions.", ephemeral=True)
            return

        setup_cog = self.bot.get_cog("SetupCog")
        ai_chat = setup_cog._get_guild_config(interaction.guild.id).get("ai_chat", {}) if setup_cog else {}
        if setup_cog is None or not ai_chat.get("enabled"):
            await interaction.response.send_message("An administrator must enable AI chat with `/setup all` first.", ephemeral=True)
            return
        owner = interaction.guild.get_member(archived["owner_id"])
        if owner is None:
            try:
                owner = await interaction.guild.fetch_member(archived["owner_id"])
            except discord.HTTPException:
                await interaction.response.send_message("The original session owner is no longer in this server.", ephemeral=True)
                return
        additional_members = []
        for member_id in archived.get("members", []):
            member = interaction.guild.get_member(member_id)
            if member is None:
                try:
                    member = await interaction.guild.fetch_member(member_id)
                except discord.HTTPException:
                    continue
            additional_members.append(member)

        await interaction.response.defer(ephemeral=True)
        try:
            channel, _ = await setup_cog.create_private_ai_channel(
                interaction.guild,
                owner,
                base_name=archived.get("channel_name", "ai-chat").rsplit("-", 1)[0],
                additional_members=additional_members,
            )
        except (ValueError, discord.Forbidden, discord.HTTPException) as error:
            await interaction.followup.send(f"The session couldn't be reopened: {error}", ephemeral=True)
            return

        self.channel_conversations[channel.id] = archived["messages"]
        self.channel_context_stats[channel.id] = {
            "model": archived.get("model"),
            "context_length": archived.get("context_length"),
            "prompt_tokens": archived.get("prompt_tokens"),
            "tokens_per_character": 0.5,
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        await self._persist_channel_session(
            interaction.guild.id,
            channel,
            owner.id,
            model=archived.get("model"),
            context_length=archived.get("context_length"),
            prompt_tokens=archived.get("prompt_tokens"),
        )
        await interaction.followup.send(f"Reopened the archived session in {channel.mention}.", ephemeral=True)

    @app_commands.command(name="ai-chat-add-member", description="Invite a member to this private AI channel")
    @app_commands.describe(member="The member to invite")
    @app_commands.guild_only()
    async def ai_chat_add_member(self, interaction: discord.Interaction, member: discord.Member):
        if await self._get_ai_chat_command_config(interaction) is None:
            return
        if member.bot:
            await interaction.response.send_message("Bot accounts can't be added as AI chat members.", ephemeral=True)
            return
        overwrite = interaction.channel.overwrites_for(member)
        overwrite.view_channel = True
        overwrite.send_messages = True
        overwrite.read_message_history = True
        try:
            await interaction.channel.set_permissions(
                member,
                overwrite=overwrite,
                reason=f"AI chat access granted by {interaction.user}",
            )
        except discord.Forbidden:
            await interaction.response.send_message("I need Manage Channels permission to add members.", ephemeral=True)
            return
        setup_cog = self.bot.get_cog("SetupCog")
        guild_config = setup_cog._get_guild_config(interaction.guild.id) if setup_cog else {}
        ai_chat = guild_config.setdefault("ai_chat", {})
        channel_members = ai_chat.setdefault("channel_members", {}).setdefault(str(interaction.channel_id), [])
        if member.id not in channel_members:
            channel_members.append(member.id)
        active_session = await self.session_store.get_by_channel(interaction.guild.id, interaction.channel_id)
        if active_session:
            await self.session_store.update_members(active_session["session_id"], channel_members)
        if setup_cog:
            setup_cog._save_config()
        await interaction.response.send_message(
            f"{member.mention} can now view and use this AI chat. Their messages will share this channel's context.",
            ephemeral=True,
        )

    @app_commands.command(name="ai-chat-remove-member", description="Remove a member from this private AI channel")
    @app_commands.describe(member="The member to remove")
    @app_commands.guild_only()
    async def ai_chat_remove_member(self, interaction: discord.Interaction, member: discord.Member):
        ai_chat = await self._get_ai_chat_command_config(interaction)
        if ai_chat is None:
            return
        if member.guild_permissions.administrator:
            await interaction.response.send_message("Administrators retain access to AI channels.", ephemeral=True)
            return
        owner_id = self._get_channel_owner_id(ai_chat, interaction.channel_id, 0)
        if member.id == owner_id:
            await interaction.response.send_message("The channel owner can't be removed from their own AI chat.", ephemeral=True)
            return
        try:
            await interaction.channel.set_permissions(
                member,
                overwrite=None,
                reason=f"AI chat access removed by {interaction.user}",
            )
        except discord.Forbidden:
            await interaction.response.send_message("I need Manage Channels permission to remove members.", ephemeral=True)
            return
        setup_cog = self.bot.get_cog("SetupCog")
        guild_config = setup_cog._get_guild_config(interaction.guild.id) if setup_cog else {}
        ai_chat_config = guild_config.setdefault("ai_chat", {})
        channel_members = ai_chat_config.setdefault("channel_members", {}).setdefault(str(interaction.channel_id), [])
        ai_chat_config["channel_members"][str(interaction.channel_id)] = [user_id for user_id in channel_members if user_id != member.id]
        active_session = await self.session_store.get_by_channel(interaction.guild.id, interaction.channel_id)
        if active_session:
            await self.session_store.update_members(active_session["session_id"], ai_chat_config["channel_members"][str(interaction.channel_id)])
        if setup_cog:
            setup_cog._save_config()
        await interaction.response.send_message(f"Removed {member.mention} from this AI chat.", ephemeral=True)
    
    def _get_conversation(self, user_id: int) -> list:
        """Get conversation history for a user"""
        return self.conversations.get(user_id, [])
    
    def _add_to_conversation(self, user_id: int, role: str, content: str):
        """Add a message to conversation history"""
        if user_id not in self.conversations:
            self.conversations[user_id] = []
        
        message = {
            "role": role,
            "content": content,
            "timestamp": datetime.datetime.utcnow().isoformat()
        }
        self.conversations[user_id].append(message)
        
        # Keep only the last N messages
        if len(self.conversations[user_id]) > self.max_history_length:
            self.conversations[user_id] = self.conversations[user_id][-self.max_history_length:]
    
    def _clear_conversation(self, user_id: int):
        """Clear conversation history for a user"""
        if user_id in self.conversations:
            del self.conversations[user_id]

    async def _get_regular_session(
        self,
        guild_id: int,
        owner_id: int,
        channel_id: int,
        channel_name: str,
    ) -> list:
        """Get or create a regular AI session for a user+channel."""
        lock = self.regular_session_locks.setdefault((guild_id, owner_id, channel_id), asyncio.Lock())
        async with lock:
            session = await self.session_store.get_by_user_channel(
                guild_id, owner_id, channel_id, session_type="regular"
            )
            if session is not None:
                return session["messages"]
            # Create new regular session
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            await self.session_store.save_active_session(
                session_id=uuid.uuid4().hex,
                guild_id=guild_id,
                owner_id=owner_id,
                channel_id=channel_id,
                channel_name=channel_name,
                created_at=now,
                last_activity=now,
                model=None,
                context_length=None,
                prompt_tokens=None,
                messages=[],
                members=[],
                session_type="regular",
            )
            return []

    async def _save_regular_session(
        self,
        guild_id: int,
        owner_id: int,
        channel_id: int,
        channel_name: str,
        messages: list,
        model: Optional[str] = None,
        context_length: Optional[int] = None,
        prompt_tokens: Optional[int] = None,
    ) -> None:
        """Save a regular AI session."""
        lock = self.regular_session_locks.setdefault((guild_id, owner_id, channel_id), asyncio.Lock())
        async with lock:
            existing = await self.session_store.get_by_user_channel(
                guild_id, owner_id, channel_id, session_type="regular"
            )
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            await self.session_store.save_active_session(
                session_id=existing["session_id"] if existing else uuid.uuid4().hex,
                guild_id=guild_id,
                owner_id=owner_id,
                channel_id=channel_id,
                channel_name=channel_name,
                created_at=existing.get("created_at", now) if existing else now,
                last_activity=now,
                model=model,
                context_length=context_length,
                prompt_tokens=prompt_tokens,
                messages=messages,
                members=[],
                session_type="regular",
            )

    async def _clear_regular_session(self, guild_id: int, owner_id: int, channel_id: int) -> None:
        """Clear a regular AI session."""
        lock = self.regular_session_locks.setdefault((guild_id, owner_id, channel_id), asyncio.Lock())
        async with lock:
            session = await self.session_store.get_by_user_channel(
                guild_id, owner_id, channel_id, session_type="regular"
            )
            if session:
                await self.session_store.archive(session["session_id"])
    
    def _get_guild_ai_settings(self, guild_id: int) -> dict:
        """Get AI settings for a guild (would normally come from setup cog)"""
        # For now, return default settings
        # In a full implementation, this would read from the setup cog's stored config
        return {
            "default_model": None,  # Will use pool selection
            "temperature": 0.7
        }

    async def _classify_ask_task(self, question: str) -> str:
        allowed_tasks = {"default", "code", "reason", "creative"}
        try:
            response = await self.bot.openrouter_service.chat_completion(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Classify the user's request into exactly one category: default, code, reason, or creative. "
                            "Use code for programming tasks, reason for analysis or problem solving, creative for writing or brainstorming, "
                            "and default for ordinary questions. Reply with only the category word."
                        ),
                    },
                    {"role": "user", "content": question[:4000]},
                ],
                model=TASK_CLASSIFIER_MODEL,
                max_tokens=8,
                temperature=0.0,
            )
            classification = response["choices"][0]["message"]["content"].strip().lower()
            classification = classification.strip("`'\" .\n")
            return classification if classification in allowed_tasks else "default"
        except Exception as error:
            logger.warning("Task classification failed; using the default model pool: %s", error)
            return "default"
    
    @app_commands.command(name="ask", description="Ask the AI a general question")
    @app_commands.describe(
        question="Your question for the AI",
        model="Specific model to use (optional, uses pool if not specified)"
    )
    async def ask(
        self,
        interaction: discord.Interaction,
        question: str,
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
            ai_settings = guild_config.get("ai_settings", self._get_guild_ai_settings(0))

            task_type = None
            if model is None:
                task_type = await self._classify_ask_task(question)
                pool = self.bot.openrouter_service.MODEL_POOLS.get(
                    task_type,
                    self.bot.openrouter_service.DEFAULT_MODEL_POOL,
                )
                model = self.bot.openrouter_service.select_model_from_pool(
                    pool
                )

            if private_chat_enabled and setup_cog:
                ai_chat = guild_config.get("ai_chat", {})
                owner_channels = ai_chat.get("channels", {}).get(str(interaction.user.id), [])
                if not isinstance(owner_channels, list):
                    owner_channels = [owner_channels] if owner_channels else []
                current_is_owned_channel = interaction.channel_id in {
                    int(channel_id) for channel_id in owner_channels
                    if str(channel_id).isdigit()
                }
                if current_is_owned_channel and isinstance(interaction.channel, discord.TextChannel):
                    target_channel = interaction.channel
                else:
                    target_channel, _ = await setup_cog.create_private_ai_channel(
                        interaction.guild,
                        interaction.user,
                    )

                question_embed = discord.Embed(
                    title=f"Question from {interaction.user.display_name}",
                    description=question[:4000],
                    color=0x3498db,
                )
                await target_channel.send(
                    embed=question_embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                await self.on_message(SimpleNamespace(
                    guild=interaction.guild,
                    author=interaction.user,
                    channel=target_channel,
                    content=question,
                    attachments=[],
                    model_override=model,
                ))
                await interaction.followup.send(
                    f"Your question was sent to {target_channel.mention}.",
                    ephemeral=True,
                )
                return

            # Use session store for regular sessions (per user+channel)
            if interaction.guild and isinstance(interaction.channel, discord.TextChannel):
                channel_id = interaction.channel.id
                channel_name = interaction.channel.name
                history = await self._get_regular_session(
                    interaction.guild.id,
                    interaction.user.id,
                    channel_id,
                    channel_name,
                )
            else:
                # Fallback to in-memory for DMs
                history = self._get_conversation(interaction.user.id)

            # Prepare messages
            messages = [{"role": "user", "content": question}]

            # Get response from OpenRouter
            response = await self.bot.openrouter_service.chat_completion_with_fallback(
                messages=messages,
                model=model,
                max_tokens=1500,
                temperature=ai_settings.get("temperature", 0.7)
            )
            model = response.get("_used_model", model)

            # Extract the response
            ai_message = response["choices"][0]["message"]["content"]

            # Add to conversation history
            user_id = interaction.user.id
            if interaction.guild and isinstance(interaction.channel, discord.TextChannel):
                history.append({"role": "user", "content": question, "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()})
                history.append({"role": "assistant", "content": ai_message, "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()})
                # Trim to max history length
                if len(history) > self.max_history_length * 2:
                    history = history[-self.max_history_length * 2:]
                await self._save_regular_session(
                    interaction.guild.id,
                    interaction.user.id,
                    interaction.channel.id,
                    interaction.channel.name,
                    history,
                    model=model,
                    context_length=None,
                    prompt_tokens=response.get("usage", {}).get("prompt_tokens"),
                )
            else:
                self._add_to_conversation(user_id, "user", question)
                self._add_to_conversation(user_id, "assistant", ai_message)

            # Create embed
            embed = discord.Embed(
                title="🤖 AI Response",
                description=ai_message,
                color=0x3498db  # info color
            )
            embed.add_field(name="Model Used", value=model, inline=True)
            if task_type is not None:
                embed.add_field(name="Task Type", value=task_type.title(), inline=True)
            embed.set_footer(text=f"Asked by {interaction.user.display_name}")

            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"Error in ask command: {e}")
            embed = discord.Embed(
                title="❌ Error",
                description=f"An error occurred while processing your request: {str(e)}",
                color=0xe74c3c  # error color
            )
            await interaction.followup.send(embed=embed, ephemeral=private_chat_enabled)
    
    @app_commands.command(name="converse", description="Start or continue a conversation with the AI (with history) in a private channel")
    @app_commands.describe(
        message="Your message to continue the conversation",
        model="Specific model to use (optional, uses pool if not specified)",
        reset="Reset the conversation history before sending this message"
    )
    async def converse(
        self,
        interaction: discord.Interaction,
        message: str,
        model: Optional[str] = None,
        reset: bool = False
    ):
        await interaction.response.defer(ephemeral=True)

        try:
            if interaction.guild is None:
                await interaction.followup.send("This command must be used in a server.", ephemeral=True)
                return

            setup_cog = self.bot.get_cog("SetupCog")
            if setup_cog is None:
                await interaction.followup.send("Setup cog unavailable.", ephemeral=True)
                return

            guild_config = setup_cog._get_guild_config(interaction.guild.id)
            ai_chat = guild_config.get("ai_chat", {})
            
            # Enable AI chat if not already enabled
            if not ai_chat.get("enabled"):
                ai_chat["enabled"] = True
                setup_cog._save_config()

            # Get or create private AI channel for this user
            channel, _ = await setup_cog.create_private_ai_channel(
                interaction.guild,
                interaction.user,
            )

            # Reset conversation if requested
            if reset:
                stored_session = await self.session_store.get_by_channel(interaction.guild.id, channel.id)
                if stored_session:
                    await self.session_store.archive(stored_session["session_id"])
                self.channel_conversations.pop(channel.id, None)
                self.channel_context_stats.pop(channel.id, None)
                await interaction.followup.send("Conversation history has been reset.", ephemeral=True)

            # Get conversation history from the channel's session
            await self._restore_or_initialize_channel(interaction.guild, channel, ai_chat)
            conversation_history = self.channel_conversations.get(channel.id, [])

            # Use provided model or select from text pool
            if model is None:
                model = self.bot.openrouter_service.select_model_from_pool(
                    self.bot.openrouter_service.DEFAULT_TEXT_POOL
                )

            # Prepare messages with conversation history
            messages = []
            for msg in conversation_history:
                messages.append({"role": msg["role"], "content": msg["content"]})
            messages.append({"role": "user", "content": message})

            # Get response from OpenRouter
            ai_settings = guild_config.get("ai_settings", self._get_guild_ai_settings(interaction.guild.id))
            response = await self.bot.openrouter_service.chat_completion_with_fallback(
                messages=messages,
                model=model,
                max_tokens=1500,
                temperature=ai_settings.get("temperature", 0.7)
            )
            model = response.get("_used_model", model)

            # Extract the response
            ai_message = response["choices"][0]["message"]["content"]

            # Add to conversation history
            timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self.channel_conversations.setdefault(channel.id, []).extend([
                {"role": "user", "content": message, "timestamp": timestamp},
                {"role": "assistant", "content": ai_message, "timestamp": timestamp},
            ])
            await self._persist_channel_session(
                interaction.guild.id,
                channel,
                interaction.user.id,
                model=model,
                context_length=None,
                prompt_tokens=response.get("usage", {}).get("prompt_tokens"),
            )

            embed = discord.Embed(
                title="💬 AI Conversation",
                description=ai_message,
                color=0x9b59b6,
            )
            embed.add_field(name="Model Used", value=model, inline=True)
            embed.add_field(
                name="Conversation Length",
                value=f"{len(self.channel_conversations.get(channel.id, [])) // 2} exchanges",
                inline=True,
            )
            embed.set_footer(text=f"Chatting in {channel.mention}")
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as error:
            logger.error("Error in converse command: %s", error)
            embed = discord.Embed(
                title="❌ Error",
                description=f"An error occurred while processing your request: {error}",
                color=0xe74c3c,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="ai-sessions", description="List your private AI chat sessions")
    @app_commands.guild_only()
    async def ai_sessions(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        setup_cog = self.bot.get_cog("SetupCog")
        if setup_cog is None or interaction.guild is None:
            await interaction.followup.send("Setup unavailable.", ephemeral=True)
            return

        guild_config = setup_cog._get_guild_config(interaction.guild.id)
        ai_chat = guild_config.get("ai_chat", {})
        owner_channels = ai_chat.get("channels", {}).get(str(interaction.user.id), [])
        if not isinstance(owner_channels, list):
            owner_channels = [owner_channels] if owner_channels else []

        if not owner_channels:
            await interaction.followup.send("You have no AI chat sessions. Use `/converse` or `/ask` to create one.", ephemeral=True)
            return

        embed = discord.Embed(title="Your AI Chat Sessions", color=0x3498db)
        for channel_id in owner_channels:
            channel = interaction.guild.get_channel(channel_id)
            if channel is None:
                continue
            session = await self.session_store.get_by_channel(interaction.guild.id, channel_id)
            exchanges = 0
            last_activity = "Unknown"
            status = "Active"
            if session:
                exchanges = len(session.get("messages", [])) // 2
                last_activity = session.get("last_activity", "Unknown")[:19].replace("T", " ")
                status = session.get("status", "Active").title()
            embed.add_field(
                name=f"#{channel.name} ({status})",
                value=f"Exchanges: {exchanges} | Last: {last_activity}\nUse in {channel.mention}",
                inline=False,
            )

        embed.set_footer(text=f"Total: {len(owner_channels)} session(s) | Max: 10")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="models-refresh", description="Refresh model pools from OpenRouter (admin only)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def models_refresh(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Run the model pool updater script
            import subprocess
            result = subprocess.run(
                ["python3", "/Users/aidenchang/Documents/important/projects/Discord Bots/Infinity_lounge_bot/update_model_pools.py"],
                capture_output=True,
                text=True,
                timeout=120
            )
            
            if result.returncode == 0:
                # Reload the config
                import importlib
                import config
                importlib.reload(config)
                
                # Update the bot's model pools
                self.bot.openrouter_service.MODEL_POOLS = config.MODEL_POOLS
                
                embed = discord.Embed(
                    title="✅ Model Pools Refreshed",
                    description="Successfully fetched and updated free model pools from OpenRouter.",
                    color=0x2ecc71
                )
                embed.add_field(
                    name="Output",
                    value=f"```\n{result.stdout[-1000:]}\n```",
                    inline=False
                )
            else:
                embed = discord.Embed(
                    title="❌ Refresh Failed",
                    description=f"Error updating model pools:\n```\n{result.stderr[-1000:]}\n```",
                    color=0xe74c3c
                )
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except subprocess.TimeoutExpired:
            embed = discord.Embed(
                title="❌ Timeout",
                description="Model refresh timed out after 120 seconds.",
                color=0xe74c3c
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as error:
            logger.error("Error in models_refresh command: %s", error)
            embed = discord.Embed(
                title="❌ Error",
                description=f"An error occurred while refreshing models: {error}",
                color=0xe74c3c
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="models", description="List available AI models by pool and provider")
    @app_commands.describe(
        provider="Filter by provider (optional, e.g. 'nvidia', 'openai'). Leave empty for all.",
        pool="Filter by pool (optional, e.g. 'image', 'text', 'code'). Leave empty for all."
    )
    async def models(self, interaction: discord.Interaction, provider: Optional[str] = None, pool: Optional[str] = None):
        await interaction.response.defer(ephemeral=True)

        service = self.bot.openrouter_service
        pools = service.MODEL_POOLS
        providers = service.api_providers

        # If provider specified, fetch models from that provider
        provider_models = []
        if provider:
            provider = provider.strip().lower()
            if provider not in providers:
                await interaction.followup.send(
                    f"❌ Unknown provider `{provider}`. Use `/setup providers list` to see configured providers.",
                    ephemeral=True
                )
                return
            try:
                import aiohttp
                prov = providers[provider]
                headers = {"Authorization": f"Bearer {prov['api_key']}"}
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"{prov['base_url']}/models", headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            provider_models = [m.get("id", "unknown") for m in data.get("data", [])]
                        else:
                            await interaction.followup.send(
                                f"❌ Failed to fetch models from `{provider}`: HTTP {resp.status}",
                                ephemeral=True
                            )
                            return
            except Exception as e:
                await interaction.followup.send(
                    f"❌ Error fetching models from `{provider}`: {str(e)[:300]}",
                    ephemeral=True
                )
                return

        embed = discord.Embed(
            title="🤖 Available AI Models",
            description=f"Showing models for pool: `{pool or 'all'}` and provider: `{provider or 'all'}`",
            color=0x3498db,
        )

        if provider_models:
            embed.add_field(
                name=f"📦 Provider `{provider}` models ({len(provider_models)})",
                value="\n".join(f"`{m}`" for m in provider_models[:20]) + (f"\n...and {len(provider_models)-20} more" if len(provider_models) > 20 else ""),
                inline=False
            )

        if pool:
            pool = pool.lower()
            if pool in pools:
                embed.add_field(
                    name=f"{pool.title()} pool ({len(pools[pool])})",
                    value="\n".join(f"`{m}`" for m in pools[pool][:15]) + (f"\n...and {len(pools[pool])-15} more" if len(pools[pool]) > 15 else ""),
                    inline=False
                )
            else:
                embed.add_field(
                    name="❌ Unknown pool",
                    value=f"Available pools: {', '.join(pools.keys())}",
                    inline=False
                )
        else:
            for name, models in pools.items():
                if not models:
                    continue
                title = "Default (general text)" if name == "default" else name.title()
                embed.add_field(
                    name=f"{title} ({len(models)})",
                    value="\n".join(f"`{m}`" for m in models[:10]) + (f"\n...and {len(models)-10} more" if len(models) > 10 else ""),
                    inline=False,
                )

        if providers and not provider:
            embed.add_field(
                name="🔌 Configured API providers",
                value="\n".join(f"`{p}` → `{providers[p]['base_url']}`" for p in sorted(providers)[:10]),
                inline=False
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="set-model", description="Set the model used in this AI chat channel (only affects this channel)")
    @app_commands.describe(
        model="The model to use (exact model ID from /models, e.g. 'google/gemma-4-26b-a4b-it:free'). Leave empty to reset to auto pool.",
    )
    @app_commands.guild_only()
    async def set_model(self, interaction: discord.Interaction, model: Optional[str] = None):
        setup_cog = self.bot.get_cog("SetupCog")
        guild_config = setup_cog._get_guild_config(interaction.guild.id) if setup_cog else {}
        ai_chat = guild_config.get("ai_chat", {})

        # This channel must be a registered AI chat channel
        is_ai_channel = False
        if ai_chat.get("channel_id") == interaction.channel.id:
            is_ai_channel = True
        else:
            for registered_ids in ai_chat.get("channels", {}).values():
                if not isinstance(registered_ids, list):
                    registered_ids = [registered_ids]
                if interaction.channel.id in {int(cid) for cid in registered_ids if str(cid).isdigit()}:
                    is_ai_channel = True
                    break
        if not is_ai_channel:
            embed = discord.Embed(
                title="❌ Not an AI chat channel",
                description="This channel is not an AI chat channel. Use `/setup ai` to register one first, or use `/ask model:` and `/converse model:` for one-off model overrides.",
                color=0xe74c3c,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if model is None or not model.strip():
            # Reset to auto pool
            self.channel_models.pop(interaction.channel.id, None)
            embed = discord.Embed(
                title="🔄 Channel model reset",
                description=f"This channel ({interaction.channel.mention}) will use the **auto model pool** again (random model from the pool per request).",
                color=0x3498db,
            )
            await interaction.response.send_message(embed=embed)
            return

        model = model.strip()

        # Validate the model exists in the catalog (warn but allow if lookup fails)
        service = self.bot.openrouter_service
        try:
            catalog = await service.get_model_catalog(force_refresh=False)
            if model not in catalog:
                # Try case-insensitive / provider-less match
                matches = [m for m in catalog if m.lower() == model.lower()]
                if not matches:
                    embed = discord.Embed(
                        title="⚠️ Unknown model",
                        description=f"`{model}` was not found in the OpenRouter catalog. It has been set anyway — requests will fail if the model doesn't exist.\n\nUse `/models` to see available models.",
                        color=0xf39c12,
                    )
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                    return
                model = matches[0]
        except Exception as error:
            logger.warning("Could not verify model %s against catalog: %s", model, error)

        self.channel_models[interaction.channel.id] = model

        # Persist so it survives restarts (update the channel's stored session model)
        try:
            if interaction.channel.id in self.channel_context_stats:
                self.channel_context_stats[interaction.channel.id]["model"] = model
            # Persist via session store if the session exists
            await self._persist_channel_session(
                interaction.guild.id,
                interaction.channel,
                self._get_channel_owner_id(ai_chat, interaction.channel.id, interaction.user.id),
                model=model,
                context_length=self.channel_context_stats.get(interaction.channel.id, {}).get("context_length"),
                prompt_tokens=self.channel_context_stats.get(interaction.channel.id, {}).get("prompt_tokens"),
            )
        except Exception as error:
            logger.warning("Could not persist channel model for %s: %s", interaction.channel.id, error)

        embed = discord.Embed(
            title="✅ Channel model set",
            description=f"This channel ({interaction.channel.mention}) will now use:\n`{model}`\n\nThis only affects **this channel** — other AI chats keep their own model. Use `/set-model` with no model to reset to the auto pool.",
            color=0x2ecc71,
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="channel-model", description="Show which model this AI chat channel is using")
    @app_commands.guild_only()
    async def channel_model(self, interaction: discord.Interaction):
        channel_id = interaction.channel.id
        if channel_id in self.channel_models:
            model = self.channel_models[channel_id]
            embed = discord.Embed(
                title="🤖 This channel's model",
                description=f"`{model}`\n\nChange it with `/set-model`, or reset to the auto pool with `/set-model` (no model).",
                color=0x3498db,
            )
        else:
            pool = self.bot.openrouter_service.DEFAULT_MODEL_POOL
            embed = discord.Embed(
                title="🤖 This channel's model",
                description=f"Using the **auto pool** (random pick per request):\n" + "\n".join(f"`{m}`" for m in pool),
                color=0x3498db,
            )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="models-check", description="Test up to five text models with one short request each")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def models_check(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        now = asyncio.get_running_loop().time()
        cooldown = self.model_check_cooldowns.get(interaction.user.id)
        if cooldown is not None and now - cooldown < 60:
            remaining = max(1, int(60 - (now - cooldown)))
            await interaction.followup.send(
                f"Model checks are limited to once per minute. Try again in {remaining} seconds.",
                ephemeral=True,
            )
            return
        if self.model_check_lock.locked():
            await interaction.followup.send("A model check is already running. Try again shortly.", ephemeral=True)
            return

        service = self.bot.openrouter_service
        models = list(dict.fromkeys(service.DEFAULT_TEXT_POOL))[:5]
        if not models:
            await interaction.followup.send("No text models are configured to test.", ephemeral=True)
            return

        results = []
        async with self.model_check_lock:
            self.model_check_cooldowns[interaction.user.id] = now
            for model in models:
                try:
                    response = await asyncio.wait_for(
                        service.chat_completion(
                            messages=[{"role": "user", "content": "Reply with OK."}],
                            model=model,
                            max_tokens=1,
                            temperature=0.0,
                        ),
                        timeout=30,
                    )
                    content = response["choices"][0]["message"].get("content")
                    if not isinstance(content, str):
                        raise RuntimeError("No text response returned")
                    results.append(f"✅ `{model}`: responding")
                except asyncio.TimeoutError:
                    results.append(f"⏱️ `{model}`: timed out after 30 seconds")
                except Exception as error:
                    detail = " ".join(str(error).split())[:160]
                    results.append(f"❌ `{model}`: {detail}")

        embed = discord.Embed(
            title="Text Model Check",
            description="\n".join(results),
            color=0x3498db,
        )
        embed.set_footer(text="Maximum 5 one-token test requests; results can change as provider capacity changes.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="model", description="View or set your preferred AI model")
    @app_commands.describe(
        model="The AI model to set as your preference (leave empty to view current)",
        pool="Which model pool to choose from"
    )
    @app_commands.choices(pool=[
        app_commands.Choice(name="Default (general text)", value="default"),
        app_commands.Choice(name="Text", value="text"),
        app_commands.Choice(name="Code", value="code"),
        app_commands.Choice(name="Reasoning", value="reason"),
        app_commands.Choice(name="Creative", value="creative"),
        app_commands.Choice(name="Image", value="image"),
        app_commands.Choice(name="Audio", value="audio"),
    ])
    async def model(
        self, 
        interaction: discord.Interaction, 
        model: Optional[str] = None,
        pool: Optional[str] = None
    ):
        await interaction.response.defer()
        
        try:
            # If pool is specified, show models from that pool
            if pool is not None:
                pool_map = self.bot.openrouter_service.MODEL_POOLS
                
                if pool.lower() not in pool_map:
                    embed = discord.Embed(
                        title="❌ Error",
                        description=f"Invalid pool. Choose from: {', '.join(pool_map.keys())}",
                        color=0xe74c3c
                    )
                    await interaction.followup.send(embed=embed)
                    return
                
                models = pool_map[pool.lower()]
                
                embed = discord.Embed(
                    title=f"🤖 {pool.title()} Model Pool",
                    description="Available models:\n" + "\n".join([f"• {m}" for m in models]),
                    color=0x3498db
                )
                
                if model is not None:
                    if model in models:
                        embed.add_field(name="Your Selection", value=model, inline=False)
                        embed.set_footer(text=f"Selected by {interaction.user.display_name}")
                    else:
                        embed.add_field(name="⚠️ Invalid Selection", value=f"{model} is not in the {pool} pool", inline=False)
                
                await interaction.followup.send(embed=embed)
                return
            
            # If model is specified, set it as preference (in a full implementation, we'd save this)
            if model is not None:
                # Validate that model exists in any pool
                all_models = list(dict.fromkeys(
                    model_name
                    for models in self.bot.openrouter_service.MODEL_POOLS.values()
                    for model_name in models
                ))
                custom_provider_model = False
                if "::" in model:
                    provider_alias, provider_model = model.split("::", 1)
                    custom_provider_model = (
                        provider_alias in self.bot.openrouter_service.api_providers
                        and bool(provider_model.strip())
                    )

                if model not in all_models and not custom_provider_model:
                    embed = discord.Embed(
                        title="❌ Error",
                        description=f"Model '{model}' was not found in a built-in pool or configured provider.",
                        color=0xe74c3c
                    )
                    await interaction.followup.send(embed=embed)
                    return
                
                # In a full implementation, we'd save this preference to user/guild settings
                embed = discord.Embed(
                    title="🤖 AI Model Preference Set",
                    description=f"Your preferred AI model has been set to: `{model}`\n(Note: This preference is not persisted across restarts in this demo version)",
                    color=0x2ecc71
                )
                embed.set_footer(text=f"Set by {interaction.user.display_name}")
                await interaction.followup.send(embed=embed)
                return
            
            # If neither model nor pool specified, show current status and available pools
            embed = discord.Embed(
                title="🤖 AI Model Information",
                description="Current AI model selection uses pool-based selection unless overridden.",
                color=0x3498db
            )
            
            embed.add_field(
                name="Available Pools",
                value="• **default** - General-purpose models\n• **text** - General conversation\n• **code** - Programming assistance\n• **reason** - Logical reasoning\n• **creative** - Creative writing\n• **image** - Image generation\n• **audio** - Audio/TTS generation",
                inline=False
            )
            
            embed.add_field(
                name="Usage Examples",
                value="`/model pool:default` - View general-purpose models\n`/ask model:gemini::gemini-3.8-flash` - Use a configured compatible provider",
                inline=False
            )
            
            embed.set_footer(text=f"Requested by {interaction.user.display_name}")
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Error in model command: {e}")
            embed = discord.Embed(
                title="❌ Error",
                description=f"An error occurred while processing your request: {str(e)}",
                color=0xe74c3c  # error color
            )
            await interaction.followup.send(embed=embed)
    
    @app_commands.command(name="history", description="View your conversation history with the AI")
    @app_commands.describe(
        limit="Number of recent exchanges to show (default: 5)"
    )
    async def history(
        self, 
        interaction: discord.Interaction, 
        limit: Optional[int] = 5
    ):
        await interaction.response.defer()
        
        try:
            user_id = interaction.user.id
            conversation = self._get_conversation(user_id)
            
            if not conversation:
                embed = discord.Embed(
                    title="📜 Conversation History",
                    description="You have no conversation history with the AI.",
                    color=0x95a5a6  # gray
                )
            else:
                # Limit the history shown
                if limit is None or limit < 1:
                    limit = 5
                elif limit > 20:  # Cap at 20 exchanges to prevent too long messages
                    limit = 20
                
                # Get the last N exchanges (each exchange is 2 messages: user + assistant)
                max_messages = limit * 2
                recent_conversation = conversation[-max_messages:] if len(conversation) > max_messages else conversation
                
                if not recent_conversation:
                    embed = discord.Embed(
                        title="📜 Conversation History",
                        description="No conversation history to display.",
                        color=0x95a5a6
                    )
                else:
                    embed = discord.Embed(
                        title="📜 Conversation History",
                        description=f"Showing last {len(recent_conversation)//2} exchange(s):",
                        color=0x3498db
                    )
                    
                    # Format the conversation history
                    for i in range(0, len(recent_conversation), 2):
                        if i + 1 < len(recent_conversation):
                            user_msg = recent_conversation[i]
                            ai_msg = recent_conversation[i + 1]
                            
                            # Truncate long messages for display
                            user_content = user_msg['content'][:100] + ("..." if len(user_msg['content']) > 100 else "")
                            ai_content = ai_msg['content'][:100] + ("..." if len(ai_msg['content']) > 100 else "")
                            
                            timestamp = datetime.datetime.fromisoformat(user_msg['timestamp']).strftime("%H:%M")
                            
                            embed.add_field(
                                name=f"Exchange {len(recent_conversation)//2 - i//2} ({timestamp})",
                                value=f"**You:** {user_content}\n**AI:** {ai_content}",
                                inline=False
                            )
                    
                    if len(conversation) > len(recent_conversation):
                        embed.set_footer(text=f"Showing {len(recent_conversation)//2} of {len(conversation)//2} total exchanges")
                    else:
                        embed.set_footer(text=f"Showing all {len(conversation)//2} exchanges")
            
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Error in history command: {e}")
            embed = discord.Embed(
                title="❌ Error",
                description=f"An error occurred while retrieving conversation history: {str(e)}",
                color=0xe74c3c  # error color
            )
            await interaction.followup.send(embed=embed)
    
    @app_commands.command(name="clear-history", description="Clear your conversation history with the AI")
    async def clear_history(
        self, 
        interaction: discord.Interaction
    ):
        await interaction.response.defer()
        
        try:
            user_id = interaction.user.id
            if user_id in self.conversations:
                del self.conversations[user_id]
                embed = discord.Embed(
                    title="🗑️ Conversation Cleared",
                    description="Your conversation history with the AI has been cleared.",
                    color=0x2ecc71  # green
                )
            else:
                embed = discord.Embed(
                    title="🗑️ Conversation Cleared",
                    description="You had no conversation history to clear.",
                    color=0x95a5a6  # gray
                )
            
            embed.set_footer(text=f"Cleared by {interaction.user.display_name}")
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Error in clear command: {e}")
            embed = discord.Embed(
                title="❌ Error",
                description=f"An error occurred while clearing conversation history: {str(e)}",
                color=0xe74c3c  # error color
            )
            await interaction.followup.send(embed=embed)

    @app_commands.command(name="ai-chat-context", description="View the current private AI chat session size")
    @app_commands.guild_only()
    async def ai_chat_context(self, interaction: discord.Interaction):
        ai_chat = await self._get_ai_chat_command_config(interaction)
        if ai_chat is None:
            return
        channel_id = interaction.channel_id
        history = self.channel_conversations.get(channel_id, [])
        exchanges = len(history) // 2
        stats = self.channel_context_stats.get(channel_id, {})
        context_length = stats.get("context_length")
        prompt_tokens = stats.get("prompt_tokens")
        model = stats.get("model")
        if prompt_tokens is not None and model:
            if context_length:
                percentage = min(100, round(prompt_tokens / context_length * 100))
                description = (
                    f"Last request used **{prompt_tokens:,} / {context_length:,} tokens** ({percentage}%) "
                    f"on `{model}` across {exchanges} retained exchange(s). Older exchanges are removed "
                    "automatically to fit this model's context window."
                )
            else:
                description = (
                    f"Last request used **{prompt_tokens:,} tokens** on `{model}` across {exchanges} retained "
                    "exchange(s). This provider does not publish a context-window size, so history is managed "
                    "with a conservative token estimate."
                )
        else:
            setup_cog = self.bot.get_cog("SetupCog")
            guild_config = setup_cog._get_guild_config(interaction.guild.id) if setup_cog else {}
            service = self.bot.openrouter_service
            model = guild_config.get("ai_settings", {}).get("default_model")
            model = model or service.select_model_from_pool(service.DEFAULT_MODEL_POOL)
            try:
                context_length = await service.get_model_context_length(model)
            except Exception:
                context_length = None
            context_description = f"{context_length:,} tokens" if context_length else "not reported by the model catalog"
            description = f"No token usage data is available yet. `{model}` has a published context window of {context_description}."
        await interaction.response.send_message(description, ephemeral=True)

    @app_commands.command(name="ai-chat-reset", description="Clear the current private AI chat session")
    @app_commands.guild_only()
    async def ai_chat_reset(self, interaction: discord.Interaction):
        await self.archive_and_clear_channel_session(interaction)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return

        setup_cog = self.bot.get_cog("SetupCog")
        if setup_cog is None:
            return
        guild_config = setup_cog._get_guild_config(message.guild.id)
        ai_chat = guild_config.get("ai_chat", {})
        chat_channels = ai_chat.get("channels", {})
        registered_channel = False
        for registered_ids in chat_channels.values():
            if not isinstance(registered_ids, list):
                registered_ids = [registered_ids]
            try:
                if message.channel.id in {int(channel_id) for channel_id in registered_ids}:
                    registered_channel = True
                    break
            except (TypeError, ValueError):
                continue
        if ai_chat.get("channel_id") == message.channel.id:
            registered_channel = True
        if not ai_chat.get("enabled") or not registered_channel:
            return

        await self._restore_or_initialize_channel(message.guild, message.channel, ai_chat)

        content = message.content.strip()
        if message.attachments:
            attachment_text = "\n".join(
                f"Attachment: {attachment.filename} ({attachment.url})"
                for attachment in message.attachments
            )
            content = f"{content}\n{attachment_text}".strip()
        if not content:
            return

        service = self.bot.openrouter_service
        if service is None:
            await message.channel.send("The AI service is unavailable right now.")
            return

        channel_id = message.channel.id
        if not self._allow_channel_request(channel_id, message.author.id):
            now = asyncio.get_running_loop().time()
            key = (channel_id, message.author.id)
            last_notice = self.channel_rate_limit_notices.get(key, 0.0)
            if now - last_notice >= 30:
                self.channel_rate_limit_notices[key] = now
                await message.channel.send(
                    "Please slow down before sending another AI request.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            return

        model = (
            getattr(message, "model_override", None)
            or self.channel_models.get(channel_id)
            or guild_config.get("ai_settings", {}).get("default_model")
        )
        if not model:
            model = service.select_model_from_pool(service.DEFAULT_MODEL_POOL)
        temperature = guild_config.get("ai_settings", {}).get("temperature", 0.7)
        prompt = f"{message.author.display_name}: {content[:3500]}"
        lock = self.channel_locks.setdefault(channel_id, asyncio.Lock())

        try:
            async with lock:
                history = self.channel_conversations.setdefault(channel_id, [])
                context_length = None
                try:
                    context_length = await service.get_model_context_length(model)
                except Exception as error:
                    logger.warning("Could not retrieve context window for %s: %s", model, error)
                effective_context_length = context_length or 8192
                max_output_tokens = min(1500, max(128, effective_context_length // 4))
                previous_stats = self.channel_context_stats.get(channel_id, {})
                tokens_per_character = (
                    previous_stats.get("tokens_per_character", 0.5)
                    if previous_stats.get("model") == model
                    else 0.5
                )
                system_prompt = "You are the AI assistant in a private Discord chat channel. Be helpful, concise, and format responses for Discord."
                self._trim_channel_context(
                    history,
                    context_length=effective_context_length,
                    max_output_tokens=max_output_tokens,
                    tokens_per_character=tokens_per_character,
                    current_prompt=prompt,
                    system_prompt=system_prompt,
                )
                messages = [
                    {"role": "system", "content": system_prompt},
                    *[
                        {"role": item["role"], "content": item["content"]}
                        for item in history
                    ],
                    {"role": "user", "content": prompt},
                ]
                async with message.channel.typing():
                    response = await service.chat_completion_with_fallback(
                        messages=messages,
                        model=model,
                        max_tokens=max_output_tokens,
                        temperature=temperature,
                    )
                model = response.get("_used_model", model)
                if model != previous_stats.get("model"):
                    try:
                        context_length = await service.get_model_context_length(model)
                    except Exception:
                        pass
                answer = response["choices"][0]["message"]["content"]
                if not answer:
                    answer = "I couldn't produce a text response for that message."
                timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
                history.extend([
                    {"role": "user", "content": prompt, "timestamp": timestamp},
                    {"role": "assistant", "content": answer, "timestamp": timestamp},
                ])
                usage = response.get("usage", {})
                prompt_tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
                prompt_characters = sum(len(item["content"]) for item in messages)
                measured_ratio = (
                    prompt_tokens / prompt_characters
                    if isinstance(prompt_tokens, int) and prompt_tokens > 0 and prompt_characters
                    else tokens_per_character
                )
                self.channel_context_stats[channel_id] = {
                    "model": model,
                    "context_length": context_length,
                    "prompt_tokens": prompt_tokens,
                    "tokens_per_character": measured_ratio,
                    "updated_at": timestamp,
                }
                self._record_ai_chat_usage(message.guild.id, message.author.id, model, response)
                owner_id = self._get_channel_owner_id(ai_chat, channel_id, message.author.id)
                await self._persist_channel_session(
                    message.guild.id,
                    message.channel,
                    owner_id,
                    model=model,
                    context_length=context_length,
                    prompt_tokens=prompt_tokens,
                )
                for start in range(0, len(answer), 2000):
                    await message.channel.send(
                        answer[start:start + 2000],
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
        except Exception as error:
            logger.error("AI chat channel request failed in guild %s: %s", message.guild.id, error)
            # Show error type and message, but sanitize for user
            error_type = type(error).__name__
            error_msg = str(error)[:200]  # Truncate long errors
            await message.channel.send(
                f"❌ **{error_type}**: {error_msg}\nPlease try again later.",
                allowed_mentions=discord.AllowedMentions.none(),
            )

async def setup(bot):
    await bot.add_cog(AICog(bot))