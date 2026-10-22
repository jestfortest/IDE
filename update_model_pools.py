#!/usr/bin/env python3
"""Auto-fetch and update model pools from OpenRouter API.

This script:
1. Fetches models from OpenRouter API
2. Filters only FREE models (those with :free suffix)
3. Categorizes them into appropriate pools (text, code, reasoning, creative, image, audio)
4. Updates config.py with the new model pools
5. Can be run manually or scheduled via cron

Run: python3 update_model_pools.py
"""

import json
import re
import sys
from pathlib import Path

# OpenRouter API endpoint
API_URL = "https://openrouter.ai/api/v1/models"
CONFIG_PATH = Path("/Users/aidenchang/Documents/important/projects/Discord Bots/Infinity_lounge_bot/config.py")

# Path to excluded providers config (can be updated without code changes)
EXCLUDED_PROVIDERS_CONFIG = Path(__file__).parent / "excluded_providers.json"


def load_excluded_providers():
    """Load excluded providers from config file, with defaults as fallback."""
    default_providers = {"poolside", "dots", "dots-studio", "space-bunny-alpha", "apodex"}
    try:
        if EXCLUDED_PROVIDERS_CONFIG.exists():
            with open(EXCLUDED_PROVIDERS_CONFIG, "r") as f:
                data = json.load(f)
                providers = set(data.get("excluded_providers", []))
                print(f"Loaded {len(providers)} excluded providers from config")
                return providers
        else:
            # Create default config file
            save_excluded_providers(default_providers)
            return default_providers
    except Exception as e:
        print(f"Warning: Failed to load excluded providers config: {e}")
        return default_providers


def save_excluded_providers(providers):
    """Save excluded providers to config file."""
    try:
        with open(EXCLUDED_PROVIDERS_CONFIG, "w") as f:
            json.dump({"excluded_providers": sorted(list(providers))}, f, indent=2)
        print(f"Saved {len(providers)} excluded providers to config")
    except Exception as e:
        print(f"Warning: Failed to save excluded providers config: {e}")


def detect_problematic_providers(models):
    """Analyze models to detect potentially problematic providers."""
    # Heuristic: providers with many models that fail or have issues
    # This is a placeholder for future heuristic-based detection
    # For now, return empty set
    return set()


def fetch_models():
    """Fetch models from OpenRouter API."""
    import requests
    
    print("Fetching models from OpenRouter API...")
    
    for attempt in range(3):
        try:
            resp = requests.get(API_URL, timeout=30)
            if resp.status_code != 200:
                print(f"Error: OpenRouter returned status {resp.status_code}")
                print(resp.text[:500])
                return None
            
            data = resp.json()
            models = data.get("data", [])
            print(f"Found {len(models)} total models")
            return models
        except Exception as e:
            print(f"Attempt {attempt + 1} failed: {e}")
            if attempt < 2:
                import time
                time.sleep(5)
    
    print("All fetch attempts failed.")
    return None


def is_free_model(model_id):
    """Check if a model is free (has :free suffix)."""
    return model_id.endswith(":free")


def is_excluded_provider(model_id, excluded_providers):
    """Check if a model is from an excluded provider."""
    provider = model_id.split("/")[0].lower()
    return provider in excluded_providers


def filter_free_models(models, excluded_providers=None):
    """Filter models to only include free ones, excluding excluded providers."""
    if excluded_providers is None:
        excluded_providers = load_excluded_providers()
    free_models = []
    for model in models:
        model_id = model.get("id", "")
        if is_free_model(model_id) and not is_excluded_provider(model_id, excluded_providers):
            free_models.append(model_id)
    
    # Remove duplicates while preserving order
    seen = set()
    unique_free = []
    for m in free_models:
        if m not in seen:
            seen.add(m)
            unique_free.append(m)
    
    return unique_free


def categorize_models(free_models):
    """Categorize free models into their appropriate pools."""
    
    # Keywords for each pool
    code_keywords = ["code", "codestral", "coder", "qwen/qwen3", "deepseek", "mistralai/", "qwen/qwen3-8b", "qwen/qwen3-coder"]
    reasoning_keywords = ["nemotron-3-ultra", "nemotron-3-super", "nemotron-3-ultra-550b", "nemotron-3-super-120b", "reasoning", "thinking"]
    creative_keywords = ["gemini", "gemma", "creative", "writing", "nemotron-3-ultra"]
    image_keywords = ["ming", "image", "dalle", "stable-diffusion", "flux", "midjourney", "sdxl", "playground"]
    audio_keywords = ["fish", "s2.", "audio", "speech", "tts", "whisper", "elevenlabs"]
    
    text_pool = []
    code_pool = []
    reasoning_pool = []
    creative_pool = []
    image_pool = []
    audio_pool = []
    
    for model in free_models:
        model_lower = model.lower()
        
        # Check for code models first
        if any(kw in model_lower for kw in code_keywords):
            code_pool.append(model)
        elif any(kw in model_lower for kw in reasoning_keywords):
            reasoning_pool.append(model)
        elif any(kw in model_lower for kw in creative_keywords):
            creative_pool.append(model)
        elif any(kw in model_lower for kw in image_keywords):
            image_pool.append(model)
        elif any(kw in model_lower for kw in audio_keywords):
            audio_pool.append(model)
        else:
            # Default to text pool
            pass  # Will add to text_pool at the end
    
    # All remaining models go to text pool
    text_pool = [m for m in free_models if m not in code_pool and m not in reasoning_pool and m not in creative_pool and m not in image_pool and m not in audio_pool]
    
    # Do NOT force pools to have models - empty pools should stay empty
    # This allows the bot to show a proper "no models available" message
    
    return {
        "text": text_pool,
        "code": code_pool,
        "reasoning": reasoning_pool,
        "creative": creative_pool,
        "image": image_pool,
        "audio": audio_pool,
    }


def read_current_pools(config_path):
    """Parse the current pool model lists from config.py."""
    with open(config_path, "r") as f:
        content = f.read()
    pools = {}
    var_names = {
        "text": "DEFAULT_TEXT_POOL",
        "code": "DEFAULT_CODE_POOL",
        "reasoning": "DEFAULT_REASONING_POOL",
        "creative": "DEFAULT_CREATIVE_POOL",
        "image": "DEFAULT_IMAGE_GEN_POOL",
        "audio": "DEFAULT_AUDIO_GEN_POOL",
    }
    for pool_name, var in var_names.items():
        match = re.search(rf"{var}\s*=\s*\[(.*?)\]", content, re.DOTALL)
        pools[pool_name] = re.findall(r'"([^"]+)"', match.group(1)) if match else []
    return pools


def merge_pools(current_pools, discovered_pools):
    """Keep all existing pool models, and randomly append newly discovered ones.

    Existing models are never removed here (the bot's deprecation check handles
    expired ones). New models found in the catalog get shuffled into their pool.
    """
    import random
    merged = {}
    for pool_name, discovered in discovered_pools.items():
        existing = list(current_pools.get(pool_name, []))
        new_models = [m for m in discovered if m not in existing]
        if new_models:
            random.shuffle(new_models)
            existing.extend(new_models)
            print(f"  + {pool_name}: {len(new_models)} new model(s) appended")
        merged[pool_name] = existing
    return merged


def update_config(config_path, pools):
    """Update config.py with new model pools."""
    
    # Read current config
    with open(config_path, "r") as f:
        content = f.read()
    
    # Helper to format pool array
    def format_pool(pool_name, models):
        pool_str = ",\n    ".join([f'"{m}"' for m in models])
        return f"{pool_name} = [\n    {pool_str}\n    ]"
    
    # Update each pool
    content = re.sub(
        r"DEFAULT_TEXT_POOL = \[.*?\]",
        format_pool("DEFAULT_TEXT_POOL", pools["text"]),
        content,
        flags=re.DOTALL
    )
    
    content = re.sub(
        r"DEFAULT_CODE_POOL = \[.*?\]",
        format_pool("DEFAULT_CODE_POOL", pools["code"]),
        content,
        flags=re.DOTALL
    )
    
    content = re.sub(
        r"DEFAULT_REASONING_POOL = \[.*?\]",
        format_pool("DEFAULT_REASONING_POOL", pools["reasoning"]),
        content,
        flags=re.DOTALL
    )
    
    content = re.sub(
        r"DEFAULT_CREATIVE_POOL = \[.*?\]",
        format_pool("DEFAULT_CREATIVE_POOL", pools["creative"]),
        content,
        flags=re.DOTALL
    )
    
    content = re.sub(
        r"DEFAULT_IMAGE_GEN_POOL = \[.*?\]",
        format_pool("DEFAULT_IMAGE_GEN_POOL", pools["image"]),
        content,
        flags=re.DOTALL
    )
    
    content = re.sub(
        r"DEFAULT_AUDIO_GEN_POOL = \[.*?\]",
        format_pool("DEFAULT_AUDIO_GEN_POOL", pools["audio"]),
        content,
        flags=re.DOTALL
    )
    
    # Write updated config
    with open(config_path, "w") as f:
        f.write(content)
    
    print("Config updated successfully!")


def main():
    print("=" * 60)
    print("OpenRouter Model Pool Updater")
    print("=" * 60)
    
    # Load excluded providers (dynamic from excluded_providers.json)
    excluded_providers = load_excluded_providers()
    
    # Fetch models from API
    models = fetch_models()
    if models is None or len(models) == 0:
        print("ERROR: Failed to fetch models from API. Aborting update to prevent emptying config.")
        sys.exit(1)
    
    # Filter to free models
    free_models = filter_free_models(models, excluded_providers)
    print(f"Found {len(free_models)} free models out of {len(models)} total")
    
    if len(free_models) == 0:
        print("ERROR: No free models found. Aborting update.")
        sys.exit(1)
    
    # Categorize models into pools
    discovered_pools = categorize_models(free_models)
    
    # Read current pools from config.py and append new models
    current_pools = read_current_pools(CONFIG_PATH)
    pools = merge_pools(current_pools, discovered_pools)
    
    print("\nModel distribution (after merge):")
    for pool_name, models in pools.items():
        print(f"  {pool_name}: {len(models)} models")
    
    # Update config
    update_config(
        CONFIG_PATH,
        pools,
    )
    
    print("\n" + "=" * 60)
    print("Update complete!")
    print("=" * 60)
    print("Run 'python3 -m py_compile config.py' to verify syntax")


if __name__ == "__main__":
    main()