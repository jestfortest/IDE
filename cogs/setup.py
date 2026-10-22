import discord
from discord import app_commands
from discord.ext import commands
import logging
from typing import Optional
import asyncio
import json
import os
import datetime
import re
from cogs.moderator import staff_level_required

logger = logging.getLogger(__name__)

LOG_EVENT_LABELS = {
    "message_delete": "Message deleted",
    "message_edit": "Message edited",
    "member_join": "Member joined",
    "member_leave": "Member left",
    "member_update": "Member profile or roles changed",
    "member_ban": "Member banned",
    "member_unban": "Member unbanned",
    "role_create": "Role created",
    "role_update": "Role updated",
    "role_delete": "Role deleted",
    "channel_create": "Channel created",
    "channel_update": "Channel updated",
    "channel_delete": "Channel deleted",
    "voice_join": "Member joined voice",
    "voice_leave": "Member left voice",
    "voice_move": "Member moved voice channels",
    "api_provider": "API provider changed",
}


def bot_owner_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        return await interaction.client.is_owner(interaction.user)

    return app_commands.check(predicate)


class LogEventSelect(discord.ui.Select):
    def __init__(self, view):
        options = [
            discord.SelectOption(
                label=label,
                value=event,
                default=event in view.selected_events,
            )
            for event, label in LOG_EVENT_LABELS.items()
        ]
        super().__init__(
            placeholder="Select events to log (empty selection disables event logging)",
            min_values=0,
            max_values=len(options),
            options=options,
        )
        self.checklist_view = view

    async def callback(self, interaction: discord.Interaction):
        view = self.checklist_view
        if interaction.guild is None or interaction.guild.id != view.guild_id:
            await interaction.response.send_message("This logging checklist is no longer valid for this server.", ephemeral=True)
            return
        if interaction.user.id != view.owner_id:
            await interaction.response.send_message("Only the administrator who opened this checklist can change it.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Administrator permission is required to change logging settings.", ephemeral=True)
            return

        view.selected_events = set(self.values)
        config = view.cog._get_guild_config(view.guild_id)
        config["logs"]["events"] = {
            event: event in view.selected_events
            for event in LOG_EVENT_LABELS
        }
        view.cog._save_config()
        await interaction.response.edit_message(embed=view.make_embed(), view=view)


class LogEventChecklistView(discord.ui.View):
    def __init__(self, cog, guild_id: int, owner_id: int, events: dict):
        super().__init__(timeout=600)
        self.cog = cog
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.selected_events = {event for event, enabled in events.items() if enabled and event in LOG_EVENT_LABELS}
        self.add_item(LogEventSelect(self))

    def make_embed(self) -> discord.Embed:
        selected = [LOG_EVENT_LABELS[event] for event in LOG_EVENT_LABELS if event in self.selected_events]
        embed = discord.Embed(
            title="Logging Event Checklist",
            description="Choose which events should be sent to the configured logs channel. Changes save as soon as you select them.",
            color=0x3498db,
        )
        embed.add_field(
            name=f"Enabled ({len(selected)})",
            value="\n".join(selected) if selected else "No events selected",
            inline=False,
        )
        return embed


class SetupWizardModal(discord.ui.Modal):
    def __init__(self, cog, guild_id: int, step: int):
        titles = [
            "Welcome Setup (1/7)",
            "Logs Setup (2/7)",
            "Autoroles Setup (3/7)",
            "General Setup (4/7)",
            "Staff Roles (5/7)",
            "AI Chat Channel (6/7)",
            "Transcript Channel (7/7)",
        ]
        super().__init__(title=titles[step])
        self.cog = cog
        self.guild_id = guild_id
        self.step = step
        config = cog._get_guild_config(guild_id)
        self.fields = {}

        def add_field(key, label, default="", *, max_length=100, required=False, style=discord.TextStyle.short, placeholder=None):
            field = discord.ui.TextInput(
                label=label,
                default=str(default)[:max_length],
                max_length=max_length,
                required=required,
                style=style,
                placeholder=placeholder,
            )
            self.add_item(field)
            self.fields[key] = field

        guild = cog.bot.get_guild(guild_id)

        def channel_default(channel_id):
            channel = guild.get_channel(channel_id) if guild and channel_id else None
            return channel.name if isinstance(channel, discord.TextChannel) else channel_id or ""

        if step == 0:
            settings = config["welcome"]
            add_field("enabled", "Enable welcome messages? (yes/no)", "yes" if settings.get("enabled") else "no", required=True)
            add_field("channel_id", "Welcome channel name or mention", channel_default(settings.get("channel_id")), placeholder="welcome, #welcome, or mention")
            add_field("create_channel", "Create channel if missing? (yes/no)", "no", required=True)
            add_field("message", "Welcome message", settings.get("message", ""), max_length=1000, style=discord.TextStyle.paragraph, placeholder="Use {user} and {server}")
        elif step == 1:
            settings = config["logs"]
            selected_events = ", ".join(
                LOG_EVENT_LABELS[event]
                for event, enabled in settings.get("events", {}).items()
                if enabled and event in LOG_EVENT_LABELS
            )
            add_field("enabled", "Enable logging? (yes/no)", "yes" if settings.get("enabled") else "no", required=True)
            add_field("channel_id", "Logging channel name or mention", channel_default(settings.get("channel_id")), placeholder="server-logs, #logs, or mention")
            add_field("create_channel", "Create channel if missing? (yes/no)", "no", required=True)
            add_field(
                "events",
                "Events to log? (names or 'all')",
                selected_events,
                max_length=1000,
                placeholder="message_delete, member_join, role_create, all",
            )
        elif step == 2:
            settings = config["autoroles"]
            role_ids = ",".join(str(role_id) for role_id in settings.get("role_ids", []))
            add_field("enabled", "Enable autoroles? (yes/no)", "yes" if settings.get("enabled") else "no", required=True)
            add_field("trigger", "Assignment trigger (on_join)", settings.get("trigger", "on_join"), required=True)
            add_field("role_ids", "Role IDs or mentions, comma-separated", role_ids, max_length=4000, placeholder="Role IDs or <@&role> mentions")
        elif step == 3:
            add_field("prefix", "Prefix", config.get("prefix", "!"), max_length=10, required=True)
            ai_settings = config.get("ai_settings", {})
            add_field("default_model", "Default model", ai_settings.get("default_model") or "", placeholder="Model identifier")
            add_field("temperature", "Temp", ai_settings.get("temperature", 0.7), required=True)
            announcements = config.get("announcements", {})
            add_field(
                "announcement_channel_id",
                "Announce channel",
                channel_default(announcements.get("channel_id")),
                placeholder="#announcements or blank",
            )
        elif step == 4:
            staff_roles = config.get("staff_roles", {})
            add_field(
                "moderator_roles",
                "Mod roles",
                ",".join(str(role_id) for role_id in staff_roles.get("moderator", [])),
                max_length=4000,
                placeholder="Role IDs or mentions",
            )
            add_field(
                "senior_moderator_roles",
                "Senior roles",
                ",".join(str(role_id) for role_id in staff_roles.get("senior_moderator", [])),
                max_length=4000,
                placeholder="Role IDs or mentions",
            )
        elif step == 5:
            ai_chat = config.get("ai_chat", {})
            add_field(
                "ai_chat_enabled",
                "Enable AI chat channel? (yes/no)",
                "yes" if ai_chat.get("enabled") else "no",
                required=True,
            )
            add_field(
                "ai_chat_channel_id",
                "New private channel name",
                "ai-chat",
                max_length=100,
                placeholder="ai-chat",
            )
        else:
            transcripts = config.get("transcripts", {})
            add_field(
                "transcripts_enabled",
                "Save deleted sessions? (yes/no)",
                "yes" if transcripts.get("enabled") else "no",
                required=True,
            )
            add_field(
                "transcripts_channel_name",
                "New private channel name",
                "ai-transcripts",
                max_length=100,
                placeholder="ai-transcripts",
            )

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None or guild.id != self.guild_id:
            await interaction.response.send_message("This setup form is no longer valid for this server.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Administrator permission is required to continue setup.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        config = self.cog._get_guild_config(guild.id)
        values = {key: field.value.strip() for key, field in self.fields.items()}

        try:
            if self.step == 0:
                enabled = self._parse_enabled(values["enabled"])
                channel_id = self._resolve_existing_channel(guild, values["channel_id"], allow_blank=True)
                create_if_missing = self._parse_enabled(values["create_channel"])
                if channel_id is None and (values["channel_id"] or create_if_missing):
                    if not create_if_missing:
                        raise ValueError("Channel not found. Enter a name or mention, or choose create if missing.")
                    await self._request_category(
                        interaction,
                        guild,
                        values["channel_id"] or "welcome",
                        {"enabled": enabled, "message": self.fields["message"].value},
                    )
                    return
                config["welcome"].update(
                    enabled=enabled,
                    channel_id=channel_id,
                    message=self.fields["message"].value,
                )
            elif self.step == 1:
                enabled = self._parse_enabled(values["enabled"])
                channel_id = self._resolve_existing_channel(guild, values["channel_id"], allow_blank=True)
                create_if_missing = self._parse_enabled(values["create_channel"])
                if channel_id is None and (values["channel_id"] or create_if_missing):
                    if not create_if_missing:
                        raise ValueError("Channel not found. Enter a name or mention, or choose create if missing.")
                    await self._request_category(
                        interaction,
                        guild,
                        values["channel_id"] or "server-logs",
                        {"enabled": enabled},
                    )
                    return
                config["logs"]["enabled"] = enabled
                config["logs"]["channel_id"] = channel_id
                config["logs"]["events"] = self._parse_log_events(values.get("events", ""))
            elif self.step == 2:
                trigger = values["trigger"].lower()
                if trigger != "on_join":
                    raise ValueError("Only the on_join autorole trigger is currently supported.")
                role_ids = self._parse_roles(guild, values["role_ids"])
                config["autoroles"].update(
                    enabled=self._parse_enabled(values["enabled"]),
                    trigger=trigger,
                    role_ids=role_ids,
                )
            elif self.step == 3:
                prefix = values["prefix"]
                if not prefix:
                    raise ValueError("The command prefix cannot be blank.")
                try:
                    temperature = float(values["temperature"])
                except ValueError as error:
                    raise ValueError("AI temperature must be a number between 0.0 and 2.0.") from error
                if not 0.0 <= temperature <= 2.0:
                    raise ValueError("AI temperature must be between 0.0 and 2.0.")
                config["prefix"] = prefix
                ai_settings = config.setdefault("ai_settings", {})
                ai_settings["temperature"] = temperature
                model = values["default_model"]
                if model:
                    ai_settings["default_model"] = None if model.lower() == "none" else model
                channel_value = values["announcement_channel_id"]
                if channel_value:
                    channel_id = self._resolve_existing_channel(guild, channel_value, allow_blank=True)
                    if channel_id is None:
                        raise ValueError("Announcement channel not found. Enter a text channel name or mention.")
                    config.setdefault("announcements", {})["channel_id"] = channel_id
            elif self.step == 4:
                config.setdefault("staff_roles", {})
                config["staff_roles"]["moderator"] = self._parse_roles(guild, values.get("moderator_roles", ""))
                config["staff_roles"]["senior_moderator"] = self._parse_roles(guild, values.get("senior_moderator_roles", ""))
            elif self.step == 5:
                enabled = self._parse_enabled(values["ai_chat_enabled"])
                if enabled:
                    ai_chat = config.setdefault("ai_chat", {})
                    existing_ids = ai_chat.get("channels", {}).get(str(interaction.user.id), [])
                    if not isinstance(existing_ids, list):
                        existing_ids = [existing_ids] if existing_ids else []
                    if any(isinstance(guild.get_channel(channel_id), discord.TextChannel) for channel_id in existing_ids):
                        ai_chat["enabled"] = True
                    else:
                        await self._request_category(
                            interaction,
                            guild,
                            values["ai_chat_channel_id"] or "ai-chat",
                            {"enabled": True, "owner_id": interaction.user.id},
                        )
                        return
                else:
                    config.setdefault("ai_chat", {}).update(enabled=False)
            else:
                if self._parse_enabled(values["transcripts_enabled"]):
                    await self._request_category(
                        interaction,
                        guild,
                        values["transcripts_channel_name"] or "ai-transcripts",
                        {"enabled": True},
                    )
                    return
                config.setdefault("transcripts", {}).update(enabled=False)
        except ValueError as error:
            await interaction.followup.send(f"Setup was not saved for this section: {error}", ephemeral=True)
            return
        except discord.Forbidden:
            await interaction.followup.send(
                "The bot needs Manage Channels permission to create that channel.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as error:
            logger.error("Could not create setup channel: %s", error)
            await interaction.followup.send("Discord could not create the channel. Please try again.", ephemeral=True)
            return

        self.cog._save_config()
        if self.step < 6:
            view = SetupWizardNextView(self.cog, guild.id, self.step + 1, interaction.user.id)
            await interaction.followup.send(
                "Section saved. Continue to the next setup section when ready.",
                view=view,
                ephemeral=True,
            )
        else:
            await interaction.followup.send("Setup complete. All sections have been saved.", ephemeral=True)

    @staticmethod
    def _parse_log_events(value: str) -> dict:
        if not value or value.strip() == "":
            return {event: False for event in LOG_EVENT_LABELS}

        allowed = {event: event for event in LOG_EVENT_LABELS}
        aliases = {
            "message_deleted": "message_delete",
            "message_delete": "message_delete",
            "message_edit": "message_edit",
            "member_joined": "member_join",
            "member_join": "member_join",
            "member_left": "member_leave",
            "member_leave": "member_leave",
            "member_updated": "member_update",
            "member_update": "member_update",
            "member_banned": "member_ban",
            "member_ban": "member_ban",
            "member_unbanned": "member_unban",
            "member_unban": "member_unban",
            "role_created": "role_create",
            "role_create": "role_create",
            "role_updated": "role_update",
            "role_update": "role_update",
            "role_deleted": "role_delete",
            "role_delete": "role_delete",
            "channel_created": "channel_create",
            "channel_create": "channel_create",
            "channel_updated": "channel_update",
            "channel_update": "channel_update",
            "channel_deleted": "channel_delete",
            "channel_delete": "channel_delete",
            "voice_joined": "voice_join",
            "voice_join": "voice_join",
            "voice_left": "voice_leave",
            "voice_leave": "voice_leave",
            "voice_moved": "voice_move",
            "voice_move": "voice_move",
        }

        selected = set()
        for chunk in re.split(r"[,;\n]+", value):
            token = re.sub(r"[^a-z0-9_]+", "_", chunk.strip().lower()).strip("_")
            if not token:
                continue
            if token in {"all", "every", "everything"}:
                selected = set(LOG_EVENT_LABELS)
                break
            selected.add(aliases.get(token, token))

        results = {event: event in selected for event in LOG_EVENT_LABELS}
        if not any(results.values()):
            return {event: False for event in LOG_EVENT_LABELS}
        return results

    @staticmethod
    def _parse_enabled(value: str) -> bool:
        normalized = value.lower()
        if normalized in {"yes", "true", "on", "enabled"}:
            return True
        if normalized in {"no", "false", "off", "disabled"}:
            return False
        raise ValueError("Enter yes or no for the enabled setting.")

    @staticmethod
    def _resolve_existing_channel(
        guild: discord.Guild,
        value: str,
        *,
        allow_blank: bool,
    ) -> Optional[int]:
        value = value.strip()
        if not value and allow_blank:
            return None
        match = re.fullmatch(r"(?:<#(\d+)>|(\d+))", value)
        if match:
            channel = guild.get_channel(int(match.group(1) or match.group(2)))
            if isinstance(channel, discord.TextChannel):
                return channel.id
            raise ValueError("That channel ID or mention is not a text channel in this server.")

        channel_name = value.lstrip("#").strip()
        matches = [
            channel for channel in guild.text_channels
            if channel.name.casefold() == channel_name.casefold()
        ]
        if len(matches) == 1:
            return matches[0].id
        if len(matches) > 1:
            raise ValueError("More than one channel has that name; use its channel mention instead.")
        return None

    async def _request_category(
        self,
        interaction: discord.Interaction,
        guild: discord.Guild,
        requested_name: str,
        settings: dict,
    ) -> None:
        new_name = re.sub(r"[^a-z0-9-]+", "-", requested_name.lower()).strip("-")[:100]
        if not new_name:
            raise ValueError("Enter a channel name to create, or leave it blank for the default name.")
        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_channels:
            raise ValueError("The bot needs Manage Channels permission to create channels.")
        view = SetupChannelCategoryView(self.cog, guild.id, interaction.user.id, self.step, new_name, settings)
        await interaction.followup.send(
            f"Choose a category for the new `#{new_name}` channel.",
            view=view,
            ephemeral=True,
        )

    @staticmethod
    def _parse_roles(guild: discord.Guild, value: str) -> list:
        if not value:
            return []
        role_ids = []
        for item in value.split(","):
            match = re.fullmatch(r"(?:<@&(\d+)>|(\d+))", item.strip())
            role_id = int(match.group(1) or match.group(2)) if match else None
            if role_id is None or guild.get_role(role_id) is None:
                raise ValueError("Each autorole must be a valid role ID or role mention from this server.")
            if role_id not in role_ids:
                role_ids.append(role_id)
        return role_ids


class SetupWizardNextView(discord.ui.View):
    def __init__(self, cog, guild_id: int, step: int, owner_id: int):
        super().__init__(timeout=600)
        self.cog = cog
        self.guild_id = guild_id
        self.step = step
        self.owner_id = owner_id

    @discord.ui.button(label="Continue setup", style=discord.ButtonStyle.primary)
    async def continue_setup(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the administrator who started setup can continue it.", ephemeral=True)
            return
        await interaction.response.send_modal(SetupWizardModal(self.cog, self.guild_id, self.step))


class SetupCategorySelect(discord.ui.Select):
    def __init__(self, setup_view, categories):
        options = [
            discord.SelectOption(
                label="No category",
                value="none",
                description="Create this channel at the server root",
            )
        ]
        options.extend(
            discord.SelectOption(label=category.name[:100], value=str(category.id))
            for category in sorted(categories, key=lambda item: item.name.casefold())[:24]
        )
        super().__init__(
            placeholder="Choose a category",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.setup_view = setup_view

    async def callback(self, interaction: discord.Interaction):
        await self.setup_view.create_channel(interaction, self.values[0])


class SetupChannelCategoryView(discord.ui.View):
    def __init__(self, cog, guild_id: int, owner_id: int, step: int, channel_name: str, settings: dict):
        super().__init__(timeout=600)
        self.cog = cog
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.step = step
        self.channel_name = channel_name
        self.settings = settings
        guild = cog.bot.get_guild(guild_id)
        self.add_item(SetupCategorySelect(self, guild.categories if guild else []))

    async def create_channel(self, interaction: discord.Interaction, category_value: str):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the administrator who started setup can choose the category.", ephemeral=True)
            return
        guild = interaction.guild
        if guild is None or guild.id != self.guild_id:
            await interaction.response.send_message("This category picker is no longer valid for this server.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Administrator permission is required to create setup channels.", ephemeral=True)
            return
        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_channels:
            await interaction.response.send_message("The bot needs Manage Channels permission to create channels.", ephemeral=True)
            return

        category = None
        if category_value != "none":
            candidate = guild.get_channel(int(category_value))
            if not isinstance(candidate, discord.CategoryChannel):
                await interaction.response.send_message("That category is no longer available. Run the setup section again.", ephemeral=True)
                return
            category = candidate

        await interaction.response.defer(ephemeral=True)
        try:
            if self.step == 5:
                channel, _ = await self.cog.create_private_ai_channel(
                    guild,
                    interaction.user,
                    base_name=self.channel_name,
                    category=category,
                    category_selected=True,
                    reuse_existing=True,
                )
            else:
                overwrites = None
                if self.step == 6:
                    overwrites = {
                        guild.default_role: discord.PermissionOverwrite(view_channel=False),
                        bot_member: discord.PermissionOverwrite(
                            view_channel=True,
                            send_messages=True,
                            read_message_history=True,
                            manage_channels=True,
                            manage_messages=True,
                            embed_links=True,
                            attach_files=True,
                        ),
                    }
                channel = await guild.create_text_channel(
                    self.channel_name,
                    category=category,
                    overwrites=overwrites,
                    reason="Created by setup wizard",
                )
        except discord.Forbidden:
            await interaction.followup.send("The bot was not allowed to create that channel.", ephemeral=True)
            return
        except discord.HTTPException as error:
            logger.error("Could not create setup channel: %s", error)
            await interaction.followup.send("Discord could not create the channel. Please try again.", ephemeral=True)
            return

        config = self.cog._get_guild_config(guild.id)
        if self.step == 0:
            config["welcome"].update(self.settings, channel_id=channel.id)
        elif self.step == 1:
            config["logs"].update(self.settings, channel_id=channel.id)
        elif self.step == 5:
            ai_chat = config.setdefault("ai_chat", {})
            ai_chat["enabled"] = True
        else:
            config["transcripts"] = {
                **self.settings,
                "channel_id": channel.id,
            }
        self.cog._save_config()

        category_label = f" under {category.name}" if category else " at the server root"
        channel_type = {
            5: "private AI chat channel",
            6: "private transcript channel",
        }.get(self.step, "channel")
        message = f"Created {channel_type} {channel.mention}{category_label} and saved this setup section."
        if self.step < 6:
            view = SetupWizardNextView(self.cog, guild.id, self.step + 1, self.owner_id)
            await interaction.followup.send(
                f"{message} Continue to the next section when ready.",
                view=view,
                ephemeral=True,
            )
        else:
            await interaction.followup.send(message, ephemeral=True)


# Preset providers: (display name, default alias, base URL, key placeholder)
PROVIDER_PRESETS = [
    ("🟢 OpenAI", "openai", "https://api.openai.com/v1", "sk-..."),
    ("🟠 Anthropic Claude", "claude", "https://api.anthropic.com/v1", "sk-ant-..."),
    ("🔵 Google Gemini", "gemini", "https://generativelanguage.googleapis.com/v1beta/openai/", "AIza..."),
    ("🌐 OpenRouter", "openrouter", "https://openrouter.ai/api/v1", "sk-or-..."),
    ("⚫ Nvidia NIM", "nvidia", "https://integrate.api.nvidia.com/v1", "nvapi-..."),
    ("⚪ Mistral", "mistral", "https://api.mistral.ai/v1", "sk-..."),
    ("🟣 Groq", "groq", "https://api.groq.com/openai/v1", "gsk_..."),
    ("🔷 Cerebras", "cerebras", "https://api.cerebras.ai/v1", "csk-..."),
    ("🟣 Together AI", "together", "https://api.together.xyz/v1", "sk-..."),
    ("🤖 HuggingFace", "huggingface", "https://api-inference.huggingface.co/v1", "hf_..."),
    ("⚙️ Custom", "my-provider", "https://", "your-key"),
]


class ProviderPresetSelect(discord.ui.Select):
    """Dropdown to pick a provider preset, then opens the key-entry modal pre-filled"""
    def __init__(self, service, user_id, user_name):
        options = [
            discord.SelectOption(label=preset[0], value=str(i), description=f"`{preset[2]}`")
            for i, preset in enumerate(PROVIDER_PRESETS)
        ]
        super().__init__(placeholder="👉 Pick your provider (auto-fills the next form)", options=options)
        self.service = service
        self.user_id = user_id
        self.user_name = user_name

    async def callback(self, interaction: discord.Interaction):
        preset = PROVIDER_PRESETS[int(self.values[0])]
        modal = APIProviderStep1Modal(
            service=self.service,
            user_id=self.user_id,
            user_name=self.user_name,
            default_alias=preset[1],
            default_url=preset[2],
            key_placeholder=preset[3],
            preset_label=preset[0].strip(),
        )
        await interaction.response.send_modal(modal)


class APIProviderStep1Modal(discord.ui.Modal, title="Add API Provider"):
    """Modal for provider name, API key, and base URL (pre-filled from preset)"""
    alias = discord.ui.TextInput(
        label="Provider name (alias)",
        placeholder="nvidia",
        default="my-provider",
        max_length=32,
    )
    base_url = discord.ui.TextInput(
        label="API base URL",
        placeholder="https://api.openai.com/v1",
        default="https://",
        max_length=300,
    )
    api_key = discord.ui.TextInput(
        label="API key",
        placeholder="Paste your API key here",
        max_length=4000,
        style=discord.TextStyle.short,
    )

    def __init__(self, service, user_id: int, user_name: str,
                 default_alias: str = "my-provider", default_url: str = "https://",
                 key_placeholder: str = "your-key", preset_label: str = ""):
        super().__init__()
        self.service = service
        self.user_id = user_id
        self.user_name = user_name
        if default_alias:
            self.alias.default = default_alias
        if default_url:
            self.base_url.default = default_url
        self.api_key.placeholder = f"Enter your {preset_label or 'provider'} API key ({key_placeholder})"

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        # Validate inputs
        alias = self.alias.value.strip().lower()
        base_url = self.base_url.value.strip().rstrip("/")
        api_key = self.api_key.value.strip()
        
        if not alias or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", alias):
            await interaction.followup.send(
                "❌ Invalid name. Use 1-32 lowercase letters, numbers, hyphens, or underscores.",
                ephemeral=True
            )
            return
        
        if not base_url.startswith("https://"):
            await interaction.followup.send(
                "❌ Base URL must start with https://",
                ephemeral=True
            )
            return
        
        if not api_key:
            await interaction.followup.send(
                "❌ API key cannot be empty.",
                ephemeral=True
            )
            return
        
        # Try to fetch models from the provider
        await interaction.followup.send("🔄 Fetching available models...", ephemeral=True)
        
        try:
            import aiohttp
            headers = {"Authorization": f"Bearer {api_key}"}
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{base_url}/models", headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        await interaction.edit_original_response(
                            content=f"❌ Failed to fetch models: HTTP {resp.status}\n{error_text[:500]}"
                        )
                        return
                    
                    data = await resp.json()
                    models = data.get("data", [])
                    
                    if not models:
                        # Ask user to enter model IDs manually
                        manual_view = APIProviderNoModelsView(
                            service=self.service,
                            alias=alias,
                            base_url=base_url,
                            api_key=api_key,
                            user_id=self.user_id,
                            user_name=self.user_name,
                        )
                        embed = discord.Embed(
                            title="⚠️ No models found",
                            description=f"The provider didn't return a model list (endpoint: `{base_url}/models`).\n\nUse **Manual model input** below to type the model IDs yourself, or **Skip model import** to save the provider anyway.",
                            color=0xf39c12
                        )
                        await interaction.edit_original_response(
                            content=None,
                            embed=embed,
                            view=manual_view
                        )
                        return
                    
                    # Show model selection view
                    view = APIProviderModelSelectView(
                        service=self.service,
                        alias=alias,
                        base_url=base_url,
                        api_key=api_key,
                        user_id=self.user_id,
                        user_name=self.user_name,
                        models=models,
                    )
                    
                    embed = discord.Embed(
                        title=f"📦 Found {len(models)} models",
                        description=f"Provider: **{alias}**\nURL: `{base_url}`\n\nSelect which models to import to your pools:",
                        color=0x3498db
                    )
                    await interaction.edit_original_response(
                        content=None,
                        embed=embed,
                        view=view
                    )
                    
        except Exception as e:
            await interaction.edit_original_response(
                content=f"❌ Error fetching models: {str(e)[:500]}"
            )


class APIProviderModelSelectView(discord.ui.View):
    """View for selecting which models to import"""
    def __init__(self, service, alias, base_url, api_key, user_id, user_name, models, timeout=300):
        super().__init__(timeout=timeout)
        self.service = service
        self.alias = alias
        self.base_url = base_url
        self.api_key = api_key
        self.user_id = user_id
        self.user_name = user_name
        self.models = models
        self.selected_models = set()
        
        # Prioritize image/generation models so they show first
        gen_hints = ("image", "diffusion", "flux", "sdxl", "stable-diffusion", "dall", "playground", "midjourney", "sdxl", "tts", "speech", "audio", "whisper", "s2.")
        def sort_key(m):
            mid = m.get("id", "").lower()
            return 0 if any(h in mid for h in gen_hints) else 1
        models_sorted = sorted(models, key=sort_key)
        
        # Max 24 model buttons + 1 confirm = 25 (Discord's component limit)
        max_buttons = 24
        for model in models_sorted[:max_buttons]:
            model_id = model.get("id", "unknown")
            model_name = model.get("name", model_id).split("/")[-1][:45]
            select = ModelCheckbox(model_id, model_name)
            self.add_item(select)
        
        # Add confirm button
        confirm = discord.ui.Button(
            label="Save selected models",
            style=discord.ButtonStyle.success,
            custom_id="api_provider:confirm_models"
        )
        confirm.callback = self.confirm_callback
        self.add_item(confirm)
    
    async def confirm_callback(self, interaction: discord.Interaction):
        if not self.selected_models:
            await interaction.response.send_message(
                "⚠️ You haven't selected any models. Please select at least one model or cancel.",
                ephemeral=True
            )
            return
        
        # Save the provider
        try:
            self.service.add_api_provider(
                self.alias, self.base_url, self.api_key,
                actor_id=self.user_id, actor_name=self.user_name
            )
        except (OSError, ValueError) as error:
            await interaction.response.send_message(f"❌ Provider was not saved: {error}", ephemeral=True)
            return
        
        # Create summary
        selected_list = "\n".join(f"`{self.alias}::{m}`" for m in sorted(self.selected_models))
        
        embed = discord.Embed(
            title="✅ Provider Saved!",
            description=f"Provider **{self.alias}** has been added with {len(self.selected_models)} model(s):\n\n{selected_list}",
            color=0x2ecc71
        )
        embed.add_field(
            name="How to use",
            value=f"Use models as `{self.alias}::model-id` with `/ask` or `/converse` commands.\nExample: `/ask What's 2+2? model:{self.alias}::some-model`",
            inline=False
        )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
        
        # Audit log
        setup_cog = interaction.client.get_cog("SetupCog")
        if setup_cog:
            try:
                await setup_cog._send_log_event(
                    interaction.guild,
                    "api_provider",
                    "API Provider Added",
                    f"Provider `{self.alias}` with {len(self.selected_models)} models was added by {interaction.user.mention}",
                    fields=[
                        ("Alias", self.alias),
                        ("Base URL", self.base_url),
                        ("Models", ", ".join(sorted(self.selected_models))[:1000]),
                        ("Added by", f"{interaction.user} ({interaction.user.id})"),
                    ],
                )
            except Exception as e:
                logger.warning("Could not send API provider add log: %s", e)
        
        # Stop the view
        self.stop()


class ModelCheckbox(discord.ui.Button):
    """A checkbox button for a single model"""
    def __init__(self, model_id, model_name):
        import hashlib
        # custom_id must be <= 100 chars
        if len(model_id) <= 90:
            cid = f"api_model:{model_id}"
        else:
            digest = hashlib.md5(model_id.encode()).hexdigest()
            cid = f"api_model:{digest}"
        super().__init__(
            label=model_name,
            style=discord.ButtonStyle.secondary,
            custom_id=cid
        )
        self.model_id = model_id
        self.selected = False
    
    async def callback(self, interaction: discord.Interaction):
        self.selected = not self.selected
        view = self.view
        
        if self.selected:
            view.selected_models.add(self.model_id)
            self.style = discord.ButtonStyle.success
            self.label = f"✓ {self.label}"
        else:
            view.selected_models.discard(self.model_id)
            self.style = discord.ButtonStyle.secondary
            self.label = self.label.lstrip("✓ ")
        
        # Update confirm button label
        for item in view.children:
            if isinstance(item, discord.ui.Button) and item.custom_id == "api_provider:confirm_models":
                item.label = f"Save {len(view.selected_models)} models"
        
        await interaction.response.edit_message(view=view)


class PoolModelSelectView(discord.ui.View):
    """View for selecting models to add to a specific pool"""
    def __init__(self, service, pool_name: str, models: list, user_id: int, user_name: str, timeout=300):
        super().__init__(timeout=timeout)
        self.service = service
        self.pool_name = pool_name
        self.models = models
        self.user_id = user_id
        self.user_name = user_name
        self.selected_models = set()

        # Prioritize matching models for this pool
        pool_keywords = {
            "image": ("image", "diffusion", "flux", "sdxl", "stable-diffusion", "dall", "playground", "ming"),
            "audio": ("audio", "speech", "tts", "whisper", "elevenlabs", "fish", "s2."),
            "code": ("code", "coder", "codestral", "deepseek", "qwen"),
            "reason": ("reason", "thinking", "ultra", "super", "nemotron"),
            "creative": ("creative", "writing", "gemini", "gemma"),
            "text": ("text", "instruct", "chat"),
        }
        keywords = pool_keywords.get(pool_name, ())

        def sort_key(m):
            mid = m.get("id", "").lower()
            return 0 if any(k in mid for k in keywords) else 1

        models_sorted = sorted(models, key=sort_key)

        # Max 24 buttons + 1 confirm = 25
        for model in models_sorted[:24]:
            model_id = model.get("id", "unknown")
            model_name = model.get("name", model_id).split("/")[-1][:45]
            select = ModelCheckbox(model_id, model_name)
            self.add_item(select)

        confirm = discord.ui.Button(
            label="Save pool models",
            style=discord.ButtonStyle.success,
            custom_id="pool_select:confirm"
        )
        confirm.callback = self.confirm_callback
        self.add_item(confirm)

    async def confirm_callback(self, interaction: discord.Interaction):
        if not self.selected_models:
            await interaction.response.send_message(
                "⚠️ Please select at least one model for the pool.",
                ephemeral=True
            )
            return

        # Save to pool via OpenRouterService
        self.service.save_pool_override(
            self.pool_name,
            sorted(list(self.selected_models)),
            actor_id=self.user_id,
            actor_name=self.user_name
        )

        model_list = "\n".join(f"• `{m}`" for m in sorted(self.selected_models))
        embed = discord.Embed(
            title=f"✅ `{self.pool_name}` pool updated!",
            description=f"The **{self.pool_name}** pool now has {len(self.selected_models)} model(s):\n\n{model_list}",
            color=0x2ecc71
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        self.stop()


class ManualModelInputModal(discord.ui.Modal, title="Enter Model IDs"):
    """Modal for manually entering model IDs when /models endpoint is unavailable"""
    model_ids = discord.ui.TextInput(
        label="Model ID(s)",
        placeholder="meta/llama-3.1-8b-instruct, nvidia/flux.1-schnell",
        max_length=2000,
        style=discord.TextStyle.paragraph,
        required=False,
    )

    def __init__(self, service, alias, base_url, api_key, user_id, user_name):
        super().__init__()
        self.service = service
        self.alias = alias
        self.base_url = base_url
        self.api_key = api_key
        self.user_id = user_id
        self.user_name = user_name

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.model_ids.value or ""
        model_ids = [m.strip() for m in raw.replace(",", "\n").split() if m.strip()]

        if not model_ids:
            await interaction.response.send_message(
                "❌ No model IDs provided. Please enter at least one model ID.",
                ephemeral=True
            )
            return

        # Save the provider
        try:
            self.service.add_api_provider(
                self.alias, self.base_url, self.api_key,
                actor_id=self.user_id, actor_name=self.user_name
            )
        except (OSError, ValueError) as error:
            await interaction.response.send_message(f"❌ Provider was not saved: {error}", ephemeral=True)
            return

        selected_list = "\n".join(f"`{self.alias}::{m}`" for m in model_ids)
        embed = discord.Embed(
            title="✅ Provider Saved (manual model list)",
            description=f"Provider **{self.alias}** saved with {len(model_ids)} model(s):\n\n{selected_list}",
            color=0x2ecc71
        )
        embed.add_field(
            name="How to use",
            value=f"Use models as `{self.alias}::model-id` with `/ask` or `/converse`.\nExample: `/ask hello model:{self.alias}::{model_ids[0]}`",
            inline=False
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

        setup_cog = interaction.client.get_cog("SetupCog")
        if setup_cog:
            try:
                await setup_cog._send_log_event(
                    interaction.guild,
                    "api_provider",
                    "API Provider Added (manual models)",
                    f"Provider `{self.alias}` with {len(model_ids)} manual models was added by {interaction.user.mention}",
                    fields=[
                        ("Alias", self.alias),
                        ("Base URL", self.base_url),
                        ("Models", ", ".join(model_ids)[:1000]),
                        ("Added by", f"{interaction.user} ({interaction.user.id})"),
                    ],
                )
            except Exception as e:
                logger.warning("Could not send API provider add log: %s", e)


class APIProviderNoModelsView(discord.ui.View):
    """View shown when the provider's /models endpoint returns nothing"""
    def __init__(self, service, alias, base_url, api_key, user_id, user_name, timeout=300):
        super().__init__(timeout=timeout)
        self.service = service
        self.alias = alias
        self.base_url = base_url
        self.api_key = api_key
        self.user_id = user_id
        self.user_name = user_name

    @discord.ui.button(label="✍️ Manual model input", style=discord.ButtonStyle.primary, custom_id="api_nmodels:manual")
    async def manual_input(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ManualModelInputModal(
            self.service, self.alias, self.base_url, self.api_key,
            self.user_id, self.user_name
        ))

    @discord.ui.button(label="Save without models", style=discord.ButtonStyle.secondary, custom_id="api_nmodels:skip")
    async def skip_import(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            self.service.add_api_provider(
                self.alias, self.base_url, self.api_key,
                actor_id=self.user_id, actor_name=self.user_name
            )
        except (OSError, ValueError) as error:
            await interaction.response.send_message(f"❌ Provider was not saved: {error}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"✅ Provider `{self.alias}` saved without any imported models. "
            f"You can use it with explicit model IDs: `/ask hello model:{self.alias}::some-model-id`",
            ephemeral=True
        )


class SetupCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config_file = "/Users/aidenchang/Documents/important/projects/Discord Bots/Infinity_lounge_bot/setup_config.json"
        self.config = self._load_config()
        self.ai_chat_creation_locks = {}

    def _load_config(self) -> dict:
        """Load configuration from file"""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Error loading config: {e}")
                return {}
        return {}

    def _save_config(self):
        """Save configuration to file"""
        try:
            with open(self.config_file, 'w') as f:
                json.dump(self.config, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving config: {e}")

    def _get_guild_config(self, guild_id: int) -> dict:
        """Get configuration for a specific guild"""
        guild_key = str(guild_id)
        if guild_key not in self.config:
            self.config[guild_key] = {
                "welcome": {"enabled": False, "message": "", "channel_id": None},
                "logs": {"enabled": False, "channel_id": None, "events": {"member_leave": True}},
                "announcements": {"channel_id": None},
                "ai_chat": {
                    "enabled": False,
                    "channel_id": None,
                    "owner_id": None,
                    "category_id": None,
                    "channels": {},
                    "channel_members": {},
                    "next_channel_number": 1,
                    "usage": {},
                },
                "transcripts": {"enabled": False, "channel_id": None},
                "staff_roles": {"moderator": [], "senior_moderator": []},
                "autoroles": {"enabled": False, "role_ids": [], "trigger": "on_join"},
                "prefix": "!",
                "ai_settings": {"default_model": None, "temperature": 0.7}
            }
            self._save_config()
        else:
            # Fill autorole defaults for configs created by older versions.
            autoroles = self.config[guild_key].setdefault("autoroles", {})
            defaults = {"enabled": False, "role_ids": [], "trigger": "on_join"}
            needs_save = any(key not in autoroles for key in defaults)
            for key, value in defaults.items():
                autoroles.setdefault(key, value)
            logs = self.config[guild_key].setdefault("logs", {})
            if "events" not in logs:
                logs["events"] = {"member_leave": True}
                needs_save = True
            if "announcements" not in self.config[guild_key]:
                self.config[guild_key]["announcements"] = {"channel_id": None}
                needs_save = True
            else:
                self.config[guild_key]["announcements"].setdefault("channel_id", None)
            if "ai_chat" not in self.config[guild_key]:
                self.config[guild_key]["ai_chat"] = {
                    "enabled": False,
                    "channel_id": None,
                    "owner_id": None,
                    "category_id": None,
                    "channels": {},
                    "channel_members": {},
                    "next_channel_number": 1,
                    "usage": {},
                }
                needs_save = True
            else:
                ai_chat = self.config[guild_key]["ai_chat"]
                defaults = {
                    "enabled": False,
                    "channel_id": None,
                    "owner_id": None,
                    "category_id": None,
                    "channels": {},
                    "channel_members": {},
                    "next_channel_number": 1,
                    "usage": {},
                }
                for key, value in defaults.items():
                    if key not in ai_chat:
                        ai_chat[key] = value
                        needs_save = True
                if not isinstance(ai_chat.get("channels"), dict):
                    ai_chat["channels"] = {}
                    needs_save = True
                else:
                    for owner_key, channel_ids in list(ai_chat["channels"].items()):
                        if isinstance(channel_ids, list):
                            normalized_ids = [channel_id for channel_id in channel_ids if isinstance(channel_id, int)]
                        elif isinstance(channel_ids, int):
                            normalized_ids = [channel_ids]
                        else:
                            normalized_ids = []
                        if normalized_ids != channel_ids:
                            ai_chat["channels"][owner_key] = normalized_ids
                            needs_save = True
                if not isinstance(ai_chat.get("channel_members"), dict):
                    ai_chat["channel_members"] = {}
                    needs_save = True
                if not isinstance(ai_chat.get("usage"), dict):
                    ai_chat["usage"] = {}
                    needs_save = True
                if ai_chat.get("channel_id") and ai_chat.get("owner_id"):
                    owner_key = str(ai_chat["owner_id"])
                    owner_channels = ai_chat["channels"].setdefault(owner_key, [])
                    if ai_chat["channel_id"] not in owner_channels:
                        owner_channels.append(ai_chat["channel_id"])
                        needs_save = True
            if "transcripts" not in self.config[guild_key]:
                self.config[guild_key]["transcripts"] = {"enabled": False, "channel_id": None}
                needs_save = True
            else:
                transcripts = self.config[guild_key]["transcripts"]
                for key, value in (("enabled", False), ("channel_id", None)):
                    if key not in transcripts:
                        transcripts[key] = value
                        needs_save = True
            if "staff_roles" not in self.config[guild_key]:
                self.config[guild_key]["staff_roles"] = {}
                needs_save = True
            staff_roles = self.config[guild_key]["staff_roles"]
            for level in ("moderator", "senior_moderator"):
                if level not in staff_roles:
                    staff_roles[level] = []
                    needs_save = True
            if needs_save:
                self._save_config()
        return self.config[guild_key]

    async def create_private_ai_channel(
        self,
        guild: discord.Guild,
        owner: discord.Member,
        *,
        base_name: str = "ai-chat",
        category: Optional[discord.CategoryChannel] = None,
        category_selected: bool = False,
        reuse_existing: bool = False,
        additional_members: Optional[list[discord.Member]] = None,
    ):
        guild_config = self._get_guild_config(guild.id)
        ai_chat = guild_config.setdefault("ai_chat", {})
        owner_key = str(owner.id)
        lock = self.ai_chat_creation_locks.setdefault(guild.id, asyncio.Lock())

        async with lock:
            channels = ai_chat.setdefault("channels", {})
            owner_channels = channels.get(owner_key, [])
            if not isinstance(owner_channels, list):
                owner_channels = [owner_channels] if owner_channels else []
            owner_channels = [
                channel_id for channel_id in owner_channels
                if isinstance(guild.get_channel(channel_id), discord.TextChannel)
            ]
            channels[owner_key] = owner_channels
            if reuse_existing and owner_channels:
                existing_channel = guild.get_channel(owner_channels[-1])
                ai_chat["enabled"] = True
                self._save_config()
                return existing_channel, False
            if len(owner_channels) >= 10:
                raise ValueError("You already have 10 active AI chat sessions. Archive or wait for one to expire before creating another.")

            bot_member = guild.me
            if bot_member is None or not bot_member.guild_permissions.manage_channels:
                raise ValueError("The bot needs Manage Channels permission to create AI chat channels.")

            if category_selected:
                ai_chat["category_id"] = category.id if category else None
            elif category is None and ai_chat.get("category_id"):
                candidate = guild.get_channel(ai_chat["category_id"])
                category = candidate if isinstance(candidate, discord.CategoryChannel) else None

            normalized_name = re.sub(r"[^a-z0-9-]+", "-", base_name.lower()).strip("-") or "ai-chat"
            normalized_name = normalized_name[:90]
            number = max(1, int(ai_chat.get("next_channel_number", 1)))
            existing_names = {channel.name.casefold() for channel in guild.text_channels}
            channel_name = f"{normalized_name}-{number}"
            while channel_name.casefold() in existing_names:
                number += 1
                channel_name = f"{normalized_name}-{number}"

            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                owner: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                ),
                bot_member: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    manage_channels=True,
                    manage_messages=True,
                    embed_links=True,
                    attach_files=True,
                ),
            }
            for member in additional_members or []:
                if member.id != owner.id and not member.bot:
                    overwrites[member] = discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True,
                    )
            channel = await guild.create_text_channel(
                channel_name,
                category=category,
                overwrites=overwrites,
                reason=f"Private AI chat requested by {owner}",
            )

            ai_chat["enabled"] = True
            ai_chat["channels"][owner_key].append(channel.id)
            ai_chat["next_channel_number"] = number + 1
            ai_chat.setdefault("usage", {})
            ai_chat.setdefault("channel_members", {})
            ai_chat.setdefault("channel_members", {})[str(channel.id)] = [
                member.id for member in (additional_members or []) if member.id != owner.id and not member.bot
            ]
            if not ai_chat.get("channel_id"):
                ai_chat["channel_id"] = channel.id
                ai_chat["owner_id"] = owner.id
            self._save_config()

            ai_cog = self.bot.get_cog("AICog")
            if ai_cog is not None:
                try:
                    await ai_cog.register_channel_session(guild, owner, channel, additional_members or [])
                    await ai_cog.create_chat_panel(channel)
                except discord.HTTPException as error:
                    logger.warning("Could not post AI session panel in channel %s: %s", channel.id, error)
            return channel, True

    # MAIN SETUP GROUP
    setup = app_commands.Group(
        name="setup",
        description="Bot configuration settings",
    )

    providers = app_commands.Group(
        name="providers",
        description="Manage OpenAI-compatible API providers",
        parent=setup,
        default_permissions=discord.Permissions(administrator=True),
    )

    @providers.command(name="add", description="Add an OpenAI-compatible provider API key")
    @app_commands.guild_only()
    async def provider_add(self, interaction: discord.Interaction):
        if not self.bot.openrouter_service:
            await interaction.response.send_message("The AI service is not ready yet.", ephemeral=True)
            return

        # Send a preset picker; selecting one opens a pre-filled modal
        view = discord.ui.View(timeout=120)
        select = ProviderPresetSelect(
            self.bot.openrouter_service,
            interaction.user.id,
            str(interaction.user)
        )
        view.add_item(select)

        embed = discord.Embed(
            title="🔌 Add API Provider",
            description="**Pick your provider** below — it will auto-fill the name and API base URL.\n\n"
                        "Common ones are ready to go:\n"
                        "• 🟢 OpenAI\n• 🟠 Anthropic Claude\n• 🔵 Google Gemini\n"
                        "• 🌐 OpenRouter\n• ⚫ Nvidia NIM\n• ⚪ Mistral\n"
                        "• 🟣 Groq / Cerebras / Together AI / HuggingFace\n• ⚙️ Custom (fill in your own URL)\n\n"
                        "You'll paste your API key, then choose which models to import.",
            color=0x3498db,
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @providers.command(name="list", description="List configured API provider aliases")
    @app_commands.guild_only()
    async def provider_list(self, interaction: discord.Interaction):
        providers = self.bot.openrouter_service.api_providers
        if providers:
            description = "\n".join(
                f"`{alias}` → `{provider['base_url']}`"
                for alias, provider in sorted(providers.items())
            )
        else:
            description = "No additional API providers are configured."
        await interaction.response.send_message(description, ephemeral=True)

    @providers.command(name="remove", description="Remove an API provider and its stored key")
    @app_commands.describe(alias="The provider alias to remove")
    @staff_level_required("senior_moderator")
    @app_commands.guild_only()
    async def provider_remove(self, interaction: discord.Interaction, alias: str):
        alias_norm = alias.strip().lower()
        if self.bot.openrouter_service.remove_api_provider(
            alias, actor_id=interaction.user.id, actor_name=str(interaction.user)
        ):
            message = f"❌ Provider `{alias_norm}` and its stored key were removed."
            await self._send_log_event(
                interaction.guild,
                "api_provider",
                "API Provider Removed",
                f"Provider `{alias_norm}` was removed by {interaction.user.mention}",
                fields=[
                    ("Alias", alias_norm),
                    ("Removed by", f"{interaction.user} ({interaction.user.id})"),
                ],
            )
        else:
            message = f"⚠️ No provider named `{alias_norm}` is configured."
        await interaction.response.send_message(message, ephemeral=True)

    async def _reject_non_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator:
            return False
        await interaction.response.send_message(
            "You're not an administrator, so you can't use this command.",
            ephemeral=True,
        )
        return True

    @setup.command(name="all", description="Run the guided setup for all server settings")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def setup_all(self, interaction: discord.Interaction):
        if await self._reject_non_admin(interaction):
            return
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        self._get_guild_config(interaction.guild.id)
        await interaction.response.send_modal(SetupWizardModal(self, interaction.guild.id, 0))

    permissions = app_commands.Group(
        name="permissions",
        description="Configure moderator role levels",
        parent=setup,
        default_permissions=discord.Permissions(administrator=True),
    )

    @permissions.command(name="set", description="Add a role to a moderation level")
    @app_commands.describe(level="The moderation level", role="The role to grant this level")
    @app_commands.choices(level=[
        app_commands.Choice(name="Moderator (kick, mute, warn, clear)", value="moderator"),
        app_commands.Choice(name="Senior moderator (ban, unlock, slowmode, etc.)", value="senior_moderator"),
    ])
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def permissions_set(
        self,
        interaction: discord.Interaction,
        level: app_commands.Choice[str],
        role: discord.Role,
    ):
        config = self._get_guild_config(interaction.guild.id)
        role_ids = config["staff_roles"][level.value]
        if role.id not in role_ids:
            role_ids.append(role.id)
            self._save_config()
        await interaction.response.send_message(
            f"{role.mention} can now use {level.name.lower()} commands.",
            ephemeral=True,
        )

    @permissions.command(name="remove", description="Remove a role from a moderation level")
    @app_commands.describe(level="The moderation level", role="The role to remove")
    @app_commands.choices(level=[
        app_commands.Choice(name="Moderator (kick, mute, warn, clear)", value="moderator"),
        app_commands.Choice(name="Senior moderator (ban, unlock, slowmode, etc.)", value="senior_moderator"),
    ])
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def permissions_remove(
        self,
        interaction: discord.Interaction,
        level: app_commands.Choice[str],
        role: discord.Role,
    ):
        config = self._get_guild_config(interaction.guild.id)
        role_ids = config["staff_roles"][level.value]
        if role.id in role_ids:
            role_ids.remove(role.id)
            self._save_config()
            response = f"Removed {role.mention} from {level.name.lower()}."
        else:
            response = f"{role.mention} was not assigned to {level.name.lower()}."
        await interaction.response.send_message(response, ephemeral=True)

    @permissions.command(name="list", description="List the configured moderation roles")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def permissions_list(self, interaction: discord.Interaction):
        staff_roles = self._get_guild_config(interaction.guild.id)["staff_roles"]
        lines = []
        for level, label in (("moderator", "Moderator"), ("senior_moderator", "Senior moderator")):
            roles = [interaction.guild.get_role(role_id) for role_id in staff_roles[level]]
            mentions = [role.mention for role in roles if role is not None]
            lines.append(f"**{label}:** {', '.join(mentions) if mentions else 'No roles assigned'}")
        lines.append("**Moderator commands:** `/kick`, `/mute`, `/unmute`, `/warn`, `/warnings`, `/clear`")
        lines.append("**Senior moderator commands:** all moderator commands plus `/ban`, `/unban`, `/slowmode`, `/lock`, `/unlock`")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    announcements = app_commands.Group(
        name="announcements",
        description="Configure the announcement channel",
        parent=setup,
        default_permissions=discord.Permissions(administrator=True),
    )

    @announcements.command(name="channel", description="Set the channel used by /announce")
    @app_commands.describe(channel="The text or announcement channel for bot announcements")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def announcements_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ):
        config = self._get_guild_config(interaction.guild.id)
        config.setdefault("announcements", {})["channel_id"] = channel.id
        self._save_config()
        await interaction.response.send_message(
            f"Announcements will be sent to {channel.mention}.",
            ephemeral=True,
        )

    @app_commands.command(name="announce", description="Post an announcement in the configured channel")
    @app_commands.describe(message="The announcement text to post")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def announce(
        self,
        interaction: discord.Interaction,
        message: app_commands.Range[str, 1, 2000],
    ):
        guild = interaction.guild
        config = self._get_guild_config(guild.id)
        channel_id = config.get("announcements", {}).get("channel_id")
        channel = guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "No announcement channel is configured. Set one with `/setup announcements channel`.",
                ephemeral=True,
            )
            return

        bot_member = guild.me
        if bot_member is None or not channel.permissions_for(bot_member).send_messages:
            await interaction.response.send_message(
                f"I don't have permission to send messages in {channel.mention}.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await channel.send(message, allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden:
            await interaction.followup.send(
                f"Discord denied permission to post in {channel.mention}.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as error:
            logger.warning("Could not post announcement in guild %s: %s", guild.id, error)
            await interaction.followup.send(
                "Discord couldn't post the announcement. Please try again.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Announcement posted in {channel.mention}.",
            ephemeral=True,
        )

    # Welcome Group (nested under setup)
    welcome = app_commands.Group(
        name="welcome",
        description="Configure welcome messages",
        parent=setup,
        default_permissions=discord.Permissions(administrator=True),
    )

    @welcome.command(name="enable", description="Enable or disable welcome messages")
    @app_commands.describe(enabled="Whether to enable welcome messages")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def welcome_enable(
        self,
        interaction: discord.Interaction,
        enabled: bool
    ):
        await interaction.response.defer(ephemeral=True)

        try:
            guild_config = self._get_guild_config(interaction.guild.id)
            guild_config["welcome"]["enabled"] = enabled
            self._save_config()

            status = "enabled" if enabled else "disabled"
            embed = discord.Embed(
                title="📬 Welcome Messages Updated",
                description=f"Welcome messages have been {status}.",
                color=0x2ecc71 if enabled else 0x95a5a6
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Error in welcome enable command: {e}")
            embed = discord.Embed(
                title="❌ Error",
                description=f"An error occurred: {str(e)}",
                color=0xe74c3c
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    @welcome.command(name="message", description="Set the welcome message")
    @app_commands.describe(message="The welcome message to send (use {user} for mention, {server} for server name)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def welcome_message(
        self,
        interaction: discord.Interaction,
        message: str
    ):
        await interaction.response.defer(ephemeral=True)

        try:
            guild_config = self._get_guild_config(interaction.guild.id)
            guild_config["welcome"]["message"] = message
            self._save_config()

            embed = discord.Embed(
                title="📝 Welcome Message Set",
                description=f"Welcome message has been updated to:\n{message}",
                color=0x3498db
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Error in welcome message command: {e}")
            embed = discord.Embed(
                title="❌ Error",
                description=f"An error occurred: {str(e)}",
                color=0xe74c3c
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    @welcome.command(name="channel", description="Set the welcome channel")
    @app_commands.describe(channel="The channel to send welcome messages to")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def welcome_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel
    ):
        await interaction.response.defer(ephemeral=True)

        try:
            guild_config = self._get_guild_config(interaction.guild.id)
            guild_config["welcome"]["channel_id"] = channel.id
            self._save_config()

            embed = discord.Embed(
                title="📍 Welcome Channel Set",
                description=f"Welcome messages will be sent to {channel.mention}",
                color=0x3498db
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Error in welcome channel command: {e}")
            embed = discord.Embed(
                title="❌ Error",
                description=f"An error occurred: {str(e)}",
                color=0xe74c3c
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    # Logs Group (nested under setup)
    logs = app_commands.Group(
        name="logs",
        description="Configure logging channels",
        parent=setup,
        default_permissions=discord.Permissions(administrator=True),
    )

    @logs.command(name="enable", description="Enable or disable logging")
    @app_commands.describe(enabled="Whether to enable logging")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def logs_enable(
        self,
        interaction: discord.Interaction,
        enabled: bool
    ):
        await interaction.response.defer(ephemeral=True)

        try:
            guild_config = self._get_guild_config(interaction.guild.id)
            guild_config["logs"]["enabled"] = enabled
            self._save_config()

            status = "enabled" if enabled else "disabled"
            embed = discord.Embed(
                title="📋 Logging Updated",
                description=f"Logging has been {status}.",
                color=0x2ecc71 if enabled else 0x95a5a6
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Error in logs enable command: {e}")
            embed = discord.Embed(
                title="❌ Error",
                description=f"An error occurred: {str(e)}",
                color=0xe74c3c
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    @logs.command(name="channel", description="Set the logging channel")
    @app_commands.describe(channel="The channel to send logs to")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def logs_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel
    ):
        await interaction.response.defer(ephemeral=True)

        try:
            guild_config = self._get_guild_config(interaction.guild.id)
            guild_config["logs"]["channel_id"] = channel.id
            self._save_config()

            embed = discord.Embed(
                title="📍 Logs Channel Set",
                description=f"Logs will be sent to {channel.mention}",
                color=0x3498db
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Error in logs channel command: {e}")
            embed = discord.Embed(
                title="❌ Error",
                description=f"An error occurred: {str(e)}",
                color=0xe74c3c
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    @logs.command(name="events", description="Choose which server events to log")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def logs_events(self, interaction: discord.Interaction):
        events = self._get_guild_config(interaction.guild.id)["logs"].get("events", {})
        view = LogEventChecklistView(self, interaction.guild.id, interaction.user.id, events)
        await interaction.response.send_message(
            embed=view.make_embed(),
            view=view,
            ephemeral=True,
        )

    # Autoroles Group (nested under setup)
    autoroles = app_commands.Group(
        name="autoroles",
        description="Configure automatic roles",
        parent=setup,
        default_permissions=discord.Permissions(administrator=True),
    )

    @autoroles.command(name="enable", description="Enable or disable autoroles")
    @app_commands.describe(enabled="Whether to enable autoroles")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def autoroles_enable(
        self,
        interaction: discord.Interaction,
        enabled: bool
    ):
        await interaction.response.defer(ephemeral=True)

        try:
            guild_config = self._get_guild_config(interaction.guild.id)
            guild_config["autoroles"]["enabled"] = enabled
            self._save_config()

            status = "enabled" if enabled else "disabled"
            embed = discord.Embed(
                title="👥 Autoroles Updated",
                description=f"Autoroles have been {status}.",
                color=0x2ecc71 if enabled else 0x95a5a6
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Error in autoroles enable command: {e}")
            embed = discord.Embed(
                title="❌ Error",
                description=f"An error occurred: {str(e)}",
                color=0xe74c3c
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    @autoroles.command(name="trigger", description="Set when to assign autoroles")
    @app_commands.describe(trigger="When to assign roles: on_join (default) or on_reaction")
    @app_commands.choices(trigger=[
        app_commands.Choice(name="On Member Join", value="on_join"),
        app_commands.Choice(name="On Reaction (coming soon)", value="on_reaction"),
    ])
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def autoroles_trigger(
        self,
        interaction: discord.Interaction,
        trigger: str
    ):
        await interaction.response.defer(ephemeral=True)

        try:
            guild_config = self._get_guild_config(interaction.guild.id)
            
            if trigger not in ["on_join", "on_reaction"]:
                embed = discord.Embed(
                    title="❌ Invalid Trigger",
                    description="Valid triggers: `on_join`, `on_reaction`",
                    color=0xe74c3c
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

            guild_config["autoroles"]["trigger"] = trigger
            self._save_config()

            trigger_name = "On Member Join" if trigger == "on_join" else "On Reaction"
            embed = discord.Embed(
                title="⚡ Autorole Trigger Updated",
                description=f"Autoroles will now be assigned: **{trigger_name}**",
                color=0x2ecc71
            )
            if trigger == "on_reaction":
                embed.add_field(name="⚠️ Note", value="Reaction-based autoroles coming in a future update. Currently only `on_join` is fully functional.", inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Error in autoroles trigger command: {e}")
            embed = discord.Embed(
                title="❌ Error",
                description=f"An error occurred: {str(e)}",
                color=0xe74c3c
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    @autoroles.command(name="add", description="Add a role to autoroles")
    @app_commands.describe(role="The role to automatically give to new members")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def autoroles_add(
        self,
        interaction: discord.Interaction,
        role: discord.Role
    ):
        await interaction.response.defer(ephemeral=True)

        try:
            guild_config = self._get_guild_config(interaction.guild.id)
            if role.id not in guild_config["autoroles"]["role_ids"]:
                guild_config["autoroles"]["role_ids"].append(role.id)
                self._save_config()

                embed = discord.Embed(
                    title="➕ Role Added to Autoroles",
                    description=f"{role.mention} has been added to autoroles.",
                    color=0x2ecc71
                )
            else:
                embed = discord.Embed(
                    title="⚠️ Role Already in Autoroles",
                    description=f"{role.mention} is already in autoroles.",
                    color=0xf1c40f
                )
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Error in autoroles add command: {e}")
            embed = discord.Embed(
                title="❌ Error",
                description=f"An error occurred: {str(e)}",
                color=0xe74c3c
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    @autoroles.command(name="remove", description="Remove a role from autoroles")
    @app_commands.describe(role="The role to remove from autoroles")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def autoroles_remove(
        self,
        interaction: discord.Interaction,
        role: discord.Role
    ):
        await interaction.response.defer(ephemeral=True)

        try:
            guild_config = self._get_guild_config(interaction.guild.id)
            if role.id in guild_config["autoroles"]["role_ids"]:
                guild_config["autoroles"]["role_ids"].remove(role.id)
                self._save_config()

                embed = discord.Embed(
                    title="➖ Role Removed from Autoroles",
                    description=f"{role.mention} has been removed from autoroles.",
                    color=0x2ecc71
                )
            else:
                embed = discord.Embed(
                    title="⚠️ Role Not in Autoroles",
                    description=f"{role.mention} is not in autoroles.",
                    color=0xf1c40f
                )
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Error in autoroles remove command: {e}")
            embed = discord.Embed(
                title="❌ Error",
                description=f"An error occurred: {str(e)}",
                color=0xe74c3c
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    @autoroles.command(name="list", description="List all autoroles")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def autoroles_list(
        self,
        interaction: discord.Interaction
    ):
        await interaction.response.defer(ephemeral=True)

        try:
            guild_config = self._get_guild_config(interaction.guild.id)
            role_ids = guild_config["autoroles"]["role_ids"]
            trigger = guild_config["autoroles"].get("trigger", "on_join")

            if not role_ids:
                embed = discord.Embed(
                    title="📋 Autoroles List",
                    description="No autoroles configured.",
                    color=0x95a5a6
                )
            else:
                roles = []
                for role_id in role_ids:
                    role = interaction.guild.get_role(role_id)
                    if role:
                        roles.append(role.mention)

                if roles:
                    embed = discord.Embed(
                        title="📋 Autoroles List",
                        description="\n".join(roles),
                        color=0x3498db
                    )
                else:
                    embed = discord.Embed(
                        title="📋 Autoroles List",
                        description="No valid autoroles found (roles may have been deleted).",
                        color=0x95a5a6
                    )
            
            embed.add_field(name="Trigger", value=trigger.replace("_", " ").title(), inline=True)
            embed.add_field(name="Enabled", value=str(guild_config["autoroles"]["enabled"]), inline=True)
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Error in autoroles list command: {e}")
            embed = discord.Embed(
                title="❌ Error",
                description=f"An error occurred: {str(e)}",
                color=0xe74c3c
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    # Prefix Command (now a sub-command of setup)
    @setup.command(name="prefix", description="Set or view the bot prefix")
    @app_commands.describe(prefix="The new prefix to set (leave empty to view current)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def prefix(
        self,
        interaction: discord.Interaction,
        prefix: Optional[str] = None
    ):
        await interaction.response.defer(ephemeral=True)

        try:
            guild_config = self._get_guild_config(interaction.guild.id)

            if prefix is None:
                # View current prefix
                current_prefix = guild_config.get("prefix", "!")
                embed = discord.Embed(
                    title="🔤 Current Prefix",
                    description=f"The current prefix is: `{current_prefix}`",
                    color=0x3498db
                )
            else:
                # Set new prefix
                if len(prefix) > 10:
                    embed = discord.Embed(
                        title="❌ Error",
                        description="Prefix must be 10 characters or less.",
                        color=0xe74c3c
                    )
                    await interaction.followup.send(embed=embed, ephemeral=True)
                    return

                guild_config["prefix"] = prefix
                self._save_config()

                embed = discord.Embed(
                    title="🔤 Prefix Updated",
                    description=f"The prefix has been updated to: `{prefix}`",
                    color=0x2ecc71
                )

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Error in prefix command: {e}")
            embed = discord.Embed(
                title="❌ Error",
                description=f"An error occurred: {str(e)}",
                color=0xe74c3c
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    # AI Settings Group (nested under setup)
    ai_settings = app_commands.Group(
        name="ai",
        description="Configure AI settings",
        parent=setup,
        default_permissions=discord.Permissions(administrator=True),
    )

    @ai_settings.command(name="model", description="Set the default AI model for this server")
    @app_commands.describe(model="The AI model to use as default (leave empty to view current)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def ai_model(
        self,
        interaction: discord.Interaction,
        model: Optional[str] = None
    ):
        await interaction.response.defer(ephemeral=True)

        try:
            guild_config = self._get_guild_config(interaction.guild.id)

            if model is None:
                # View current model
                current_model = guild_config.get("ai_settings", {}).get("default_model", "Not set")
                embed = discord.Embed(
                    title="🤖 Current AI Model",
                    description=f"The current default AI model is: `{current_model}`",
                    color=0x3498db
                )
            else:
                # Set new model
                if "ai_settings" not in guild_config:
                    guild_config["ai_settings"] = {}
                guild_config["ai_settings"]["default_model"] = model
                self._save_config()

                embed = discord.Embed(
                    title="🤖 AI Model Updated",
                    description=f"The default AI model has been updated to: `{model}`",
                    color=0x2ecc71
                )

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Error in AI model command: {e}")
            embed = discord.Embed(
                title="❌ Error",
                description=f"An error occurred: {str(e)}",
                color=0xe74c3c
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    @ai_settings.command(name="temperature", description="Set the AI temperature (creativity)")
    @app_commands.describe(temperature="The temperature value (0.0 to 2.0)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def ai_temperature(
        self,
        interaction: discord.Interaction,
        temperature: float
    ):
        await interaction.response.defer(ephemeral=True)

        try:
            # Validate temperature
            if temperature < 0.0 or temperature > 2.0:
                embed = discord.Embed(
                    title="❌ Error",
                    description="Temperature must be between 0.0 and 2.0.",
                    color=0xe74c3c
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

            guild_config = self._get_guild_config(interaction.guild.id)
            if "ai_settings" not in guild_config:
                guild_config["ai_settings"] = {}
            guild_config["ai_settings"]["temperature"] = temperature
            self._save_config()

            embed = discord.Embed(
                title="🌡️ AI Temperature Updated",
                description=f"The AI temperature has been updated to: {temperature}",
                color=0x2ecc71
            )

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Error in AI temperature command: {e}")
            embed = discord.Embed(
                title="❌ Error",
                description=f"An error occurred: {str(e)}",
                color=0xe74c3c
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    @ai_settings.command(name="pool-edit", description="Edit a model pool by selecting models from available providers")
    @app_commands.describe(
        pool="The pool to edit (text, code, reasoning, creative, image, audio)",
        provider="The provider to fetch models from (leave empty for OpenRouter free models)"
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def ai_pool_edit(
        self,
        interaction: discord.Interaction,
        pool: str,
        provider: Optional[str] = None
    ):
        pool = pool.lower()
        valid_pools = ["text", "code", "reason", "reasoning", "creative", "image", "audio", "default"]
        if pool not in valid_pools:
            await interaction.response.send_message(
                f"❌ Invalid pool. Choose from: {', '.join(valid_pools)}",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        service = self.bot.openrouter_service

        # Fetch models
        models = []
        if provider:
            provider = provider.strip().lower()
            providers = service.api_providers
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
                            models = [m for m in data.get("data", []) if isinstance(m, dict) and "id" in m]
                        else:
                            await interaction.followup.send(
                                f"❌ Failed to fetch models from `{provider}`: HTTP {resp.status}",
                                ephemeral=True
                            )
                            return
            except Exception as e:
                await interaction.followup.send(
                    f"❌ Error fetching models: {str(e)[:300]}",
                    ephemeral=True
                )
                return
        else:
            # Use OpenRouter free models
            try:
                import aiohttp
                headers = {"Authorization": f"Bearer {service.api_key}"}
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        "https://openrouter.ai/api/v1/models",
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=30)
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            models = [m for m in data.get("data", []) if isinstance(m, dict) and "id" in m and m["id"].endswith(":free")]
                        else:
                            await interaction.followup.send(
                                f"❌ Failed to fetch OpenRouter models: HTTP {resp.status}",
                                ephemeral=True
                            )
                            return
            except Exception as e:
                await interaction.followup.send(
                    f"❌ Error fetching OpenRouter models: {str(e)[:300]}",
                    ephemeral=True
                )
                return

        if not models:
            await interaction.followup.send(
                "⚠️ No models found. Try a different provider or check your API key.",
                ephemeral=True
            )
            return

        # Show model selection view
        view = PoolModelSelectView(
            service=service,
            pool_name=pool,
            models=models,
            user_id=interaction.user.id,
            user_name=str(interaction.user),
        )

        embed = discord.Embed(
            title=f"📦 Edit `{pool}` Pool",
            description=f"Select models to add to the **{pool}** pool. Current models will be replaced.\n\nFound {len(models)} models from `{provider or 'OpenRouter (free)'}`:",
            color=0x3498db
        )

        await interaction.followup.send(
            embed=embed,
            view=view,
            ephemeral=True
        )

    async def _send_log_event(self, guild: discord.Guild, event: str, title: str, description: str, fields=None):
        logs_config = self._get_guild_config(guild.id).get("logs", {})
        if not logs_config.get("enabled", False) or not logs_config.get("events", {}).get(event, False):
            return

        channel_id = logs_config.get("channel_id")
        channel = guild.get_channel(channel_id) if channel_id else None
        bot_member = guild.me
        if not channel or not bot_member or not channel.permissions_for(bot_member).send_messages:
            return

        embed = discord.Embed(
            title=title,
            description=description[:4000],
            color=0x5865f2,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        for name, value in fields or []:
            embed.add_field(name=name, value=(value or "[empty]")[:1024], inline=False)
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as error:
            logger.warning("Could not send %s log in guild %s: %s", event, guild.id, error)

    # Events
    @commands.Cog.listener()
    async def on_member_join(self, member):
        """Assign autoroles and send welcome message when a member joins"""
        try:
            guild_config = self._get_guild_config(member.guild.id)
            await self._send_log_event(
                member.guild,
                "member_join",
                "Member Joined",
                f"{member.mention} (`{member.id}`) joined the server.",
            )
            
            # Handle autoroles
            autoroles_config = guild_config.get("autoroles", {})
            if autoroles_config.get("enabled", False) and autoroles_config.get("trigger", "on_join") == "on_join":
                role_ids = autoroles_config.get("role_ids", [])
                for role_id in role_ids:
                    role = member.guild.get_role(role_id)
                    if role and role < member.guild.me.top_role:
                        try:
                            await member.add_roles(role, reason="Autorole on join")
                        except Exception as e:
                            logger.error(f"Failed to assign autorole {role.name}: {e}")
            
            # Handle welcome message
            welcome_config = guild_config.get("welcome", {})
            if not welcome_config.get("enabled", False):
                return

            message = welcome_config.get("message", "")
            if not message:
                return

            channel_id = welcome_config.get("channel_id")
            if not channel_id:
                # Try to find a suitable channel (system channel or first text channel)
                channel = member.guild.system_channel
                if not channel:
                    for ch in member.guild.text_channels:
                        if ch.permissions_for(member.guild.me).send_messages:
                            channel = ch
                            break
                if not channel:
                    return
            else:
                channel = self.bot.get_channel(channel_id)
                if not channel or not channel.permissions_for(member.guild.me).send_messages:
                    return

            # Replace placeholders
            formatted_message = message.replace("{user}", member.mention).replace("{server}", member.guild.name)

            await channel.send(formatted_message)

        except Exception as e:
            logger.error(f"Error in on_member_join: {e}")

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        """Log when a member leaves"""
        try:
            await self._send_log_event(
                member.guild,
                "member_leave",
                "Member Left",
                f"{member.mention} (`{member.id}`) left the server.",
            )
        except Exception as e:
            logger.error(f"Error logging member leave: {e}")

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload):
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        message = payload.cached_message
        author = f"{message.author.mention} (`{message.author.id}`)" if message else "Author unavailable (message was not cached)"
        channel = guild.get_channel(payload.channel_id)
        channel_name = channel.mention if channel else f"channel `{payload.channel_id}`"
        content = message.content if message and message.content else "Message text unavailable"
        await self._send_log_event(
            guild,
            "message_delete",
            "Message Deleted",
            f"A message by {author} was deleted in {channel_name}.",
            [("Content", content)],
        )

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload):
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        message = payload.cached_message
        before_content = message.content if message else "Previous text unavailable (message was not cached)"
        after_content = payload.data.get("content", "Updated content unavailable")
        if message and before_content == after_content:
            return
        author = f"{message.author.mention} (`{message.author.id}`)" if message else "Author unavailable"
        channel = guild.get_channel(payload.channel_id)
        channel_name = channel.mention if channel else f"channel `{payload.channel_id}`"
        await self._send_log_event(
            guild,
            "message_edit",
            "Message Edited",
            f"A message by {author} was edited in {channel_name}.",
            [("Before", before_content), ("After", after_content)],
        )

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        changes = []
        if before.nick != after.nick:
            changes.append(("Nickname", f"{before.nick or before.name} -> {after.nick or after.name}"))
        before_roles = {role.id: role.name for role in before.roles}
        after_roles = {role.id: role.name for role in after.roles}
        added = [name for role_id, name in after_roles.items() if role_id not in before_roles]
        removed = [name for role_id, name in before_roles.items() if role_id not in after_roles]
        if added:
            changes.append(("Roles added", ", ".join(added)))
        if removed:
            changes.append(("Roles removed", ", ".join(removed)))
        if changes:
            await self._send_log_event(
                after.guild,
                "member_update",
                "Member Updated",
                f"{after.mention} (`{after.id}`) profile or roles changed.",
                changes,
            )

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user):
        await self._send_log_event(guild, "member_ban", "Member Banned", f"{user} (`{user.id}`) was banned.")

    @commands.Cog.listener()
    async def on_member_unban(self, guild, user):
        await self._send_log_event(guild, "member_unban", "Member Unbanned", f"{user} (`{user.id}`) was unbanned.")

    @commands.Cog.listener()
    async def on_guild_role_create(self, role):
        await self._send_log_event(role.guild, "role_create", "Role Created", f"{role.mention} (`{role.id}`) was created.")

    @commands.Cog.listener()
    async def on_guild_role_update(self, before, after):
        if before.name != after.name or before.color != after.color:
            await self._send_log_event(
                after.guild,
                "role_update",
                "Role Updated",
                f"Role `{before.name}` (`{after.id}`) changed to `{after.name}`.",
            )

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role):
        await self._send_log_event(role.guild, "role_delete", "Role Deleted", f"Role `{role.name}` (`{role.id}`) was deleted.")

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        await self._send_log_event(channel.guild, "channel_create", "Channel Created", f"`{channel.name}` (`{channel.id}`) was created.")

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        if before.name != after.name or before.category_id != after.category_id:
            await self._send_log_event(
                after.guild,
                "channel_update",
                "Channel Updated",
                f"Channel `{before.name}` (`{after.id}`) changed to `{after.name}`.",
            )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        await self._send_log_event(channel.guild, "channel_delete", "Channel Deleted", f"`{channel.name}` (`{channel.id}`) was deleted.")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if before.channel == after.channel:
            return
        if before.channel is None and after.channel is not None:
            event, change = "voice_join", f"joined {after.channel.mention}"
        elif after.channel is None and before.channel is not None:
            event, change = "voice_leave", f"left {before.channel.mention}"
        else:
            event, change = "voice_move", f"moved from {before.channel.mention} to {after.channel.mention}"
        await self._send_log_event(
            member.guild,
            event,
            "Voice Channel Activity",
            f"{member.mention} (`{member.id}`) {change}.",
        )


async def setup(bot):
    await bot.add_cog(SetupCog(bot))