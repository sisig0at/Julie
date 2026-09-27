"""User configuration storage.

The API key, provider and model are stored OUTSIDE the app bundle so they are
never baked into the code or the packaged exe. Location:
  - Linux:   ~/.config/jarvis/config.json
  - Windows: %APPDATA%/Jarvis/config.json
"""

import json
import os
import sys

APP_NAME = "jarvis"

DEFAULT_CONFIG = {
    "provider": "groq",          # "groq" | "openrouter" | "ollama" (manual mode)
    "api_key": "",
    "model": "llama-3.3-70b-versatile",       # Groq model
    "openrouter_model": "openai/gpt-4o-mini", # OpenRouter model
    "ollama_model": "llama3.2",              # Local Ollama model
    "vision_model": "qwen/qwen3.6-27b",       # Groq vision model
    "openrouter_vision": "qwen/qwen2.5-vl-72b-instruct",  # OpenRouter vision
    "ollama_vision": "llava",                 # Local Ollama vision model
    # Dual-brain routing: "manual" = provider above decides everything (legacy
    # behaviour), "dual" = brain_router.py sends simple prompts to local Ollama
    # and hard ones to the cloud brain.
    "brain_mode": "manual",
    "cloud_provider": "",       # dual-mode cloud brain: "groq" | "openrouter"
                                # "" = follow "provider" when it is a cloud one
}

# Ollama exposes an OpenAI-compatible API, so the same client is reused with
# this base_url and without a real API key.
OLLAMA_BASE_URL = "http://localhost:11434/v1"
OLLAMA_API_TAGS = "http://localhost:11434/api/tags"   # native endpoint to list local models

GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "llama-3.1-70b-versatile",
    "qwen/qwen3-32b",
    "qwen/qwen3-27b",
    "qwen/qwen3.6-27b",
    "gemma2-9b-it",
    "mixtral-8x7b-32768",
]

OPENROUTER_FALLBACK_MODELS = [
    "openai/gpt-4o-mini",
    "openai/gpt-4o",
    "anthropic/claude-3.5-haiku",
    "anthropic/claude-3.5-sonnet",
    "meta-llama/llama-3.3-70b-instruct",
    "deepseek/deepseek-chat",
    "qwen/qwen-2.5-72b-instruct",
    "mistralai/mistral-small-24b-instruct",
]

# Shown in the UI when the local Ollama service can't be queried for the
# models the user actually has pulled.
OLLAMA_FALLBACK_MODELS = [
    "llama3.2",
    "llama3.1",
    "qwen2.5:7b",
    "qwen2.5",
    "mistral",
    "gemma2",
    "phi3",
    "llava",
]


def config_dir() -> str:
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "Jarvis")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_NAME)


class AppConfig:
    def __init__(self):
        self.path = os.path.join(config_dir(), "config.json")
        self.data = dict(DEFAULT_CONFIG)
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                self.data.update(stored)
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"⚠️ Config load failed: {e}")

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
            try:
                os.chmod(self.path, 0o600)
            except Exception:
                pass
        except Exception as e:
            print(f"⚠️ Config save failed: {e}")

    def is_configured(self) -> bool:
        if self.provider() == "ollama":
            return True  # local service, no API key required
        return bool(self.data.get("api_key", "").strip())

    def provider(self) -> str:
        return self.data.get("provider", "groq")

    def brain_mode(self) -> str:
        """'manual' (legacy single-provider behaviour) or 'dual' (router on top)."""
        mode = str(self.data.get("brain_mode", "manual")).strip().lower()
        return "dual" if mode == "dual" else "manual"

    def cloud_provider(self) -> str:
        """Cloud brain used in dual mode: explicit cloud_provider if set, else
        the manual provider when it is a cloud one, else groq."""
        p = str(self.data.get("cloud_provider", "")).strip().lower()
        if p in ("groq", "openrouter"):
            return p
        if self.provider() in ("groq", "openrouter"):
            return self.provider()
        return "groq"

    def cloud_model(self) -> str:
        if self.cloud_provider() == "openrouter":
            return self.data.get("openrouter_model") or DEFAULT_CONFIG["openrouter_model"]
        return self.data.get("model") or DEFAULT_CONFIG["model"]

    def local_model(self) -> str:
        """Model of the local brain (Ollama) in dual mode."""
        return self.data.get("ollama_model") or DEFAULT_CONFIG["ollama_model"]

    def active_model(self) -> str:
        if self.provider() == "openrouter":
            return self.data.get("openrouter_model") or DEFAULT_CONFIG["openrouter_model"]
        if self.provider() == "ollama":
            return self.data.get("ollama_model") or DEFAULT_CONFIG["ollama_model"]
        return self.data.get("model") or DEFAULT_CONFIG["model"]

    def active_vision_model(self) -> str:
        if self.provider() == "openrouter":
            return self.data.get("openrouter_vision") or DEFAULT_CONFIG["openrouter_vision"]
        if self.provider() == "ollama":
            return self.data.get("ollama_vision") or DEFAULT_CONFIG["ollama_vision"]
        return self.data.get("vision_model") or DEFAULT_CONFIG["vision_model"]

    def update(self, provider=None, api_key=None, model=None, openrouter_model=None,
               ollama_model=None, brain_mode=None, cloud_provider=None) -> dict:
        if provider in ("groq", "openrouter", "ollama"):
            self.data["provider"] = provider
        if api_key is not None:
            self.data["api_key"] = api_key.strip()
        if model:
            self.data["model"] = model
        if openrouter_model:
            # empty string = field was hidden (never saved over a hidden row)
            self.data["openrouter_model"] = openrouter_model
        if ollama_model:
            self.data["ollama_model"] = ollama_model
        if brain_mode in ("manual", "dual"):
            self.data["brain_mode"] = brain_mode
        if cloud_provider in ("groq", "openrouter"):
            self.data["cloud_provider"] = cloud_provider
        self.save()
        return self.public()

    def public(self) -> dict:
        """No secrets here - safe to send to the UI."""
        return {
            "configured": self.is_configured(),
            "provider": self.provider(),
            "model": self.data.get("model", ""),
            "openrouter_model": self.data.get("openrouter_model", ""),
            "ollama_model": self.data.get("ollama_model", ""),
            "brain_mode": self.brain_mode(),
            "cloud_provider": self.cloud_provider(),
        }
