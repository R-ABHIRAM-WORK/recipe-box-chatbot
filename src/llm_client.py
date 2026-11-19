"""Zero-dependency OpenAI-compatible LLM client with multi-provider fallback.

Supported providers (all OpenAI Chat Completions compatible, zero pip installs):
  1. Groq          (GROQ_API_KEY)       — default primary: openai/gpt-oss-120b -> qwen/qwen3.8-27b -> openai/gpt-oss-20b
  2. OpenRouter    (OPENROUTER_API_KEY) — fallback: nvidia/nemotron-3-ultra-550b-a55b:free -> google/gemma-4-31b-it:free -> openrouter/free
  3. Google Gemini (GEMINI_API_KEY)     — fallback: gemini-2.5-flash (via Google's OpenAI-compatible endpoint)

Reads `.env` first (overriding stale OS env vars for keys present in `.env`) while
still picking up keys already configured in the OS environment.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Optional
import urllib.error
import urllib.request

_RE_RETRY_AFTER = re.compile(r"try again in\s+(\d+(?:\.\d+)?)\s*(ms|s)", re.IGNORECASE)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"

DEFAULT_GROQ_MODELS = [
    "openai/gpt-oss-120b",
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-20b",
]
DEFAULT_OPENROUTER_MODELS = [
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "openrouter/free",
]
DEFAULT_GEMINI_MODELS = [
    "gemini-3.1-flash-lite",
    "gemini-3.8-flash",
]


@dataclass
class ProviderRoute:
    provider: str
    base_url: str
    api_key: str
    model: str


def load_dotenv(env_path: Path = PROJECT_ROOT / ".env", override: bool = True) -> dict[str, str]:
    """Minimal `.env` loader (no external dependencies)."""
    loaded: dict[str, str] = {}
    if not env_path.exists():
        return loaded
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            loaded[key] = val
            if override or key not in os.environ:
                os.environ[key] = val
    return loaded


class LLMClient:
    """Multi-provider OpenAI-compatible Chat Completions client with tool calling."""

    def __init__(
        self,
        routes: Optional[list[ProviderRoute]] = None,
        timeout: int = 90,
    ) -> None:
        load_dotenv(override=True)
        self.timeout = timeout
        self.last_model_used: Optional[str] = None
        self.last_provider_used: Optional[str] = None

        if routes is not None:
            self.routes = list(routes)
        else:
            self.routes = self._build_routes_from_env()

        if not self.routes:
            raise RuntimeError(
                "No API key found. Set GROQ_API_KEY, GEMINI_API_KEY, or OPENROUTER_API_KEY "
                "in your environment or in `.env` (see `.env.example`)."
            )

    @property
    def models(self) -> list[str]:
        return [f"{r.provider}:{r.model}" for r in self.routes]

    def _build_routes_from_env(self) -> list[ProviderRoute]:
        preferred = os.environ.get("LLM_PROVIDER", "groq").strip().lower()
        custom_model = os.environ.get("LLM_MODEL", "").strip()
        custom_fallbacks = [
            m.strip() for m in os.environ.get("LLM_FALLBACK_MODELS", "").split(",") if m.strip()
        ]
        custom_base = os.environ.get("LLM_BASE_URL", "").strip()

        groq_key = os.environ.get("GROQ_API_KEY", "").strip()
        or_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        gem_key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
        oai_key = os.environ.get("OPENAI_API_KEY", "").strip()

        provider_order = [preferred] + [p for p in ("groq", "gemini", "openrouter", "openai") if p != preferred]
        routes: list[ProviderRoute] = []

        for idx, prov in enumerate(provider_order):
            is_primary = idx == 0
            if prov == "groq" and groq_key:
                base = custom_base if (is_primary and custom_base) else GROQ_BASE_URL
                models = (
                    list(dict.fromkeys(([custom_model] if custom_model else []) + custom_fallbacks + DEFAULT_GROQ_MODELS))
                    if is_primary
                    else list(DEFAULT_GROQ_MODELS)
                )
                for m in models:
                    routes.append(ProviderRoute("groq", base.rstrip("/"), groq_key, m))
            elif prov == "gemini" and gem_key:
                base = custom_base if (is_primary and custom_base) else GEMINI_BASE_URL
                models = (
                    list(dict.fromkeys(([custom_model] if custom_model else []) + custom_fallbacks + DEFAULT_GEMINI_MODELS))
                    if is_primary
                    else list(DEFAULT_GEMINI_MODELS)
                )
                for m in models:
                    routes.append(ProviderRoute("gemini", base.rstrip("/"), gem_key, m))
            elif prov == "openrouter" and or_key:
                base = custom_base if (is_primary and custom_base) else OPENROUTER_BASE_URL
                models = (
                    list(dict.fromkeys(([custom_model] if custom_model else []) + custom_fallbacks + DEFAULT_OPENROUTER_MODELS))
                    if is_primary
                    else list(DEFAULT_OPENROUTER_MODELS)
                )
                for m in models:
                    routes.append(ProviderRoute("openrouter", base.rstrip("/"), or_key, m))
            elif prov == "openai" and oai_key:
                base = custom_base or "https://api.openai.com/v1"
                model = custom_model or "gpt-4o-mini"
                routes.append(ProviderRoute("openai", base.rstrip("/"), oai_key, model))

        return routes

    def chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        """Call `/chat/completions`, walking the provider/model pecking order on 429/5xx/errors."""
        errors: list[str] = []
        for route in self.routes:
            endpoint = f"{route.base_url}/chat/completions"
            headers = {
                "Authorization": f"Bearer {route.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "RecipeBoxChatbot/1.0",
            }
            if route.provider == "openrouter":
                headers["HTTP-Referer"] = "https://github.com/recipe-box-chatbot"
                headers["X-Title"] = "Recipe Box Grounded Chatbot"

            payload: dict[str, Any] = {
                "model": route.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = "auto"

            body = json.dumps(payload).encode("utf-8")

            for attempt in range(3):
                req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    if "error" in data:
                        errors.append(f"{route.provider}:{route.model}: API error {data['error']}")
                        break
                    choices = data.get("choices") or []
                    if not choices:
                        errors.append(f"{route.provider}:{route.model}: empty choices")
                        break
                    self.last_provider_used = route.provider
                    self.last_model_used = f"{route.provider}:{data.get('model') or route.model}"
                    return choices[0]["message"]
                except urllib.error.HTTPError as exc:
                    err_body = ""
                    try:
                        err_body = exc.read().decode("utf-8", errors="ignore")[:320]
                    except Exception:
                        pass
                    if exc.code == 429 and attempt < 2 and "per-day" not in err_body.lower() and "tpd" not in err_body.lower():
                        m_wait = _RE_RETRY_AFTER.search(err_body)
                        wait_s = 5.0
                        if m_wait:
                            val = float(m_wait.group(1))
                            wait_s = (val / 1000.0) if m_wait.group(2).lower() == "ms" else val
                        if wait_s <= 62.0:
                            time.sleep(min(wait_s + 1.0, 63.0))
                            continue
                    errors.append(f"{route.provider}:{route.model}: HTTP {exc.code} {err_body}")
                    break
                except Exception as exc:
                    errors.append(f"{route.provider}:{route.model}: {exc}")
                    break

        raise RuntimeError(
            "All configured providers/models failed:\n  - " + "\n  - ".join(errors)
        )
