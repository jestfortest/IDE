import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

DB_PATH = Path(__file__).parent / "bot.db"
ROUTER_MODEL = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"
TASK_CLASSIFIER_MODEL = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"

DEFAULT_TEXT_POOL = [
    "nvidia/nemotron-3.5-super-vl:free",
    "liquid/lfm-2.5-2.6b:free",
    "nvidia/nemotron-3.5-lightning:free",
    "nvidia/nemotron-3.5-content-safety:free",
    "xingchen-agi/xing4.0-29b-a4b:free",
    "minimax/minimax-m3.1-flash:free",
    "recursive/flint-v1.0:free"
    ]

DEFAULT_CODE_POOL = [
    "cohere/north-mini-code:free"
    ]

DEFAULT_REASONING_POOL = [
    "thinkingmachines/inkling-small:free",
    "thinkingmachines/inkling:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "nvidia/nemotron-3-super-120b-a12b:free"
    ]

DEFAULT_CREATIVE_POOL = [
    "google/gemma-4-26b-a4b-it:free",
    "google/gemma-4-31b-it:free"
    ]

DEFAULT_IMAGE_GEN_POOL = [
    
    ]

DEFAULT_AUDIO_GEN_POOL = [
    "fish-audio/s2.1-pro-free:free"
    ]

DEFAULT_MODEL_POOL = DEFAULT_TEXT_POOL

MODEL_POOLS = {
    "default": DEFAULT_TEXT_POOL,
    "text": DEFAULT_TEXT_POOL,
    "code": DEFAULT_CODE_POOL,
    "reason": DEFAULT_REASONING_POOL,
    "creative": DEFAULT_CREATIVE_POOL,
    "image": DEFAULT_IMAGE_GEN_POOL,
    "audio": DEFAULT_AUDIO_GEN_POOL,
}

EXCLUDED_PROVIDERS = {"poolside", "dots", "space-bunny-alpha"}

EMBED_COLORS = {
    "success": 0x2ecc71,
    "error": 0xe74c3c,
    "warning": 0xf39c12,
    "info": 0x3498db,
}