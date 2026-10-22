import requests
import base64
import numpy as np
import threading
import time

import httpx
import os
from typing import Optional
from groq import Groq
from openai import OpenAI
from config import Config
import logging

from logging_setup import timed

logger = logging.getLogger(__name__)


class _CompletionAdapter:
    def __init__(self, parent):
        self.parent = parent

    def create(self, messages, model=None, max_tokens=80, temperature=None, top_p=None, stream=False):
        text = self.parent.chat_completion(
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
        )

        class _Message:
            def __init__(self, content):
                self.content = content

        class _Choice:
            def __init__(self, content):
                self.message = _Message(content)

        class _Response:
            def __init__(self, content):
                self.choices = [_Choice(content)]

        return _Response(text)


class _ChatAdapter:
    def __init__(self, parent):
        self.completions = _CompletionAdapter(parent)


class _ClientAdapter:
    def __init__(self, parent):
        self.chat = _ChatAdapter(parent)


_SHARED_CLIENTS: dict = {}
_SHARED_CLIENTS_LOCK = threading.Lock()


def _shared_openai_client(api_key: str, base_url: Optional[str]) -> OpenAI:
    """One client (and connection pool) per endpoint for the whole server, kept alive between requests.
    A new client per call, and the default 5-second idle limit, meant most voice turns paid a fresh secure
    connection to the model provider (0.3 s to several seconds) before the model even started."""
    key = (api_key, base_url)
    with _SHARED_CLIENTS_LOCK:
        client = _SHARED_CLIENTS.get(key)
        if client is None:
            http_client = httpx.Client(
                limits=httpx.Limits(max_connections=50, max_keepalive_connections=20, keepalive_expiry=120),
                timeout=httpx.Timeout(60.0, connect=10.0),
            )
            client = OpenAI(api_key=api_key, base_url=base_url, http_client=http_client)
            _SHARED_CLIENTS[key] = client
        return client


def warm_model_connection(services: "AIServices") -> None:
    """Open the connection to the model provider ahead of the first question (cheap request, no model cost)."""
    try:
        if services.openai_client is None:
            return
        client = services.openai_client.with_options(timeout=10)
        if services.provider == "openrouter":
            # OpenRouter has no /models/{id} (it answered 404, so no call was ever warmed): its key-info page is free.
            client.get("/key", cast_to=httpx.Response)
        else:
            client.models.retrieve(services.config.OPENAI_MODEL)
    except Exception as error:
        logger.debug("model connection warm-up skipped: %s", error)


_REASONING_MODEL_HINTS = ("gpt-oss", "/o1", "/o3", "/o4", "gpt-5", "deepseek-r1", "qwq", "thinking", "reasoning")


def _no_thinking_models() -> set:
    """VOICE_NO_THINKING_MODELS: models sent with thinking switched off (comma-separated OpenRouter ids)."""
    default = "inception/mercury-3,google/gemini-3.1-flash-lite"
    return {item.strip() for item in os.getenv("VOICE_NO_THINKING_MODELS", default).split(",") if item.strip()}


def is_reasoning_model(model: str) -> bool:
    """Models that think (hidden tokens) before answering: they need a reasoning setting and extra token room."""
    name = str(model or "").lower()
    return any(hint in name for hint in _REASONING_MODEL_HINTS)


class AIServices:
    def __init__(self):
        self.config = Config()
        self.provider = self._resolve_provider()
        self.groq_client = None
        self.openai_client = None
        # Streaming (voice) only: a second model tried when the main one fails or stalls, and the stall limit.
        self.fallback_model = ""
        self.stream_timeout: Optional[float] = None

        logger.debug(f"Initializing AIServices...")
        logger.debug(f"LLM provider selected: {self.provider}")
        logger.debug(f"GROQ_API_KEY present: {'Yes' if self.config.GROQ_API_KEY else 'No'}")
        logger.debug(f"OPENAI_API_KEY present: {'Yes' if self.config.OPENAI_API_KEY else 'No'}")
        logger.debug(f"OPENROUTER_API_KEY present: {'Yes' if self.config.OPENROUTER_API_KEY else 'No'}")

        if self.provider == "groq" and self.config.GROQ_API_KEY:
            try:
                self.groq_client = Groq(api_key=self.config.GROQ_API_KEY)
                logger.debug("Groq client initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize Groq client: {e}")

        if self.provider in {"openai", "openrouter"} and ((self.provider == "openai" and self.config.OPENAI_API_KEY) or (self.provider == "openrouter" and self.config.OPENROUTER_API_KEY)):
            try:
                self.openai_client = _shared_openai_client(
                    self.config.OPENAI_API_KEY if self.provider == "openai" else self.config.OPENROUTER_API_KEY,
                    "https://openrouter.ai/api/v1" if self.provider == "openrouter" else None,
                )
                self.groq_client = _ClientAdapter(self)
                if not self.config.GROQ_API_KEY:
                    self.config.GROQ_API_KEY = "openrouter-proxy" if self.provider == "openrouter" else "openai-proxy"
                logger.debug(f"{self.provider.title()} client initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize OpenAI client: {e}")

    def _resolve_provider(self) -> str:
        if self.config.LLM_PROVIDER in {"groq", "openai", "openrouter"}:
            return self.config.LLM_PROVIDER
        if self.config.OPENROUTER_API_KEY:
            return "openrouter"
        if self.config.GROQ_API_KEY and self.config.GROQ_API_KEY != "your_groq_api_key_here":
            return "groq"
        if self.config.OPENAI_API_KEY:
            return "openai"
        return "groq"

    def is_llm_configured(self) -> bool:
        if self.provider in {"openai", "openrouter"}:
            key = self.config.OPENAI_API_KEY if self.provider == "openai" else self.config.OPENROUTER_API_KEY
            return bool(self.openai_client and key)
        return bool(self.groq_client and self.config.GROQ_API_KEY and self.config.GROQ_API_KEY != "your_groq_api_key_here")

    def _openrouter_extra_body(self, model: str = ""):
        """Return low-latency routing preferences for OpenRouter requests only."""
        model = model or self.config.OPENROUTER_MODEL
        if self.provider != "openrouter":
            return None

        provider_order = [
            provider.strip().lower()
            for provider in os.getenv(
                "RETELL_OPENROUTER_PROVIDER_ORDER",
                "coreweave,deepinfra,groq",
            ).split(",")
            if provider.strip()
        ]
        preferences = {"allow_fallbacks": True}
        if provider_order:
            preferences["order"] = provider_order

        extra_body = {"provider": preferences}
        # Reasoning ("thinking") models such as GPT-OSS get low effort: on a live voice turn the first spoken word
        # matters most. Ordinary chat models (e.g. gpt-4.1-mini) get no reasoning setting at all.
        reasoning_effort = os.getenv("RETELL_OPENROUTER_REASONING_EFFORT", "low").strip().lower()
        if reasoning_effort in {"none", "minimal", "low", "medium", "high"} and is_reasoning_model(model):
            extra_body["reasoning"] = {"effort": reasoning_effort}
        elif model in _no_thinking_models():
            # Models that can optionally think first (Mercury, Gemini Flash-Lite): answer straight away on a call.
            extra_body["reasoning"] = {"enabled": False}
        return extra_body

    def chat_completion(self, messages, max_tokens=80, temperature: Optional[float] = None, top_p: Optional[float] = None):
        temperature = self.config.LLM_TEMPERATURE if temperature is None else temperature
        top_p = self.config.LLM_TOP_P if top_p is None else top_p
        if self.provider in {"openai", "openrouter"}:
            if not self.openai_client:
                raise RuntimeError(f"{self.provider.title()} client is not initialized")
            model = self.config.OPENAI_MODEL if self.provider == "openai" else self.config.OPENROUTER_MODEL
            with timed(logger, f"LLM {model}", warn_after_ms=4000):
                response = self.openai_client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    top_p=top_p,
                    extra_body=self._openrouter_extra_body(),
                )
            return (response.choices[0].message.content or "").strip()

        if not self.groq_client:
            raise RuntimeError("Groq client is not initialized")
        response = self.groq_client.chat.completions.create(
            messages=messages,
            model=self.config.GROQ_MODEL,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            stream=False
        )
        return (response.choices[0].message.content or "").strip()

    def stream_chat_completion(self, messages, max_tokens=80, temperature: Optional[float] = None, top_p: Optional[float] = None,
                               model: Optional[str] = None):
        """Yield text deltas for low-latency voice responses. `model` overrides the main model for this request
        (the main model, then the fallback, are tried if it fails)."""
        temperature = self.config.LLM_TEMPERATURE if temperature is None else temperature
        top_p = self.config.LLM_TOP_P if top_p is None else top_p
        if self.provider not in {"openai", "openrouter"} or not self.openai_client:
            raise RuntimeError("Streaming is currently available for OpenAI-compatible providers only")

        main = self.config.OPENAI_MODEL if self.provider == "openai" else self.config.OPENROUTER_MODEL
        models = list(dict.fromkeys(name for name in (model, main, self.fallback_model) if name))
        for attempt, model in enumerate(models):
            started = False
            try:
                stream = self.openai_client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    top_p=top_p,
                    stream=True,
                    extra_body=self._openrouter_extra_body(model),
                    # A stalled host must not hang a live call: no data for this long counts as a failure.
                    timeout=self.stream_timeout,
                )
                for chunk in stream:
                    if not chunk.choices:
                        continue
                    content = chunk.choices[0].delta.content
                    if content:
                        started = True
                        yield content
                return
            except Exception as error:
                # Fall back only before anything was said: a reply cannot be restarted halfway through.
                if started or attempt == len(models) - 1:
                    raise
                logger.warning("model %s failed (%s: %s); answering with fallback %s", model, type(error).__name__, error, models[attempt + 1])
    
