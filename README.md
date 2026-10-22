# Infinity Lounge Bot

A Discord bot powered by OpenRouter with AI chat, image/audio generation, moderation tools, and flexible API provider support.

## ⭐ Quick Start

```
# 1. Install dependencies
pip install -r requirements.txt

# 2. Create .env file
echo 'DISCORD_TOKEN=your_discord_bot_token
OPENROUTER_API_KEY=your_openrouter_api_key' > .env

# 3. Run the bot
python3 bot.py
```

Invite link (requires `Bot` and `applications.commands` scopes with `Administrator` permissions):
```
https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&permissions=8&scope=bot+applications.commands
```

## 🤖 AI Commands (Slash Commands)

| Command | Description |
|---------|-------------|
| `/chat <message>` | General AI chat (text pool) |
| `/code <prompt>` | Code generation & analysis |
| `/reason <prompt>` | Deep reasoning tasks |
| `/creative <prompt>` | Creative writing & brainstorming |
| `/vision <attachment>` | Image analysis (upload an image) |
| `/image <prompt>` | Generate images (currently no free models available — contact staff) |
| `/audio <text>` | Text-to-speech with voice support |

**Optional parameters:**
- `model:` — Use a specific model instead of the pool's random selection
- `provider:` — Filter by provider when listing models

Examples:
```
/chat What's the weather like on Mars?
/code Write a Python function to reverse a linked list
/audio Hello, this is a test voice:b347db033a6549378b48d00acb0d06cd
/image A cyberpunk cityscape model:nvidia::your-flux-model
```

## 🤖 AI Session Commands

| Command | Description |
|---------|-------------|
| `/ask <question>` | Ask the AI in a private numbered channel |
| `/converse <message>` | Continue a conversation with history |
| `/ai-sessions` | List all your active & archived sessions |
| `/ai-chat-reopen <id>` | Reopen an archived conversation |
| `/set-model <model>` | Set the model for this specific AI chat channel |
| `/channel-model` | Check which model your channel is using |
| `/models` | List all available model pools |
| `/models-refresh` | (Admin) Refresh model pools from OpenRouter |
| `/models-check` | (Admin) Test up to 5 text models |

## 🛠️ Moderation Commands

Requires **Moderator** or **Senior Moderator** roles (configured by staff):

| Permission Level | Commands |
|-----------------|----------|
| **Moderator** | `/kick`, `/mute`, `/unmute`, `/warn`, `/warnings`, `/clear` |
| **Senior Moderator** | All moderator commands + `/ban`, `/unban`, `/slowmode`, `/lock`, `/unlock` |

## ⚙️ Setup Commands

| Command | Description |
|---------|-------------|
| `/setup all` | Run the full guided setup wizard |
| `/setup welcome` | Configure welcome messages |
| `/setup logs` | Set up moderation/logging channels |
| `/setup roles` | Configure autoroles |
| `/setup prefix` | Change the bot's command prefix |
| `/setup ai` | Enable & configure AI chat channels |
| `/setup permissions` | Assign Moderator / Senior Moderator roles |

### Provider Management (`/setup providers`)

| Command | Permission | Description |
|---------|-----------|-------------|
| `add` | Anyone | Add an OpenAI-compatible API provider. Pick from presets (OpenAI, Claude, Gemini, OpenRouter, Nvidia NIM, etc.) then auto-fetch models to select what you want to import |
| `list` | Anyone | List configured provider aliases + URLs |
| `remove` | **Senior Moderator only** | Remove a provider and its stored key |

All provider actions (add/remove) are logged to `api_audit.log` and to your configured log channel. **API keys are never displayed or written to logs.**

### Model Pool Editing

| Command | Description |
|---------|-------------|
| `/setup ai pool-edit` | Pick a pool (text/code/reason/creative/image/audio) and select models from any provider — current models are kept, new ones are appended |

## 💡 Model Notes

### Current Pools
- **Text (4):** nvidia nemotron-3.5 series, liquid/lfm-2.5
- **Code (1):** cohere/north-mini-code
- **Reasoning (5):** thinkingmachines inkling series, nvidia nemotron-3 series
- **Creative (2):** google/gemma-4 series
- **Image (0):** No free image models currently on OpenRouter
- **Audio (1):** fish-audio/s2.1-pro-free (TTS endpoint)

### Daily Auto-Refresh
Every 24 hours the bot:
1. Fetches the latest OpenRouter free model catalog
2. Removes expired/deprecated models from pools
3. Randomly appends newly-discovered free models to existing pools
4. Updates all pools in `config.py`

Use `/models` to see what's available and `/models-refresh` to trigger it manually.

## 📁 Project Structure

```
bot.py                          # Main bot entry + OpenRouterService
config.py                       # Model pools, colors, API endpoints
requirements.txt                # discord.py, aiohttp, python-dotenv
update_model_pools.py           # Standalone model pool updater
api_audit.log                   # Audit log for API key actions
excluded_providers.json         # Dynamic excluded provider list
model_pool_overrides.json       # Per-pool model overrides
cogs/
  ├── ai.py                     # AI commands, sessions, model pools
  ├── chat.py                   # /chat general conversation
  ├── code.py                   # /code generation
  ├── reason.py                 # /reason reasoning
  ├── creative.py               # /creative writing
  ├── vision.py                 # /vision image analysis
  ├── image.py                  # /image generation
  ├── audio.py                  # /audio / TTS generation
  ├── moderator.py              # Moderation commands (kick/ban/mute/warn/etc)
  └── setup.py                  # Setup wizard, permissions, provider management
services/
  └── ai_sessions.py            # SQLite session store for conversations
```

## 🛡️ Security & Privacy

- API keys stored in `api_providers.json` (chmod 600) — never displayed in Discord or logs
- All add/remove API key actions logged to `api_audit.log` with actor info
- API key add: available to all guild members
- API key remove: **Senior Moderator only**
- AI sessions are private — users only see their own sessions
- Sessions auto-prune after 30 days of inactivity

## 🔧 Configuration

### `.env` (required)
```ini
DISCORD_TOKEN=your_discord_bot_token
OPENROUTER_API_KEY=your_openrouter_api_key
```

### Required Discord Intents
- Message Content Intent ✅
- Server Members Intent ✅
- AutoMod-related intents ✅

Enable these in the Discord Developer Portal under Bot → Privileged Gateway Intents.

## 📋 Requirements

```
discord.py>=2.3.2
python-dotenv
aiohttp
requests
```

## ❓ Need Help?

- Use `/models` to list all available models
- Use `/setup providers add` to configure custom providers
- Use `/setup ai pool-edit` to build your own model pools
- Use `/set-model` to change models per AI chat channel
- Use `/ai model` to set a server-wide default model

---

*Built with discord.py 2.x + OpenRouter API. Free models only.*