# Infinity Lounge Bot

A Discord bot with AI chat, image/audio generation, moderation tools, and flexible API provider support — all running on free models.

---

## ⚡️ Quick Start

```bash
pip install -r requirements.txt
```

Create a `.env` file in the project root:

```ini
DISCORD_TOKEN=your_discord_bot_token
OPENROUTER_API_KEY=your_openrouter_api_key
```

Run:

```bash
python3 bot.py
```

**Invite link** (requires `Bot` + `applications.commands` scopes with `Administrator` permissions):

```
https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&permissions=8&scope=bot+applications.commands
```

---

## 🤖 AI Commands

All commands are slash-based and work in any server channel.

| Command | What It Does |
|---|---|
| `/ask <question>` | Ask a general question — responds in a private numbered AI channel (`ai-chat-1`, `ai-chat-2`, etc.) |
| `/chat <message>` | Send a message to an existing AI chat channel |
| `/converse <message>` | Continue a conversation with history (per-user sessions) |
| `/code <prompt>` | Code generation & analysis |
| `/reason <prompt>` | Deep reasoning tasks |
| `/creative <prompt>` | Creative writing, brainstorming |
| `/vision <attachment>` | Analyze an uploaded image |
| `/image <prompt>` | Generate an image (uses image model pool) |
| `/audio <text>` | Text-to-speech (`voice:` parameter for custom voices) |

**Optional parameters:**
- `model:` — pick a specific model instead of the pool's random selection
- `voice:` — `/audio` only: set a custom voice ID (e.g., for Fish Audio)

Examples:
```
/ask What's the weather like on Mars?
/chat That's interesting, tell me more
/code Write a Python function to reverse a linked list
/audio Hello, this is a test voice:b347db033a6549378b48d00acb0d06cd
/image A cyberpunk cityscape model:nvidia::your-flux-model
```

---

## 💬 Session Commands

| Command | Description |
|---|---|
| `/ai-sessions` | List your active and archived conversations |
| `/ai-chat-reopen <session_id>` | Reopen an archived session |
| `/set-model <model_id>` | Set a per-channel model override |
| `/channel-model` | Show which model your current AI channel is using |

Sessions live in private numbered channels and auto-prune after 30 days of inactivity. You only see your own sessions.

---

## 🧠 Model Pools

The bot maintains separate pools of free models for different tasks. Each request picks one **at random** from the appropriate pool. If a model fails, it falls back to the next.

| Pool | Models |
|---|---|
| text | `liquid/lfm-2.5-2.6b:free`, `nvidia/nemotron-3.5-lightning:free`, `nvidia/nemotron-3.5-content-safety:free`, `nvidia/nemotron-3.5-super-vl:free` |
| code | `cohere/north-mini-code:free` |
| reason | `thinkingmachines/inkling-small:free`, `thinkingmachines/inkling:free`, `nvidia/nemotron-3-ultra-550b-a55b:free`, `nvidia/nemotron-3-super-120b-a12b:free` |
| creative | `google/gemma-4-26b-a4b-it:free`, `google/gemma-4-31b-it:free` |
| image | _none currently available_ |
| audio | `fish-audio/s2.1-pro-free:free` |

### Model Commands

| Command | What It Does |
|---|---|
| `/models` | List all pools and configured providers |
| `/models provider:<alias> pool:<pool>` | Filter by provider or pool |
| `/models-refresh` | **(Admin)** Re-fetch free models. New ones are appended, existing are kept |
| `/models-check` | **(Admin)** Test up to 5 text models with a short prompt |

> 🔄 **Daily auto-refresh:** the bot fetches the latest OpenRouter free model catalog every 24 hours — removing expired models and appending newly-available ones.

---

## 🔧 Provider Presets

When you add a provider via `/setup providers add`, pick from presets that auto-fill the API URL.

| Preset | Base URL | Example Model Format |
|---|---|---|
| 🟢 OpenAI | `https://api.openai.com/v1` | `openai::gpt-4o` |
| 🟠 Anthropic Claude | `https://api.anthropic.com/v1` | `claude::claude-3-5-sonnet-20241022` |
| 🔵 Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini::gemini-2.0-flash` |
| 🌐 OpenRouter | `https://openrouter.ai/api/v1` | `openrouter::nvidia/nemotron-3-ultra-550b-a55b:free` |
| ⚫ Nvidia NIM | `https://integrate.api.nvidia.com/v1` | `nvidia::nvidia/foundational-llm` |
| ⚪ Mistral | `https://api.mistral.ai/v1` | `mistral::mistral-large-2` |
| 🟣 Groq | `https://api.groq.com/openai/v1` | `groq::llama3-70b-8192` |
| 🔷 Cerebras | `https://api.cerebras.ai/v1` | `cerebras::llama3.1-70b` |
| 🟣 Together AI | `https://api.together.xyz/v1` | `together::meta-llama-3-70b` |
| 🤗 HuggingFace | `https://api-inference.huggingface.co/v1` | `hf::meta-llama-3-70b` |
| ⚙️ Custom | _type your own_ | |

If `/models` returns nothing, you get a manual entry option (type model IDs directly) or a "save without models" option.

Use added providers with the `alias::model-id` format:
```
/ask Hello model:nvidia::meta/llama-3.1-8b-instruct
```

---

## 🔨 Setup Commands

### Wizard

| Command | Description |
|---|---|
| `/setup all` | Run the full guided wizard (welcome, logs, roles, AI channels, permissions) |

### Sections

| Command | Description |
|---|---|
| `/setup welcome` | Configure welcome messages |
| `/setup logs` | Set up moderation & audit log channels |
| `/setup roles` | Configure autoroles |
| `/setup prefix <prefix>` | Change the bot prefix |
| `/setup ai` | Enable & configure private AI chat channels |
| `/setup permissions` | Assign **Moderator** / **Senior Moderator** roles |

### Provider Management

| Command | Permission | Description |
|---|---|---|
| `/setup providers add` | Anyone | Pick a preset → enter your API key → select models to import |
| `/setup providers list` | Anyone | List provider aliases + URLs |
| `/setup providers remove` | **Senior Moderator only** | Remove a provider + its stored key |

All provider actions are logged to `api_audit.log` and to your Discord log channel. **API keys are never displayed or logged.**

### Model Pool Editing

| Command | Description |
|---|---|
| `/setup ai pool-edit pool:<pool> [provider:<alias>]` | Pick a pool and select up to 24 models from any provider (current models are kept, new ones are appended) |

---

## 🛡️ Moderation

Requires **Moderator** or **Senior Moderator** roles.

| Level | Commands |
|---|---|
| Moderator | `/kick`, `/mute`, `/unmute`, `/warn`, `/warnings`, `/clear` |
| Senior Moderator | All Moderator commands **+** `/ban`, `/unban`, `/slowmode`, `/lock`, `/unlock` |

Permission errors are specific — no more "application did not respond":

```
❌ You need Senior Moderator permissions to use this command.
This command requires: Ban Members, Manage Messages, Manage Channels.
Ask an admin to give you the Senior Moderator role via /setup permissions.
```

---

## 🔒 Security & Privacy

- API keys stored in `api_providers.json` (auto chmod 600) — never shown in Discord or in logs
- Every provider add/remove logged to `api_audit.log` with alias + URL + actor (**no key**)
- API key add: any guild member | API key remove: **Senior Moderator only**
- AI sessions are private — you only see your own
- Sessions auto-prune after 30 days

---

## ⚙️ Configuration

**`.env`** (required):
```ini
DISCORD_TOKEN=your_discord_bot_token
OPENROUTER_API_KEY=your_openrouter_api_key
```

**Discord Developer Portal** — enable these intents under Bot → Privileged Gateway Intents:
- Message Content Intent
- Server Members Intent
- AutoMod execution & related intents

### Key Files

| File | Purpose |
|---|---|
| `bot.py` | Main bot + `OpenRouterService` (model pools, providers, catalog refresh) |
| `config.py` | Model pools, colors, endpoints |
| `cogs/` | Command modules (`ai.py`, `chat.py`, `moderator.py`, `setup.py`, `image.py`, etc.) |
| `services/ai_sessions.py` | SQLite session storage for conversations |
| `update_model_pools.py` | Standalone model pool updater |
| `api_audit.log` | Audit trail for API key actions |
| `model_pool_overrides.json` | Per-pool overrides from `/setup ai pool-edit` |

---

## 📦 Requirements

```
discord.py >= 2.3.2
python-dotenv
aiohttp
requests
```

Install:
```bash
pip install -r requirements.txt
```

---

## 🆘 Quick Help

| If you want to... | Run |
|---|---|
| See all models | `/models` |
| Add your own provider | `/setup providers add` |
| Build a custom pool | `/setup ai pool-edit pool:text` |
| Change model in one channel | `/set-model` |
| Check your sessions | `/ai-sessions` |
| Check your permissions | `/setup permissions list` |

---

*Made with ❤️ for the Infinity Lounge community. Free models only — no paid tiers.*